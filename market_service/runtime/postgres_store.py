"""Typed PostgreSQL adapter for durable market snapshots and signals."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from .contracts import (
    AgentMemory,
    AnalystBriefing,
    InferenceArtifact,
    MarketRunEnvelope,
    _json_safe,
)


class PostgresRuntimeStore:
    """Connection-pool boundary for the durable ledger.

    ``DATABASE_URL`` is only required here — at the Postgres boundary — and
    nowhere else. Redis-only surfaces (``Settings.from_redis_env``) never
    construct this store, so Redis ops run without Postgres config.
    """

    def __init__(self, database_url: str | None):
        if not database_url:
            raise ValueError(
                "DATABASE_URL is required to construct a PostgresRuntimeStore. "
                "Postgres is only consulted when persistence is explicitly "
                "required; Redis-only surfaces should run Redis alone."
            )
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def ping(self) -> bool:
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        return await self.pool.fetchval("SELECT 1") == 1

    async def latest_state(self, symbol: str) -> dict[str, Any] | None:
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            "SELECT * FROM latest_market_state WHERE symbol = $1", symbol.upper()
        )
        return dict(row) if row else None

    async def insert_run(self, envelope: MarketRunEnvelope) -> bool:
        """Append one canonical run (deduped, 2026-08-27 pass).

        The `envelope` jsonb column is left NULL: coverage /
        canonical_state / domain_outputs / errors are stored as first-class
        columns and the full envelope is reconstructed on read from them.
        The old double-write made every row ~2x its necessary size.
        """
        envelope.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO market_run
               (run_id, symbol, generated_at, completed_at, status, data_source,
                schema_version, coverage, canonical_state, domain_outputs, errors,
                source_metadata)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT (run_id) DO NOTHING
               RETURNING run_id""",
            uuid.UUID(envelope.run_id), envelope.symbol,
            datetime.fromisoformat(envelope.generated_at),
            datetime.fromisoformat(envelope.completed_at), envelope.status, envelope.data_source,
            envelope.schema_version, json.dumps(_json_safe(envelope.coverage)),
            json.dumps(_json_safe(envelope.canonical_state)), json.dumps(_json_safe(envelope.domain_outputs)),
            json.dumps(_json_safe(list(envelope.errors))), json.dumps(_json_safe(envelope.source_metadata or {})),
        )
        return row is not None

    @staticmethod
    def _envelope_from_row(row: dict) -> MarketRunEnvelope:
        """Reconstruct a MarketRunEnvelope from split columns.

        Prefers the reconstructed mapping; falls back to the stored
        `envelope` column for legacy rows (written before the dedupe pass)
        so pre-existing envelopes keep round-tripping byte-identically.
        """
        stored = row.get("envelope")
        if stored is not None:
            return MarketRunEnvelope.from_mapping(
                json.loads(stored) if isinstance(stored, str) else stored
            )
        def _as_dict(v):
            if isinstance(v, str):
                v = json.loads(v)
            return dict(v or {})
        generated_at = row["generated_at"]
        completed_at = row["completed_at"]
        errors = row.get("errors")
        if isinstance(errors, str):
            errors = json.loads(errors)
        return MarketRunEnvelope.from_mapping({
            "schema_version": row["schema_version"],
            "run_id": str(row["run_id"]),
            "symbol": row["symbol"],
            "generated_at": generated_at.isoformat() if hasattr(generated_at, "isoformat") else str(generated_at),
            "completed_at": completed_at.isoformat() if hasattr(completed_at, "isoformat") else str(completed_at),
            "status": row["status"],
            "data_source": row["data_source"],
            "coverage": _as_dict(row.get("coverage")),
            "canonical_state": _as_dict(row.get("canonical_state")),
            "domain_outputs": _as_dict(row.get("domain_outputs")),
            "errors": tuple(errors or ()),
            "source_metadata": _as_dict(row.get("source_metadata")),
        })

    _RUN_COLUMNS = (
        "run_id, symbol, generated_at, completed_at, status, data_source, "
        "schema_version, coverage, canonical_state, domain_outputs, errors, "
        "source_metadata, envelope"
    )

    async def read_run(self, run_id: str) -> MarketRunEnvelope | None:
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            f"SELECT {self._RUN_COLUMNS} FROM market_run WHERE run_id = $1", run_id
        )
        if row is None:
            return None
        return self._envelope_from_row(dict(row))

    async def insert_analyst_briefing(self, briefing: AnalystBriefing) -> bool:
        """Durable, idempotent write of one ``AnalystBriefing``.

        Primary key is ``(session_id, run_id)``: a re-run of the same
        session over the same canonical run_id updates the row instead of
        producing a duplicate. The briefing always carries the run_id of
        the canonical envelope it was produced from, so the durable record
        is linked back to ``market_run`` by ``run_id`` even though there is
        no FK (the agent layer must not be able to corrupt canonical state
        by deleting a briefing).
        """
        briefing.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        try:
            generated_at = datetime.fromisoformat(
                briefing.generated_at.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            generated_at = datetime.now()
        row = await self.pool.fetchrow(
            """INSERT INTO analyst_briefing
               (session_id, run_id, schema_version, model_provider, model_name,
                generated_at, briefing, parse_errors, envelope_summary)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (session_id, run_id) DO UPDATE
               SET schema_version = EXCLUDED.schema_version,
                   model_provider = EXCLUDED.model_provider,
                   model_name = EXCLUDED.model_name,
                   generated_at = EXCLUDED.generated_at,
                   briefing = EXCLUDED.briefing,
                   parse_errors = EXCLUDED.parse_errors,
                   envelope_summary = EXCLUDED.envelope_summary
               RETURNING session_id, run_id""",
            uuid.UUID(briefing.session_id),
            uuid.UUID(briefing.run_id),
            briefing.schema_version,
            briefing.model_provider,
            briefing.model_name,
            generated_at,
            json.dumps(briefing.to_dict(), default=str),
            json.dumps(list(briefing.parse_errors), default=str),
            json.dumps(briefing.envelope_summary, default=str),
        )
        return row is not None

    async def read_analyst_briefing(
        self, session_id: str, run_id: str
    ) -> AnalystBriefing | None:
        """Read one durable briefing by its (session_id, run_id) key."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        raw = await self.pool.fetchval(
            "SELECT briefing FROM analyst_briefing "
            "WHERE session_id = $1 AND run_id = $2",
            uuid.UUID(session_id), uuid.UUID(run_id),
        )
        if raw is None:
            return None
        return AnalystBriefing.from_mapping(
            json.loads(raw) if isinstance(raw, str) else raw
        )

    async def read_briefings_for_run(
        self, run_id: str, limit: int = 32,
    ) -> list[AnalystBriefing]:
        """All briefings produced for one canonical run_id (any session)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT briefing FROM analyst_briefing "
            "WHERE run_id = $1 ORDER BY generated_at DESC LIMIT $2",
            uuid.UUID(run_id), limit,
        )
        out: list[AnalystBriefing] = []
        for row in rows:
            raw = row["briefing"]
            if raw is None:
                continue
            try:
                out.append(AnalystBriefing.from_mapping(
                    json.loads(raw) if isinstance(raw, str) else raw
                ))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    async def latest_run(self, symbol: str) -> MarketRunEnvelope | None:
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            f"SELECT {self._RUN_COLUMNS} FROM market_run "
            "WHERE symbol = $1 ORDER BY completed_at DESC LIMIT 1",
            symbol.upper(),
        )
        if row is None:
            return None
        return self._envelope_from_row(dict(row))

    async def insert_agent_memory(self, memory: AgentMemory) -> bool:
        """Durable, idempotent write of one ``AgentMemory``.

        Postgres is the durable ledger for agent memory; Redis is only the
        live projection. The row is schema-versioned and immutable-by-id:
        re-writing the same ``memory_id`` updates fields (used by
        ``forget`` to tombstone) instead of duplicating. ``forgotten``
        rows survive for audit but are excluded from recall.
        """
        memory.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        try:
            created_at = datetime.fromisoformat(
                memory.created_at.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            created_at = datetime.now()
        updated_at_raw = memory.updated_at
        updated_at: datetime | None = None
        if updated_at_raw:
            try:
                updated_at = datetime.fromisoformat(
                    updated_at_raw.replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                updated_at = None
        row = await self.pool.fetchrow(
            """INSERT INTO agent_memory
               (memory_id, session_id, run_id, kind, title, content,
                importance, tags, evidence_refs, created_at, updated_at,
                forgotten, schema_version, payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (memory_id) DO UPDATE
               SET run_id = EXCLUDED.run_id,
                   kind = EXCLUDED.kind,
                   title = EXCLUDED.title,
                   content = EXCLUDED.content,
                   importance = EXCLUDED.importance,
                   tags = EXCLUDED.tags,
                   evidence_refs = EXCLUDED.evidence_refs,
                   updated_at = EXCLUDED.updated_at,
                   forgotten = EXCLUDED.forgotten,
                   payload = EXCLUDED.payload
               RETURNING memory_id""",
            uuid.UUID(memory.memory_id),
            uuid.UUID(memory.session_id),
            uuid.UUID(memory.run_id) if memory.run_id else None,
            memory.kind,
            memory.title,
            memory.content,
            memory.importance,
            json.dumps(list(memory.tags), default=str),
            json.dumps(list(memory.evidence_refs), default=str),
            created_at,
            updated_at,
            memory.forgotten,
            memory.schema_version,
            memory.to_json(),
        )
        return row is not None

    async def read_recent_memories(
        self, session_id: str, kind: str | None = None, limit: int = 32,
    ) -> list[AgentMemory]:
        """Most recent non-forgotten memories for one session.

        Durable authority is Postgres; Redis is the fallen-back live
        projection. Optional ``kind`` filter mirrors the recall surface
        of the memory node.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        sql = (
            "SELECT payload FROM agent_memory "
            "WHERE session_id = $1 AND NOT forgotten "
            "ORDER BY created_at DESC LIMIT $2"
        )
        params: list[Any] = [uuid.UUID(session_id), limit]
        if kind is not None:
            sql = (
                "SELECT payload FROM agent_memory "
                "WHERE session_id = $1 AND kind = $2::text AND NOT forgotten "
                "ORDER BY created_at DESC LIMIT $3"
            )
            params = [uuid.UUID(session_id), kind, limit]
        rows = await self.pool.fetch(sql, *params)
        out: list[AgentMemory] = []
        for row in rows:
            raw = row["payload"]
            if raw is None:
                continue
            try:
                out.append(AgentMemory.from_mapping(
                    json.loads(raw) if isinstance(raw, str) else raw
                ))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    async def read_memory(self, memory_id: str) -> AgentMemory | None:
        """Read one durable memory by id (including forgotten rows)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        raw = await self.pool.fetchval(
            "SELECT payload FROM agent_memory WHERE memory_id = $1",
            uuid.UUID(memory_id),
        )
        if raw is None:
            return None
        try:
            return AgentMemory.from_mapping(
                json.loads(raw) if isinstance(raw, str) else raw
            )
        except (ValueError, json.JSONDecodeError):
            return None

    async def forget_memory(self, memory_id: str) -> bool:
        """Tombstone one agent memory (audit row survives)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """UPDATE agent_memory
               SET forgotten = TRUE, updated_at = now()
               WHERE memory_id = $1
               RETURNING memory_id""",
            uuid.UUID(memory_id),
        )
        return row is not None

    async def count_memories(self, session_id: str) -> dict[str, int]:
        """Per-kind counts for one session's durable memories."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT kind, COUNT(*) AS n FROM agent_memory "
            "WHERE session_id = $1 AND NOT forgotten "
            "GROUP BY kind ORDER BY kind",
            uuid.UUID(session_id),
        )
        return {str(row["kind"]): int(row["n"]) for row in rows}

    async def has_run(self, run_id: str) -> bool:
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        return bool(await self.pool.fetchval("SELECT EXISTS (SELECT 1 FROM market_run WHERE run_id = $1)", uuid.UUID(run_id)))

    async def record_wall_snapshot(self, symbol: str, run_id: str,
                                   payload: dict[str, Any]) -> bool:
        """Layer C write: insert a wall_snapshot row.

        Mirrors the discipline of ``insert_run``: idempotent on
        (symbol, cycle_ts) so re-runs are safe; postgres-first ordering
        so the row is durable before any redis publication.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        cycle_ts_raw = payload.get("cycle_ts") or ""
        # cycle_ts is stored as TIMESTAMPTZ; coerce ISO strings, fall
        # back to now() if the caller passed a non-parseable value.
        try:
            cycle_ts_value = datetime.fromisoformat(cycle_ts_raw.replace("Z", "+00:00")) \
                if cycle_ts_raw else datetime.now()
        except (TypeError, ValueError):
            cycle_ts_value = datetime.now()
        asks_json = json.dumps(payload.get("asks") or [])
        bids_json = json.dumps(payload.get("bids") or [])

        def _opt_float(key: str) -> float | None:
            v = payload.get(key)
            return float(v) if v is not None else None

        row = await self.pool.fetchrow(
            """INSERT INTO wall_snapshot
               (symbol, cycle_ts, run_id, schema_version,
                asks, bids, fuel_ratio, bid_pool, ask_pool,
                bid_floor, ask_target, ask_walls_built, ask_walls_eroded)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (symbol, cycle_ts) DO NOTHING
               RETURNING symbol, cycle_ts""",
            symbol.upper(), cycle_ts_value, uuid.UUID(run_id),
            int(payload.get("schema_version") or 1),
            asks_json, bids_json,
            _opt_float("fuel_ratio"),
            _opt_float("bid_pool"),
            _opt_float("ask_pool"),
            _opt_float("bid_floor"),
            _opt_float("ask_target"),
            int(payload.get("ask_walls_built") or 0),
            int(payload.get("ask_walls_eroded") or 0),
        )
        return row is not None

    async def read_last_wall_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Layer C read: most recent wall_snapshot for ``symbol``.

        Returns None when no snapshot exists yet — same semantics as
        the Redis read, but durable across Redis restarts.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """SELECT symbol, cycle_ts, run_id, schema_version, asks, bids,
                      fuel_ratio, bid_pool, ask_pool, bid_floor, ask_target,
                      ask_walls_built, ask_walls_eroded
               FROM wall_snapshot
               WHERE symbol = $1
               ORDER BY cycle_ts DESC LIMIT 1""",
            symbol.upper(),
        )
        if row is None:
            return None
        result = dict(row)
        # asyncpg returns JSONB columns already-decoded as Python
        # objects; the existing code uses json.loads so be defensive.
        for col in ("asks", "bids"):
            v = result.get(col)
            if isinstance(v, str):
                try:
                    result[col] = json.loads(v)
                except (ValueError, json.JSONDecodeError):
                    result[col] = []
        if "cycle_ts" in result and hasattr(result["cycle_ts"], "isoformat"):
            result["cycle_ts"] = result["cycle_ts"].isoformat()
        return result

    async def read_wall_history(self, symbol: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Layer C read: full wall_snapshot history for ``symbol``, newest first.

        Mirrors ``RedisRuntimeStore.read_wall_history`` so the migration
        analysis can probe every recorded wall level from the durable ledger.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            """SELECT symbol, cycle_ts, run_id, schema_version, asks, bids,
                      fuel_ratio, bid_pool, ask_pool, bid_floor, ask_target,
                      ask_walls_built, ask_walls_eroded
               FROM wall_snapshot
               WHERE symbol = $1
               ORDER BY cycle_ts DESC LIMIT $2""",
            symbol.upper(), limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            for col in ("asks", "bids"):
                v = result.get(col)
                if isinstance(v, str):
                    try:
                        result[col] = json.loads(v)
                    except (ValueError, json.JSONDecodeError):
                        result[col] = []
            if "cycle_ts" in result and hasattr(result["cycle_ts"], "isoformat"):
                result["cycle_ts"] = result["cycle_ts"].isoformat()
            out.append(result)
        return out

    async def wall_snapshot_count(self, symbol: str) -> int:
        """Diagnostic count for the wall_snapshot table for one symbol."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        return int(await self.pool.fetchval(
            "SELECT COUNT(*) FROM wall_snapshot WHERE symbol = $1", symbol.upper()))

    # ------------------------------------------------------------------
    # Keystone history — cross-cycle keystone-migration ledger
    # ------------------------------------------------------------------

    @staticmethod
    def _opt_float(value: Any) -> float | None:
        """Null-discipline coercion: None stays None (no fabricated zero)."""
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None

    async def record_keystone_snapshot(self, symbol: str, run_id: str,
                                       payload: dict[str, Any]) -> bool:
        """Layer C write: insert a keystone_history row.

        Mirrors the discipline of ``record_wall_snapshot``: idempotent on
        (symbol, cycle_ts) so re-runs are safe; postgres-first ordering so
        the row is durable before any redis publication. Metric columns are
        nullable per the null discipline (None = source did not provide).
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        cycle_ts_raw = payload.get("cycle_ts") or ""
        try:
            cycle_ts_value = datetime.fromisoformat(cycle_ts_raw.replace("Z", "+00:00")) \
                if cycle_ts_raw else datetime.now()
        except (TypeError, ValueError):
            cycle_ts_value = datetime.now()
        row = await self.pool.fetchrow(
            """INSERT INTO keystone_history
               (symbol, cycle_ts, run_id, schema_version,
                keystone_price, window_qty, tight_lo, tight_hi,
                wide_lo, wide_hi, keystone_bid_qty, ask_ladder_notional)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT (symbol, cycle_ts) DO NOTHING
               RETURNING symbol, cycle_ts""",
            symbol.upper(), cycle_ts_value, uuid.UUID(run_id),
            int(payload.get("schema_version") or 1),
            self._opt_float(payload.get("keystone_price")),
            self._opt_float(payload.get("window_qty")),
            self._opt_float(payload.get("tight_lo")),
            self._opt_float(payload.get("tight_hi")),
            self._opt_float(payload.get("wide_lo")),
            self._opt_float(payload.get("wide_hi")),
            self._opt_float(payload.get("keystone_bid_qty")),
            self._opt_float(payload.get("ask_ladder_notional")),
        )
        return row is not None

    def _decode_keystone_row(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        if "cycle_ts" in result and hasattr(result["cycle_ts"], "isoformat"):
            result["cycle_ts"] = result["cycle_ts"].isoformat()
        return result

    async def read_last_keystone_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Layer C read: most recent keystone_history row for ``symbol``.

        Returns None when no snapshot exists yet — same semantics as the
        Redis read, but durable across Redis restarts.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """SELECT symbol, cycle_ts, run_id, schema_version,
                      keystone_price, window_qty, tight_lo, tight_hi,
                      wide_lo, wide_hi, keystone_bid_qty, ask_ladder_notional
               FROM keystone_history
               WHERE symbol = $1
               ORDER BY cycle_ts DESC LIMIT 1""",
            symbol.upper(),
        )
        return self._decode_keystone_row(row) if row is not None else None

    async def read_keystone_history(self, symbol: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Layer C read: full keystone_history for ``symbol``, newest first.

        Mirrors ``RedisRuntimeStore.read_keystone_history`` so the
        cross-cycle migration verdict can probe every recorded keystone
        from the durable ledger.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            """SELECT symbol, cycle_ts, run_id, schema_version,
                      keystone_price, window_qty, tight_lo, tight_hi,
                      wide_lo, wide_hi, keystone_bid_qty, ask_ladder_notional
               FROM keystone_history
               WHERE symbol = $1
               ORDER BY cycle_ts DESC LIMIT $2""",
            symbol.upper(), limit,
        )
        return [self._decode_keystone_row(r) for r in rows]

    async def keystone_snapshot_count(self, symbol: str) -> int:
        """Diagnostic count for the keystone_history table for one symbol."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        return int(await self.pool.fetchval(
            "SELECT COUNT(*) FROM keystone_history WHERE symbol = $1", symbol.upper()))

    # ------------------------------------------------------------------
    # Pass-3 microstructure evidence ledger (postgres-first durable layer).
    # Redis latest-evidence keys are projections of these rows only.
    # ------------------------------------------------------------------

    async def insert_microstructure_evidence(self, evidence: dict[str, Any]) -> bool:
        """Persist one immutable MicrostructureEvidence row (idempotent on id)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO microstructure_evidence
               (symbol, venue, evidence_id, generated_at_ms, schema_version,
                interval_seconds, window_start_ms, window_end_ms, tick_size,
                depth_estimator, input_hash, model_version,
                price_impact_fit, sensitivity_fit, depth_scaling_fit,
                block_average_depth, coverage, status, evidence)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
               ON CONFLICT (symbol, venue, evidence_id) DO NOTHING
               RETURNING evidence_id""",
            str(evidence["symbol"]).upper(),
            str(evidence["venue"]),
            str(evidence["evidence_id"]),
            int(evidence["generated_at_ms"]),
            int(evidence.get("schema_version") or 1),
            int(evidence["interval_seconds"]),
            int(evidence["window_start_ms"]),
            int(evidence["window_end_ms"]),
            evidence["tick_size"],
            str(evidence["depth_estimator"]),
            str(evidence["input_hash"]),
            str(evidence["model_version"]),
            json.dumps(evidence.get("price_impact_fit")),
            json.dumps(evidence.get("sensitivity_fit")),
            json.dumps(evidence.get("depth_scaling_fit")),
            evidence.get("block_average_depth"),
            json.dumps(evidence.get("coverage") or {}),
            str(evidence["status"]),
            json.dumps(evidence),
        )
        return row is not None

    async def read_microstructure_evidence(
        self, symbol: str, venue: str, evidence_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Durable read: latest evidence for (symbol, venue) or one exact id."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        if evidence_id:
            row = await self.pool.fetchrow(
                """SELECT evidence FROM microstructure_evidence
                   WHERE symbol = $1 AND venue = $2 AND evidence_id = $3""",
                symbol.upper(), venue, evidence_id,
            )
        else:
            row = await self.pool.fetchrow(
                """SELECT evidence FROM microstructure_evidence
                   WHERE symbol = $1 AND venue = $2
                   ORDER BY window_end_ms DESC LIMIT 1""",
                symbol.upper(), venue,
            )
        if row is None:
            return None
        evidence = row["evidence"]
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (ValueError, json.JSONDecodeError):
                return None
        return evidence if isinstance(evidence, dict) else None

    async def list_microstructure_evidence(
        self, symbol: str, venue: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Durable read: the most recent evidence rows for (symbol, venue)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            """SELECT evidence FROM microstructure_evidence
               WHERE symbol = $1 AND venue = $2
               ORDER BY window_end_ms DESC LIMIT $3""",
            symbol.upper(), venue, limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            evidence = row["evidence"]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (ValueError, json.JSONDecodeError):
                    continue
            if isinstance(evidence, dict):
                out.append(evidence)
        return out

    # ------------------------------------------------------------------
    # Inference-engine artifact ledger (postgres-first durable layer).
    # Redis latest-inference keys are projections of these rows only.
    # ------------------------------------------------------------------

    async def insert_inference_artifact(self, artifact: InferenceArtifact) -> bool:
        """Durable, idempotent write of one ``InferenceArtifact``.

        Primary key is ``artifact_id``: each engine cycle produces exactly
        one immutable row. The interpretation column is NULL whenever the
        hard status gate refused the LLM call — that is the durable record
        of the refusal, never a missing value.
        """
        artifact.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO inference_artifact
               (artifact_id, symbol, venue, generated_at, completed_at,
                schema_version, status, window_minutes, interval_seconds,
                deterministic_state, capability_log, input_hash, model_version,
                interpretation, session_id, errors)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
               ON CONFLICT (artifact_id) DO NOTHING
               RETURNING artifact_id""",
            uuid.UUID(artifact.artifact_id),
            artifact.symbol,
            artifact.venue,
            datetime.fromisoformat(artifact.generated_at.replace("Z", "+00:00")),
            datetime.fromisoformat(artifact.completed_at.replace("Z", "+00:00")),
            artifact.schema_version,
            artifact.status,
            artifact.window_minutes,
            artifact.interval_seconds,
            json.dumps(artifact.deterministic_state, default=str),
            json.dumps(list(artifact.capability_log), default=str),
            artifact.input_hash,
            artifact.model_version,
            json.dumps(artifact.interpretation, default=str)
            if artifact.interpretation is not None else None,
            uuid.UUID(artifact.session_id) if artifact.session_id else None,
            json.dumps(list(artifact.errors), default=str),
        )
        return row is not None

    async def read_inference_artifact(
        self, symbol: str | None = None, artifact_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Durable read: one exact artifact_id, or the latest for a symbol."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        if artifact_id:
            row = await self.pool.fetchrow(
                "SELECT deterministic_state, interpretation, status, artifact_id,"
                " symbol, venue, generated_at, input_hash, model_version,"
                " window_minutes, interval_seconds, capability_log, session_id, errors"
                " FROM inference_artifact WHERE artifact_id = $1",
                uuid.UUID(artifact_id),
            )
        elif symbol:
            row = await self.pool.fetchrow(
                "SELECT deterministic_state, interpretation, status, artifact_id,"
                " symbol, venue, generated_at, input_hash, model_version,"
                " window_minutes, interval_seconds, capability_log, session_id, errors"
                " FROM inference_artifact WHERE symbol = $1"
                " ORDER BY generated_at DESC LIMIT 1",
                symbol.upper(),
            )
        else:
            return None
        if row is None:
            return None
        result = dict(row)
        for col in ("deterministic_state", "capability_log", "errors"):
            value = result.get(col)
            if isinstance(value, str):
                try:
                    result[col] = json.loads(value)
                except (ValueError, json.JSONDecodeError):
                    result[col] = {} if col == "deterministic_state" else []
        interp = result.get("interpretation")
        if isinstance(interp, str):
            try:
                result["interpretation"] = json.loads(interp)
            except (ValueError, json.JSONDecodeError):
                result["interpretation"] = None
        if hasattr(result.get("generated_at"), "isoformat"):
            result["generated_at"] = result["generated_at"].isoformat()
        return result

    async def read_inference_history(
        self, symbol: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Durable read: recent artifacts for one symbol, newest first."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT artifact_id, symbol, venue, generated_at, status,"
            " window_minutes, interval_seconds, input_hash, model_version"
            " FROM inference_artifact WHERE symbol = $1"
            " ORDER BY generated_at DESC LIMIT $2",
            symbol.upper(), limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            if hasattr(entry.get("generated_at"), "isoformat"):
                entry["generated_at"] = entry["generated_at"].isoformat()
            out.append(entry)
        return out
