"""Relaxed OUTPUT loop — authored output over the deterministic frame.

Pins the §5 integration: the frame (base-level instantiation), the relaxed
collapse schema (honest nulls, frame-conformant paths), the authored-output
gate tiers (hard compulsion / quality findings / carried conflicts), the
ASSEMBLE fact-check (no rewriting), the memory-is-never-evidence guard, the
correction turn (the agent fixes its own output), and honest reporting
(``final_validation`` describes the object that SHIPS).
"""

from __future__ import annotations

import json
import unittest

from market_service.nooa_harness.engine.core.output import (
    _assemble_authored,
    build_output_frame,
)
from market_service.nooa_harness.engine.core.synthesize import (
    check_authored_output,
    path_resolves,
    validate_collapse,
)
from tests.test_engine import (
    _collapse_turn,
    _engine,
    _staged_narration,
    _track_tools,
    _STAGED_SUMMARY,
    _TRACK_ASSEMBLE,
    _TRACK_DISCIPLINE,
    _TRACK_FORECAST,
    _TRACK_HYPOTHESIZE,
    _TRACK_INTERPRET,
    _wake,
)
from tests.test_track_characterization import _evidence_prefix


def _frame(**kw):
    base = {
        "citable_heads": ["calc.ofi.intervals", "calc.price.delta"],
        "state_keys": ["gate"],
        "vectors": ["verdict", "base", "chain", "proof", "frame",
                    "directive", "gate"],
        "known_nulls": ["deterministic_state.microstructure_evidence"],
        "verdict_choice": "H0",
        "delta_required": False,
        "summary_floor": 80,
        "summary_target": 200,
    }
    base.update(kw)
    return base


def _authored(**kw):
    base = {
        "summary": "x" * 250,
        "hypothesis": {"H0": "h", "H1": "n", "evidence_refs": []},
        "hypothesis_probabilities": {
            "H0": {"probability": "medium", "p": None,
                   "basis_path": "calc.ofi.intervals → ofi"},
            "H1": {"probability": "low", "p": None,
                   "basis_path": "calc.ofi.intervals → ofi"},
            "chosen": "H0", "tiebreak": None,
        },
        "evidence": [{"path": "calc.ofi.intervals → ofi", "value": "12.5",
                      "interpretation": "sustained flow"}],
        "confidence": "medium",
    }
    base.update(kw)
    return base


class PathResolutionTests(unittest.TestCase):
    def test_memory_heads_never_resolve(self):
        self.assertFalse(path_resolves(_frame(), "memory.recall → rows"))
        self.assertFalse(path_resolves(_frame(), "memory.remember"))

    def test_namespaces_and_citable_heads_resolve(self):
        self.assertTrue(path_resolves(_frame(), "deterministic_state.gate"))
        self.assertTrue(path_resolves(_frame(), "ledger.observation.verdict-chosen"))
        self.assertTrue(path_resolves(_frame(), "calc.ofi.intervals → ofi"))

    def test_unknown_head_does_not_resolve(self):
        self.assertFalse(path_resolves(_frame(), "bogus.tool → x"))

    def test_refusal_findings_resolve_under_null_discipline(self):
        self.assertTrue(
            path_resolves(_frame(), "calc.scenario.evaluate → refusal"))


class FrameBuilderTests(unittest.TestCase):
    def test_memory_tools_are_not_citable_homes(self):
        frame = build_output_frame(
            accumulated={"calc.ofi.intervals": {}, "memory.recall": {}},
            deterministic_state={"gate": {}, "microstructure_evidence": None},
            verdict_choice="H0", delta_required=True)
        self.assertEqual(frame["citable_heads"], ["calc.ofi.intervals"])
        self.assertIn("deterministic_state.microstructure_evidence",
                      frame["known_nulls"])
        self.assertTrue(frame["delta_required"])
        self.assertEqual(frame["verdict_choice"], "H0")


class CollapseSchemaTests(unittest.TestCase):
    def test_frame_null_value_is_honest(self):
        ok, reasons = validate_collapse({
            "readings": [{"vector": "base",
                          "path": "deterministic_state.microstructure_evidence",
                          "value": None, "meaning": "not provided this run"}],
            "contradictions": [], "direction": "nowhere"}, _frame())
        self.assertTrue(ok, reasons)

    def test_refusal_value_is_honest(self):
        ok, reasons = validate_collapse({
            "readings": [{"vector": "proof",
                          "path": "calc.scenario.evaluate → refusal",
                          "value": None, "meaning": "deterministic refusal"}],
            "contradictions": [], "direction": "unevaluable"}, _frame())
        self.assertTrue(ok, reasons)

    def test_fake_placeholder_null_is_rejected_without_frame_backing(self):
        ok, reasons = validate_collapse({
            "readings": [{"vector": "base", "path": "calc.ofi.intervals → ofi",
                          "value": None, "meaning": "flow"}],
            "contradictions": [], "direction": "up"}, _frame())
        self.assertFalse(ok)
        self.assertTrue(any("value" in r for r in reasons))

    def test_uncitable_path_rejected_with_frame(self):
        ok, reasons = validate_collapse({
            "readings": [{"vector": "base", "path": "bogus.tool → x",
                          "value": "1", "meaning": "made up"}],
            "contradictions": [], "direction": "up"}, _frame())
        self.assertFalse(ok)
        self.assertTrue(any("not citable" in r for r in reasons))


class AuthoredGateTierTests(unittest.TestCase):
    def test_valid_authored_output_passes(self):
        hard, findings, conflicts = check_authored_output(_authored(), _frame())
        self.assertEqual(hard, [])
        self.assertEqual(conflicts, [])

    def test_stub_summary_is_hard(self):
        hard, _, _ = check_authored_output(
            _authored(summary="tiny"), _frame())
        self.assertTrue(any("summary" in m for m in hard))

    def test_thin_summary_is_finding_not_gate(self):
        hard, findings, _ = check_authored_output(
            _authored(summary="x" * 120), _frame())
        self.assertFalse(any("summary" in m for m in hard))
        self.assertTrue(any("thin" in f for f in findings))

    def test_empty_evidence_is_hard(self):
        hard, _, _ = check_authored_output(_authored(evidence=[]), _frame())
        self.assertTrue(any("evidence" in m for m in hard))

    def test_memory_citation_is_not_evidence(self):
        hard, _, _ = check_authored_output(_authored(evidence=[
            {"path": "memory.recall → rows", "value": "1",
             "interpretation": "my own note"}]), _frame())
        self.assertTrue(any("frame-backed" in m for m in hard))

    def test_missing_choice_is_hard(self):
        hard, _, _ = check_authored_output(_authored(
            hypothesis_probabilities={"H0": {}, "H1": {}}), _frame())
        self.assertTrue(any("chosen=H0|H1" in m for m in hard))

    def test_choice_mismatch_hard_then_conflict(self):
        authored = _authored(hypothesis_probabilities={
            "H0": {"probability": "medium"}, "H1": {"probability": "high"},
            "chosen": "H1", "tiebreak": None})
        hard, _, conflicts = check_authored_output(authored, _frame())
        self.assertTrue(any("adjudicated choice" in m for m in hard))
        self.assertEqual(conflicts, [])
        hard2, _, conflicts2 = check_authored_output(
            authored, _frame(), final_pass=True)
        self.assertFalse(any("adjudicated choice" in m for m in hard2))
        self.assertTrue(any("adjudicated choice" in c for c in conflicts2))

    def test_delta_compulsion_is_conditional(self):
        authored = _authored()
        hard, _, _ = check_authored_output(
            authored, _frame(delta_required=True))
        self.assertTrue(any("ΔP" in m for m in hard))
        with_delta = _authored(evidence=list(authored["evidence"]) + [
            {"path": "calc.price.delta → route_a_direct.delta_ticks",
             "value": "+0.5", "interpretation": "derived move"}])
        hard2, _, _ = check_authored_output(
            with_delta, _frame(delta_required=True))
        self.assertFalse(any("ΔP" in m for m in hard2))

    def test_refused_scenario_demands_unevaluable(self):
        hard, _, _ = check_authored_output(
            _authored(scenario={"verdict": "reachable", "rationale": "x" * 90}),
            _frame(), scenario={"target_price": "1"}, scenario_refused=True)
        self.assertTrue(any("unevaluable" in m for m in hard))


class AssembleAuthoredTests(unittest.TestCase):
    def test_fact_check_drops_only_unbacked(self):
        final, dropped = _assemble_authored(_authored(evidence=[
            {"path": "calc.ofi.intervals → ofi", "value": "12.5"},
            {"path": "bogus.tool → x", "value": "1", "interpretation": "made up"},
            {"path": "memory.recall → rows", "value": "1", "interpretation": "mine"},
        ]), _frame())
        self.assertEqual([e["path"] for e in final["evidence"]],
                         ["calc.ofi.intervals → ofi"])
        self.assertEqual(dropped, ["bogus.tool → x", "memory.recall → rows"])

    def test_no_backfill_no_rewrite(self):
        # Authentic output: a thin summary and a null hypothesis are KEPT —
        # the engine never substitutes staged content behind the agent.
        final, _ = _assemble_authored(
            {"summary": "tiny", "hypothesis": None}, _frame())
        self.assertEqual(final["summary"], "tiny")
        self.assertIsNone(final["hypothesis"])


def _walk_tail():
    return [
        _track_tools(*_TRACK_ASSEMBLE),
        _track_tools(*_TRACK_INTERPRET),
        _track_tools(*_TRACK_FORECAST),
        _track_tools(*_TRACK_HYPOTHESIZE),
        _staged_narration("P6", final=True),
        _track_tools(*_TRACK_DISCIPLINE),
        _staged_narration("P6", final=True),
    ]


_THIN_DERIVE = json.dumps({
    "phase": "P6", "summary": "Short derived summary.",
    "evidence": [{"path": "bogus.tool → x", "value": 1,
                  "interpretation": "made up"}],
    "confidence": "low",
    "hypothesis": {"H0": "h", "H1": "n", "evidence_refs": []},
    "hypothesis_probabilities": {
        "H0": {"probability": "medium", "p": None,
               "basis_path": "calc.ofi.intervals → ofi"},
        "H1": {"probability": "low", "p": None,
               "basis_path": "calc.ofi.intervals → ofi"},
        "chosen": "H0", "tiebreak": None},
    "tool_calls": [],
})

_BAD_DERIVE = json.dumps({
    "phase": "P6", "summary": "tiny",
    "evidence": [], "confidence": "low",
    "hypothesis": {"H0": "h", "H1": "n", "evidence_refs": []},
    "hypothesis_probabilities": {
        "H0": {"probability": "medium"}, "H1": {"probability": "low"},
        "chosen": "H0", "tiebreak": None},
    "tool_calls": [],
})


class AuthoredShipWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_flagged_ship_keeps_agent_words_and_honest_verdict(self):
        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(), *_walk_tail(),
            _collapse_turn(),          # valid collapse
            _THIN_DERIVE,              # authored output fails the hard gate
            # correction turn unscripted → degrades to the flagged ship
        ])
        artifact, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="flagged ship walk")
        self.assertEqual(meta.get("terminal"), "settled")
        # The agent's words SHIP as authored — never replaced by older turns.
        self.assertEqual(artifact.interpretation.get("summary"),
                         "Short derived summary.")
        self.assertEqual(artifact.interpretation.get("evidence"), [])
        # The verdict describes the object that shipped.
        fv = meta.get("final_validation") or {}
        self.assertFalse(fv.get("passed"))
        self.assertTrue(any("summary" in m for m in fv.get("missing") or []))
        self.assertTrue(any("evidence" in m for m in fv.get("missing") or []))
        conf = artifact.run_record["deterministic_state"].get("conformance") or {}
        self.assertEqual(conf.get("tier"), "authored")
        self.assertFalse(conf.get("correction_used"))

    async def test_correction_turn_fixes_the_authored_output(self):
        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(), *_walk_tail(),
            _collapse_turn(),
            _BAD_DERIVE,                    # hard misses → correction turn
            _staged_narration("P6", final=True),   # the agent fixes its output
        ])
        artifact, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="correction walk")
        self.assertEqual(meta.get("terminal"), "settled")
        fv = meta.get("final_validation") or {}
        self.assertTrue(fv.get("passed"), fv)
        self.assertTrue(fv.get("correction_used"))
        self.assertEqual(artifact.interpretation.get("summary"), _STAGED_SUMMARY)
        # The corrected citations are frame-backed and kept.
        self.assertGreaterEqual(
            len(artifact.interpretation.get("evidence") or []), 2)
        conf = artifact.run_record["deterministic_state"].get("conformance") or {}
        self.assertTrue(conf.get("correction_used"))


if __name__ == "__main__":
    unittest.main()
