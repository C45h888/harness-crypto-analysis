"""runtime_telemetry_event: durable out-of-band state-emission history

Revision ID: 0018_runtime_telemetry_event
Revises: 0017_canonical_output
Create Date: 2026-10-19

The table is the telemetry kernel's durable replay source. It is deliberately
separate from canonical market, worker, governance, and inference ledgers.
"""

from alembic import op

revision = "0018_runtime_telemetry_event"
down_revision = "0017_canonical_output"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS runtime_telemetry_trace_idx;")
    op.execute("DROP INDEX IF EXISTS runtime_telemetry_subject_order_idx;")
    op.execute("DROP TABLE IF EXISTS runtime_telemetry_event;")
