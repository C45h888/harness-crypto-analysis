CREATE TABLE IF NOT EXISTS market_snapshot (
    id BIGSERIAL PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    price NUMERIC NOT NULL,
    spot_buy_share NUMERIC,
    futures_buy_share NUMERIC,
    spot_cvd_usd NUMERIC,
    futures_cvd_usd NUMERIC,
    spot_obi_top_n NUMERIC,
    futures_obi_top_n NUMERIC,
    funding_rate NUMERIC,
    mark_price NUMERIC,
    index_price NUMERIC,
    open_interest NUMERIC,
    flow_window_seconds INTEGER NOT NULL,
    spot_flow_coverage_seconds INTEGER,
    futures_flow_coverage_seconds INTEGER,
    source_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_snapshot_symbol_time_unique UNIQUE (symbol, observed_at)
);

CREATE INDEX IF NOT EXISTS market_snapshot_symbol_observed_at_idx
    ON market_snapshot (symbol, observed_at DESC);

CREATE TABLE IF NOT EXISTS signal_event (
    id BIGSERIAL PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    severity SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),
    summary TEXT NOT NULL,
    evidence JSONB NOT NULL,
    snapshot_id BIGINT REFERENCES market_snapshot(id) ON DELETE SET NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT signal_event_dedup UNIQUE (symbol, observed_at, signal_type)
);

CREATE INDEX IF NOT EXISTS signal_event_symbol_observed_at_idx
    ON signal_event (symbol, observed_at DESC);

CREATE VIEW latest_market_state AS
SELECT DISTINCT ON (symbol) *
FROM market_snapshot
ORDER BY symbol, observed_at DESC;

CREATE TABLE IF NOT EXISTS market_run (
    run_id UUID PRIMARY KEY,
    symbol TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'invalid')),
    data_source TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    canonical_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    domain_outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    envelope JSONB NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS market_run_symbol_completed_at_idx
    ON market_run (symbol, completed_at DESC);

CREATE INDEX IF NOT EXISTS market_run_status_idx
    ON market_run (status);

CREATE INDEX IF NOT EXISTS market_run_data_source_idx
    ON market_run (data_source);

-- Durable wall-history seam. Each row is one cycle's wall snapshot;
-- mirrors the discipline of market_run (postgres-first, exact-run,
-- schema-versioned). Layer C surfaces this through
-- PostgresRuntimeStore.record_wall_snapshot / read_last_wall_snapshot.
CREATE TABLE IF NOT EXISTS wall_snapshot (
    symbol TEXT NOT NULL,
    cycle_ts TIMESTAMPTZ NOT NULL,
    run_id UUID NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    asks JSONB NOT NULL DEFAULT '[]'::jsonb,
    bids JSONB NOT NULL DEFAULT '[]'::jsonb,
    fuel_ratio DOUBLE PRECISION NOT NULL,
    bid_pool DOUBLE PRECISION NOT NULL,
    ask_pool DOUBLE PRECISION NOT NULL,
    bid_floor DOUBLE PRECISION NOT NULL,
    ask_target DOUBLE PRECISION NOT NULL,
    ask_walls_built INTEGER NOT NULL DEFAULT 0,
    ask_walls_eroded INTEGER NOT NULL DEFAULT 0,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, cycle_ts)
);

CREATE INDEX IF NOT EXISTS wall_snapshot_run_id_idx
    ON wall_snapshot (run_id);

CREATE INDEX IF NOT EXISTS wall_symbol_completed_at_idx
    ON wall_snapshot (symbol, cycle_ts DESC);

-- Durable record for validated AnalystBriefing artifacts produced by the
-- NOOA analyst suite. Primary key is (session_id, run_id) so the same
-- analyst session over the same canonical envelope updates in place;
-- the run_id field links the briefing back to the immutable market_run
-- envelope it was produced from. Mirrors the discipline of market_run:
-- schema-versioned, postgres-first, no FK to market_run (the agent layer
-- must not be able to corrupt canonical state by deleting a briefing).
CREATE TABLE IF NOT EXISTS analyst_briefing (
    session_id UUID NOT NULL,
    run_id UUID NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version IN (1, 2)),
    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    briefing JSONB NOT NULL,
    parse_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    envelope_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, run_id)
);

CREATE INDEX IF NOT EXISTS analyst_briefing_run_id_idx
    ON analyst_briefing (run_id);

CREATE INDEX IF NOT EXISTS analyst_briefing_generated_at_idx
    ON analyst_briefing (generated_at DESC);

-- Durable ledger for the MemoryNode — the analyst's own curated knowledge
-- (observations, hypotheses, requests, briefings, facts/notes) so later
-- analyst cycles can recall what the model concluded on earlier runs.
-- Discipline mirrors analyst_briefing: schema-versioned, durable-ledger-first
-- write order, immutable-by-id (re-writing the same memory_id updates fields
-- in place — forget() tombstones with forgotten = TRUE), forgotten rows
-- survive for audit but are excluded from recall, no FK to market_run
-- (the agent layer must not corrupt canonical state by deleting a memory).
-- The payload JSONB carries the full validated AgentMemory.to_dict() so
-- recall/seeding reads one column (same shape as analyst_briefing).
CREATE TABLE IF NOT EXISTS agent_memory (
    memory_id UUID NOT NULL,
    session_id UUID NOT NULL,
    run_id UUID,
    kind TEXT NOT NULL
        CHECK (kind IN ('observation', 'hypothesis', 'request',
                        'briefing', 'fact', 'note')),
    title TEXT,
    content TEXT NOT NULL,
    importance DOUBLE PRECISION NOT NULL DEFAULT 5.0
        CHECK (importance >= 0 AND importance <= 10),
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ,
    forgotten BOOLEAN NOT NULL DEFAULT FALSE,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version IN (1, 2)),
    payload JSONB NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id)
);

CREATE INDEX IF NOT EXISTS agent_memory_session_created_idx
    ON agent_memory (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_memory_run_id_idx
    ON agent_memory (run_id);

CREATE INDEX IF NOT EXISTS agent_memory_kind_idx
    ON agent_memory (session_id, kind, created_at DESC);
