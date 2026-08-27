"""dedupe market_run: envelope column becomes reconstructable.

Revision ID: 0007_envelope_dedupe
Revises: 0006_keystone_history
Create Date: 2026-08-27

The ``envelope`` column stored a full JSON copy of the run envelope while
``coverage``, ``canonical_state``, ``domain_outputs`` and ``errors`` were
*also* stored as first-class jsonb columns. Verified live (2026-08-27):
envelope->'canonical_state' == canonical_state (and coverage /
domain_outputs likewise) on every recent row — the envelope column is
100% redundant, ~1.2 GB of the 2.5 GB table.

New writes leave ``envelope`` NULL; reads reconstruct the envelope from
the split columns, falling back to the stored envelope for legacy rows.
This migration only drops the NOT NULL constraint — no data deletion.
Reclaim the dead space afterwards with VACUUM FULL market_run.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_envelope_dedupe"
down_revision = "0006_keystone_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE market_run ALTER COLUMN envelope DROP NOT NULL;")


def downgrade() -> None:
    # Re-backfill any rows written post-dedupe before restoring the
    # constraint so downgrade never fails on NULL envelopes.
    op.execute(
        """
        UPDATE market_run SET envelope = jsonb_build_object(
            'schema_version', schema_version,
            'run_id', run_id,
            'symbol', symbol,
            'generated_at', generated_at,
            'completed_at', completed_at,
            'status', status,
            'data_source', data_source,
            'coverage', coverage,
            'canonical_state', canonical_state,
            'domain_outputs', domain_outputs,
            'errors', errors,
            'source_metadata', source_metadata
        ) WHERE envelope IS NULL;
        """
    )
    op.execute("ALTER TABLE market_run ALTER COLUMN envelope SET NOT NULL;")
