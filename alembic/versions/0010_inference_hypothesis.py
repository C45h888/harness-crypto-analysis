"""inference_artifact: hypothesis validation layer (Pass C)

Revision ID: 0010_inference_hypothesis
Revises: 0009_inference_artifact
Create Date: 2026-09-01

Adds paper-centered hypothesis validation to the inference engine.
The engine now forms a hypothesis via MemoryNode paper recall,
validates it with split AD/OFI calculations, and the final
DeltaP formula remains a derived statistical hypothesis, not a shortcut.

- hypothesis: JSONB {H0, H1, evidence_refs, paper_refs}
- hypothesis_verdict: TEXT validated|invalidated|inconclusive
- verdict_reason: TEXT
- calculations: JSONB {ofi_blocks, ad_blocks, observations_preview, derived_diagnostic}
Mirrors additive pattern: nullable, backward compatible.
"""

from alembic import op

revision = "0010_inference_hypothesis"
down_revision = "0009_inference_artifact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE inference_artifact
            ADD COLUMN IF NOT EXISTS hypothesis JSONB,
            ADD COLUMN IF NOT EXISTS hypothesis_verdict TEXT
                CHECK (hypothesis_verdict IN ('validated','invalidated','inconclusive')),
            ADD COLUMN IF NOT EXISTS verdict_reason TEXT,
            ADD COLUMN IF NOT EXISTS calculations JSONB;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS inference_artifact_verdict_idx
            ON inference_artifact (hypothesis_verdict);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS inference_artifact_verdict_idx;")
    op.execute(
        """
        ALTER TABLE inference_artifact
            DROP COLUMN IF EXISTS calculations,
            DROP COLUMN IF EXISTS verdict_reason,
            DROP COLUMN IF EXISTS hypothesis_verdict,
            DROP COLUMN IF EXISTS hypothesis;
        """
    )
