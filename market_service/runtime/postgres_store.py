"""Typed PostgreSQL adapter for durable market snapshots and signals."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from .contracts import AgentMemory, AnalystBriefing, MarketRunEnvelope


class PostgresRuntimeStore:
    """Connection-pool boundary for the durable ledger."""

    def __init__(self, database_url: str):
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
        envelope.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO market_run
               (run_id, symbol, generated_at, completed_at, status, data_source,
                schema_version, coverage, canonical_state, domain_outputs, errors,
                source_metadata, envelope)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (run_id) DO NOTHING
               RETURNING run_id""",
            uuid.UUID(envelope.run_id), envelope.symbol,
            datetime.fromisoformat(envelope.generated_at),
            datetime.fromisoformat(envelope.completed_at), envelope.status, envelope.data_source,
            envelope.schema_version, json.dumps(envelope.coverage),
            json.dumps(envelope.canonical_state), json.dumps(envelope.domain_outputs),
            json.dumps(list(envelope.errors)), json.dumps(envelope.source_metadata or {}),
            envelope.to_json(),
        )
        return row is not None

    async def read_run(self, run_id: str) -> MarketRunEnvelope | None:
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        raw = await self.pool.fetchval("SELECT envelope FROM market_run WHERE run_id = $1", run_id)
        if raw is None:
            return None
        return MarketRunEnvelope.from_mapping(json.loads(raw) if isinstance(raw, str) else raw)

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
        raw = await self.pool.fetchval(
            "SELECT envelope FROM market_run WHERE symbol = $1 ORDER BY completed_at DESC LIMIT 1",
            symbol.upper(),
        )
        if raw is None:
            return None
        return MarketRunEnvelope.from_mapping(json.loads(raw) if isinstance(raw, str) else raw)

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
            "ORDER BY created_at DESC LIMIT $3"
        )
        params: list[Any] = [uuid.UUID(session_id), limit]
        if kind is not None:
            sql = (
                "SELECT payload FROM agent_memory "
                "WHERE session_id = $1 AND kind = $2 AND NOT forgotten "
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
            float(payload.get("fuel_ratio") or 0.0),
            float(payload.get("bid_pool") or 0.0),
            float(payload.get("ask_pool") or 0.0),
            float(payload.get("bid_floor") or 0.0),
            float(payload.get("ask_target") or 0.0),
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

    async def wall_snapshot_count(self, symbol: str) -> int:
        """Diagnostic count for the wall_snapshot table for one symbol."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        return int(await self.pool.fetchval(
            "SELECT COUNT(*) FROM wall_snapshot WHERE symbol = $1", symbol.upper()))
