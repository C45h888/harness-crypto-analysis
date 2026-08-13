"""baseline: create the canonical market schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-13

This is the baseline migration, mirroring the previous single-file schema
(db/init/001_schema.sql) exactly. It is intentionally idempotent
(IF NOT EXISTS / OR REPLACE) so `alembic upgrade head` is safe to run both
against a fresh database and against an existing volume that predates
migrations. Once applied, Alembic records this revision in alembic_version
and every future change becomes a new revision on top of the baseline.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS market_snapshot_symbol_observed_at_idx
            ON market_snapshot (symbol, observed_at DESC);
        """
    )
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS signal_event_symbol_observed_at_idx
            ON signal_event (symbol, observed_at DESC);
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW latest_market_state AS
        SELECT DISTINCT ON (symbol) *
        FROM market_snapshot
        ORDER BY symbol, observed_at DESC;
        """
    )
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS market_run_symbol_completed_at_idx
            ON market_run (symbol, completed_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS market_run_status_idx
            ON market_run (status);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS market_run_data_source_idx
            ON market_run (data_source);
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS latest_market_state;")
    op.execute("DROP TABLE IF EXISTS signal_event;")
    op.execute("DROP TABLE IF EXISTS market_snapshot;")
    op.execute("DROP TABLE IF EXISTS market_run;")
