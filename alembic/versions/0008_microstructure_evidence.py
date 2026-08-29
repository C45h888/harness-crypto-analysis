"""microstructure_evidence: durable ledger for Pass-3 OFI fit evidence

Revision ID: 0008_microstructure_evidence
Revises: 0007_envelope_dedupe
Create Date: 2026-08-28

Adds the microstructure_evidence table backing the deterministic
paper-derived OFI price-impact fits. Mirrors the discipline of
``wall_snapshot`` / ``keystone_history`` (postgres-first, schema-versioned,
null discipline for insufficient coefficients). Redis latest-evidence keys
are projections of this table only.

Mirrors the additive block already in db/init/001_schema.sql.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_microstructure_evidence"
down_revision = "0007_envelope_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS microstructure_evidence_symbol_window_idx
            ON microstructure_evidence (symbol, venue, window_end_ms DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS microstructure_evidence_symbol_window_idx;")
    op.execute("DROP TABLE IF EXISTS microstructure_evidence;")
