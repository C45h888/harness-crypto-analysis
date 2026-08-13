"""wall_snapshot: durable wall-history table for Layer C

Revision ID: 0002_wall_snapshot
Revises: 0001_baseline
Create Date: 2026-08-13

Adds the wall_snapshot table that backs the prior-cycle seam for
``market_service.analysis.wall_migration``. Mirrors the discipline of
``market_run`` (postgres-first, exact-run, schema-versioned) so
wall history survives Redis restarts.

Mirrors the additive block already in db/init/001_schema.sql.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_wall_snapshot"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wall_snapshot (
            symbol TEXT NOT NULL,
            cycle_ts TIMESTAMPTZ NOT NULL,
            run_id UUID NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            asks JSONB NOT NULL DEFAULT '[]'::jsonb,
            bids JSONB NOT NULL DEFAULT '[]'::jsonb,
            fuel_ratio DOUBLE PRECISION NOT NULL,
            bid_pool DOUBLE PRECISION NOT NULL,
            ask_pool DOUBLE PRECISION NOT NULL,
            bid_floor DOUBLE PRECISION NOT NULL,
            ask_target DOUBLE PRECISION NOT NULL,
            ask_walls_built INTEGER NOT NULL DEFAULT 0,
            ask_walls_eroded INTEGER NOT NULL DEFAULT 0,
            inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, cycle_ts)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS wall_snapshot_run_id_idx
            ON wall_snapshot (run_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS wall_symbol_completed_at_idx
            ON wall_snapshot (symbol, cycle_ts DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS wall_symbol_completed_at_idx;")
    op.execute("DROP INDEX IF EXISTS wall_snapshot_run_id_idx;")
    op.execute("DROP TABLE IF EXISTS wall_snapshot;")