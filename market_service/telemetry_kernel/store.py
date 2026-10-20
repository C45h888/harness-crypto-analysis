"""Storage adapter for the isolated telemetry Redis namespace and PG ledger.

The adapter reuses the runtime's already-owned Redis and PostgreSQL clients,
but never writes to the canonical market, worker, governance, or inference
keyspaces/tables.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from market_service.telemetry_kernel.contracts import TelemetryEvent

_PUBLISH_EVENT_LUA = r"""
-- One telemetry event: dedupe marker + bounded stream + latest projection.
-- KEYS[1] event stream, KEYS[2] latest projection, KEYS[3] event marker.
-- ARGV[1] event JSON, ARGV[2] projection JSON, ARGV[3] maxlen,
-- ARGV[4] marker TTL seconds, ARGV[5] event id.
if redis.call('EXISTS', KEYS[3]) == 1 then return 'duplicate' end
redis.call('SET', KEYS[3], '1', 'EX', ARGV[4])
local stream_id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[3], '*',
    'event_id', ARGV[5], 'payload', ARGV[1])
local current = redis.call('GET', KEYS[2])
local replace_latest = true
if current then
    local ok, old = pcall(cjson.decode, current)
    local new_ok, new = pcall(cjson.decode, ARGV[2])
    if ok and new_ok and type(old) == 'table' and type(new) == 'table' then
        local old_ts = tonumber(old.last_event_at_ms or -1)
        local new_ts = tonumber(new.last_event_at_ms or -1)
        local old_seq = tonumber(old.source_sequence or -1)
        local new_seq = tonumber(new.source_sequence or -1)
        if new_ts < old_ts or (new_ts == old_ts and new_seq < old_seq) then
            replace_latest = false
        elseif new_ts == old_ts and new_seq == old_seq then
            local old_id = tostring(old.last_event_id or '')
            local new_id = tostring(new.last_event_id or '')
            if new_id <= old_id then replace_latest = false end
        end
    end
end
if replace_latest then redis.call('SET', KEYS[2], ARGV[2]) end
return stream_id
"""


class TelemetryStore:
    """Own all key construction and I/O for the telemetry namespace."""

    def __init__(
        self,
        redis_store: Any,
        postgres_store: Any | None = None,
        *,
        stream_maxlen: int | None = None,
        marker_ttl_s: int = 2_592_000,
    ) -> None:
        self.redis_store = redis_store
        self.redis = redis_store.redis
        self.postgres_store = postgres_store
        base_prefix = str(getattr(redis_store, "prefix", "marketflow") or "marketflow")
        self.prefix = f"{base_prefix.strip(':')}:telemetry"
        self.stream_maxlen = max(1, int(
            stream_maxlen if stream_maxlen is not None
            else getattr(redis_store, "stream_maxlen", 10_000)
        ))
        self.marker_ttl_s = max(1, int(marker_ttl_s))

    @staticmethod
    def _part(value: str) -> str:
        return quote(str(value), safe="-_.")

    def event_stream(self, event: TelemetryEvent) -> str:
        return ":".join((
            self.prefix, "events", event.plane, self._part(event.owner),
            self._part(event.subject_type), self._part(event.subject_id),
        ))

    def latest_key(self, event: TelemetryEvent) -> str:
        return ":".join((
            self.prefix, "latest", event.plane, self._part(event.owner),
            self._part(event.subject_type), self._part(event.subject_id),
        ))

    def health_key(self, owner: str) -> str:
        """Per-owner sink/emitter health, OUTSIDE the event stream."""
        return f"{self.prefix}:health:{self._part(owner)}"

    def cursor_key(self, consumer: str) -> str:
        """Durable projection/replay cursor for one read consumer."""
        return f"{self.prefix}:cursor:{self._part(consumer)}"

    def _event_marker(self, event: TelemetryEvent) -> str:
        return f"{self.prefix}:event-id:{event.event_id}"

    async def write_health(self, owner: str, payload: dict[str, Any]) -> None:
        """Write sink health directly — never through ``emit``.

        Routing health through the event path would make degraded health
        emit an event that can itself fail and re-degrade, unbounded. This
        is the non-recursive local surface the kernel owns.
        """
        await self.redis.set(
            self.health_key(owner),
            json.dumps(payload, default=str, separators=(",", ":")),
        )

    async def read_health(self, owner: str) -> dict[str, Any] | None:
        return await self._read_json(self.health_key(owner))

    async def write_cursor(self, consumer: str, cursor: dict[str, Any]) -> None:
        await self.redis.set(
            self.cursor_key(consumer),
            json.dumps(cursor, default=str, separators=(",", ":")),
        )

    async def read_cursor(self, consumer: str) -> dict[str, Any] | None:
        return await self._read_json(self.cursor_key(consumer))

    async def _read_json(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw:
            return None
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    async def persist(self, event: TelemetryEvent) -> bool | None:
        """Append to the separate durable telemetry ledger; None means PG absent."""
        recorder = getattr(self.postgres_store, "record_telemetry_event", None)
        if not callable(recorder):
            return None
        return bool(await recorder(event.to_dict()))

    async def publish(self, event: TelemetryEvent, projection: dict[str, Any]) -> str:
        body = json.dumps(event.to_dict(), default=str, separators=(",", ":"))
        projected = json.dumps(projection, default=str, separators=(",", ":"))
        result = await self.redis.eval(
            _PUBLISH_EVENT_LUA,
            3,
            self.event_stream(event),
            self.latest_key(event),
            self._event_marker(event),
            body,
            projected,
            str(self.stream_maxlen),
            str(self.marker_ttl_s),
            event.event_id,
        )
        return result.decode("utf-8", errors="replace") if isinstance(result, bytes) else str(result)

    async def read_projection(
        self, plane: str, owner: str, subject_type: str, subject_id: str,
    ) -> dict[str, Any] | None:
        key = ":".join((
            self.prefix, "latest", plane, self._part(owner),
            self._part(subject_type), self._part(subject_id),
        ))
        raw = await self.redis.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw:
            return None
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    async def replace_projection(self, projection: dict[str, Any]) -> None:
        """Restore a Redis materialized view from durable PG replay."""
        key = ":".join((
            self.prefix, "latest", str(projection["plane"]),
            self._part(str(projection["owner"])),
            self._part(str(projection["subject_type"])),
            self._part(str(projection["subject_id"])),
        ))
        await self.redis.set(
            key, json.dumps(projection, default=str, separators=(",", ":")),
        )

    async def read_events(
        self,
        plane: str,
        owner: str,
        subject_type: str,
        subject_id: str,
        *,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        reader = getattr(self.postgres_store, "read_telemetry_events", None)
        if callable(reader):
            return await reader(
                plane=plane, owner=owner, subject_type=subject_type,
                subject_id=subject_id, limit=limit,
            )
        rows = await self.redis.xrevrange(
            ":".join((
                self.prefix, "events", plane, self._part(owner),
                self._part(subject_type), self._part(subject_id),
            )),
            count=max(1, int(limit)),
        )
        events: list[dict[str, Any]] = []
        for _stream_id, fields in reversed(rows or []):
            raw = (fields or {}).get("payload")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                value = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                events.append(value)
        return events
