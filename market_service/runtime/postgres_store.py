"""Typed PostgreSQL adapter for durable market snapshots and signals."""

from __future__ import annotations

from typing import Any
import json

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
            envelope.run_id, envelope.symbol, envelope.generated_at,
            envelope.completed_at, envelope.status, envelope.data_source,
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
