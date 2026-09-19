"""microstructure_event_tape: durable append-only microstructure tape

Revision ID: 0012_microstructure_event_tape
Revises: 0011_substrate_calculation
Create Date: 2026-09-20

Adds the durable EVENT TAPE backing the forward plane. Capture appends every
published best-quote transition (with its additive L2 ladder projection) to
this table so tape reads are no longer bounded by the volatile Redis stream
maxlen. The forward read path merges durable rows with the live stream —
"forced tape read": the deterministic substrate consumes the full retained
history, not just what Redis still holds.

Mirrors the discipline of microstructure_evidence (postgres-first,
schema-versioned payloads, idempotent inserts).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_microstructure_event_tape"
down_revision = "0011_substrate_calculation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS microstructure_event_tape_window_idx
            ON microstructure_event_tape (symbol, venue, exchange_ts_ms);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS microstructure_event_tape;")
