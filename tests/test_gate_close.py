"""Gate CLOSE verdicts — a loop close is a gate, not a receipt type.

Pins the consolidated surface:
  - closure vocabulary lives in ``gates.GateDisposition``;
  - ``close_loop`` records a VERDICT row on the engine ledger;
  - the projection ``loop_receipts`` is derived from those rows;
  - no second record type exists (``engine.receipts`` is gone).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from market_service.nooa_harness.engine import gates
from market_service.nooa_harness.engine.loop_states import (
    LoopStep,
    NestedLoop,
    SubLoop,
)


def _st(**over):
    st = SimpleNamespace(
        deterministic_state={}, ledger_stage_ids=[], loop_receipts=[])
    for key, value in over.items():
        setattr(st, key, value)
    return st


class GateDispositionTests(unittest.TestCase):
    def test_disposition_vocabulary_is_honest(self):
        self.assertEqual(
            {d.value for d in gates.GateDisposition},
            {"completed", "not_applicable", "refused", "denied", "skipped"},
        )


class CloseLoopTests(unittest.TestCase):
    def test_close_records_a_verdict_row(self):
        st = _st()
        verdict = gates.close_loop(
            st, loop=NestedLoop.REASONING, sub_loop=SubLoop.SYNTHESIS,
            steps=(LoopStep.INTERPRET, LoopStep.RECONCILE),
            payload={"reason_position": 3}, refs=("wake-1",))
        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.gate, gates.GateKind.CLOSE)
        self.assertEqual(verdict.disposition, gates.GateDisposition.COMPLETED)
        rows = st.deterministic_state["ledger"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "verdict")
        payload = rows[0]["payload"]
        self.assertEqual(payload["gate"], "close")
        self.assertEqual(payload["disposition"], "completed")
        self.assertEqual(payload["facts"]["loop"], "reasoning")
        self.assertEqual(payload["facts"]["sub_loop"], "synthesis")
        self.assertEqual(payload["facts"]["reason_position"], 3)
        self.assertEqual(payload["refs"], ["wake-1"])
        self.assertTrue(st.ledger_stage_ids[0].startswith("verdict:close#"))

    def test_refused_close_is_a_refused_verdict(self):
        st = _st()
        verdict = gates.close_loop(
            st, loop=NestedLoop.VALIDATION, sub_loop=SubLoop.RECOVERY,
            disposition=gates.GateDisposition.REFUSED,
            payload={"missing": ["x"]})
        self.assertTrue(verdict.refused)
        self.assertEqual(verdict.disposition, gates.GateDisposition.REFUSED)
        # A close refusal is never routed to a terminal.
        self.assertIsNone(gates.route_refusal(verdict))

    def test_close_accepts_a_bare_state_dict(self):
        state: dict = {}
        verdict = gates.close_loop(
            state, loop=NestedLoop.OUTPUT, sub_loop=SubLoop.MEMORY,
            payload={"delivered": True})
        self.assertTrue(verdict.passed)
        self.assertEqual(state["ledger"][0]["kind"], "verdict")


class NoReceiptTypeTests(unittest.TestCase):
    def test_receipts_module_is_gone(self):
        import importlib
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("market_service.nooa_harness.engine.receipts")


if __name__ == "__main__":
    unittest.main()
