"""substrate_calculation: durable substrate-worker calculation ledger

Revision ID: 0011_substrate_calculation
Revises: 0010_inference_hypothesis
Create Date: 2026-09-08

Adds the substrate_calculation table backing the Phase 3 PG-first write
path (``market_service.substrate_worker``). Every worker fire inserts its
``SubstrateStatePayload`` here BEFORE publishing the Redis projection, so
substrate state survives Redis restarts. Mirrors the discipline of
``wall_snapshot`` (postgres-first, schema-versioned).

Numbering note: the handoff called this 0010, but 0010_inference_hypothesis
already occupies that slot — this is 0011 with identical content.

Mirrors the additive block already in db/init/001_schema.sql.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_substrate_calculation"
down_revision = "0010_inference_hypothesis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS substrate_calculation_symbol_substrate_idx
            ON substrate_calculation (symbol, substrate, computed_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS substrate_calculation_symbol_substrate_idx;")
    op.execute("DROP TABLE IF EXISTS substrate_calculation;")
