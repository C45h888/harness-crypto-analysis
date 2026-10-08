"""Deterministic principles — advisory guard, never a gate."""

import unittest

from market_service.nooa_harness.engine.controller import CycleController
from market_service.nooa_harness.engine.principles import (
    FETCH_TOOLS,
    PrincipleFinding,
    evaluate_principles,
    in_fetch_window,
    render_findings,
)


def _controller():
    return CycleController(None)


class FetchWindowTests(unittest.TestCase):
    def test_tape_and_snapshot_tools_admitted(self):
        self.assertTrue(in_fetch_window("micro.capture_status"))
        self.assertTrue(in_fetch_window("micro.fit_beta"))
        self.assertTrue(in_fetch_window("micro.ofi_intervals"))
        self.assertTrue(in_fetch_window("market.read"))
        self.assertTrue(in_fetch_window("substrate.read"))

    def test_invoke_calc_and_workers_denied(self):
        self.assertFalse(in_fetch_window("calc.ofi.intervals"))
        self.assertFalse(in_fetch_window("calc.forward.forecast"))
        self.assertFalse(in_fetch_window("substrate.tape"))
        self.assertFalse(in_fetch_window(""))
        self.assertFalse(in_fetch_window("market.derivatives"))

    def test_window_principle_scoped_to_fetch_turns(self):
        turn = {"tool_calls": [{"name": "calc.ofi.intervals"}]}
        scoped = evaluate_principles(
            controller=_controller(), plan=None, turn=turn, fetch_turn=True)
        self.assertEqual([f.principle for f in scoped], ["fetch_window"])
        self.assertEqual(scoped[0].severity, "advisory")
        # Later loops command the full registry — no finding there.
        unscoped = evaluate_principles(
            controller=_controller(), plan=None, turn=turn)
        self.assertEqual(unscoped, [])

    def test_conformant_fetch_turn_is_clean(self):
        turn = {"tool_calls": [{"name": "micro.fit_beta"},
                               {"name": "market.read"}]}
        self.assertEqual(
            evaluate_principles(
                controller=_controller(), plan=None, turn=turn,
                fetch_turn=True),
            [])


class HypothesisTimingTests(unittest.TestCase):
    def test_early_h0_is_a_finding(self):
        findings = evaluate_principles(
            controller=_controller(), plan={"kind": "general"},
            turn={"hypothesis": {"H0": "beta > 0"}}, reason_position=0)
        self.assertEqual([f.principle for f in findings],
                         ["hypothesis_timing"])

    def test_h0_at_hypothesize_position_conforms(self):
        self.assertEqual(
            evaluate_principles(
                controller=_controller(), plan={"kind": "general"},
                turn={"hypothesis": {"H0": "beta > 0"}}, reason_position=2),
            [])

    def test_turn_without_h0_conforms(self):
        self.assertEqual(
            evaluate_principles(
                controller=_controller(), plan={"kind": "general"},
                turn={"tool_calls": []}, reason_position=0),
            [])


class ScenarioCoverageTests(unittest.TestCase):
    def test_target_plan_without_attempt_is_a_finding(self):
        plan = {"kind": "price_target", "targets": [{"value": "100.50"}],
                "invalidations": []}
        findings = evaluate_principles(
            controller=_controller(), plan=plan, turn={})
        self.assertEqual([f.principle for f in findings],
                         ["scenario_coverage"])

    def test_targetless_plan_conforms(self):
        self.assertEqual(
            evaluate_principles(
                controller=_controller(),
                plan={"kind": "general", "targets": [], "invalidations": []},
                turn={}),
            [])

    def test_attempted_scenario_tool_conforms(self):
        plan = {"kind": "price_target", "targets": [{"value": "100.50"}],
                "invalidations": []}
        controller = _controller().record_outcome(
            "calc.scenario.evaluate",
            {"capability": "calc.scenario.evaluate", "scope": {},
             "result": "ok",
             "detail": {"status": "refused", "reason": "thin tape"}},
            None,
        )
        self.assertEqual(
            evaluate_principles(controller=controller, plan=plan, turn={}),
            [])


class RenderTests(unittest.TestCase):
    def test_empty_renders_empty(self):
        self.assertEqual(render_findings([]), "")

    def test_findings_render_as_steer_block(self):
        block = render_findings([PrincipleFinding(
            principle="scenario_coverage", detail="no tool yet")])
        self.assertIn("PRINCIPLES", block)
        self.assertIn("never denials", block)
        self.assertIn("[scenario_coverage]", block)


if __name__ == "__main__":
    unittest.main()
