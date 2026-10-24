"""Drop dead chains: keystone_history, wall_snapshot, signal_event.

Revision ID: 0020_drop_dead_chains
Revises: 0019_drop_dead_planes
Create Date: 2026-10-26

The three cross-cycle/derived ledgers have no production readers:
``keystone_history`` / ``wall_snapshot`` lost their last readers when the
agent's ``market.keystone_history`` / ``market.wall_history`` tools and the
operator ``--keystone-history`` path were deleted (ns.read is the only read
surface and no namespace maps to these families), and ``signal_event`` lost
its only writer when the ``record_signal_events`` fan-out was removed from
``record_substrate_state``.

Drops are IF EXISTS and order-independent; downgrade is a no-op (dead
chains are not resurrected — restore from backup for forensics).
"""

from alembic import op

revision = "0020_drop_dead_chains"
down_revision = "0019_drop_dead_planes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS signal_event;")
    op.execute("DROP TABLE IF EXISTS wall_snapshot;")
    op.execute("DROP TABLE IF EXISTS keystone_history;")


def downgrade() -> None:
    pass
