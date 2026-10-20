"""Gate substrate — one home, one verdict type, one refusal router."""

from __future__ import annotations

import ast
import pathlib
import unittest

from market_service.nooa_harness.engine import gates
from market_service.nooa_harness.engine.ledger import (
    CONTEXT_KINDS,
    LedgerKind,
    record_finding,
    render_ledger,
)


class RegistryTests(unittest.TestCase):
    def test_every_kind_has_a_worker(self):
        self.assertEqual(set(gates.GATE_WORKERS), set(gates.GateKind))

    def test_unknown_kind_fails_closed(self):
        verdict = gates.evaluate("not-a-kind", {})  # type: ignore[arg-type]
        self.assertTrue(verdict.refused)
        self.assertIn("no worker registered", verdict.reasons[0])


class RoutingTests(unittest.TestCase):
    def test_refusal_router_is_the_single_mapping(self):
        self.assertEqual(gates.route_refusal(gates.GateVerdict(
            gate=gates.GateKind.FRAMING, status=gates.GateStatus.REFUSED)),
            "gate_refused")
        self.assertEqual(gates.route_refusal(gates.GateVerdict(
            gate=gates.GateKind.PLAN, status=gates.GateStatus.REFUSED)),
            "gate_refused")
        self.assertEqual(gates.route_refusal(gates.GateVerdict(
            gate=gates.GateKind.CONTENT, status=gates.GateStatus.REFUSED)),
            "validation_failed")
        self.assertEqual(gates.route_refusal(gates.GateVerdict(
            gate=gates.GateKind.CHAIN, status=gates.GateStatus.REFUSED)),
            "validation_failed")

    def test_budget_and_close_are_non_terminal(self):
        for kind in (gates.GateKind.BUDGET, gates.GateKind.CLOSE):
            self.assertIsNone(gates.route_refusal(gates.GateVerdict(
                gate=kind, status=gates.GateStatus.REFUSED)))

    def test_passed_verdict_has_no_route(self):
        self.assertIsNone(gates.route_refusal(gates.GateVerdict(
            gate=gates.GateKind.PLAN, status=gates.GateStatus.PASSED)))


class PurityTests(unittest.TestCase):
    def test_identical_snapshots_identical_verdicts(self):
        snapshot = {"tool_rounds_used": 3, "dispatched_in_pass": 1,
                    "max_tool_rounds": 11, "max_dispatches_per_pass": 3}
        first = gates.evaluate(gates.GateKind.BUDGET, snapshot)
        second = gates.evaluate(gates.GateKind.BUDGET, snapshot)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_worker_fault_fails_closed(self):
        original = gates.GATE_WORKERS[gates.GateKind.DATA]

        class _Boom(gates.GateWorker):
            kind = gates.GateKind.DATA

            def evaluate(self, snapshot):
                raise RuntimeError("boom")

        gates.GATE_WORKERS[gates.GateKind.DATA] = _Boom()
        try:
            verdict = gates.evaluate(gates.GateKind.DATA, {})
        finally:
            gates.GATE_WORKERS[gates.GateKind.DATA] = original
        self.assertTrue(verdict.refused)
        self.assertIn("gate_worker_error:RuntimeError", verdict.reasons[0])

    def test_no_module_level_core_imports(self):
        # Workers take snapshots. The only core imports are two lazy, pure
        # leaf helpers inside the content worker (documented exception).
        src = pathlib.Path(gates.__file__).read_text()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertFalse(node.module.startswith("core"),
                                 f"top-level core import: {node.module}")


class FramingTests(unittest.TestCase):
    def _snapshot(self, **over):
        base = {"seed_plan": {"x": 1}, "task_workflow": {"kind": "general"},
                "seeded_plan_state": {"x": 1},
                "task_workflow_state": {"kind": "general"},
                "comprehension": {"x": 1}, "understanding": {"x": 1}}
        base.update(over)
        return base

    def test_complete_framing_passes(self):
        self.assertTrue(gates.evaluate(gates.GateKind.FRAMING,
                                       self._snapshot()).passed)

    def test_missing_understanding_refuses(self):
        verdict = gates.evaluate(gates.GateKind.FRAMING,
                                 self._snapshot(understanding=None))
        self.assertTrue(verdict.refused)
        self.assertIn("understanding", verdict.reasons[0])


class PromptIsolationTests(unittest.TestCase):
    def test_verdict_and_finding_rows_never_render_as_context(self):
        state = {}
        gates.record_verdict(gates.GateVerdict(
            gate=gates.GateKind.PLAN, status=gates.GateStatus.PASSED), state)
        record_finding(state, "validation", "thin summary")
        rendered = render_ledger(state)
        self.assertEqual(rendered, "")
        kinds = [row["kind"] for row in state["ledger"]]
        self.assertIn(LedgerKind.VERDICT.value, kinds)
        self.assertIn(LedgerKind.FINDING.value, kinds)
        self.assertNotIn(LedgerKind.VERDICT, CONTEXT_KINDS)
        self.assertNotIn(LedgerKind.FINDING, CONTEXT_KINDS)


if __name__ == "__main__":
    unittest.main()
