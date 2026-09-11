"""Reader authority — consumer-group + raw/WS read routing.

SEMANTIC JURISDICTION: this file owns ALL of the "what do we read, and in
what order" surface of a substrate worker:

* consumer-group lifecycle on the raw evidence stream (ensure + heal);
* the blocking raw read (XREADGROUP BLOCK) with NOGROUP/connection heal;
* the WS input surface (Phase 2): microstructure event stream read + the
  status-transition recovery signal;
* read-time boundaries: window rollover detection and high-water mark.

It does NOT own: fire decisions, dedupe, compute, persistence, or the
supervisor heartbeat. Those live in ``fire.py`` / ``supervisor.py``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from market_service.substrate_worker.core.base import SubstrateBase, _now_ms, _store_fn
from market_service.substrate_worker.contracts import (
    ROLLOVER_PERIOD_MS,
    TriggerDecision,
)

log = logging.getLogger(__name__)

_EVT_READ_LIMIT = 500          # max stream entries per read burst
_WS_ERR_LOG_COOLDOWN_MS = 60_000


def _is_nogroup(exc: BaseException) -> bool:
    """NOGROUP = our consumer group (or its stream) vanished after start."""
    return "NOGROUP" in str(exc).upper()


def is_recoverable_redis_exc(exc: Exception) -> bool:
    """True for Redis connection-level errors the worker should back off on."""
    return type(exc).__name__ in (
        "ConnectionError", "ResponseError", "TimeoutError", "RedisError",
        "ConnectionClosedError", "ConnectionResetError", "OperationalError",
    )


class ReaderMixin(SubstrateBase):
    """Read-routing authority: raw + WS surfaces, heal, recovery, rollover."""

    # ---- raw consumer group ----

    async def _ensure_group(self) -> None:
        """Create the raw + (optional) WS consumer groups if absent (MKSTREAM)."""
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" in str(exc).upper():
                log.debug("substrate group %s already exists", self._group)
            else:
                raise
        if self._ws_stream is not None:
            try:
                await self._redis.xgroup_create(
                    self._ws_stream, self._ws_group, id="0", mkstream=True,
                )
            except Exception as exc:
                if "BUSYGROUP" in str(exc).upper():
                    log.debug("substrate ws group %s already exists", self._ws_group)
                else:
                    raise

    async def _register_scripts(self) -> None:
        """Load the dedupe/supervisor Lua script (best-effort)."""
        if self._dedupe_lua_sha is not None:
            return
        try:
            self._dedupe_lua_sha = str(await self._redis.script_load(_DEDUPE_LUA))
        except Exception as exc:
            log.warning("substrate dedupe script registration failed: %s", exc)
            self._last_error = f"script_register: {exc}"
            self._dedupe_lua_sha = None

    async def _xreadgroup_main(self, block_ms: int | None) -> Any:
        """One blocking XREADGROUP on the raw evidence stream."""
        return await self._redis.xreadgroup(
            self._group, self._consumer,
            {self._stream: ">"},
            count=_EVT_READ_LIMIT,
            block=block_ms if block_ms is not None else self.read_block_ms,
            noack=True,
        )

    async def _healed_read(
        self, read_fn: Callable[[], Awaitable[Any]], scope: str,
    ) -> Any | None:
        """Run one group read, self-healing a vanished group once.

        Returns the response, or None when the read degrades (recoverable
        error recorded on ``_last_error``). NOGROUP re-creates via
        ``_ensure_group`` and retries exactly once — a repeat failure is
        NOT retried (that would spin); it degrades like any other
        recoverable error.
        """
        try:
            return await read_fn()
        except Exception as exc:
            if not _is_nogroup(exc):
                raise
            log.warning(
                "substrate %s lost group (%s); re-creating",
                self.SUBSTRATE_NAME, exc,
            )
            try:
                await self._ensure_group()
                return await read_fn()
            except Exception as retry_exc:
                if _is_nogroup(retry_exc) or is_recoverable_redis_exc(retry_exc):
                    self._last_error = f"{scope}: {retry_exc}"
                    return None
                raise

    async def _read_once(self, block_ms: int | None = None) -> list[dict[str, Any]]:
        """Blocking read of ONE batch of the raw stream (new entries only)."""
        try:
            resp = await self._healed_read(
                lambda: self._xreadgroup_main(block_ms), "read",
            )
        except Exception as exc:
            if is_recoverable_redis_exc(exc):
                self._last_error = f"read: {exc}"
                return []
            raise
        if resp is None:
            return []
        rows: list[dict[str, Any]] = []
        if not resp:
            return rows
        for _key, entries in resp:
            for entry_id, fields in entries or []:
                rows.append({
                    "id": entry_id.decode("utf-8", errors="replace")
                    if isinstance(entry_id, bytes) else str(entry_id),
                    "fields": fields if isinstance(fields, dict) else {},
                })
        return rows

    @staticmethod
    def _entry_ms(entry_id: str) -> int | None:
        """Event-time millis from a Redis stream entry id (``ms-seq``)."""
        try:
            return int(str(entry_id).split("-", 1)[0])
        except (TypeError, ValueError):
            return None

    # ---- WS input surface (Phase 2) ----

    @staticmethod
    def _status_transition_recovery(fields: dict[str, Any]) -> dict[str, Any] | None:
        """Detect a gap/reconnecting → running transition (L4 recovery)."""
        to_state = fields.get("state") or fields.get("to_state")
        from_state = fields.get("from_state") or fields.get("from")
        raw_payload = fields.get("payload")
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8", errors="replace")
        if isinstance(raw_payload, str) and raw_payload:
            try:
                decoded = json.loads(raw_payload)
            except (ValueError, TypeError):
                decoded = None
            if isinstance(decoded, dict):
                to_state = to_state or decoded.get("state") or decoded.get("to_state")
                from_state = (
                    from_state or decoded.get("from_state") or decoded.get("from")
                )
        to_s = str(to_state).lower() if to_state is not None else ""
        from_s = str(from_state).lower() if from_state is not None else ""
        if to_s in ("running", "connected") and from_s in ("gap", "reconnecting"):
            return {"from_state": from_s, "to_state": to_s}
        return None

    async def _xreadgroup_ws(self, block_ms: int | None) -> Any:
        """One blocking XREADGROUP on the microstructure event stream."""
        return await self._redis.xreadgroup(
            self._ws_group, self._ws_consumer,
            {self._ws_stream: ">"},
            count=_EVT_READ_LIMIT,
            block=block_ms if block_ms is not None else self.read_block_ms,
            noack=True,
        )

    def _note_ws_error(self, detail: str) -> None:
        """Record a ws-read failure on the heartbeat + rate-limited log."""
        self._last_error = detail
        now_ms = _now_ms()
        if now_ms - self._ws_err_log_ms >= _WS_ERR_LOG_COOLDOWN_MS:
            self._ws_err_log_ms = now_ms
            log.warning("substrate %s %s", self.SUBSTRATE_NAME, detail)

    async def _read_ws_once(
        self, block_ms: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read ONE batch of WS events + the status-transition tail.

        Returns ``(ws_rows, recovery)``. Workers without ``ws_input`` (or a
        store without the microstructure surface, e.g. test seams) return
        ``([], False)`` without touching Redis.
        """
        if self._ws_stream is None and self._status_stream is None:
            return [], False
        ws_rows: list[dict[str, Any]] = []
        recovery = False
        if self._ws_stream is not None:
            try:
                resp = await self._healed_read(
                    lambda: self._xreadgroup_ws(block_ms), "ws read",
                )
            except Exception as exc:
                if is_recoverable_redis_exc(exc):
                    self._note_ws_error(f"ws read: {exc}")
                    return [], False
                raise
            if resp is None:
                self._note_ws_error(str(self._last_error or "ws read degraded"))
                return [], False
            for _key, entries in resp or []:
                for entry_id, fields in entries or []:
                    ws_rows.append({
                        "id": entry_id.decode("utf-8", errors="replace")
                        if isinstance(entry_id, bytes) else str(entry_id),
                        "fields": fields if isinstance(fields, dict) else {},
                    })
        if self._status_stream is not None:
            try:
                resp = await self._redis.xread(
                    {self._status_stream: "$"},
                    count=_EVT_READ_LIMIT,
                    block=0,
                )
            except Exception as exc:
                if is_recoverable_redis_exc(exc):
                    self._last_error = f"ws status read: {exc}"
                    return ws_rows, False
                raise
            if isinstance(resp, (list, tuple)):
                for _key, entries in resp:
                    for entry in entries or []:
                        if isinstance(entry, (list, tuple)) and len(entry) == 2:
                            _eid, fields = entry
                        elif isinstance(entry, dict):
                            fields = entry.get("fields", entry)
                        else:
                            continue
                        if not isinstance(fields, dict):
                            continue
                        detail = self._status_transition_recovery(fields)
                        if detail is not None:
                            recovery = True
        return ws_rows, recovery

    def _rollover_decision(self, rows: list[dict[str, Any]]) -> TriggerDecision | None:
        """Fire when a declared period boundary crossed between batches."""
        newest_ms = self._entry_ms(rows[-1]["id"]) if rows else None
        previous_ms, self._prev_newest_ms = self._prev_newest_ms, newest_ms or self._prev_newest_ms
        if newest_ms is None or previous_ms is None:
            return None
        profile = type(self).CADENCE
        periods = profile.rollovers if profile is not None else ()
        crossed: dict[str, Any] = {}
        for period in periods:
            length = ROLLOVER_PERIOD_MS.get(period)
            if not length:
                continue
            from_bucket, to_bucket = previous_ms // length, newest_ms // length
            if to_bucket != from_bucket:
                crossed[period] = {
                    "period": period, "from_bucket": from_bucket, "to_bucket": to_bucket,
                }
        if not crossed:
            return None
        return TriggerDecision(fired=True, source="rollover", predicates=crossed)

    @staticmethod
    def _high_water(rows: list[dict[str, Any]]) -> str:
        """The newest consumed entry id — the dedupe/collapse water mark."""
        return rows[-1]["id"] if rows else ""


_DEDUPE_LUA = r"""
-- deterministic substrate fire dedupe (condition-identity collapse)
-- KEYS[1] = supervisor key; ARGV[1] = candidate json; ARGV[2] = cooldown_ms;
-- ARGV[3] = high_water (consumed entry id or ''); ARGV[4] = trigger source
local current = redis.call("GET", KEYS[1])
if current then
    local ok, dec = pcall(cjson.decode, current)
    if ok and type(dec) == "table" then
        local last_water = tostring(dec.high_water or "")
        local last_source = tostring(dec.trigger_source or "")
        if tostring(ARGV[3]) == last_water and tostring(ARGV[4]) == last_source then
            return "0"
        end
    end
end
redis.call("SET", KEYS[1], ARGV[1])
return "1"
"""