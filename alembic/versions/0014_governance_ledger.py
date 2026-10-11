"""governance_ledger: durable audit of plane governance decisions

Revision ID: 0014_governance_ledger
Revises: 0013_horizon_spans
Create Date: 2026-10-12

The two plane governance FSMs (docs/PLANE_GOVERNANCE_SPEC.md) are the sole
legality authority for the calculation and analysis worker planes. Every
decision they reach — allowed OR denied — lands here. A denial is evidence
about the system and must survive the process that produced it.

Rows are append-only. No backfill: governance decisions accumulate from
launch; pre-migration fires were ungoverned and are honestly absent.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_governance_ledger"
down_revision = "0013_horizon_spans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS governance_ledger;")
