"""keystone_history: cross-cycle keystone ledger (Pass 2 pivot)

Revision ID: 0006_keystone_history
Revises: 0005_contract_v2_schema_versions
Create Date: 2026-08-23

Adds the keystone_history table that backs the cross-cycle keystone
migration ledger. Clean separation from wall_snapshot (buyer defence vs
seller walls). Mirrors the discipline of wall_snapshot (postgres-first,
exact-run, schema-versioned, idempotent on (symbol, cycle_ts)).

Nullable metric columns follow the null discipline: null means the cycle
did not provide a value, never a fabricated zero.

Mirrors the additive block already in db/init/001_schema.sql.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_keystone_history"
down_revision = "0005_contract_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS keystone_history (
            symbol TEXT NOT NULL,
            cycle_ts TIMESTAMPTZ NOT NULL,
            run_id UUID NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            keystone_price DOUBLE PRECISION,
            window_qty DOUBLE PRECISION,
            tight_lo DOUBLE PRECISION,
            tight_hi DOUBLE PRECISION,
            wide_lo DOUBLE PRECISION,
            wide_hi DOUBLE PRECISION,
            keystone_bid_qty DOUBLE PRECISION,
            ask_ladder_notional DOUBLE PRECISION,
            inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, cycle_ts)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS keystone_history_run_id_idx
            ON keystone_history (run_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS keystone_history_symbol_cycle_idx
            ON keystone_history (symbol, cycle_ts DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS keystone_history_symbol_cycle_idx;")
    op.execute("DROP INDEX IF EXISTS keystone_history_run_id_idx;")
    op.execute("DROP TABLE IF EXISTS keystone_history;")
