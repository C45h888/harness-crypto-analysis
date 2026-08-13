"""Typed PostgreSQL adapter for durable market snapshots and signals."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from .contracts import MarketRunEnvelope


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
