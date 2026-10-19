"""C2 answer composer (engine/core/answer.py) — the pure render layer.

Pins the format contract of ``docs/CLI_ANSWER_SURFACE_PLAN.md`` §2:

  - ANSWER prose blocks are the agent's words VERBATIM (the renderer never
    authors, never fills an emptied block);
  - the GATE DATA blocks carry the full compulsion list — every hard-tier
    item of ``check_authored_output`` appears as a block or as an explicit
    missing/refusal line;
  - the OUTPUT CONFORMANCE footer is never inline and is honest
    (hard_misses / conflicts / quality_findings / correction_used / terminal);
  - the ΔP block renders ONLY over a target-bearing directive;
  - re-rendering from the same run record is exact (parity by construction).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from market_service.nooa_harness.engine.core.answer import (
    MISSING,
    NOT_APPLICABLE,
    answer_from_record,
    answer_inputs_from_record,
    compose_answer,
    compose_answer_structured,
)


def _authored(**over):
    base = {
        "summary": "Flow is one-sided and depth is stable, so the move persists now.",
        "evidence": [
            {"path": "calc.ofi.intervals → ofi", "value": "12.5",
             "interpretation": "sustained one-sided flow"},
            {"path": "calc.depth.average → mean_ad", "value": "10.5",
             "interpretation": "passive depth holds"},
        ],
        "scenario_rationale": "The target sits inside the forward band at this horizon.",
        "model_separation": "nu*OFI is heteroskedastic; never merged.",
        "limitations": ["synthetic window"],
    }
    base.update(over)
    return base


def _gate(**over):
    base = {
        "hypothesis": {"H0": "beta > 0 (flow moves price)",
                       "H1": "beta <= 0 (flow no longer moves price)"},
        "adjudication": {
            "H0": {"probability": "medium", "p": None,
                   "basis_path": "calc.forward.fit → data.status"},
            "H1": {"probability": "low", "p": None,
                   "basis_path": "calc.forward.fit → data.status"},
            "chosen": "H0", "tiebreak": None,
        },
        "scenario": {"applicable": True, "verdict": "reachable",
                     "target": "245.30", "horizon": "1h",
                     "cited": [{"path": "calc.scenario.evaluate → verdict",
                                "value": "reachable",
                                "interpretation": "inside the forward band"}],
                     "rationale": None},
        "directive": {"kind": "price_target", "horizon": "1h",
                      "horizon_regime": "long", "targets": ["245.30"],
                      "invalidations": [], "parse_note": "targets=[245.30]",
                      "answer": [{"path": "calc.scenario.evaluate → verdict",
                                  "value": "reachable",
                                  "interpretation": "band says reachable"}]},
        "delta": {"required": True,
                  "cited": [{"path": "calc.price.delta → route_a.delta_ticks",
                             "value": "+0.53",
                             "interpretation": "derived move with band"}]},
        "refusals": [{"source": "directive.horizon='5m'",
                      "reason": "unsupported horizon (between native and long)"}],
        "confidence": "medium",
    }
    base.update(over)
    return base


def _conformance(**over):
    base = {"hard_misses": [], "conflicts": [], "quality_findings": [],
            "correction_used": False}
    base.update(over)
    return base


def _meta(**over):
    base = {"symbol": "BTCUSDT", "venue": "spot",
            "artifact_id": "00000000-0000-0000-0000-000000000001",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "session_id": "sess", "task": "Explain the evidence", "gate": "provisional"}
    base.update(over)
    return base


def _render(**kw):
    return compose_answer(
        kw.pop("authored", _authored()), kw.pop("gate", _gate()),
        kw.pop("conformance", _conformance()), kw.pop("meta", _meta()))


class VerbatimProseTests(unittest.TestCase):
    def test_agent_words_render_verbatim(self):
        out = _render()
        self.assertIn(_authored()["summary"], out)
        self.assertIn("sustained one-sided flow", out)
        self.assertIn("The target sits inside the forward band at this horizon.", out)
        self.assertIn("nu*OFI is heteroskedastic; never merged.", out)
        self.assertIn("synthetic window", out)
        self.assertIn("beta > 0 (flow moves price)", out)

    def test_renderer_authors_no_prose(self):
        """Remove every input string; only structural labels may remain."""
        authored, gate, conf, meta = _authored(), _gate(), _conformance(), _meta()
        out = compose_answer(authored, gate, conf, meta)
        payload_texts = []
        for blob in (authored, gate, conf, meta):
            payload_texts.extend(_flatten_strings(blob))
        residue = out
        for text in sorted((t for t in payload_texts if t), key=len, reverse=True):
            residue = residue.replace(text, "")
        leaked = sorted({word for word in _words(residue)
                         if word not in _LABEL_WORDS})
        self.assertEqual(leaked, [],
                         f"engine-authored words leaked into the answer: {leaked}")

    def test_golden_format(self):
        """The rendered format is pinned (reviewed line-by-line)."""
        golden = (Path(__file__).with_name("fixtures")
                  / "answer_surface_golden.txt").read_text()
        out = compose_answer(_authored(), _gate(), _conformance(), _meta())
        self.assertEqual(out, golden)

    def test_emptied_blocks_render_missing_never_filled(self):
        out = _render(authored=_authored(summary=None, evidence=[],
                                         scenario_rationale=None,
                                         model_separation=None, limitations=[]))
        self.assertGreaterEqual(out.count(MISSING), 5)
        # no invented prose in place of the missing blocks
        self.assertNotIn("Flow is one-sided", out)

    def test_null_discipline_null_never_zero(self):
        out = _render(authored=_authored(evidence=[
            {"path": "calc.ofi.intervals → ofi", "value": None,
             "interpretation": "null field, not provided"}]))
        self.assertIn("calc.ofi.intervals → ofi = null", out)
        self.assertNotIn("ofi = 0", out)


class CompulsionCompletenessTests(unittest.TestCase):
    """The composer's contract IS the check_authored_output hard tier."""

    def _assert_flagged(self, out, structured, block):
        self.assertIn(MISSING, out)
        self.assertIn(block, structured["conformance"]["missing_blocks"])

    def test_summary_compulsion(self):
        structured = compose_answer_structured(
            _authored(summary=None), _gate(), _conformance(), _meta())
        out = _render(authored=_authored(summary=None))
        self._assert_flagged(out, structured, "summary")

    def test_evidence_compulsion(self):
        structured = compose_answer_structured(
            _authored(evidence=[]), _gate(), _conformance(), _meta())
        self._assert_flagged(
            _render(authored=_authored(evidence=[])), structured, "evidence")

    def test_hypothesis_compulsion(self):
        structured = compose_answer_structured(
            _authored(), _gate(hypothesis={"H0": None, "H1": None}),
            _conformance(), _meta())
        out = _render(gate=_gate(hypothesis={"H0": None, "H1": None}))
        self._assert_flagged(out, structured, "hypothesis.H0")
        self._assert_flagged(out, structured, "hypothesis.H1")

    def test_adjudication_compulsion(self):
        structured = compose_answer_structured(
            _authored(), _gate(adjudication=None), _conformance(), _meta())
        out = _render(gate=_gate(adjudication=None))
        self._assert_flagged(out, structured, "adjudication")

    def test_scenario_compulsion_when_scenario_given(self):
        structured = compose_answer_structured(
            _authored(scenario_rationale=None),
            _gate(scenario={"applicable": True, "verdict": None, "target": None,
                            "horizon": None, "cited": []}),
            _conformance(), _meta())
        out = _render(
            authored=_authored(scenario_rationale=None),
            gate=_gate(scenario={"applicable": True, "verdict": None,
                                 "target": None, "horizon": None, "cited": []}))
        for block in ("scenario.verdict", "scenario.target", "scenario.horizon",
                      "scenario.cited", "scenario_rationale"):
            self.assertIn(block, structured["conformance"]["missing_blocks"])
        self.assertIn(MISSING, out)

    def test_delta_compulsion_when_required(self):
        structured = compose_answer_structured(
            _authored(), _gate(delta={"required": True, "cited": []}),
            _conformance(), _meta())
        out = _render(gate=_gate(delta={"required": True, "cited": []}))
        self._assert_flagged(out, structured, "delta")

    def test_missing_blocks_flag_the_ship(self):
        structured = compose_answer_structured(
            _authored(summary=None), _gate(), _conformance(), _meta())
        self.assertEqual(structured["conformance"]["terminal"], "flagged")


class DeltaConditionalityTests(unittest.TestCase):
    def test_delta_block_only_over_target_bearing_directive(self):
        out = _render()
        self.assertIn("--- delta (required by directive targets/invalidations) ---",
                      out)
        gate = _gate(delta={"required": False, "cited": []})
        out = _render(gate=gate)
        self.assertNotIn("--- delta", out)
        self.assertIsNone(
            compose_answer_structured(_authored(), gate, _conformance(), _meta())
            ["gate_data"]["delta"])


class ScenarioApplicabilityTests(unittest.TestCase):
    def test_no_scenario_is_not_applicable_never_flagged(self):
        gate = _gate(scenario={"applicable": False, "verdict": None, "target": None,
                               "horizon": None, "cited": []})
        structured = compose_answer_structured(
            _authored(scenario_rationale=None), gate, _conformance(), _meta())
        out = _render(authored=_authored(scenario_rationale=None), gate=gate)
        self.assertIn(f"{NOT_APPLICABLE} (no scenario given)", out)
        self.assertNotIn(MISSING, out)
        self.assertEqual(structured["conformance"]["terminal"], "settled")

    def test_general_directive_answer_is_not_applicable(self):
        gate = _gate(delta={"required": False, "cited": []},
                     directive={"kind": "general", "horizon": None,
                                "horizon_regime": "none", "targets": [],
                                "invalidations": [], "parse_note": "no values",
                                "answer": []})
        out = _render(gate=gate)
        self.assertIn(f"{NOT_APPLICABLE} (no targets bound)", out)


class FooterHonestyTests(unittest.TestCase):
    def test_footer_carries_conformance_verbatim(self):
        out = _render(conformance=_conformance(
            hard_misses=["summary required (≥80 chars)"],
            conflicts=["hypothesis_probabilities.chosen must equal H0"],
            quality_findings=["summary thin (120/200)"],
            correction_used=True))
        self.assertIn("OUTPUT CONFORMANCE", out)
        self.assertIn("- summary required (≥80 chars)", out)
        self.assertIn("- hypothesis_probabilities.chosen must equal H0", out)
        self.assertIn("- summary thin (120/200)", out)
        self.assertIn("correction_used: true", out)
        self.assertIn("terminal: flagged", out)

    def test_clean_run_settles(self):
        out = _render()
        self.assertIn("correction_used: false", out)
        self.assertIn("terminal: settled", out)

    def test_footer_is_never_inline(self):
        """No conformance vocabulary appears inside the agent's prose blocks."""
        out = _render(conformance=_conformance(
            hard_misses=["summary required (≥80 chars)"]))
        prose = out.split("GATE DATA")[0]
        self.assertNotIn("hard_misses", prose)
        self.assertNotIn("OUTPUT CONFORMANCE", prose)


class RunRecordParityTests(unittest.TestCase):
    def _record(self, **over):
        record = {
            "artifact_id": "00000000-0000-0000-0000-000000000001",
            "symbol": "BTCUSDT", "venue": "spot",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "session_id": "sess", "status": "provisional",
            "deterministic_state": {
                "task": "can price hit 245.30?",
                "task_directive": {
                    "kind": "price_target",
                    "horizon": {"value": "1h", "regime": "long",
                                "resolved_ms": 3600000},
                    "targets": [{"value": "245.30", "kind": "target",
                                 "provenance": "cli:scenario"}],
                    "invalidations": [],
                    "refusals": [{"field": "horizon", "value": "5m",
                                  "reason": "unsupported horizon"}],
                    "parse_note": "targets=[245.30]",
                },
                "scenario": {"target_price": "245.30", "horizon": "1h"},
                "conformance": {"tier": "authored", "dropped": [],
                                "hard_misses": [], "conflicts": [],
                                "quality_findings": [], "correction_used": False},
            },
            "interpretation": {
                "summary": _authored()["summary"],
                "evidence": [
                    {"path": "calc.price.delta → route_a.delta_ticks",
                     "value": "+0.53", "interpretation": "derived move with band"},
                    {"path": "calc.scenario.evaluate → verdict",
                     "value": "reachable", "interpretation": "band says reachable"},
                ],
                "confidence": "medium",
                "limitations": ["synthetic window"],
                "model_separation": "nu*OFI is heteroskedastic; never merged.",
                "scenario": {"verdict": "reachable",
                             "rationale": "The target sits inside the band."},
                "hypothesis_probabilities": {
                    "H0": {"probability": "medium", "p": None,
                           "basis_path": "calc.forward.fit → data.status"},
                    "H1": {"probability": "low", "p": None,
                           "basis_path": "calc.forward.fit → data.status"},
                    "chosen": "H0", "tiebreak": None},
            },
            "hypothesis": {"H0": "beta > 0", "H1": "beta <= 0"},
        }
        record.update(over)
        return record

    def test_answer_from_record_renders_the_full_compulsion(self):
        out = answer_from_record(self._record())
        self.assertIsNotNone(out)
        for marker in ("--- summary ---", "--- evidence ---", "--- hypothesis ---",
                       "--- adjudication ---", "--- scenario ---",
                       "--- directive ---",
                       "--- delta (required by directive targets/invalidations) ---",
                       "--- refusals ---", "--- confidence ---",
                       "OUTPUT CONFORMANCE"):
            self.assertIn(marker, out)
        self.assertIn("- directive.horizon=5m: unsupported horizon", out)
        self.assertIn("targets: 245.30", out)

    def test_re_render_is_exact(self):
        record = self._record()
        self.assertEqual(answer_from_record(record), answer_from_record(record))

    def test_inputs_consume_record_directives_and_delta(self):
        _, gate_data, conformance, run_meta = answer_inputs_from_record(self._record())
        self.assertTrue(gate_data["delta"]["required"])
        self.assertEqual(gate_data["directive"]["targets"], ["245.30"])
        self.assertEqual(gate_data["confidence"], "medium")
        self.assertEqual(conformance["correction_used"], False)
        self.assertEqual(run_meta["task"], "can price hit 245.30?")

    def test_no_authored_ship_renders_no_answer(self):
        record = self._record()
        record["deterministic_state"].pop("conformance")
        self.assertIsNone(answer_from_record(record))
        degraded = self._record()
        degraded["interpretation"] = None
        self.assertIsNone(answer_from_record(degraded))

    def test_memory_paths_never_gain_engine_evidence(self):
        record = self._record()
        record["interpretation"]["evidence"] = [
            {"path": "memory.recall → rows", "value": "1",
             "interpretation": "remembered"}]
        record["deterministic_state"]["conformance"]["hard_misses"] = [
            "evidence must cite ≥1 frame-backed path (memory.* is never evidence)"]
        out = answer_from_record(record)
        # the agent's own words ship; the gate's honest miss rides the footer
        self.assertIn("memory.recall → rows", out)
        self.assertIn("memory.* is never evidence", out)
        self.assertIn("terminal: flagged", out)
        # and the renderer adds no evidence the agent did not write
        structured = compose_answer_structured(*answer_inputs_from_record(record))
        self.assertEqual(len(structured["answer"]["evidence"]), 1)


def _flatten_strings(blob) -> list[str]:
    out: list[str] = []
    if isinstance(blob, str):
        out.append(blob)
    elif isinstance(blob, dict):
        for value in blob.values():
            out.extend(_flatten_strings(value))
    elif isinstance(blob, (list, tuple)):
        for value in blob:
            out.extend(_flatten_strings(value))
    return out


def _words(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    return {t for t in tokens if any(c.isalpha() for c in t)}


#: The ONLY words the renderer may contribute beyond the input payloads.
_LABEL_WORDS = {
    "ANSWER", "GATE", "DATA", "OUTPUT", "CONFORMANCE", "H0", "H1",
    "missing", "flagged", "not", "applicable", "no", "scenario", "given",
    "targets", "bound", "none", "null", "true", "false", "settled",
    "required", "by", "directive", "invalidations", "run", "artifact",
    "task", "summary", "evidence", "rationale", "model", "separation",
    "limitations", "hypothesis", "adjudication", "verdict", "target",
    "horizon", "cited", "kind", "answer", "basis", "chosen", "tiebreak",
    "refusals", "confidence", "hard", "misses", "conflicts", "quality",
    "findings", "correction", "used", "terminal", "delta", "p", "of",
    "note",
}


if __name__ == "__main__":
    unittest.main(verbosity=2)
