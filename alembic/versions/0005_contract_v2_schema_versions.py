"""contract v2: relax schema_version CHECK constraints for analyst tables

Revision ID: 0005_contract_v2
Revises: 0004_agent_memory
Create Date: 2026-08-16

The analyst-layer contract tightening (typed EvidenceEntry, Consensus,
KeyEvidence, Disagreement) bumps ``ANALYST_BRIEFING_SCHEMA_VERSION`` and
``SPECIALIST_REPORT_SCHEMA_VERSION`` from 1 to 2. The ``analyst_briefing``
table's CHECK constraint was ``schema_version = 1``; this migration relaxes
it to ``schema_version IN (1, 2)`` so both v1 (legacy) and v2 (typed)
briefings can coexist in the durable ledger.

``agent_memory`` is also relaxed to ``IN (1, 2)`` preemptively — its
schema version has not changed yet, but the memory table stores evidence_refs
that reference briefing key_evidence paths, and the forward-looking contract
tightening may bump it next.

No data migration is needed: the ``briefing`` JSONB column stores the full
``to_dict()`` payload, and ``AnalystBriefing.from_mapping`` handles both v1
(plain dict consensus/evidence) and v2 (typed) payloads transparently via
the ``Consensus.from_mapping`` / ``KeyEvidence.from_mapping`` constructors.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_contract_v2"
down_revision = "0004_agent_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Relax the analyst_briefing schema_version CHECK from = 1 to IN (1, 2).
    op.execute(
        "ALTER TABLE analyst_briefing "
        "DROP CONSTRAINT IF EXISTS analyst_briefing_schema_version_check;"
    )
    op.execute(
        "ALTER TABLE analyst_briefing "
        "ADD CONSTRAINT analyst_briefing_schema_version_check "
        "CHECK (schema_version IN (1, 2));"
    )

    # Relax the agent_memory schema_version CHECK from = 1 to IN (1, 2).
    op.execute(
        "ALTER TABLE agent_memory "
        "DROP CONSTRAINT IF EXISTS agent_memory_schema_version_check;"
    )
    op.execute(
        "ALTER TABLE agent_memory "
        "ADD CONSTRAINT agent_memory_schema_version_check "
        "CHECK (schema_version IN (1, 2));"
    )


def downgrade() -> None:
    # Restore the original CHECK constraints (= 1 only).
    # WARNING: this will fail if any v2 rows exist. Run a data migration
    # to downgrade v2 rows to v1 format before applying this downgrade.
    op.execute(
        "ALTER TABLE agent_memory "
        "DROP CONSTRAINT IF EXISTS agent_memory_schema_version_check;"
    )
    op.execute(
        "ALTER TABLE agent_memory "
        "ADD CONSTRAINT agent_memory_schema_version_check "
        "CHECK (schema_version = 1);"
    )
    op.execute(
        "ALTER TABLE analyst_briefing "
        "DROP CONSTRAINT IF EXISTS analyst_briefing_schema_version_check;"
    )
    op.execute(
        "ALTER TABLE analyst_briefing "
        "ADD CONSTRAINT analyst_briefing_schema_version_check "
        "CHECK (schema_version = 1);"
    )
