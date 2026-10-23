"""Agentic synthesis tests — strict collapse, full-material derive, FSM-visible failure."""
from __future__ import annotations

import asyncio
import unittest

from market_service.nooa_harness.engine.core.synthesize import validate_collapse
from tests.test_engine import (
    _collapse_turn,
    _engine,
    _staged_narration,
    _track_tools,
    _TRACK_ASSEMBLE,
    _TRACK_DISCIPLINE,
    _TRACK_FORECAST,
    _TRACK_HYPOTHESIZE,
    _TRACK_INTERPRET,
    _wake,
)
from tests.test_track_characterization import _close, _evidence_prefix


class CollapseSchemaTests(unittest.TestCase):
    def test_valid_collapse_passes(self):
        import json
        ok, reasons = validate_collapse(json.loads(_collapse_turn()))
        self.assertTrue(ok, reasons)

    def test_empty_readings_fails(self):
        ok, reasons = validate_collapse(
            {"readings": [], "contradictions": [], "direction": "x"})
        self.assertFalse(ok)
        self.assertTrue(any("readings" in r for r in reasons))

    def test_unknown_vector_fails(self):
        ok, reasons = validate_collapse({
            "readings": [{"vector": "vibes", "path": "p",
                          "value": "v", "meaning": "m"}],
            "contradictions": [], "direction": "x"})
        self.assertFalse(ok)
        self.assertTrue(any("vector" in r for r in reasons))

    def test_null_value_and_empty_direction_fail(self):
        ok, reasons = validate_collapse({
            "readings": [{"vector": "base", "path": "p",
                          "value": None, "meaning": ""}],
            "contradictions": "nope", "direction": ""})
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 4)


class SynthesisWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_collapse_and_derive_settle_without_synthesis_note(self):
        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_FORECAST),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _staged_narration("P6", final=True),
            _track_tools(*_TRACK_DISCIPLINE),
            _staged_narration("P6", final=True),  # repair followup close
            _collapse_turn(),  # output collapse (valid)
            _staged_narration("P6", final=True),  # derive (valid final)
        ])
        artifact, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="synthesis walk")
        self.assertEqual(meta.get("terminal"), "settled")
        ds = artifact.run_record["deterministic_state"]
        self.assertNotIn("synthesis_note", ds)
        by_cap = {e.get("capability") for e in artifact.run_record["capability_log"]}
        self.assertNotIn("engine.synthesis.failed", by_cap)
        # Both synthesis passes spent output budget.
        traversal = ds.get("loop_traversal") or {}
        self.assertGreaterEqual(
            (traversal.get("output") or {}).get("passes_spent", 0), 2)

    async def test_failed_collapse_is_fsm_visible_and_degrades(self):
        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_FORECAST),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _staged_narration("P6", final=True),
            _track_tools(*_TRACK_DISCIPLINE),
            _staged_narration("P6", final=True),  # collapse slot: not a collapse
            _staged_narration("P6", final=True),  # derive slot: valid final
        ])
        artifact, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="failed synthesis walk")
        # The cycle still settles (relaxed-loop degrade), and the failure is
        # FSM-visible for the semantics-review surface — never silent and
        # never a false REFUSED receipt on a settled artifact.
        self.assertEqual(meta.get("terminal"), "settled")
        ds = artifact.run_record["deterministic_state"]
        note = ds.get("synthesis_note") or {}
        self.assertEqual(note.get("stage"), "collapse")
        self.assertEqual(note.get("degraded_to"), "derive-without-collapse")
        by_cap = {e.get("capability") for e in artifact.run_record["capability_log"]}
        self.assertIn("engine.synthesis.degraded", by_cap)
        self.assertNotIn("engine.synthesis.failed", by_cap)
        receipts = (ds.get("loop_receipts") or {}).get("receipts") \
            if isinstance(ds.get("loop_receipts"), dict) else None
        if receipts is None:
            receipts = ds.get("loop_receipts") or []
        refused = [r for r in receipts
                   if isinstance(r, dict)
                   and r.get("sub_loop") == "composition"
                   and r.get("disposition") == "refused"]
        self.assertFalse(refused, "a settled artifact must not carry a "
                                  "REFUSED composition receipt")


class ToolPolicySingleOwnerTests(unittest.TestCase):
    """N9c: one tool-policy owner — kb strings quote synthesize's table.

    The OUTPUT-sequence tool rule lives in exactly one place
    (config.TURN_TOOL_POLICY). Every prompt that states it must render the
    canonical sentence, and the enforced behavior must match the table.
    """

    def test_prompts_quote_the_canonical_sentences(self):
        from market_service.nooa_harness.engine import kb
        from market_service.nooa_harness.engine.config import (
            TURN_TOOL_POLICY, tool_policy_sentence,
        )
        from market_service.nooa_harness.engine.core.synthesize import (
            compose_collapse_prompt,
        )
        # Table shape: collapse opens the memory window, everything else shut.
        self.assertEqual(TURN_TOOL_POLICY["collapse"], ("memory.",))
        for turn in ("derive", "composition", "finalize", "answer"):
            self.assertEqual(TURN_TOOL_POLICY[turn], ())
        # kb-owned prompts quote the owner verbatim.
        self.assertIn(
            tool_policy_sentence("composition"),
            kb.compose_output_prompt(
                directive_block="", synthesis_material={},
                accumulated_keys=[], traversal={}, chain_line=""),
        )
        finalize = kb.compose_forced_final_prompt(
            task="t", missing=[], evidence_digest="",
        )
        self.assertIn(tool_policy_sentence("finalize"), finalize)
        self.assertIn(
            tool_policy_sentence("collapse"),
            compose_collapse_prompt(
                task="t", verdict_rows=[], base_summary=None,
                chain_line="", frame_steer="", gate_line=""),
        )
        material = {
            "task": "t", "engine_verdict": "v", "verdict_reason": "r",
            "gate_status": "validated", "gate_reasons": [],
            "summary": "s", "evidence": [], "hypothesis": {},
            "adjudication": {}, "scenario": {},
            "forward_scenario": {}, "hypothesis_evidence": {},
            "confidence": "medium", "limitations": [],
            "model_separation": "m", "calculations": {},
            "dropped": [], "conformance": {},
            "engine_findings": {},
        }
        self.assertIn(
            tool_policy_sentence("answer"),
            kb.compose_answer_prompt(material=material),
        )

    def test_enforcement_matches_the_table(self):
        # synthesize drops non-memory calls on collapse and all calls on
        # derive — the behavior the sentences promise.
        import inspect
        from market_service.nooa_harness.engine.core import synthesize
        from market_service.nooa_harness.engine.config import TURN_TOOL_POLICY
        src = inspect.getsource(synthesize)
        self.assertIn('startswith("memory.")', src)
        self.assertIn("tool_calls", src)
        self.assertEqual(TURN_TOOL_POLICY["derive"], ())


if __name__ == "__main__":
    unittest.main()
