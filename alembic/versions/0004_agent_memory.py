"""agent_memory: durable ledger for MemoryNode artifacts (AgentMemory)

Revision ID: 0004_agent_memory
Revises: 0003_analyst_briefing
Create Date: 2026-08-15

Adds the ``agent_memory`` table that backs the harness memory node. The
node persists the analyst's own curated knowledge (observations,
hypotheses, requests, briefings, facts/notes) so later analyst cycles can
recall what the model concluded on earlier runs before reasoning again.

Discipline mirrors ``analyst_briefing``:
  - schema-versioned (CHECK schema_version = 1);
  - durable-ledger-first write order — the Postgres row commits before the
    Redis ``marketflow:agent:<SESSION_ID>:memory`` stream is published;
  - immutable-by-id: re-writing the same ``memory_id`` updates fields in
    place (e.g. ``forget`` tombstones with ``forgotten = TRUE``) instead of
    duplicating;
  - ``forgotten`` rows survive for audit but are excluded from recall;
  - no FK to ``market_run``: the agent layer must not be able to corrupt
    canonical state by deleting a memory.

``payload`` carries the full validated ``AgentMemory.to_dict()`` so the
seed/recall paths read one JSONB column (same shape as ``analyst_briefing``).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_agent_memory"
down_revision = "0003_analyst_briefing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_memory (
            memory_id UUID NOT NULL,
            session_id UUID NOT NULL,
            run_id UUID,
            kind TEXT NOT NULL
                CHECK (kind IN ('observation', 'hypothesis', 'request',
                                'briefing', 'fact', 'note')),
            title TEXT,
            content TEXT NOT NULL,
            importance DOUBLE PRECISION NOT NULL DEFAULT 5.0
                CHECK (importance >= 0 AND importance <= 10),
            tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ,
            forgotten BOOLEAN NOT NULL DEFAULT FALSE,
            schema_version INTEGER NOT NULL DEFAULT 1
                CHECK (schema_version = 1),
            payload JSONB NOT NULL,
            inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (memory_id)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS agent_memory_session_created_idx
            ON agent_memory (session_id, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS agent_memory_run_id_idx
            ON agent_memory (run_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS agent_memory_kind_idx
            ON agent_memory (session_id, kind, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS agent_memory_kind_idx;")
    op.execute("DROP INDEX IF EXISTS agent_memory_run_id_idx;")
    op.execute("DROP INDEX IF EXISTS agent_memory_session_created_idx;")
    op.execute("DROP TABLE IF EXISTS agent_memory;")