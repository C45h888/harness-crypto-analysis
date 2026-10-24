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
    -- Dedupe pass (0007): new writes leave this NULL; the envelope is
    -- reconstructed from the split columns on read. Legacy rows keep it.
    envelope JSONB,
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
-- Durable ledger for immutable Pass-3 MicrostructureEvidence objects
-- (paper-derived OFI price-impact fits). Postgres-first: the Redis
-- latest-evidence key is only a projection of this table. The full
-- evidence JSON is retained in ``evidence`` for exact replay; the
-- scalar columns exist for querying and the null discipline applies
-- (null coefficient = the fit was insufficient, never zero).
CREATE TABLE IF NOT EXISTS microstructure_evidence (
    symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    generated_at_ms BIGINT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    interval_seconds INTEGER NOT NULL,
    window_start_ms BIGINT NOT NULL,
    window_end_ms BIGINT NOT NULL,
    tick_size NUMERIC NOT NULL,
    depth_estimator TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    model_version TEXT NOT NULL,
    price_impact_fit JSONB,
    sensitivity_fit JSONB,
    depth_scaling_fit JSONB,
    block_average_depth NUMERIC,
    coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL,
    evidence JSONB NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, venue, evidence_id)
);

CREATE INDEX IF NOT EXISTS microstructure_evidence_symbol_window_idx
    ON microstructure_evidence (symbol, venue, window_end_ms DESC);

-- Durable ledger for the inference engine's immutable artifacts. Each row is
-- one engine cycle: the deterministic state the engine computed itself
-- (never LLM-produced), the capability dispatch audit trail, and the one
-- bounded LLM narration of that state. Postgres-first; the Redis
-- latest-inference key is a projection of this table. Null discipline: a
-- NULL interpretation means the hard status gate refused the LLM call
-- (status = 'insufficient'), never "nothing to say".
CREATE TABLE IF NOT EXISTS inference_artifact (
    artifact_id UUID PRIMARY KEY,
    symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    status TEXT NOT NULL CHECK (status IN ('validated', 'provisional', 'insufficient')),
    window_minutes INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    deterministic_state JSONB NOT NULL,
    capability_log JSONB NOT NULL DEFAULT '[]'::jsonb,
    input_hash TEXT NOT NULL,
    model_version TEXT NOT NULL,
    interpretation JSONB,
    session_id UUID,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS inference_artifact_symbol_generated_idx
    ON inference_artifact (symbol, venue, generated_at DESC);

CREATE INDEX IF NOT EXISTS inference_artifact_session_idx
    ON inference_artifact (session_id);

-- Canonical output plane (docs/CLI_ANSWER_SURFACE_PLAN.md → canonical
-- path): ONE row per engine cycle = THE deliverable — the frozen ANSWER
-- text of record (``answer_text``, exactly what shipped to the CLI
-- surface), its structured variant (``answer``), and the RUN RECORD
-- (``run_record``: deterministic state, capability log, errors) which is
-- engine bookkeeping, never the consumable output. The inference_artifact
-- table above is DEPRECATED (read-only legacy rows, pre-canonical cycles).
-- Null discipline: a NULL answer means no authored ship (degraded or
-- gate-failed cycle) — never "nothing to say".
CREATE TABLE IF NOT EXISTS canonical_output (
    run_id UUID PRIMARY KEY,
    symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    status TEXT NOT NULL CHECK (status IN ('validated', 'provisional', 'insufficient')),
    terminal TEXT,
    window_minutes INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    model_version TEXT NOT NULL,
    task TEXT,
    session_id UUID,
    answer_text TEXT,
    answer JSONB,
    interpretation JSONB,
    hypothesis JSONB,
    hypothesis_verdict TEXT CHECK (
        hypothesis_verdict IS NULL
        OR hypothesis_verdict IN ('validated', 'invalidated', 'inconclusive')),
    verdict_reason TEXT,
    calculations JSONB,
    conformance JSONB,
    run_record JSONB NOT NULL DEFAULT '{}'::jsonb,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS canonical_output_symbol_time_idx
    ON canonical_output (symbol, venue, completed_at DESC);

CREATE INDEX IF NOT EXISTS canonical_output_symbol_idx
    ON canonical_output (symbol, completed_at DESC);

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

-- Durable ledger for substrate-worker calculation fires (Phase 3 PG-first
-- path: PostgresRuntimeStore.record_substrate_state). Every worker fire
-- inserts its SubstrateStatePayload here BEFORE the Redis projection is
-- published, so substrate state survives Redis restarts. No retention
-- policy initially (revisit at volume).
CREATE TABLE IF NOT EXISTS substrate_calculation (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    substrate TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_at TIMESTAMPTZ,
    computed_at TIMESTAMPTZ NOT NULL,
    trigger JSONB NOT NULL DEFAULT '{}'::jsonb,
    freshness JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_inputs JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version IN (1, 2)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS substrate_calculation_symbol_substrate_idx
    ON substrate_calculation (symbol, substrate, computed_at DESC);

-- Durable append-only microstructure EVENT TAPE (2026-09-20 forward-plane
-- hardening). Capture appends every published best-quote transition
-- (payload = the same serialized OrderBookEvent the live Redis stream
-- carries, including the additive L2 ladder projection when enabled) so
-- tape reads are no longer bounded by the Redis stream maxlen. The forward
-- replay read path merges durable rows with the live stream. Idempotent
-- inserts; no retention policy initially (revisit at volume).
CREATE TABLE IF NOT EXISTS microstructure_event_tape (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    update_id BIGINT NOT NULL,
    exchange_ts_ms BIGINT NOT NULL,
    received_ts_ms BIGINT NOT NULL,
    payload JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, venue, update_id, exchange_ts_ms)
);

CREATE INDEX IF NOT EXISTS microstructure_event_tape_window_idx
    ON microstructure_event_tape (symbol, venue, exchange_ts_ms);

-- Durable governance ledger for the plane governance FSMs
-- (docs/PLANE_GOVERNANCE_SPEC.md). The two plane FSMs are the sole legality
-- authority for the calculation and analysis worker planes; every decision
-- (allowed or denied) lands here. A denial is evidence about the system and
-- must survive the process that produced it. Append-only; no backfill.
CREATE TABLE IF NOT EXISTS governance_ledger (
    id BIGSERIAL PRIMARY KEY,
    plane TEXT NOT NULL,
    substrate TEXT NOT NULL,
    symbol TEXT NOT NULL,
    gate TEXT NOT NULL,
    allowed BOOLEAN NOT NULL,
    rule TEXT NOT NULL,
    reason TEXT NOT NULL,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    observation JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_governance_ledger_lookup
    ON governance_ledger (plane, substrate, symbol, decided_at DESC);

CREATE INDEX IF NOT EXISTS ix_governance_ledger_denials
    ON governance_ledger (plane, symbol, decided_at DESC)
    WHERE allowed = FALSE;

-- Out-of-band, pure-state telemetry history. This ledger is not canonical
-- market/worker/governance/inference state; Redis holds its live projections.
CREATE TABLE IF NOT EXISTS runtime_telemetry_event (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    plane TEXT NOT NULL,
    owner TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_sequence BIGINT NOT NULL CHECK (source_sequence >= 0),
    occurred_at_ms BIGINT NOT NULL CHECK (occurred_at_ms >= 0),
    source_ref TEXT,
    correlation_id TEXT,
    parent_event_id TEXT,
    state JSONB NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runtime_telemetry_subject_order_idx
    ON runtime_telemetry_event
       (plane, owner, subject_type, subject_id,
        occurred_at_ms, source_sequence, event_id);

CREATE INDEX IF NOT EXISTS runtime_telemetry_trace_idx
    ON runtime_telemetry_event (correlation_id, occurred_at_ms DESC)
    WHERE correlation_id IS NOT NULL;
