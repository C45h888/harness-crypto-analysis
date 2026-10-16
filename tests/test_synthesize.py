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
        ds = artifact.deterministic_state
        self.assertNotIn("synthesis_note", ds)
        by_cap = {e.get("capability") for e in artifact.capability_log}
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
        # The cycle still settles (degraded single-call), but the failure is
        # FSM-visible for the semantics-review surface — never silent.
        self.assertEqual(meta.get("terminal"), "settled")
        ds = artifact.deterministic_state
        note = ds.get("synthesis_note") or {}
        self.assertEqual(note.get("stage"), "collapse")
        self.assertEqual(note.get("degraded_to"), "single-call compose")
        by_cap = {e.get("capability") for e in artifact.capability_log}
        self.assertIn("engine.synthesis.failed", by_cap)
        receipts = (ds.get("loop_receipts") or {}).get("receipts") \
            if isinstance(ds.get("loop_receipts"), dict) else None
        if receipts is None:
            receipts = ds.get("loop_receipts") or []
        refused = [r for r in receipts
                   if isinstance(r, dict)
                   and r.get("sub_loop") == "composition"
                   and r.get("disposition") == "refused"]
        self.assertTrue(refused)


if __name__ == "__main__":
    unittest.main()
