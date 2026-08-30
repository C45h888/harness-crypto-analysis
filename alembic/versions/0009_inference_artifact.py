"""inference_artifact: durable ledger for the inference engine's artifacts

Revision ID: 0009_inference_artifact
Revises: 0008_microstructure_evidence
Create Date: 2026-08-28

Adds the inference_artifact table backing the new inference-engine runtime
(pass A of the agent role shift: interpretation layer -> inference engine).
Each row is one engine cycle: the deterministic state the engine computed
itself (never LLM-produced), the capability dispatch audit trail, and the
one bounded LLM narration of that state. Mirrors the discipline of
``microstructure_evidence`` (postgres-first, schema-versioned, null
discipline). Redis latest-inference keys are projections of this table only.

Mirrors the additive block already in db/init/001_schema.sql.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_inference_artifact"
down_revision = "0008_microstructure_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS inference_artifact_symbol_generated_idx
            ON inference_artifact (symbol, venue, generated_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS inference_artifact_session_idx
            ON inference_artifact (session_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS inference_artifact_session_idx;")
    op.execute("DROP INDEX IF EXISTS inference_artifact_symbol_generated_idx;")
    op.execute("DROP TABLE IF EXISTS inference_artifact;")
