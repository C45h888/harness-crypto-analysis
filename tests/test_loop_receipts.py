"""Base tests for loop receipts (engine/receipts.py).

Pins the CANONICAL, engine-authored receipt layer:

  - vocabulary: ``LoopReceipt`` is frozen + JSON-serializable;
  - emission: receipts land on the carrier AND the chartered root;
  - the receipt carries no model prose (no notes, no turn indices);
  - charter wiring: the deterministic root is declared.

The agent's memory node is a separate surface and is deliberately NOT tested
here — the engine does not write receipts into it.
"""

from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace

from market_service.nooa_harness.engine import receipts
from market_service.nooa_harness.engine.loop_states import (
    LoopReceipt,
    LoopStep,
    NestedLoop,
    ReceiptDisposition,
    SubLoop,
)


def _st(**over):
    st = SimpleNamespace(loop_receipts=[], deterministic_state={})
    for key, value in over.items():
        setattr(st, key, value)
    return st


class LoopReceiptVocabularyTests(unittest.TestCase):
    def test_receipt_is_frozen_and_serializable(self):
        receipt = LoopReceipt(
            loop=NestedLoop.REASONING,
            sub_loop=SubLoop.SYNTHESIS,
            steps=(LoopStep.INTERPRET, LoopStep.RECONCILE),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.loop = NestedLoop.OUTPUT
        self.assertEqual(
            receipt.to_dict(),
            {
                "loop": "reasoning",
                "sub_loop": "synthesis",
                "disposition": "completed",
                "steps": ["interpret", "reconcile"],
                "payload": {},
                "evidence_refs": [],
            },
        )

    def test_disposition_vocabulary_is_honest(self):
        self.assertEqual(
            {d.value for d in ReceiptDisposition},
            {"completed", "not_applicable", "refused", "denied", "skipped"},
        )


class EmitReceiptTests(unittest.TestCase):
    def test_emit_writes_carrier_and_chartered_root(self):
        st = _st()
        receipt = receipts.emit_receipt(
            st,
            loop=NestedLoop.CONTEXT,
            sub_loop=SubLoop.FRAMING,
            payload={"plan_kind": "general"},
        )
        self.assertEqual(st.loop_receipts, [receipt])
        self.assertEqual(
            st.deterministic_state["loop_receipts"], [receipt.to_dict()])

    def test_receipt_carries_no_model_prose(self):
        st = _st()
        receipt = receipts.emit_receipt(
            st, loop=NestedLoop.REASONING, sub_loop=SubLoop.SYNTHESIS)
        row = receipt.to_dict()
        self.assertNotIn("turn_indices", row)
        self.assertNotIn("notes", row)
        self.assertEqual(row["payload"], {})

    def test_append_receipt_without_carrier(self):
        state: dict = {}
        receipts.append_receipt(
            state,
            LoopReceipt(loop=NestedLoop.OUTPUT, sub_loop=SubLoop.MEMORY),
        )
        self.assertEqual(len(state["loop_receipts"]), 1)

    def test_receipts_of_reads_the_vector(self):
        state = {"loop_receipts": [{"loop": "x"}]}
        self.assertEqual(receipts.receipts_of(state), [{"loop": "x"}])

    def test_render_block_labels_receipts_canonical(self):
        block = receipts.render_receipts_block([
            {"loop": "reasoning", "sub_loop": "synthesis",
             "disposition": "completed", "steps": ["interpret"]},
        ])
        self.assertIn("canonical", block)
        self.assertIn("[reasoning/synthesis] completed", block)


class CharterWiringTests(unittest.TestCase):
    def test_root_is_declared(self):
        from market_service.nooa_harness.engine.core import context
        self.assertIn("loop_receipts", context.DETERMINISTIC_STATE_ROOTS)


if __name__ == "__main__":
    unittest.main()
