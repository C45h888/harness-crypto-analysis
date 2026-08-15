"""analyst_briefing: durable record for validated AnalystBriefing artifacts

Revision ID: 0003_analyst_briefing
Revises: 0002_wall_snapshot
Create Date: 2026-08-14

Adds the ``analyst_briefing`` table that backs the seam-closure between
the canonical runtime (which produces immutable ``market_run`` envelopes)
and the NOOA analyst suite (which produces validated ``AnalystBriefing``
artifacts).

Discipline mirrors ``market_run``:
  - schema-versioned (CHECK schema_version = 1);
  - immutable-by-insertion on (session_id, run_id) so a re-run of the
    same session over the same canonical envelope updates in place;
  - Postgres-first write order — the durable row is committed before
    the Redis agent stream is published;
  - no foreign key to ``market_run``: the agent layer must not be able
    to corrupt canonical state by deleting a briefing.

Mirrors the additive block appended to ``db/init/001_schema.sql``.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_analyst_briefing"
down_revision = "0002_wall_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS analyst_briefing (
            session_id UUID NOT NULL,
            run_id UUID NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1
                CHECK (schema_version = 1),
            model_provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            generated_at TIMESTAMPTZ NOT NULL,
            briefing JSONB NOT NULL,
            parse_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
            envelope_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (session_id, run_id)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS analyst_briefing_run_id_idx
            ON analyst_briefing (run_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS analyst_briefing_generated_at_idx
            ON analyst_briefing (generated_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analyst_briefing_generated_at_idx;")
    op.execute("DROP INDEX IF EXISTS analyst_briefing_run_id_idx;")
    op.execute("DROP TABLE IF EXISTS analyst_briefing;")