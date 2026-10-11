"""Deterministic principles — advisory guard, never a gate.

Phase R7: the fetch-window principle retired with the fetch semantic;
the remaining seed set is hypothesis_timing + scenario_coverage.
"""

import unittest

from market_service.nooa_harness.engine.controller import CycleController
from market_service.nooa_harness.engine.principles import (
    PrincipleFinding,
    evaluate_principles,
    render_findings,
)


def _controller():
    return CycleController(None)


class HypothesisTimingTests(unittest.TestCase):
    def test_early_h0_is_a_finding(self):
        findings = evaluate_principles(
            controller=_controller(), plan={"kind": "general"},
            turn={"hypothesis": {"H0": "beta > 0"}}, reason_position=0)
        self.assertEqual([f.principle for f in findings],
                         ["hypothesis_timing"])
        self.assertEqual(findings[0].severity, "advisory")

    def test_late_h0_is_clean(self):
        self.assertEqual(
            evaluate_principles(
                controller=_controller(), plan={"kind": "general"},
                turn={"hypothesis": {"H0": "beta > 0"}},
                reason_position=2),
            [])

    def test_no_hypothesis_is_clean(self):
        self.assertEqual(
            evaluate_principles(
                controller=_controller(), plan={"kind": "general"},
                turn={}, reason_position=0),
            [])


class ScenarioCoverageTests(unittest.TestCase):
    def _controller_with(self, *tools):
        from market_service.nooa_harness.engine.controller import ScenarioEvalStatus
        controller = _controller()
        for tool in tools:
            controller = controller.record_outcome(
                tool, {"result": "ok", "detail": {"ok": True}}, {})
        return controller

    def test_target_plan_without_scenario_attempt_is_a_finding(self):
        plan = {"kind": "price_target", "targets": [{"value": "120"}],
                "invalidations": []}
        findings = evaluate_principles(
            controller=self._controller_with(), plan=plan, turn={})
        self.assertEqual([f.principle for f in findings],
                         ["scenario_coverage"])

    def test_forward_scenario_attempt_covers(self):
        plan = {"kind": "price_target", "targets": [{"value": "120"}],
                "invalidations": []}
        self.assertEqual(
            evaluate_principles(
                controller=self._controller_with("calc.forward.scenario"),
                plan=plan, turn={}),
            [])

    def test_legacy_scenario_attempt_covers(self):
        plan = {"kind": "price_target", "targets": [{"value": "120"}],
                "invalidations": []}
        self.assertEqual(
            evaluate_principles(
                controller=self._controller_with("calc.scenario.evaluate"),
                plan=plan, turn={}),
            [])

    def test_non_target_plan_is_clean(self):
        self.assertEqual(
            evaluate_principles(
                controller=self._controller_with(), plan={"kind": "general"},
                turn={}),
            [])

    def test_none_plan_skips(self):
        self.assertEqual(
            evaluate_principles(
                controller=self._controller_with(), plan=None, turn={}),
            [])


class RenderFindingsTests(unittest.TestCase):
    def test_render_lists_each_finding(self):
        rendered = render_findings([
            PrincipleFinding(principle="hypothesis_timing", detail="d1"),
            PrincipleFinding(principle="scenario_coverage", detail="d2"),
        ])
        self.assertIn("[hypothesis_timing] d1", rendered)
        self.assertIn("[scenario_coverage] d2", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_render_empty_is_empty(self):
        self.assertEqual(render_findings([]), "")


if __name__ == "__main__":
    unittest.main()