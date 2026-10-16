"""substrate_calculation: accept schema_version 2 (registry-aligned).

Revision ID: 0016_substrate_state_schema_v2
Revises: 0015_agent_memory_carry_kinds
Create Date: 2026-10-16

The substrate-state contract version moved 1 -> 2
(``runtime.contracts.SCHEMA_VERSION_REGISTRY["substrate_state"]``, resolved
by identity in ``substrate_worker/contracts.py``) with no accompanying
migration, so every worker fire aborted on the 0011 inline CHECK
(``substrate_calculation_schema_version_check`` pins ``= 1``) and the
calculation plane went unhealthy.

This migration widens the CHECK to ``IN (1, 2)`` (precedent: wall/keystone
snapshot tables already carry the (1, 2) window in db/init/001_schema.sql)
so in-flight v1 rows stay valid while v2 writers land, and moves the column
DEFAULT to 2 to match the registry. The standing debt is unchanged: the
CHECK literal must be re-widened on every registry bump (a registry-derived
constraint is a later refactor, not this migration).

Mirrors db/init/001_schema.sql (substrate_calculation block).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0016_substrate_state_schema_v2"
down_revision = "0015_agent_memory_carry_kinds"
branch_labels = None
depends_on = None

_CONSTRAINT = "substrate_calculation_schema_version_check"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE substrate_calculation "
        f"DROP CONSTRAINT IF EXISTS {_CONSTRAINT};"
    )
    op.execute(
        f"ALTER TABLE substrate_calculation "
        f"ADD CONSTRAINT {_CONSTRAINT} CHECK (schema_version IN (1, 2));"
    )
    op.execute(
        "ALTER TABLE substrate_calculation "
        "ALTER COLUMN schema_version SET DEFAULT 2;"
    )


def downgrade() -> None:
    # Narrow back to the 0011 contract. v2 rows already written will violate
    # the restored CHECK on UPDATE, but no rows are touched by the DDL itself.
    op.execute(
        "ALTER TABLE substrate_calculation "
        "ALTER COLUMN schema_version SET DEFAULT 1;"
    )
    op.execute(
        f"ALTER TABLE substrate_calculation "
        f"DROP CONSTRAINT IF EXISTS {_CONSTRAINT};"
    )
    op.execute(
        f"ALTER TABLE substrate_calculation "
        f"ADD CONSTRAINT {_CONSTRAINT} CHECK (schema_version = 1);"
    )
