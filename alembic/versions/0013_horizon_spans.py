"""horizon_spans: durable per-horizon deterministic blocks

Revision ID: 0013_horizon_spans
Revises: 0012_microstructure_event_tape
Create Date: 2026-10-07

Per-horizon keys are sibling projections of the collated ledger, not
replacements: the audit unit stays run_id, each span carries its own run
back-pointer. Latest-per-(symbol,horizon) is the durable fallback when
the Redis span key is absent. No backfill: pre-migration runs are
headline-only legacy; spans accumulate from launch.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_horizon_spans"
down_revision = "0012_microstructure_event_tape"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS horizon_span (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            horizon TEXT NOT NULL,
            run_id TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            regime TEXT NOT NULL,
            status TEXT NOT NULL,
            computed_at_ms BIGINT NOT NULL,
            span JSONB NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (symbol, horizon, computed_at_ms)
        );
        CREATE INDEX IF NOT EXISTS ix_horizon_span_symbol_horizon
            ON horizon_span (symbol, horizon, computed_at_ms DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS horizon_span;")
