"""Redis operational store for latest state, streams, and refresh commands."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from redis.asyncio import Redis

from .contracts import (
    AGENT_MEMORY_SCHEMA_VERSION, AgentMemory, InferenceArtifact,
    MarketStateEnvelope,
)


# Schema version for stream-entries on the agent-artifact namespaces.
# Each artifact_type maps to the contract version its payload validates
# against so downstream tooling can read version from the stream-field
# instead of having to decode the JSON payload first.
_AGENT_ARTIFACT_SCHEMA_VERSION: dict[str, int] = {
    "observations": 1,
    "hypotheses": 1,
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

    async def publish_run(self, envelope: dict[str, Any]) -> str:
        """Publish one canonical run payload dict to the collated stream.

        The envelope dataclass was retired 2026-08-31 — the payload shape
        (field names + schema_version) is the contract, assembled by
        ``pipeline.assemble_envelope`` and JSON-safe at the write seam.
        """
        payload = json.dumps(envelope, default=str, separators=(",", ":"))
        run_id = str(envelope["run_id"])
        symbol = str(envelope["symbol"]).upper()
        schema_version = str(envelope.get("schema_version", ""))
        dedupe_key = f"{self.prefix}:run:{run_id}"
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
            script, 3, dedupe_key, self.collated_latest_key(symbol),
            self.collated_stream(symbol), payload, run_id,
            symbol, schema_version,
        ))

    async def has_run(self, run_id: str) -> bool:
        return bool(await self.redis.exists(f"{self.prefix}:run:{run_id}"))

    async def publish_agent_artifact(
        self, session_id: str, artifact_type: str, payload: str,
        *, run_id: str | None = None,
    ) -> str:
        """Publish advisory output into the agent-owned namespace only."""
        if artifact_type not in {"observations", "hypotheses", "memory"}:
            raise ValueError(f"unsupported agent artifact type: {artifact_type}")
        # Resolve schema_version from the contract so the stream-field
        # reflects the true payload version (memory=1).
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

        Memory discipline (5s cadence): the stream entry carries a *trimmed*
        payload — ``trades_raw`` is stripped because every stream consumer
        (``read_raw_window``) reads only ``trades_normalized``; the raw
        Binance arrays are a byte-for-byte duplicate of the normalized
        trades inside the same snapshot and account for ~half of the
        per-entry size. The full untrimmed payload is kept in the
        ``latest`` projection (the ``trades_raw`` fallback surface).
        Stream length is bounded by ``self.stream_maxlen`` (the hardcoded
        5000 ignored the configured cap). At the 5s cadence the cap is a
        MEMORY budget, not just a history window: ~344 KB/trimmed entry
        x 1200 entries x 3 symbols ~= 1.2 GB, sized to stay under the
        1.5 GB maxmemory with room for collated/ledger keys (1200 x 5s
        = 100 min of stream history; the 15m analysis window needs 180).

        Returns the stream id, or ``None`` when the snapshot was a duplicate.
        """
        full_body = json.dumps(payload, default=str, separators=(",", ":"))
        trimmed = {
            **payload,
            "spot": {k: v for k, v in payload.get("spot", {}).items() if k != "trades_raw"},
            "futures": {k: v for k, v in payload.get("futures", {}).items() if k != "trades_raw"},
        }
        stream_body = json.dumps(trimmed, default=str, separators=(",", ":"))
        ts = str(payload.get("observed_at_ms", ""))
        maxlen = str(int(self.stream_maxlen))
        script = """
        if ARGV[2] ~= '' then
          local guard = redis.call('SET', KEYS[3], '1', 'NX', 'EX', ARGV[3])
          if guard == false then return 'duplicate' end
        end
        redis.call('SET', KEYS[1], ARGV[1])
        return redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[5], '*',
            'ts', ARGV[2], 'payload', ARGV[4])
        """
        result = await self.redis.eval(
            script, 3,
            self.raw_latest_key(symbol), self.raw_stream(symbol),
            self.raw_dedupe_key(symbol, ts),
            full_body, ts, "600", stream_body, maxlen,
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

    # ------------------------------------------------------------------
    # Microstructure ledger — isolated from the five-second REST poller.
    # ------------------------------------------------------------------

    def microstructure_raw_stream(self, venue: str, symbol: str) -> str:
        return f"{self.prefix}:stream:microstructure:raw:{venue.lower()}:{symbol.upper()}"

    def microstructure_event_stream(self, venue: str, symbol: str) -> str:
        return f"{self.prefix}:stream:microstructure:events:{venue.lower()}:{symbol.upper()}"

    def microstructure_ofi_stream(self, venue: str, symbol: str) -> str:
        return f"{self.prefix}:stream:microstructure:ofi:{venue.lower()}:{symbol.upper()}"

    def microstructure_book_key(self, venue: str, symbol: str) -> str:
        return f"{self.prefix}:latest:microstructure:{venue.lower()}:{symbol.upper()}:book"

    def microstructure_status_key(self, venue: str, symbol: str) -> str:
        return f"{self.prefix}:latest:microstructure:{venue.lower()}:{symbol.upper()}:status"

    def microstructure_status_stream(self, venue: str, symbol: str) -> str:
        """Status-TRANSITION ledger stream (written only on state change).

        This is the event-driven companion to the latest-key status payload:
        the capture node appends one entry here whenever ``state`` *changes*
        (running -> gap -> reconnecting -> running ...), and the inference
        wake worker blocks on it to fire capture-recovery wakes without
        polling. Event-time is carried in the entry id, so age checks need
        no extra payload fields.
        """
        return f"{self.prefix}:stream:microstructure:status:{venue.lower()}:{symbol.upper()}"

    def microstructure_evidence_key(self, venue: str, symbol: str) -> str:
        return f"{self.prefix}:latest:microstructure:{venue.lower()}:{symbol.upper()}:evidence"

    async def publish_microstructure_delta(
        self, venue: str, symbol: str, payload: dict[str, Any], *, maxlen: int,
    ) -> str:
        """Append raw depth evidence without sharing poller retention or keys."""
        body = json.dumps(payload, default=str, separators=(",", ":"))
        return str(await self.redis.xadd(
            self.microstructure_raw_stream(venue, symbol),
            {"payload": body, "ts": str(payload.get("received_ts_ms", ""))},
            maxlen=max(1, int(maxlen)), approximate=True,
        ))

    async def publish_microstructure_event(
        self, venue: str, symbol: str, payload: dict[str, Any], *, maxlen: int,
    ) -> str:
        body = json.dumps(payload, default=str, separators=(",", ":"))
        return str(await self.redis.xadd(
            self.microstructure_event_stream(venue, symbol),
            {"payload": body, "ts": str((payload.get("current") or {}).get("exchange_ts_ms", ""))},
            maxlen=max(1, int(maxlen)), approximate=True,
        ))

    async def publish_microstructure_interval(
        self, venue: str, symbol: str, payload: dict[str, Any], *, maxlen: int,
    ) -> str:
        """Append one completed deterministic OFI/AD measurement interval."""
        body = json.dumps(payload, default=str, separators=(",", ":"))
        return str(await self.redis.xadd(
            self.microstructure_ofi_stream(venue, symbol),
            {"payload": body, "ts": str(payload.get("end_ts_ms", ""))},
            maxlen=max(1, int(maxlen)), approximate=True,
        ))

    async def set_microstructure_book(self, venue: str, symbol: str, payload: dict[str, Any]) -> None:
        await self.redis.set(
            self.microstructure_book_key(venue, symbol),
            json.dumps(payload, default=str, separators=(",", ":")),
        )

    async def set_microstructure_status(self, venue: str, symbol: str, payload: dict[str, Any]) -> None:
        await self.redis.set(
            self.microstructure_status_key(venue, symbol),
            json.dumps(payload, default=str, separators=(",", ":")),
        )

    async def publish_microstructure_status_transition(
        self, venue: str, symbol: str, payload: dict[str, Any], *, maxlen: int,
    ) -> str:
        """Append one status-TRANSITION entry to the status ledger stream.

        Written ONLY when capture state changes (see capture._status); the
        inference wake worker consumes this stream to fire capture-recovery
        wakes event-driven. Bounded and best-effort: a failed append is
        tolerated by the capture loop (latest-key status remains authoritative).
        """
        body = json.dumps(payload, default=str, separators=(",", ":"))
        return str(await self.redis.xadd(
            self.microstructure_status_stream(venue, symbol),
            {"payload": body, "state": str(payload.get("state", ""))},
            maxlen=max(1, int(maxlen)), approximate=True,
        ))

    async def read_microstructure_status(self, venue: str, symbol: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self.microstructure_status_key(venue, symbol))
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    async def publish_microstructure_evidence(self, venue: str, symbol: str, payload: dict[str, Any]) -> None:
        """Persist one immutable MicrostructureEvidence projection (latest)."""
        await self.redis.set(
            self.microstructure_evidence_key(venue, symbol),
            json.dumps(payload, default=str, separators=(",", ":")),
        )

    async def read_microstructure_evidence(self, venue: str, symbol: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self.microstructure_evidence_key(venue, symbol))
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    async def read_microstructure_events(
        self, venue: str, symbol: str, *, start: str = "-", end: str = "+", count: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read best-quote transition events from the dedicated event stream.

        Each entry's ``payload`` field carries one serialized OrderBookEvent;
        entries whose payload does not decode are skipped (never fabricated).
        """
        return await self._read_microstructure_stream(
            self.microstructure_event_stream(venue, symbol), start=start, end=end, count=count,
        )

    async def read_microstructure_intervals(
        self, venue: str, symbol: str, *, start: str = "-", end: str = "+", count: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read completed OFIInterval rows from the dedicated OFI stream."""
        return await self._read_microstructure_stream(
            self.microstructure_ofi_stream(venue, symbol), start=start, end=end, count=count,
        )

    async def _read_microstructure_stream(
        self, key: str, *, start: str, end: str, count: int | None,
    ) -> list[dict[str, Any]]:
        rows = await self.redis.xrange(key, min=start, max=end, count=count)
        payloads: list[dict[str, Any]] = []
        for _entry_id, fields in rows or []:
            raw = (fields or {}).get("payload")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str):
                continue
            try:
                decoded = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict):
                payloads.append(decoded)
        return payloads

    # ------------------------------------------------------------------
    # Inference-engine artifact projection — latest key + bounded stream.
    # Postgres owns the durable ledger; these are the live read surfaces.
    # ------------------------------------------------------------------

    def inference_latest_key(self, symbol: str, venue: str = "spot") -> str:
        return f"{self.prefix}:latest:inference:{venue.lower()}:{symbol.upper()}"

    def inference_wake_stream(self, symbol: str, venue: str = "spot") -> str:
        """RETIRED wake transport stream (kept named so no reference dangles).

        The wake plane no longer transports envelopes through Redis: a firing
        condition is materialized as an in-memory assertion and dispatched
        directly by the wake worker. This key is not written by the worker;
        it is retained only so older tooling/docs pointing at it fail clearly
        instead of silently writing into the nowhere.
        """
        return f"{self.prefix}:stream:inference:wake:{venue.lower()}:{symbol.upper()}"

    def inference_wake_supervisor_key(self, symbol: str, venue: str = "spot") -> str:
        """Supervisor heartbeat / last-fire ledger for one scope.

        Written by the wake worker every tick (TTL-bounded). Carries liveness
        (state, pid, last_tick_ms) plus the last fired condition (events_total,
        status_state, fired_stamp_ms) so a restarted worker can see how far the
        plane has already consumed — this key is observability + restart
        recovery, and the in-memory dedupe uses it as the atomic guard.
        """
        return f"{self.prefix}:state:inference:wake:{venue.lower()}:{symbol.upper()}:supervisor"

    def inference_stream(self, symbol: str, venue: str = "spot") -> str:
        return f"{self.prefix}:stream:inference:{venue.lower()}:{symbol.upper()}"

    async def publish_wake_record(
        self, record: dict[str, Any], *, stream_maxlen: int | None = None,
    ) -> str | None:
        """Append one informational wake record to the inference journal.

        The wake worker calls this on every fire (via the dispatcher) so a
        human/operator can audit what predicate fired and when. This is the
        INFORMATIONAL journal only — never load-bearing on control flow
        (the retired ``publish_wake``/``read_pending_wakes`` transport is
        gone). Bounded; a failure here is tolerated.
        """
        body = json.dumps(record, default=str, separators=(",", ":"))
        key = self.inference_stream(
            str(record.get("symbol", "BTCUSDT")).upper(),
            str(record.get("venue", "spot")),
        )
        return str(await self.redis.xadd(
            key,
            {"payload": body, "wake_id": str(record.get("wake_id", "")),
             "predicate": str(sorted(record.get("predicates_fired") or {}) or "")},
            maxlen=stream_maxlen or self.stream_maxlen, approximate=True,
        ))

    async def publish_inference_artifact(
        self, artifact: InferenceArtifact, *, stream_maxlen: int | None = None,
    ) -> str:
        """Publish one inference artifact: latest-key projection + stream entry."""
        artifact.validate()
        body = artifact.to_json()
        await self.redis.set(self.inference_latest_key(artifact.symbol, artifact.venue), body)
        return str(await self.redis.xadd(
            self.inference_stream(artifact.symbol, artifact.venue),
            {"payload": body, "artifact_id": artifact.artifact_id,
             "status": artifact.status},
            maxlen=stream_maxlen or self.stream_maxlen, approximate=True,
        ))

    async def read_latest_inference_artifact(
        self, symbol: str, venue: str = "spot",
    ) -> dict[str, Any] | None:
        raw = await self.redis.get(self.inference_latest_key(symbol, venue))
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    # ------------------------------------------------------------------
    # Poller control plane — dynamic symbol selection
    # ------------------------------------------------------------------
    # The poller is a long-running container that resolves its symbol set
    # at boot from env (POLL_SYMBOLS > SYMBOLS). The harness writes a
    # control key into Redis; the poller reads it every cycle so changes
    # take effect within one poll interval without a container restart.
    #
    # Precedence (highest wins):
    #   Redis control key  >  POLL_SYMBOLS env  >  SYMBOLS env
    # Deleting the control key restores env-based resolution.

    def poller_control_key(self) -> str:
        return f"{self.prefix}:poller:active_symbols"

    def poller_status_key(self) -> str:
        return f"{self.prefix}:poller:status"

    async def set_poller_symbols(self, symbols: list[str]) -> None:
        """Write the active-symbol override the poller picks up next cycle."""
        if not symbols:
            raise ValueError("set_poller_symbols: at least one symbol required")
        cleaned = sorted({s.strip().upper() for s in symbols if s.strip()})
        if not cleaned:
            raise ValueError("set_poller_symbols: at least one symbol required")
        await self.redis.set(self.poller_control_key(), json.dumps(cleaned))

    async def read_poller_symbols(self) -> list[str] | None:
        """Return the override list, or None when no control key is set."""
        raw = await self.redis.get(self.poller_control_key())
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        if isinstance(decoded, list) and decoded:
            return [s.upper() for s in decoded]
        return None

    async def clear_poller_symbols(self) -> None:
        """Delete the override so the poller falls back to env defaults."""
        await self.redis.delete(self.poller_control_key())

    async def publish_poller_status(self, payload: dict[str, Any]) -> None:
        """Operator-visible confirmation of what the poller is doing now."""
        await self.redis.set(
            self.poller_status_key(),
            json.dumps(payload, default=str, separators=(",", ":")),
        )

    async def read_poller_status(self) -> dict[str, Any] | None:
        raw = await self.redis.get(self.poller_status_key())
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    # ------------------------------------------------------------------
    # Substrate worker state — the always-fresh calculation projections
    # (spec: docs/SUBSTRATE_WORKER_SPEC.md). Each substrate worker owns:
    #   latest STRING  — the always-fresh projection the harness reads
    #   stream         — bounded history (replay/audit)
    #   supervisor     — TTL heartbeat + dedupe state (observability)
    # ------------------------------------------------------------------

    def substrate_stream(self, substrate: str, symbol: str) -> str:
        return f"{self.prefix}:stream:substrate:{substrate.lower()}:{symbol.upper()}"

    def substrate_latest_key(self, substrate: str, symbol: str) -> str:
        return f"{self.prefix}:latest:substrate:{substrate.lower()}:{symbol.upper()}"

    def substrate_supervisor_key(self, substrate: str, symbol: str) -> str:
        return f"{self.prefix}:substrate:{substrate.lower()}:{symbol.upper()}:supervisor"

    async def publish_substrate_state(
        self, substrate: str, symbol: str, payload: dict[str, Any],
    ) -> str:
        """Atomically write one substrate calculation state to Redis.

        A single Lua script SETs the ``latest`` projection AND XADDs the
        state stream in one step (same ordering-hole closure as
        ``publish_raw_evidence``): no reader can observe a latest projection
        whose stream entry is missing. Stream length is bounded by
        ``self.stream_maxlen``.
        """
        body = json.dumps(payload, default=str, separators=(",", ":"))
        ts = str(payload.get("computed_at_ms") or payload.get("observed_at_ms") or "")
        maxlen = str(int(self.stream_maxlen))
        script = """
        redis.call('SET', KEYS[1], ARGV[1])
        return redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[4], '*',
            'substrate', ARGV[2], 'symbol', ARGV[3], 'ts', ARGV[5], 'payload', ARGV[1])
        """
        return str(await self.redis.eval(
            script, 2,
            self.substrate_latest_key(substrate, symbol),
            self.substrate_stream(substrate, symbol),
            substrate.lower(), symbol.upper(), body, ts, maxlen,
        ))

    async def read_substrate_latest(
        self, substrate: str, symbol: str,
    ) -> dict[str, Any] | None:
        """Read the latest substrate state projection.

        Returns ``None`` when nothing has been aggregated yet (legitimate
        "cold start" state — not a fabricated empty payload).
        """
        raw = await self.redis.get(self.substrate_latest_key(substrate, symbol))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    async def read_substrate_history(
        self, substrate: str, symbol: str, count: int = 100,
    ) -> list[dict[str, Any]]:
        """Read recorded substrate states, newest first."""
        rows = await self.redis.xrevrange(
            self.substrate_stream(substrate, symbol), count=count,
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

    async def read_substrate_history_count(self, substrate: str, symbol: str) -> int:
        """Return XLEN of the substrate state stream for diagnostics."""
        return int(await self.redis.xlen(self.substrate_stream(substrate, symbol)))
