"""Redis operational store for latest state, streams, and refresh commands."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from redis.asyncio import Redis

from .contracts import (
    AGENT_MEMORY_SCHEMA_VERSION, ANALYST_BRIEFING_SCHEMA_VERSION,
    AgentMemory, AnalystBriefing, MarketRunEnvelope, MarketStateEnvelope,
)


# Schema version for stream-entries on the agent-artifact namespaces.
# Each artifact_type maps to the contract version its payload validates
# against so downstream tooling can read version from the stream-field
# instead of having to decode the JSON payload first.
_AGENT_ARTIFACT_SCHEMA_VERSION: dict[str, int] = {
    "observations": 1,
    "hypotheses": 1,
    "briefings": ANALYST_BRIEFING_SCHEMA_VERSION,
    "memory": AGENT_MEMORY_SCHEMA_VERSION,
}


# Default TTL for per-run STRING keys (run:<run_id>, runtime-run:<run_id>:domain:*).
# 24h bounds growth while keeping a generous re-run guard and replay window.
_PER_RUN_KEY_TTL_S = 86_400


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

    def agent_stream(self, session_id: str, artifact_type: str) -> str:
        return f"{self.prefix}:agent:{session_id}:{artifact_type}"

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

    def keystone_history_stream(self, symbol: str) -> str:
        """Bounded stream of keystone snapshots for one symbol.

        Schema is per-entry:
          schema_version, cycle_ts, run_id, symbol, payload (JSON)
        Bounded by ``stream_maxlen`` (configurable per-store). This is the
        cross-cycle keystone-migration ledger (clean separation from the
        wall-history stream: buyer defence vs seller walls).
        """
        return f"{self.prefix}:history:{symbol.upper()}:keystones"

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

    async def read_recent_events(self, symbol: str, count: int = 100) -> list[dict[str, Any]]:
        """Read the most recent raw evidence snapshots for one symbol.

        Reads the canonical raw evidence stream (written by the poller) —
        the single source of truth for market telemetry. Each returned dict
        is a decoded evidence snapshot decorated with its stream ``id`` and
        ``ts``, newest first.
        """
        rows = await self.redis.xrevrange(self.raw_stream(symbol), count=count)
        out: list[dict[str, Any]] = []
        for entry_id, fields in rows:
            raw = fields.get("payload")
            try:
                value = json.loads(raw) if raw else None
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            out.append({"id": entry_id, "ts": fields.get("ts", ""), **value})
        return out

    async def publish_run(self, envelope: MarketRunEnvelope) -> str:
        envelope.validate()
        payload = envelope.to_json()
        dedupe_key = f"{self.prefix}:run:{envelope.run_id}"
        # One Redis-side transaction makes the latest projection, stream entry,
        # and idempotency marker succeed or fail together. The XADD
        # uses MAXLEN ~ to bound the collated stream (telemetry hygiene
        # spec — prevents the AOF rewrite chain from growing
        # unbounded while preserving a generous replay window).
        # The dedupe key (and the latest-by-symbol copy) carry EXPIRE so
        # per-run_id STRING keys cannot accumulate forever.
        maxlen = int(self.collated_stream_maxlen)
        script = f"""
        if redis.call('EXISTS', KEYS[1]) == 1 then return 'duplicate' end
        redis.call('SET', KEYS[2], ARGV[1])
        local id = redis.call('XADD', KEYS[3], 'MAXLEN', '~', {maxlen}, '*',
            'event_type', 'market_run', 'run_id', ARGV[2],
            'symbol', ARGV[3], 'schema_version', ARGV[4], 'payload', ARGV[1])
        redis.call('SET', KEYS[1], ARGV[1], 'EX', {int(_PER_RUN_KEY_TTL_S)})
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

    async def publish_agent_artifact(
        self, session_id: str, artifact_type: str, payload: str,
        *, run_id: str | None = None,
    ) -> str:
        """Publish advisory output into the agent-owned namespace only."""
        if artifact_type not in {"observations", "hypotheses", "briefings", "memory"}:
            raise ValueError(f"unsupported agent artifact type: {artifact_type}")
        # Resolve schema_version from the contract so the stream-field
        # reflects the true payload version (briefings=2, memory=1).
        # The contract is the source of truth — keeps Redis entries
        # in sync if a future bump changes the constant.
        schema_version = _AGENT_ARTIFACT_SCHEMA_VERSION.get(artifact_type, 1)
        fields = {
            "schema_version": str(schema_version),
            "session_id": session_id,
            "artifact_type": artifact_type,
            "payload": payload,
        }
        if run_id is not None:
            fields["run_id"] = run_id
        return await self.redis.xadd(
            self.agent_stream(session_id, artifact_type),
            fields,
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def publish_briefing(self, briefing: AnalystBriefing) -> str:
        """Publish one validated ``AnalystBriefing`` to the agent namespace.

        The stream entry carries the briefing's ``run_id`` as a top-level
        field so downstream consumers can correlate advisory output with
        the canonical envelope without parsing the payload. The full
        briefing is JSON-encoded as the payload.
        """
        briefing.validate()
        return await self.publish_agent_artifact(
            briefing.session_id, "briefings", briefing.to_json(), run_id=briefing.run_id,
        )

    async def read_recent_briefings(
        self, session_id: str, count: int = 16,
    ) -> list[AnalystBriefing]:
        """Read the most recent briefings for one session (highest first)."""
        rows = await self.redis.xrevrange(
            self.agent_stream(session_id, "briefings"), count=count
        )
        out: list[AnalystBriefing] = []
        for _entry_id, fields in rows:
            raw = fields.get("payload")
            if not raw:
                continue
            try:
                out.append(AnalystBriefing.from_mapping(json.loads(raw)))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    async def read_recent_memories(
        self, session_id: str, count: int = 32,
    ) -> list[AgentMemory]:
        """Read the most recent agent memories for one session (highest first).

        Redis is the live projection; Postgres remains the durable authority
        for history. Forgotten memories are skipped so the live projection
        never resurrects a tombstoned row on a Postgres-empty fallback.
        """
        rows = await self.redis.xrevrange(
            self.agent_stream(session_id, "memory"), count=count
        )
        out: list[AgentMemory] = []
        for _entry_id, fields in rows:
            raw = fields.get("payload")
            if not raw:
                continue
            try:
                memory = AgentMemory.from_mapping(json.loads(raw))
            except (ValueError, json.JSONDecodeError):
                continue
            out.append(memory)
        # Dedupe by memory_id keeping the NEWEST version (xrevrange is
        # highest-first), then drop tombstoned versions: a forget() append
        # must exclude the original entry from the live read plan too.
        newest: dict[str, AgentMemory] = {}
        for m in out:
            newest.setdefault(m.memory_id, m)
        return [m for m in newest.values() if not m.forgotten]

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
                # TTL bounds growth of per-run_id per-source STRING keys
                # so a long-running harness does not accumulate them.
                pipe.set(
                    self.run_domain_state_key(state.run_id, state.source),
                    payload,
                    ex=int(_PER_RUN_KEY_TTL_S),
                )
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

    async def read_wall_history(self, symbol: str, count: int = 1000) -> list[dict[str, Any]]:
        """Read the recorded wall snapshots for ``symbol``, newest first.

        Unlike ``read_last_wall_snapshot`` this surfaces the FULL recorded
        wall history (bounded by the stream cap), so the wall-migration
        analysis can probe every historically-recorded seller-wall level and
        not just the most recent pull.
        """
        rows = await self.redis.xrevrange(self.wall_history_stream(symbol), count=count)
        out: list[dict[str, Any]] = []
        for _entry_id, fields in rows:
            raw = fields.get("payload")
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

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

    # ------------------------------------------------------------------
    # Keystone history — cross-cycle keystone-migration ledger
    # ------------------------------------------------------------------

    async def record_keystone_snapshot(self, symbol: str, run_id: str,
                                       payload: dict[str, Any]) -> str:
        """Append one cycle's keystone snapshot to the bounded history stream.

        ``payload`` is the keystone state set: cycle_ts, keystone_price,
        window_qty, tight/wide bands, keystone_bid_qty, ask_ladder_notional.
        JSON-encoded into the stream entry so callers can decode the full
        picture without the run envelope. Mirrors ``record_wall_snapshot``.
        """
        stream = self.keystone_history_stream(symbol)
        cycle_ts = payload.get("cycle_ts") or ""
        body = json.dumps(payload, separators=(",", ":"), default=str)
        return await self.redis.xadd(
            stream,
            {
                "event_type": "keystone_snapshot",
                "symbol": symbol.upper(),
                "run_id": run_id,
                "schema_version": "1",
                "cycle_ts": cycle_ts,
                "payload": body,
            },
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def read_keystone_history(self, symbol: str, count: int = 1000) -> list[dict[str, Any]]:
        """Read the recorded keystone snapshots for ``symbol``, newest first.

        Surfaces the FULL recorded keystone history (bounded by the stream
        cap) so the cross-cycle migration verdict can probe every recorded
        keystone, not just the most recent pull. Mirrors ``read_wall_history``.
        """
        rows = await self.redis.xrevrange(self.keystone_history_stream(symbol), count=count)
        out: list[dict[str, Any]] = []
        for _entry_id, fields in rows:
            raw = fields.get("payload")
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    async def read_last_keystone_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Read the most recent keystone snapshot for ``symbol``.

        Returns None when the stream is empty (legitimate "no history
        yet" state — not a fabricated zero). Mirrors ``read_last_wall_snapshot``.
        """
        rows = await self.redis.xrevrange(self.keystone_history_stream(symbol), count=1)
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

    async def read_keystone_history_count(self, symbol: str) -> int:
        """Return XLEN of the keystone history stream for diagnostics."""
        return int(await self.redis.xlen(self.keystone_history_stream(symbol)))

    # ------------------------------------------------------------------
    # Derivative evidence — command-triggered fetch writes, harness reads
    # ------------------------------------------------------------------

    def derivatives_key(self, symbol: str) -> str:
        """Latest projection for command-fetched derivative evidence."""
        return f"{self.prefix}:latest:{symbol.upper()}:derivatives"

    def derivatives_stream(self, symbol: str) -> str:
        """Append-only audit trail of every derivative evidence fetch."""
        return f"{self.prefix}:stream:domain:derivatives:{symbol.upper()}"

    async def publish_derivative_evidence(
        self,
        symbol: str,
        payload: dict[str, Any],
        ttl_s: int = 300,
    ) -> str:
        """Atomically SET latest + XADD stream for one derivative evidence fetch.

        ``ttl_s`` bounds how long the latest projection stays cached so back-to-
        back ``--analyze`` cycles within the TTL reuse the same data instead of
        re-hitting Binance. The stream entry carries the full payload so an
        audit reader can replay the fetch history.

        Single Lua script closes the SET/XADD ordering hole: no reader sees a
        ``latest`` snapshot whose stream entry is missing.
        """
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        body = json.dumps(payload, default=str, separators=(",", ":"))
        ts = str(int(time.time() * 1000))
        maxlen = int(self.stream_maxlen)
        script = f"""
        redis.call('SET', KEYS[1], ARGV[1], 'EX', {int(ttl_s)})
        return redis.call('XADD', KEYS[2], 'MAXLEN', '~', {maxlen}, '*',
            'event_type', 'derivative_evidence', 'symbol', ARGV[3],
            'schema_version', '1', 'ts', ARGV[2], 'payload', ARGV[1])
        """
        return str(await self.redis.eval(
            script, 2,
            self.derivatives_key(symbol), self.derivatives_stream(symbol),
            body, ts, symbol.upper(),
        ))

    async def read_derivative_evidence(self, symbol: str) -> dict[str, Any] | None:
        """Read the latest derivative evidence snapshot.

        Returns ``None`` when the key is missing OR the cache has expired
        (Redis returned nil after EX). Preserves the observed_at_ms field so
        callers can decide whether the snapshot is fresh enough.
        """
        raw = await self.redis.get(self.derivatives_key(symbol))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    async def derivative_cache_ttl(self, symbol: str) -> int:
        """Diagnostic: remaining TTL on the derivative evidence key."""
        return int(await self.redis.ttl(self.derivatives_key(symbol)))

    # ------------------------------------------------------------------
    # Raw evidence stream — poller writes, harness reads
    # ------------------------------------------------------------------

    def raw_stream(self, symbol: str) -> str:
        return f"{self.prefix}:stream:raw:{symbol.upper()}"

    def raw_latest_key(self, symbol: str) -> str:
        return f"{self.prefix}:latest:{symbol.upper()}:raw"

    def raw_dedupe_key(self, symbol: str, observed_at_ms: str) -> str:
        """Idempotency guard key for one raw evidence snapshot."""
        return f"{self.prefix}:raw-dedupe:{symbol.upper()}:{observed_at_ms}"

    async def publish_raw_evidence(self, symbol: str, payload: dict[str, Any]) -> str | None:
        """Atomically write one raw evidence snapshot to Redis.

        A single Lua script SETs the ``latest`` projection AND XADDs the raw
        stream in one step, so no reader can observe a latest snapshot whose
        stream entry is missing (atomicity closes the SET/XADD ordering hole).
        A per-snapshot idempotency guard (keyed on ``observed_at_ms``, with a
        TTL) dedupes a re-delivered snapshot from a concurrent poller.

        Returns the stream id, or ``None`` when the snapshot was a duplicate.
        """
        body = json.dumps(payload, default=str, separators=(",", ":"))
        ts = str(payload.get("observed_at_ms", ""))
        script = """
        if ARGV[2] ~= '' then
          local guard = redis.call('SET', KEYS[3], '1', 'NX', 'EX', ARGV[3])
          if guard == false then return 'duplicate' end
        end
        redis.call('SET', KEYS[1], ARGV[1])
        return redis.call('XADD', KEYS[2], 'MAXLEN', '~', 5000, '*',
            'ts', ARGV[2], 'payload', ARGV[1])
        """
        result = await self.redis.eval(
            script, 3,
            self.raw_latest_key(symbol), self.raw_stream(symbol),
            self.raw_dedupe_key(symbol, ts),
            body, ts, 600,
        )
        if result == "duplicate":
            return None
        return str(result)

    async def read_raw_window(
        self, symbol: str, since_ms: int,
    ) -> list[dict[str, Any]]:
        """Read all raw evidence entries since ``since_ms`` (inclusive)."""
        rows = await self.redis.xrange(
            self.raw_stream(symbol), min=str(since_ms), max="+",
        )
        out: list[dict[str, Any]] = []
        for _entry_id, fields in rows:
            raw = fields.get("payload")
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    async def read_raw_latest(self, symbol: str) -> dict[str, Any] | None:
        """Read the latest raw evidence snapshot."""
        raw = await self.redis.get(self.raw_latest_key(symbol))
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None
