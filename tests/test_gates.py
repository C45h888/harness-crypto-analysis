"""Deterministic gate base layer + agent-memory segment contract tests.

The gate layer is pure: identical inputs always yield identical verdicts and
no stores/LLM are touched. The segment field is a transport-safe contract
that must round-trip through ``from_mapping``/``to_dict``.
"""

from __future__ import annotations

import unittest

from market_service.nooa_harness.engine.gates import (
    GateKind,
    GateRefused,
    GateStatus,
    GateVerdict,
    evaluate_plan_gate,
    require_gate,
)
from market_service.runtime.contracts import AgentMemory, ValidMemoryKinds


class PlanGateTests(unittest.TestCase):
    def test_native_plan_with_horizon_passes(self):
        verdict = evaluate_plan_gate({
            "kind": "price_target", "horizon_regime": "native",
            "forecast_horizon_ms": 5000, "directive_refusals": [],
        })
        self.assertIs(verdict.gate, GateKind.PLAN)
        self.assertTrue(verdict.passed)

    def test_general_none_regime_passes_without_horizon(self):
        verdict = evaluate_plan_gate({
            "kind": "general", "horizon_regime": "none",
            "forecast_horizon_ms": None,
        })
        self.assertTrue(verdict.passed)

    def test_native_without_horizon_refuses(self):
        verdict = evaluate_plan_gate({
            "kind": "price_target", "horizon_regime": "native",
            "forecast_horizon_ms": None,
        })
        self.assertTrue(verdict.refused)
        self.assertTrue(any("forecast_horizon_ms" in r for r in verdict.reasons))

    def test_unknown_kind_refuses(self):
        verdict = evaluate_plan_gate({
            "kind": "make_me_rich", "horizon_regime": "none",
        })
        self.assertTrue(verdict.refused)

    def test_non_mapping_refuses(self):
        verdict = evaluate_plan_gate(None)
        self.assertTrue(verdict.refused)
        self.assertIs(verdict.status, GateStatus.REFUSED)

    def test_directive_refusals_are_facts_not_refusals(self):
        verdict = evaluate_plan_gate({
            "kind": "general", "horizon_regime": "none",
            "directive_refusals": [{"field": "horizon", "reason": "unparseable"}],
        })
        self.assertTrue(verdict.passed)
        self.assertEqual(
            len(verdict.facts_dict()["directive_refusals"]), 1,
        )


class RequireGateTests(unittest.TestCase):
    def test_require_returns_passed_verdict(self):
        verdict = evaluate_plan_gate({"kind": "general", "horizon_regime": "none"})
        self.assertIs(require_gate(verdict), verdict)

    def test_require_raises_on_refused(self):
        verdict = evaluate_plan_gate(None)
        with self.assertRaises(GateRefused) as ctx:
            require_gate(verdict)
        self.assertIs(ctx.exception.verdict, verdict)


class MemorySegmentContractTests(unittest.TestCase):
    def test_agent_kinds_valid_and_engine_carries_are_not(self):
        # The AGENT's own kinds are writable memory-node kinds.
        for kind in ("observation", "hypothesis", "note", "fact"):
            self.assertIn(kind, ValidMemoryKinds)
        # The engine's loop carries live on the CENTRAL LEDGER now
        # (engine/ledger.py) — they are NOT memory-node kinds.
        for kind in ("understanding", "plan", "handoff"):
            self.assertNotIn(kind, ValidMemoryKinds)

    def test_segment_round_trips(self):
        memory = AgentMemory(
            session_id="00000000-0000-0000-0000-000000000001",
            kind="note", content="plan body", segment="task-abc123",
        )
        restored = AgentMemory.from_mapping(memory.to_dict())
        self.assertEqual(restored.segment, "task-abc123")

    def test_default_segment_is_none(self):
        memory = AgentMemory(
            session_id="00000000-0000-0000-0000-000000000001",
            kind="note", content="hello",
        )
        self.assertIsNone(memory.segment)

    def test_legacy_payload_without_segment_reads_none(self):
        memory = AgentMemory(
            session_id="00000000-0000-0000-0000-000000000001",
            kind="note", content="hello",
        )
        payload = memory.to_dict()
        payload.pop("segment")
        self.assertIsNone(AgentMemory.from_mapping(payload).segment)


if __name__ == "__main__":
    unittest.main()
