"""Drop dead planes: market_run, market_snapshot, inference_artifact, horizon_span.

Revision ID: 0019_drop_dead_planes
Revises: 0018_runtime_telemetry_event
Create Date: 2026-10-22

The collated-run plane (market_run / market_snapshot) has no writer —
``publish_run`` / ``insert_run`` / ``assemble_envelope`` are deleted and the
table counts on the live runtime are 0. The ``inference_artifact`` plane is
superseded by ``canonical_output`` (2 stale legacy rows, no writers), and
the ``horizon_span`` persistence plane never gained a writer (0 rows; the
horizon *compute* in runtime/horizon_spans is untouched — only the dead
persistence table goes).

Drops are IF EXISTS and order-independent; downgrade is a no-op (dead
planes are not resurrected — restore from backup if forensics need them).
"""

from alembic import op

revision = "0019_drop_dead_planes"
down_revision = "0018_runtime_telemetry_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # market_snapshot has live dependents: drop them first. The FK from
    # signal_event is nullable bookkeeping (writes leave it NULL); the
    # latest_market_state view has no code readers (schema-only).
    op.execute(
        "ALTER TABLE IF EXISTS signal_event "
        "DROP CONSTRAINT IF EXISTS signal_event_snapshot_id_fkey;"
    )
    op.execute("DROP VIEW IF EXISTS latest_market_state;")
    op.execute("DROP TABLE IF EXISTS market_snapshot;")
    op.execute("DROP TABLE IF EXISTS market_run;")
    op.execute("DROP TABLE IF EXISTS inference_artifact;")
    op.execute("DROP TABLE IF EXISTS horizon_span;")


def downgrade() -> None:
    pass
