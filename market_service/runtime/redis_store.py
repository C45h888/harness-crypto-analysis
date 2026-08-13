"""Redis operational store for latest state, streams, and refresh commands."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from .contracts import (
    HarnessRunRequest, MarketEvent, MarketRunEnvelope, MarketStateEnvelope,
    RefreshCommand, RuntimeRunState,
)


class RedisRuntimeStore:
    """Small typed adapter; callers never need to know Redis key names."""

    def __init__(self, url: str, prefix: str = "marketflow", stream_maxlen: int = 10_000,
                 postgres_store: Any | None = None,
                 collated_stream_maxlen: int = 5_000):
        self.redis: Redis = Redis.from_url(url, decode_responses=True)
        self.prefix = prefix.strip(":")
        self.stream_maxlen = stream_maxlen
        # Doctrine §4 leaves the collated stream "intentionally
        # unbounded by default so downstream replay and auditing have
        # the full history". In practice this lets the AOF rewrite
        # chain grow without bound; the cap here bounds the
        # in-memory cost while keeping a generous replay window
        # (5_000 entries ≈ 5 hours at the 30s orchestrator cycle).
        self.collated_stream_maxlen = collated_stream_maxlen
        # Layer C: optional PostgresRuntimeStore companion for
        # durable wall-history reads/writes. When present, the
        # analysis adapter uses postgres in preference to Redis.
        self._postgres_store = postgres_store

    @staticmethod
    def _prefixed(prefix: str, key: str) -> str:
        return f"{prefix.strip(':')}:{key}"

    def latest_key(self, symbol: str, source: str) -> str:
        return f"{self.prefix}:latest:{symbol.upper()}:{source}"

    def telemetry_stream(self, symbol: str) -> str:
        return f"{self.prefix}:stream:market:{symbol.upper()}"

    def collated_latest_key(self, symbol: str) -> str:
        return f"{self.prefix}:latest:{symbol.upper()}:collated"

    def collated_stream(self, symbol: str) -> str:
        return f"{self.prefix}:stream:collated:{symbol.upper()}"

    def domain_stream(self, symbol: str, source: str) -> str:
        """Per-domain append-only projection stream.

        Sources: ``data-access``, ``calculations``, ``analysis``.
        These streams are bounded by ``stream_maxlen``; the collated stream
        is intentionally left unbounded by default so downstream replay and
        auditing have the full history.
        """
        return f"{self.prefix}:stream:domain:{source.lower()}:{symbol.upper()}"

    def domain_latest_key(self, symbol: str, source: str) -> str:
        return f"{self.prefix}:latest:{symbol.upper()}:{source.lower()}"

    def run_state_key(self, run_id: str) -> str:
        return f"{self.prefix}:runtime-run:{run_id}"

    def run_domain_state_key(self, run_id: str, source: str) -> str:
        return f"{self.prefix}:runtime-run:{run_id}:domain:{source.lower()}"

    def wall_history_stream(self, symbol: str) -> str:
        """Bounded stream of wall_snapshot payloads for one symbol.

        Schema is per-entry:
          schema_version, cycle_ts, run_id, symbol, payload (JSON)
        Bounded by ``stream_maxlen`` (configurable per-store). This is
        the prior-cycle seam for ``adapt_wall_migration``.
        """
        return f"{self.prefix}:history:{symbol.upper()}:walls"

    @property
    def command_stream(self) -> str:
        return f"{self.prefix}:stream:commands"

    @property
    def result_stream(self) -> str:
        return f"{self.prefix}:stream:results"

    @property
    def harness_request_stream(self) -> str:
        return f"{self.prefix}:stream:harness:requests"

    async def close(self) -> None:
        await self.redis.aclose()

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def ping_with_retry(self, attempts: int = 12, delay_s: float = 1.0) -> bool:
        """Tolerate transient Docker DNS/network readiness during startup."""
        for attempt in range(attempts):
            try:
                if await self.ping():
                    return True
            except Exception:
                if attempt == attempts - 1:
                    raise
            await asyncio.sleep(delay_s)
        return False

    async def publish_state(self, state: MarketStateEnvelope) -> str:
        payload = state.to_json()
        await self.redis.set(self.latest_key(state.symbol, state.source), payload)
        return await self.redis.xadd(
            self.telemetry_stream(state.symbol),
            {"event_type": "market_state", "symbol": state.symbol, "payload": payload},
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def read_latest_state(self, symbol: str, source: str) -> MarketStateEnvelope | None:
        raw = await self.redis.get(self.latest_key(symbol, source))
        return MarketStateEnvelope.from_mapping(json.loads(raw)) if raw else None

    async def read_recent_events(self, symbol: str, count: int = 100) -> list[dict[str, Any]]:
        rows = await self.redis.xrevrange(self.telemetry_stream(symbol), count=count)
        return [{"id": event_id, **fields} for event_id, fields in rows]

    async def request_refresh(self, command: RefreshCommand) -> str:
        return await self.redis.xadd(
            self.command_stream,
            command.to_fields(),
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def publish_result(self, event: MarketEvent) -> str:
        return await self.redis.xadd(
            self.result_stream,
            event.to_fields(),
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def publish_run(self, envelope: MarketRunEnvelope) -> str:
        envelope.validate()
        payload = envelope.to_json()
        dedupe_key = f"{self.prefix}:run:{envelope.run_id}"
        # One Redis-side transaction makes the latest projection, stream entry,
        # and idempotency marker succeed or fail together. The XADD
        # uses MAXLEN ~ to bound the collated stream (telemetry hygiene
        # spec — prevents the AOF rewrite chain from growing
        # unbounded while preserving a generous replay window).
        maxlen = int(self.collated_stream_maxlen)
        script = f"""
        if redis.call('EXISTS', KEYS[1]) == 1 then return 'duplicate' end
        redis.call('SET', KEYS[2], ARGV[1])
        local id = redis.call('XADD', KEYS[3], 'MAXLEN', '~', {maxlen}, '*',
            'event_type', 'market_run', 'run_id', ARGV[2],
            'symbol', ARGV[3], 'schema_version', ARGV[4], 'payload', ARGV[1])
        redis.call('SET', KEYS[1], ARGV[1])
        return id
        """
        return str(await self.redis.eval(
            script, 3, dedupe_key, self.collated_latest_key(envelope.symbol),
            self.collated_stream(envelope.symbol), payload, envelope.run_id,
            envelope.symbol, str(envelope.schema_version),
        ))

    async def read_latest_run(self, symbol: str) -> MarketRunEnvelope | None:
        raw = await self.redis.get(self.collated_latest_key(symbol))
        return MarketRunEnvelope.from_mapping(json.loads(raw)) if raw else None

    async def read_run(self, run_id: str) -> MarketRunEnvelope | None:
        raw = await self.redis.get(f"{self.prefix}:run:{run_id}")
        return MarketRunEnvelope.from_mapping(json.loads(raw)) if raw else None

    async def read_runs(self, symbol: str, count: int = 100) -> list[MarketRunEnvelope]:
        rows = await self.redis.xrevrange(self.collated_stream(symbol), count=count)
        return [MarketRunEnvelope.from_mapping(json.loads(fields["payload"])) for _, fields in rows]

    async def has_run(self, run_id: str) -> bool:
        return bool(await self.redis.exists(f"{self.prefix}:run:{run_id}"))

    async def publish_domain_state(self, state: MarketStateEnvelope) -> str:
        """Publish a domain service envelope to its latest projection + stream."""
        if state.source not in ("data-access", "calculations", "analysis"):
            raise ValueError(f"unknown domain source: {state.source}")
        state.validate() if hasattr(state, "validate") else None
        payload = state.to_json()
        latest_key = self.domain_latest_key(state.symbol, state.source)
        stream = self.domain_stream(state.symbol, state.source)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(latest_key, payload)
            if state.run_id:
                pipe.set(self.run_domain_state_key(state.run_id, state.source), payload)
            pipe.xadd(
                stream,
                {
                    "event_type": "market_state",
                    "source": state.source,
                    "symbol": state.symbol,
                    "schema_version": str(state.schema_version),
                    "status": state.status,
                    "payload": payload,
                },
                maxlen=self.stream_maxlen,
                approximate=True,
            )
            results = await pipe.execute()
        return str(results[-1])

    async def read_latest_domain_state(
        self, symbol: str, source: str
    ) -> MarketStateEnvelope | None:
        raw = await self.redis.get(self.domain_latest_key(symbol, source))
        if not raw:
            return None
        try:
            return MarketStateEnvelope.from_mapping(json.loads(raw))
        except (ValueError, json.JSONDecodeError):
            return None

    async def read_run_domain_state(
        self, run_id: str, source: str
    ) -> MarketStateEnvelope | None:
        raw = await self.redis.get(self.run_domain_state_key(run_id, source))
        if not raw:
            return None
        try:
            state = MarketStateEnvelope.from_mapping(json.loads(raw))
            return state if state.run_id == run_id else None
        except (ValueError, json.JSONDecodeError):
            return None

    async def record_wall_snapshot(self, symbol: str, run_id: str,
                                   payload: dict[str, Any]) -> str:
        """Append a wall snapshot to the bounded history stream.

        ``payload`` is the wall_migration input set: asks, bids,
        fuel_ratio, bid_pool, ask_pool, bid_floor, ask_target,
        ask_walls_built, ask_walls_eroded, cycle_ts. The payload
        is JSON-encoded into the stream entry so callers can decode
        the full picture without the run envelope.
        """
        stream = self.wall_history_stream(symbol)
        cycle_ts = payload.get("cycle_ts") or ""
        body = json.dumps(payload, separators=(",", ":"), default=str)
        return await self.redis.xadd(
            stream,
            {
                "event_type": "wall_snapshot",
                "symbol": symbol.upper(),
                "run_id": run_id,
                "schema_version": "1",
                "cycle_ts": cycle_ts,
                "payload": body,
            },
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def read_last_wall_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Read the most recent wall snapshot for ``symbol``.

        Returns None when the stream is empty (legitimate "no history
        yet" state — not a fabricated zero).
        """
        rows = await self.redis.xrevrange(self.wall_history_stream(symbol), count=1)
        if not rows:
            return None
        _entry_id, fields = rows[0]
        raw = fields.get("payload")
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    async def read_wall_history_count(self, symbol: str) -> int:
        """Return XLEN of the wall history stream for diagnostics."""
        return int(await self.redis.xlen(self.wall_history_stream(symbol)))

    async def write_runtime_run(self, state: RuntimeRunState) -> None:
        await self.redis.set(
            self.run_state_key(state.run_id),
            json.dumps(state.to_dict(), separators=(",", ":")),
        )

    async def read_runtime_run(self, run_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self.run_state_key(run_id))
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    async def request_harness_run(self, request: HarnessRunRequest) -> str:
        return await self.redis.xadd(
            self.harness_request_stream,
            request.to_fields(),
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def consume_harness_requests(
        self, last_id: str = "$", block_ms: int = 2000, count: int = 8,
    ) -> AsyncIterator[tuple[str, HarnessRunRequest]]:
        current_id = last_id
        while True:
            try:
                rows = await self.redis.xread(
                    {self.harness_request_stream: current_id},
                    block=block_ms,
                    count=count,
                )
            except Exception:
                await asyncio.sleep(1.0)
                continue
            if not rows:
                continue
            for _stream, entries in rows:
                for entry_id, fields in entries:
                    current_id = entry_id
                    yield entry_id, HarnessRunRequest.from_fields(fields)

    async def consume_commands(
        self,
        domain: str,
        last_id: str = "$",
        block_ms: int = 5_000,
        count: int = 16,
    ) -> AsyncIterator[tuple[str, RefreshCommand]]:
        """Yield ``(stream_id, RefreshCommand)`` pairs from ``stream:commands``.

        Filters by ``command.domain``. ``last_id`` starts at ``$`` so a freshly
        started node does not replay history; pass an explicit id to resume.
        """
        current_id: str = last_id
        while True:
            try:
                rows = await self.redis.xread(
                    {self.command_stream: current_id},
                    block=block_ms,
                    count=count,
                )
            except (ResponseError, RedisConnectionError, OSError, ConnectionError):
                # Docker DNS/network interruptions must not terminate a long-lived
                # node. Reconnect is handled by redis-py's lazy connection path
                # on the next read; this loop keeps the consumer alive.
                rows = []
                await asyncio.sleep(1.0)
            if not rows:
                continue
            for _stream, entries in rows:
                for entry_id, fields in entries:
                    current_id = entry_id
                    command = RefreshCommand.from_fields(fields)
                    if command.domain == domain:
                        yield entry_id, command

    async def read_results(
        self,
        run_id: str | None = None,
        count: int = 16,
    ) -> list[tuple[str, MarketEvent]]:
        """Read recent completion events from ``stream:results``.

        If ``run_id`` is supplied, only events whose payload contains a matching
        ``run_id`` are returned (used by the orchestrator to await pipeline
        steps deterministically).
        """
        rows = await self.redis.xrevrange(self.result_stream, count=count * 4)
        out: list[tuple[str, MarketEvent]] = []
        for entry_id, fields in rows:
            event = MarketEvent.from_fields(fields)
            if run_id is None:
                out.append((entry_id, event))
            elif event.payload.get("run_id") == run_id:
                out.append((entry_id, event))
            if len(out) >= count:
                break
        return out

    async def wait_for_result(
        self,
        run_id: str,
        source: str,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.25,
    ) -> MarketEvent | None:
        """Block (poll) until a result event for ``(run_id, source)`` arrives.

        Used by the orchestrator between pipeline steps. Returns ``None`` on
        timeout so the caller can decide whether to fail or retry.
        """
        import asyncio
        import time
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for _entry_id, event in await self.read_results(run_id=run_id, count=32):
                if event.payload.get("source") == source:
                    return event
            await asyncio.sleep(poll_interval_s)
        return None
