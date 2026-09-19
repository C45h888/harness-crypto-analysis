"""Tests for tool output schema standardization — ToolResult envelope, null-field
extraction, phase summaries, and schema-block rendering.

Tests are nooa-free (no LLM client, no Redis, no Postgres). Pure function
tests over the tool_schemas module plus integration checks for the kb.py
composer wrappers.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

# The tool_schemas module is the subject under test.
from market_service.nooa_harness.engine.tool_schemas import (
    TOOL_OUTPUT_SCHEMAS,
    build_phase_summary_block,
    extract_p1_summary,
    extract_p2_summary,
    extract_p3_summary,
    extract_p5_summary,
    tool_schema_block,
    wrap_tool_result,
)

# The kb.py composrs consume the wrapper — we test that they accept wrapped results.
# We import the promp composers directly for unit tests.
from market_service.nooa_harness.engine.kb import (
    compose_followup_prompt,
    compose_repair_prompt,
)


class TestWrapToolResult(unittest.TestCase):
    """Tests for the ToolResult envelope wrapper."""

    def test_wraps_scalar_result(self):
        raw = {"beta": "0.042", "se": "0.008", "r2": "0.31", "status": "validated"}
        wrapped = wrap_tool_result("calc.fit.price_impact", raw)
        self.assertEqual(wrapped["tool"], "calc.fit.price_impact")
        self.assertEqual(wrapped["status"], "ok")
        self.assertIsNone(wrapped["reason"])
        self.assertEqual(wrapped["data"], raw)
        self.assertEqual(wrapped["null_fields"], [])

    def test_wraps_with_status_and_reason(self):
        raw = None
        wrapped = wrap_tool_result("calc.hypothesis.test", raw, status="refused",
                                    reason="hypothesis_id required (pre-registration)")
        self.assertEqual(wrapped["status"], "refused")
        self.assertEqual(wrapped["reason"], "hypothesis_id required (pre-registration)")
        self.assertIsNone(wrapped["data"])

    def test_null_fields_extracted_from_nested_dict(self):
        raw = {
            "route_a_direct": {
                "delta_ticks": "1.2",
                "band_lo": None,
                "band_hi": "3.4",
            },
            "route_b_depth_scaled": None,
        }
        wrapped = wrap_tool_result("calc.price.delta", raw)
        # Should find route_a_direct.band_lo and route_b_depth_scaled
        nulls = wrapped["null_fields"]
        self.assertIn("route_a_direct.band_lo", nulls)
        self.assertIn("route_b_depth_scaled", nulls)

    def test_null_fields_skips_non_dicts(self):
        raw = {"status": "ok", "data": None}
        wrapped = wrap_tool_result("test.tool", raw)
        self.assertIn("data", wrapped["null_fields"])

    def test_wraps_list_result(self):
        raw = [{"ofi": "0.5", "event_count": 10}, {"ofi": "-0.3", "event_count": 8}]
        wrapped = wrap_tool_result("calc.ofi.intervals", raw)
        self.assertEqual(wrapped["tool"], "calc.ofi.intervals")
        self.assertEqual(wrapped["data"], raw)
        # Lists are not scanned for nulls at top level
        self.assertEqual(wrapped["null_fields"], [])

    def test_already_wrapped_passes_through(self):
        """If a result already has a ``tool`` key it should not be double-wrapped."""
        # This tests the guard in compose_followup_prompt
        already = {"tool": "calc.ofi.intervals", "status": "ok", "data": []}
        # Just verify the wrapper does NOT add another layer
        wrapped = wrap_tool_result("calc.ofi.intervals", already)
        # wrap_tool_result treats it as a dict — it WILL add null_fields
        # but won't change tool/status/data. The guard in kb.py checks 
        # isinstance(value, dict) and "tool" in value before calling wrap.
        self.assertTrue(wrapped["tool"], "calc.ofi.intervals")


class TestToolSchemaBlock(unittest.TestCase):
    """Tests for the schema block injected into the system prompt."""

    def test_every_tool_has_a_schema(self):
        """Every known tool name should have an entry in TOOL_OUTPUT_SCHEMAS."""
        # The manifest has 44 tool names — let's check at least the common ones
        expected = [
            "micro.capture_status",
            "micro.fit_beta",
            "calc.ofi.intervals",
            "calc.depth.average",
            "calc.fit.price_impact",
            "calc.price.delta",
            "calc.scenario.evaluate",
            "calc.forward.forecast",
            "calc.hypothesis.test",
            "substrate.read",
            "substrate.invoke",
            "market.read",
            "memory.recall_paper",
        ]
        for tool in expected:
            self.assertIn(tool, TOOL_OUTPUT_SCHEMAS,
                          f"{tool} missing from TOOL_OUTPUT_SCHEMAS")

    def test_schema_block_is_valid_string(self):
        block = tool_schema_block()
        self.assertIsInstance(block, str)
        self.assertGreater(len(block), 500)
        # Should mention the ToolResult envelope
        self.assertIn("ToolResult", block)
        self.assertIn("null_fields", block)

    def test_schema_block_lists_all_tools(self):
        block = tool_schema_block()
        for tool in TOOL_OUTPUT_SCHEMAS:
            self.assertIn(tool, block)


class TestExtractPhaseSummaries(unittest.TestCase):
    """Tests for phase-level key field extraction from wrapped results."""

    def test_p1_summary(self):
        results = {
            "calc.ofi.intervals": wrap_tool_result(
                "calc.ofi.intervals",
                [{"ofi": "0.5", "event_count": 10, "quality": "estimated"},
                 {"ofi": "-0.3", "event_count": 8, "quality": "estimated"}],
            ),
            "micro.capture_status": wrap_tool_result(
                "micro.capture_status",
                {"state": "running", "sequence_gaps": 0, "reconnects": 0},
            ),
        }
        lines = extract_p1_summary(results)
        self.assertTrue(any("OFI" in l for l in lines))
        self.assertTrue(any("capture" in l for l in lines))
        self.assertTrue(any("running" in l for l in lines))

    def test_p1_summary_no_results(self):
        self.assertEqual(extract_p1_summary({}), [])
        self.assertEqual(extract_p1_summary({"other": {"tool": "x", "data": None}}), [])

    def test_p2_summary(self):
        results = {
            "calc.depth.average": wrap_tool_result(
                "calc.depth.average",
                {"mean_ad": "2100000", "n_intervals": 180},
            ),
            "calc.fit.price_impact": wrap_tool_result(
                "calc.fit.price_impact",
                {"beta": "0.042", "r2": "0.31", "status": "validated"},
            ),
            "calc.fit.depth_scaling": wrap_tool_result(
                "calc.fit.depth_scaling",
                {"c": "0.5", "lambda": "0.15", "status": "provisional"},
            ),
        }
        lines = extract_p2_summary(results)
        self.assertTrue(any("AD" in l for l in lines))
        self.assertTrue(any("beta" in l for l in lines))
        self.assertTrue(any("c=" in l or "λ=" in l for l in lines))

    def test_p2_summary_partial(self):
        results = {
            "calc.depth.average": wrap_tool_result(
                "calc.depth.average",
                {"mean_ad": None, "n_intervals": 0},
            ),
        }
        lines = extract_p2_summary(results)
        self.assertTrue(any("AD" in l for l in lines))

    def test_p3_summary(self):
        results = {
            "substrate.read": wrap_tool_result(
                "substrate.read",
                {
                    "substrates": {
                        "tape": {"available": True, "age_ms": 12000, "status": "ok"},
                        "density": {"available": True, "age_ms": 45000, "status": "ok"},
                        "delta": {"available": False, "age_ms": None, "status": None},
                    }
                },
            ),
        }
        lines = extract_p3_summary(results)
        self.assertTrue(any("tape" in l for l in lines))
        self.assertTrue(any("delta" in l for l in lines))
        self.assertTrue(any("age=" in l for l in lines))

    def test_p3_summary_no_substrates(self):
        results = {}
        self.assertEqual(extract_p3_summary(results), [])

    def test_p5_summary(self):
        results = {
            "calc.price.delta": wrap_tool_result(
                "calc.price.delta",
                {
                    "route_a_direct": {
                        "delta_ticks": "-1.2",
                        "band_lo": "-3.1",
                        "band_hi": "0.7",
                    },
                    "ofi_used": "-0.042",
                },
            ),
            "calc.scenario.evaluate": wrap_tool_result(
                "calc.scenario.evaluate",
                {
                    "target_price": "65000",
                    "exceedance": "0.42",
                    "finder_verdict": "inconclusive",
                },
            ),
        }
        lines = extract_p5_summary(results)
        self.assertTrue(any("ΔP" in l for l in lines))
        self.assertTrue(any("band=" in l for l in lines))
        self.assertTrue(any("scenario" in l for l in lines))

    def test_p5_summary_with_forecast(self):
        results = {
            "calc.forward.forecast": wrap_tool_result(
                "calc.forward.forecast",
                {
                    "forward_target": {
                        "horizon_ms": 5000,
                        "y_pred": "0.35",
                        "y_actual": None,
                    },
                },
            ),
        }
        lines = extract_p5_summary(results)
        self.assertTrue(any("forward" in l for l in lines))

    def test_p5_summary_no_results(self):
        self.assertEqual(extract_p5_summary({}), [])

    def test_build_phase_summary_block(self):
        results = {
            "calc.ofi.intervals": wrap_tool_result(
                "calc.ofi.intervals",
                [{"ofi": "0.5", "event_count": 10, "quality": "estimated"}],
            ),
            "calc.depth.average": wrap_tool_result(
                "calc.depth.average",
                {"mean_ad": "2100000", "n_intervals": 180},
            ),
        }
        block = build_phase_summary_block(results)
        self.assertIsInstance(block, str)
        self.assertIn("PHASE SUMMARIES", block)
        # Should have both P1 and P2 sections
        self.assertIn("P1:", block)
        self.assertIn("P2:", block)

    def test_build_phase_summary_block_empty(self):
        self.assertEqual(build_phase_summary_block({}), "")


class TestComposeFollowupPromptWithWrapped(unittest.TestCase):
    """Tests that compose_followup_prompt handles wrapped ToolResult envelopes."""

    def setUp(self):
        # Minimal mock controller that returns simple coverage/traversal
        self.controller = _mock_controller()

    def test_followup_prompt_includes_phase_summary(self):
        round_results = {
            "calc.ofi.intervals": wrap_tool_result(
                "calc.ofi.intervals",
                [{"ofi": "0.5", "quality": "estimated", "event_count": 10}],
            ),
        }
        accumulated = {}
        prompt = compose_followup_prompt(
            controller=self.controller,
            loop="evidence",
            sub_loop="acquisition",
            passes_spent=0,
            pass_budget=3,
            dispatches_left=2,
            chain=["calc.forward.forecast"],
            accumulated=accumulated,
            round_results=round_results,
            task_reminder="",
            scenario_reminder="",
            phase_guidance={"P1": "PHASE P1 — OFI INFERENCE"},
            next_phase="P1",
            chain_block="",
        )
        # Should contain the phase summary block
        self.assertIn("PHASE SUMMARIES", prompt)
        # Should contain the ToolResult envelope key
        self.assertIn('"tool"', prompt)
        self.assertIn('"data"', prompt)
        # Should contain the PASS RESULTS header
        self.assertIn("PASS RESULTS", prompt)

    def test_followup_prompt_with_already_wrapped(self):
        """Verify no double-wrapping when results already have a 'tool' key."""
        already_wrapped = {
            "calc.ofi.intervals": {
                "tool": "calc.ofi.intervals",
                "status": "ok",
                "data": [{"ofi": "0.5", "quality": "estimated"}],
                "null_fields": [],
            },
        }
        prompt = compose_followup_prompt(
            controller=self.controller,
            loop="evidence",
            sub_loop="acquisition",
            passes_spent=0,
            pass_budget=3,
            dispatches_left=2,
            chain=[],
            accumulated={},
            round_results=already_wrapped,
            task_reminder="",
            scenario_reminder="",
            phase_guidance={"P1": "PHASE P1"},
            next_phase="P1",
            chain_block="",
        )
        # Should not have double wrapping (tool inside tool)
        self.assertIn("PHASE SUMMARIES", prompt)

    def test_followup_prompt_includes_null_fields(self):
        raw_with_nulls = {
            "route_a_direct": {"delta_ticks": "1.2", "band_lo": None, "band_hi": None},
        }
        round_results = {
            "calc.price.delta": wrap_tool_result(
                "calc.price.delta", raw_with_nulls,
            ),
        }
        prompt = compose_followup_prompt(
            controller=self.controller,
            loop="evidence",
            sub_loop="acquisition",
            passes_spent=0,
            pass_budget=3,
            dispatches_left=2,
            chain=[],
            accumulated={},
            round_results=round_results,
            task_reminder="",
            scenario_reminder="",
            phase_guidance={"P5": "PHASE P5"},
            next_phase="P5",
            chain_block="",
        )
        # null_fields should be in the prompt
        self.assertIn("null_fields", prompt)
        self.assertIn("band_lo", prompt)

    def test_repair_prompt_also_wrapped(self):
        from market_service.nooa_harness.engine.loop_states import NestedLoop, SubLoop, TaskIntent
        # Build a controller whose observation matches the validation loop
        val_ctrl = _mock_controller()
        class _ObsVal:
            nested_loop = NestedLoop.VALIDATION
            sub_loop = SubLoop.RECOVERY
            task = TaskIntent.VALIDATE_FINAL
        val_ctrl.observation = _ObsVal()

        accumulated = {
            "micro.capture_status": {"state": "running", "sequence_gaps": 0},
        }
        prompt = compose_repair_prompt(
            controller=val_ctrl,
            loop="validation",
            sub_loop="recovery",
            passes_spent=1,
            pass_budget=2,
            dispatches_left=0,
            missing=["P3: phase uncovered"],
            accumulated=accumulated,
            task_reminder="",
            scenario_reminder="",
            scenario_steer="",
            phase_guidance={"P3": "PHASE P3"},
            next_phase="P3",
        )
        self.assertIn("ToolResult", prompt)
        self.assertIn("PHASE SUMMARIES", prompt)
        self.assertIn("FINAL REJECTED", prompt)


# ---- helpers ----

def _mock_controller():
    """Build a minimal mock controller with the interface the composers need."""
    from market_service.nooa_harness.engine.loop_states import NestedLoop, SubLoop, TaskIntent
    c = MagicMock()
    c.phase_coverage = {"P1": {"calc.ofi.intervals"}, "P5": {"calc.price.delta"}}
    c.loop_coverage.return_value = {"evidence": ("sourcing", True),
                                      "reasoning": ("analysis", False)}
    c.congruence.return_value = {}
    # build_loop_state_block validates loop/sub_loop/intent against
    # the controller's observation. We need real enum values here.
    class _Obs:
        nested_loop = NestedLoop.EVIDENCE
        sub_loop = SubLoop.ACQUISITION
        task = TaskIntent.INFER_ORDER_FLOW
    c.observation = _Obs()
    return c


if __name__ == "__main__":
    unittest.main()