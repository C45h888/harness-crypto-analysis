"""agent_memory_carry_kinds: add loop-carry kinds to agent_memory.kind CHECK

Revision ID: 0015_agent_memory_carry_kinds
Revises: 0014_governance_ledger
Create Date: 2026-10-14

The inference-engine refactor extended the code contract
``runtime.contracts.ValidMemoryKinds`` (contracts.py) with three loop-internal
carry kinds that the runtime writes between loops — never proposed by the
LLM:

  - "understanding" — the frozen comprehension receipt placed at the
      comprehension -> acquisition handoff (runner._place_understanding).
  - "plan"          — the disposed task plan the acquisition loop is bound
      to (state redistribution: FRAMING -> memory segment).
  - "handoff"       — the gathered context package + reasoning needs
      written at the end of acquisition for the reasoning loop.

The 0004_agent_memory CHECK constraint was authored only for the LLM-proposed
kinds (observation/hypothesis/request/briefing/fact/note) and was never
expanded, so every one of the new carry writes failed with

    CheckViolationError: check constraint "agent_memory_kind_check"

This migration brings the durable ledger in line with the code contract so
the runtime's cross-loop carry persistence actually lands (and the reasoning
loop's partitioning recall for {understanding, plan, handoff} stops coming
back empty).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_agent_memory_carry_kinds"
down_revision = "0014_governance_ledger"
branch_labels = None
depends_on = None

_EXISTING_KINDS = (
    "'observation'", "'hypothesis'", "'request'", "'briefing'",
    "'fact'", "'note'",
)
_CARRY_KINDS = ("'understanding'", "'plan'", "'handoff'")
_FULL_KINDS = ", ".join(_EXISTING_KINDS + _CARRY_KINDS)
# Constraint name as authored by migration 0004_agent_memory.
_CONSTRAINT = "agent_memory_kind_check"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE agent_memory "
        f"DROP CONSTRAINT {_CONSTRAINT};"
    )
    op.execute(
        f"ALTER TABLE agent_memory "
        f"ADD CONSTRAINT {_CONSTRAINT} CHECK (kind IN ({_FULL_KINDS}));"
    )


def downgrade() -> None:
    # Restore the pre-refactor constraint (LLM-proposed kinds only).
    op.execute(
        f"ALTER TABLE agent_memory "
        f"DROP CONSTRAINT {_CONSTRAINT};"
    )
    op.execute(
        f"ALTER TABLE agent_memory "
        f"ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (kind IN ({', '.join(_EXISTING_KINDS)}));"
    )