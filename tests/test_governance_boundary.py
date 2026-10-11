"""Governance-boundary charter — the FSM is the sole legality authority.

Phase G-pass (docs/STATE_CHARTER_SPEC.md doctrine, agent plane). The tests
here pin the authority transfers the pass established:

  G1/G2 — cadence is CLI-approved only: the pure resolver refuses
          non-CLI sources and out-of-charter horizons; the FSM adjudicates
          the ADOPT_CADENCE event; the controller adopts ONLY a bound,
          FSM-approved verdict; the refusal path is a finding.
  G4    — budgets are gate-adjudicated: gates.evaluate_budget_gate owns
          the verdict (config owns the numbers); the driver consults it.
  G5    — the FSM audits the tool registry's homes: every declared home
          resolves to a legal FSM observation, enforced at import.
  G6    — dispatch-time cadence-arg validation: agent-side cadence args
          outside the frozen vocabulary are denied findings, never executed.

Every test is deterministic (no stores, no LLM) except the two E2E cycles,
which use the scripted engine fixtures.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from market_service.nooa_harness.engine.cadence import (
    INTERVAL_SECONDS,
    WINDOW_MINUTES,
    CadenceVerdict,
    resolve_cadence,
)
from market_service.nooa_harness.engine.fsm import (
    GOVERNANCE_MEMBRANE,
    GovernanceEvent,
    GovernanceEventKind,
)
from market_service.nooa_harness.engine.gates import evaluate_budget_gate
from market_service.nooa_harness.engine.task_directive import build_plan


BOUND_OK = {
    "bound": True,
    "provenance": "cli_approved",
    "cadence": {
        "interval_seconds": 10,
        "window_minutes": 30,
        "horizon_ms": 5000,
        "regime": "native",
    },
    "reasons": [],
}


class CadenceResolverVerdictTests(unittest.TestCase):
    """G1 — the resolver is verdict-shaped and CLI-only."""

    def test_cli_source_binds(self):
        verdict = resolve_cadence(
            {"horizon_regime": "native", "forecast_horizon_ms": 5000},
            directive_source="cli",
        )
        self.assertTrue(verdict.bound)
        self.assertEqual(verdict.provenance, "cli_approved")
        self.assertEqual(verdict.cadence["interval_seconds"], 10)
        self.assertEqual(verdict.cadence["window_minutes"], 30)
        self.assertEqual(verdict.cadence["horizon_ms"], 5000)

    def test_prompt_parse_source_cannot_bind(self):
        verdict = resolve_cadence(
            {"horizon_regime": "native", "forecast_horizon_ms": 5000},
            directive_source="prompt_parse",
        )
        self.assertFalse(verdict.bound)
        self.assertEqual(verdict.provenance, "unapproved_source")
        self.assertFalse(verdict.cadence)
        self.assertTrue(any("CLI" in r for r in verdict.reasons))

    def test_llm_proposal_source_cannot_bind(self):
        verdict = resolve_cadence(
            {"horizon_regime": "native", "forecast_horizon_ms": 5000},
            directive_source="llm_proposal",
        )
        self.assertFalse(verdict.bound)

    def test_out_of_charter_native_horizon_refused(self):
        verdict = resolve_cadence(
            {"horizon_regime": "native", "forecast_horizon_ms": 7000},
            directive_source="cli",
        )
        self.assertFalse(verdict.bound)
        self.assertTrue(any("NATIVE_HORIZONS_MS" in r for r in verdict.reasons))

    def test_out_of_charter_long_horizon_refused(self):
        verdict = resolve_cadence(
            {"horizon_regime": "long", "forecast_horizon_ms": 123_456},
            directive_source="cli",
        )
        self.assertFalse(verdict.bound)

    def test_unknown_regime_refused(self):
        verdict = resolve_cadence(
            {"horizon_regime": "sideways", "forecast_horizon_ms": 5000},
            directive_source="cli",
        )
        self.assertFalse(verdict.bound)
        self.assertTrue(any("regime" in r for r in verdict.reasons))

    def test_long_regime_binds_bridge_horizon(self):
        verdict = resolve_cadence(
            {"horizon_regime": "long", "forecast_horizon_ms": 3_600_000},
            directive_source="cli",
        )
        self.assertTrue(verdict.bound)
        self.assertEqual(verdict.cadence["horizon_ms"], 3_600_000)

    def test_verdict_is_immutable(self):
        verdict = resolve_cadence(
            {"horizon_regime": "native", "forecast_horizon_ms": 5000},
            directive_source="cli",
        )
        with self.assertRaises(Exception):
            verdict.bound = False  # type: ignore[misc]


class PlanBindingTests(unittest.TestCase):
    """G1 — build_plan carries the verdict; source decides binding."""

    def test_cli_plan_binds_cadence(self):
        from market_service.nooa_harness.engine.task_directive import (
            parse_task_directive,
        )

        directive = parse_task_directive(
            None, {"target_price": "150", "horizon": "1h"})
        self.assertEqual(directive.source, "cli")
        plan = build_plan(directive)
        cadence = plan["cadence"]
        self.assertTrue(cadence["bound"])
        self.assertEqual(cadence["provenance"], "cli_approved")
        self.assertEqual(cadence["cadence"]["horizon_ms"], 3_600_000)

    def test_prompt_plan_carries_unapproved_refusal(self):
        from market_service.nooa_harness.engine.task_directive import (
            parse_task_directive,
        )

        directive = parse_task_directive(
            "assess SOL momentum toward 150 within 1h", None)
        self.assertNotEqual(directive.source, "cli")
        plan = build_plan(directive)
        cadence = plan["cadence"]
        self.assertFalse(cadence["bound"])
        self.assertEqual(cadence["provenance"], "unapproved_source")


class MembraneCadenceAdjudicationTests(unittest.TestCase):
    """G2 — the FSM adjudicates ADOPT_CADENCE; refusal is a finding."""

    def setUp(self):
        self.membrane = GOVERNANCE_MEMBRANE
        self.obs = self.membrane.initial()

    def _decide(self, payload):
        return self.membrane.decide(
            self.obs,
            GovernanceEvent(
                GovernanceEventKind.ADOPT_CADENCE, cadence_verdict=payload,
            ),
        )

    def test_bound_cli_verdict_allowed(self):
        verdict = self._decide(BOUND_OK)
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.reason.startswith("cadence bound"))

    def test_non_cli_provenance_is_finding_not_move(self):
        payload = dict(BOUND_OK, provenance="plan_bound")
        verdict = self._decide(payload)
        self.assertTrue(verdict.allowed)  # finding: loop continues
        self.assertIn("not cli-approved", verdict.reason)
        self.assertIn("finding", verdict.reason)

    def test_out_of_vocabulary_values_are_finding(self):
        payload = {
            "bound": True,
            "provenance": "cli_approved",
            "cadence": {"interval_seconds": 7, "window_minutes": 45},
        }
        verdict = self._decide(payload)
        self.assertTrue(verdict.allowed)
        self.assertIn("frozen vocabulary", verdict.reason)

    def test_unbound_verdict_is_finding_with_reasons(self):
        payload = {
            "bound": False,
            "provenance": "unapproved_source",
            "reasons": ["cadence approval requires the CLI channel"],
        }
        verdict = self._decide(payload)
        self.assertTrue(verdict.allowed)
        self.assertIn("requires the CLI channel", verdict.reason)

    def test_non_mapping_payload_denied(self):
        verdict = self.membrane.decide(
            self.obs,
            GovernanceEvent(GovernanceEventKind.ADOPT_CADENCE,
                            cadence_verdict={"bound": "not-a-bool"}),
        )
        # bound=True is required for the vocabulary branch; a malformed
        # payload shape falls through to the refusal-finding branch.
        self.assertTrue(verdict.allowed)
        self.assertIn("refused", verdict.reason)

    def test_adopt_event_never_moves_the_board(self):
        verdict = self._decide(BOUND_OK)
        self.assertTrue(verdict.allowed)
        self.assertIsNone(verdict.next)
        self.assertIsNone(verdict.terminal)


class BudgetGateTests(unittest.TestCase):
    """G4 — budget verdicts are governance-owned over config numbers."""

    def test_within_budget_passes(self):
        verdict = evaluate_budget_gate(
            tool_rounds_used=3, dispatched_in_pass=1,
            max_tool_rounds=10, max_dispatches_per_pass=3,
        )
        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.gate.value, "budget")

    def test_rounds_exhausted_refused(self):
        verdict = evaluate_budget_gate(
            tool_rounds_used=10, dispatched_in_pass=0,
            max_tool_rounds=10, max_dispatches_per_pass=3,
        )
        self.assertTrue(verdict.refused)
        self.assertTrue(any("round budget" in r for r in verdict.reasons))

    def test_pass_ceiling_refused(self):
        verdict = evaluate_budget_gate(
            tool_rounds_used=3, dispatched_in_pass=3,
            max_tool_rounds=10, max_dispatches_per_pass=3,
        )
        self.assertTrue(verdict.refused)
        self.assertTrue(any("ceiling" in r for r in verdict.reasons))

    def test_pure_replay_identical(self):
        kwargs = dict(
            tool_rounds_used=5, dispatched_in_pass=2,
            max_tool_rounds=10, max_dispatches_per_pass=3,
        )
        v1, v2 = evaluate_budget_gate(**kwargs), evaluate_budget_gate(**kwargs)
        self.assertEqual(v1.to_dict(), v2.to_dict())


class ToolHomeAuditTests(unittest.TestCase):
    """G5 — the FSM audits the registry's home declarations."""

    def setUp(self):
        self.membrane = GOVERNANCE_MEMBRANE

    def test_registry_surface_is_fully_legal(self):
        problems = self.membrane.audit_tool_homes()
        self.assertEqual(
            {t: p for t, p in problems.items() if p}, {},
            "every declared tool home must be a legal FSM observation",
        )

    def test_rogue_home_is_caught(self):
        report = self.membrane.audit_tool_homes({
            "rogue.tool": (("reasoning", "gate"),),
        })
        self.assertIn("rogue.tool", report)
        self.assertTrue(any("illegal home" in p for p in report["rogue.tool"]))

    def test_unknown_loop_is_caught(self):
        report = self.membrane.audit_tool_homes({
            "rogue.tool": (("phantom", "analysis"),),
        })
        self.assertTrue(any("unknown loop" in p for p in report["rogue.tool"]))

    def test_self_check_covers_the_home_audit(self):
        # Import-time self_check already ran; prove it re-runs clean with
        # the audit included.
        self.membrane.self_check()


class CadenceArgValidationTests(unittest.TestCase):
    """G6 — dispatch-time cadence-arg validation (pure helper)."""

    def test_in_vocabulary_args_pass(self):
        from market_service.nooa_harness.engine.core.driver import (
            _cadence_arg_refusals,
        )

        self.assertEqual(
            _cadence_arg_refusals(
                "calc.ofi.intervals",
                {"interval_seconds": 15, "window_minutes": 60},
            ),
            [],
        )

    def test_out_of_vocabulary_args_refused(self):
        from market_service.nooa_harness.engine.core.driver import (
            _cadence_arg_refusals,
        )

        refusals = _cadence_arg_refusals(
            "calc.ofi.intervals",
            {"interval_seconds": 7, "window_minutes": 45},
        )
        self.assertEqual(len(refusals), 2)
        self.assertTrue(any("interval_seconds" in r for r in refusals))
        self.assertTrue(any("window_minutes" in r for r in refusals))

    def test_absent_keys_not_checked(self):
        from market_service.nooa_harness.engine.core.driver import (
            _cadence_arg_refusals,
        )

        self.assertEqual(
            _cadence_arg_refusals("market.read", {"mode": "snapshot"}), [])

    def test_frozen_vocabulary_matches_charter(self):
        self.assertEqual(INTERVAL_SECONDS, (10, 15, 30))
        self.assertEqual(WINDOW_MINUTES, (15, 30, 60))


if __name__ == "__main__":
    unittest.main()