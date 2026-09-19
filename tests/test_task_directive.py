"""TaskDirective — prompt parsing as a plan variable (spec TASK_DIRECTIVE_SPEC).

Covers: Phase-A parse vectors (CLI, native, long, unsupported, refusals),
Phase-B disposal (vocab validation, conflicts, CLI immunity), plan binding
per regime, gather pre-acquisition wiring (horizon + native scenario), and
the task-conformance verdict read.
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_service.nooa_harness.engine.task_directive import (  # noqa: E402
    build_plan, dispose_assessment, parse_task_directive,
)


class ParseVectorTests(unittest.TestCase):
    """Phase A — deterministic parse, refusal semantics, never guesses."""

    def test_cli_scenario_wins(self):
        d = parse_task_directive("anything", {"target_price": "220.5", "horizon": "1h"})
        self.assertEqual(d.kind, "price_target")
        self.assertEqual(d.source, "cli")
        self.assertEqual(d.horizon_regime, "long")
        self.assertEqual(d.resolved_horizon_ms, 3_600_000)
        self.assertEqual([t["value"] for t in d.targets], ["220.5"])

    def test_native_horizon_prompt(self):
        d = parse_task_directive("can SOL hit $220.00 in the next 60 seconds?", None)
        self.assertEqual(d.kind, "price_target")
        self.assertEqual(d.horizon_value, "60s")
        self.assertEqual(d.horizon_regime, "native")
        self.assertEqual(d.resolved_horizon_ms, 60_000)
        self.assertEqual([t["value"] for t in d.targets], ["220.00"])

    def test_long_horizon_prompt(self):
        d = parse_task_directive("does price reach 100.5 within the next hour?", None)
        self.assertEqual(d.horizon_regime, "long")
        self.assertEqual(d.resolved_horizon_ms, 3_600_000)
        self.assertEqual([t["value"] for t in d.targets], ["100.5"])

    def test_unsupported_horizon_is_refusal_not_guess(self):
        d = parse_task_directive("can price hit $220 in 5 minutes?", None)
        self.assertIsNone(d.resolved_horizon_ms)
        self.assertEqual(d.horizon_regime, "unsupported")
        self.assertTrue(any(r["field"] == "horizon" for r in d.refusals))

    def test_bare_number_never_binds(self):
        d = parse_task_directive("the tape shows 400 events around 12.5 levels", None)
        self.assertEqual(d.targets, ())
        self.assertEqual(d.kind, "general")

    def test_stop_verb_binds_invalidation(self):
        d = parse_task_directive("target $220, stop below $210", None)
        self.assertEqual([t["value"] for t in d.targets], ["220"])
        self.assertEqual([s["value"] for s in d.invalidations], ["210"])

    def test_empty_task_is_general(self):
        d = parse_task_directive(None, None)
        self.assertEqual(d.kind, "general")
        self.assertEqual(d.horizon_regime, "none")

    def test_hypothesis_kind(self):
        d = parse_task_directive("H0: beta still positive; H1: flow stopped moving price", None)
        self.assertEqual(d.kind, "hypothesis")


class DisposeAssessmentTests(unittest.TestCase):
    """Phase B — engine validates the proposal; rejects are recorded."""

    def test_proposal_binds_horizon_when_parse_had_none(self):
        d = parse_task_directive("is the flow pointing at $250?", None)
        self.assertEqual(d.horizon_regime, "none")
        d2, rejects = dispose_assessment(d, {
            "proposed_horizon": "30s",
            "proposed_targets": ["240"],
        })
        self.assertEqual(rejects, [])
        self.assertEqual(d2.horizon_regime, "native")
        self.assertEqual(d2.resolved_horizon_ms, 30_000)
        self.assertEqual(d2.targets[-1]["provenance"], "llm_assessment")

    def test_proposal_outside_vocabulary_is_rejected(self):
        d = parse_task_directive("will price move? thinking around 5 minutes", None)
        d2, rejects = dispose_assessment(d, {"proposed_horizon": "5m"})
        self.assertEqual(d2.resolved_horizon_ms, d.resolved_horizon_ms)
        self.assertTrue(any(r["field"] == "proposed_horizon" for r in rejects))

    def test_conflicting_proposal_never_overrides_parse(self):
        d = parse_task_directive("target $220 over 60s", None)
        d2, rejects = dispose_assessment(d, {"proposed_horizon": "1h"})
        self.assertEqual(d2.resolved_horizon_ms, 60_000)
        self.assertTrue(rejects)

    def test_cli_directive_is_immune(self):
        d = parse_task_directive(None, {"target_price": "220.5", "horizon": "1h"})
        d2, rejects = dispose_assessment(d, {"proposed_horizon": "60s", "proposed_targets": ["999"]})
        self.assertEqual(d2, d)
        self.assertEqual(rejects, [])

    def test_hypothesis_seed_requires_both_sides(self):
        d = parse_task_directive("hypothesis about flow", None)
        d2, _ = dispose_assessment(d, {"hypothesis_seed": {"H0": "beta > 0"}})
        self.assertIsNone(d2.hypothesis_seed)
        d3, _ = dispose_assessment(d, {"hypothesis_seed": {"H0": "beta > 0", "H1": "beta <= 0"}})
        self.assertEqual(d3.hypothesis_seed["H0"], "beta > 0")


class PlanBindingTests(unittest.TestCase):
    """The directive is a plan factor; the plan never invents links."""

    def test_native_target_plan_pre_acquires_scenario(self):
        d = parse_task_directive("can SOL hit $220 in the next 60 seconds?", None)
        plan = build_plan(d)
        self.assertEqual(plan["kind"], "price_target")
        self.assertEqual(plan["horizon_regime"], "native")
        self.assertEqual(plan["resolved_horizon_ms"], 60_000)
        self.assertIn("calc.forward.forecast", plan["pre_acquire"])
        self.assertIn("calc.forward.scenario", plan["pre_acquire"])
        self.assertNotIn("calc.scenario.evaluate", plan["pre_acquire"])

    def test_long_target_plan_routes_to_exceedance(self):
        d = parse_task_directive(None, {"target_price": "220", "horizon": "1h"})
        plan = build_plan(d)
        self.assertEqual(plan["horizon_regime"], "long")
        self.assertIsNone(plan["resolved_horizon_ms"])
        self.assertEqual(plan["scenario_horizon"], "1h")
        self.assertIn("calc.scenario.evaluate", plan["pre_acquire"])
        self.assertNotIn("calc.forward.scenario", plan["pre_acquire"])

    def test_general_plan_is_minimal(self):
        d = parse_task_directive("explain the evidence", None)
        plan = build_plan(d)
        self.assertEqual(plan["kind"], "general")
        self.assertEqual(plan["pre_acquire"], ["calc.forward.forecast"])
        self.assertEqual(plan["directive_refusals"], [])


class GatherWiringTests(unittest.TestCase):
    """Directive-driven pre-acquisition: horizon + native scenario binding."""

    async def _gather(self, task):
        from market_service.nooa_harness.engine import core
        from tests.test_engine import _engine, _wake

        dispatched = []

        async def spy_execute(store, name, args, **kwargs):
            dispatched.append((name, dict(args)))
            if name == "micro.capture_status":
                result = {"state": "running", "sequence_gaps": 0}
            elif name == "micro.fit_beta":
                result = {"input_hash": "fixture", "price_impact_fit": {
                    "status": "validated", "n_observations": 40, "beta": "0.001",
                }, "coverage": {"events_in_window": 5000}}
            else:
                result = {"value": "fixture"}
            return result, {"capability": name, "scope": {}, "result": "ok"}

        engine, _store, _postgres, _memory = _engine(llm_responses=[])
        with patch.object(core.context, "execute_tool", side_effect=spy_execute):
            ctx = engine.run_wake(_wake(), {"decision": "fire"}, task, None)
            await core.gather.run_gather(engine, ctx)
        return dispatched

    def test_native_target_task_pre_acquires_scenario_at_directive_horizon(self):
        dispatched = asyncio.run(self._gather(
            "can SOL hit $220.00 in the next 60 seconds?"))
        forecast_args = dict(next(args for n, args in dispatched
                                  if n == "calc.forward.forecast"))
        self.assertEqual(forecast_args["horizon_ms"], 60_000)
        scenario = next(args for n, args in dispatched
                        if n == "calc.forward.scenario")
        self.assertEqual(list(scenario["targets"]), ["220.00"])

    def test_plain_task_keeps_default_horizon_and_no_scenario(self):
        dispatched = asyncio.run(self._gather("explain the evidence"))
        forecast_args = dict(next(args for n, args in dispatched
                                  if n == "calc.forward.forecast"))
        self.assertEqual(forecast_args["horizon_ms"], 5_000)
        self.assertFalse(any(n == "calc.forward.scenario" for n, _ in dispatched))


class DirectiveVerdictReadTests(unittest.TestCase):
    """Task-conformance read (validator home, pure)."""

    def test_directive_path_citation_detected(self):
        from market_service.nooa_harness.engine.narration import has_directive_verdict
        self.assertTrue(has_directive_verdict({"evidence": [
            {"path": "deterministic_state.task_directive.targets[0]", "value": "220"}]}))
        self.assertTrue(has_directive_verdict({"evidence": [
            {"path": "calc.forward.scenario → targets[0].p_ge", "value": "0.3"}]}))
        self.assertFalse(has_directive_verdict({"evidence": [
            {"path": "calc.ofi.intervals → ofi", "value": "12"}]}))
        self.assertFalse(has_directive_verdict({}))


if __name__ == "__main__":
    unittest.main()
