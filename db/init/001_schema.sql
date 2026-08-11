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
