"""canonical_output — THE deliverable plane (the answer + its run record).

Revision ID: 0017_canonical_output
Revises: 0016_substrate_state_schema_v2
Create Date: 2026-10-19

Canonical output doctrine (docs/CLI_ANSWER_SURFACE_PLAN.md → canonical
path): one row per engine cycle carries the frozen ANSWER text of record
(``answer_text`` — exactly what shipped to the CLI surface), its structured
variant (``answer``), and the RUN RECORD (``run_record``: deterministic
state, capability log, errors) which is engine bookkeeping, never the
consumable output.

The ``inference_artifact`` table stays untouched: its rows are deprecated
read-only legacy (pre-canonical cycles). Nothing writes them anymore.

Mirrors db/init/001_schema.sql (canonical_output block).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0017_canonical_output"
down_revision = "0016_substrate_state_schema_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS canonical_output (
            run_id UUID PRIMARY KEY,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            generated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            status TEXT NOT NULL
                CHECK (status IN ('validated', 'provisional', 'insufficient')),
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
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS canonical_output_symbol_time_idx"
        " ON canonical_output (symbol, venue, completed_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS canonical_output_symbol_idx"
        " ON canonical_output (symbol, completed_at DESC);"
    )


def downgrade() -> None:
    # The canonical output table is the deliverable plane — dropping it drops
    # the answers of record. inference_artifact legacy rows are unaffected.
    op.execute("DROP INDEX IF EXISTS canonical_output_symbol_idx;")
    op.execute("DROP INDEX IF EXISTS canonical_output_symbol_time_idx;")
    op.execute("DROP TABLE IF EXISTS canonical_output;")
