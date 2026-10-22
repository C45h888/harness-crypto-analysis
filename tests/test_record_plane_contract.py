"""Record-plane contract: the ledger is canonical, every projection is derived.

Three invariants the runtime-audit found unenforced, now pinned:

1. **Every ``LedgerKind`` has a writer.** A declared kind that nothing writes
   (the retired ``SCENARIO`` member) is a third home for facts that already
   have one — it makes the ledger look richer while carrying nothing.
2. **``build_artifact_state()`` is derived and idempotent.** It runs four times
   per cycle over an already-projected state; a projection that re-reads its
   own output (or honours a tampered legacy key) would compound silently.
3. **The conformance view is ONE value in two places** — the typed
   ``CanonicalOutput.conformance`` column and
   ``run_record.deterministic_state["conformance"]``. The answer surface reads
   the run-record copy; an operator reading the column would then see a
   different verdict with nothing to compare against.
"""

from __future__ import annotations

import pathlib
import re
import unittest

_MARKET = pathlib.Path("market_service")
_ENGINE = _MARKET / "nooa_harness" / "engine"
_OUTPUT = _ENGINE / "core" / "output.py"
_LEDGER = _ENGINE / "ledger.py"


def _sources(*roots: pathlib.Path) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for root in roots:
        found.extend(
            p for p in root.rglob("*.py") if "__pycache__" not in str(p)
        )
    return found


def _normalised(path: pathlib.Path) -> str:
    """Source with runs of whitespace collapsed (formatting-insensitive)."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def _rich_state() -> dict:
    """A cycle state carrying every canonical root the projection reads."""
    from market_service.nooa_harness.engine import ledger as L

    st: dict = {}
    L.record_entry(st, L.LedgerKind.UNDERSTANDING, {"comprehension": {"c": 1}})
    L.record_entry(st, L.LedgerKind.PLAN, {"directive": {"kind": "hypothesis"}})
    L.record_entry(st, L.LedgerKind.HANDOFF, {"coverage": {"events_total": 3}})
    L.record_entry(st, L.LedgerKind.OBSERVATION,
                   {"stage": "base", "reading": {"beta": 0.2}})
    L.record_entry(st, L.LedgerKind.VERDICT,
                   {"stage": "gate", "gate": "data", "status": "passed",
                    "reasons": [], "facts": {"status": "ok", "reasons": []}})
    L.record_finding(st, "validation", "citation not frame-backed")
    L.record_finding(st, "chain_gap", "calc.forward.join refused: no events")
    L.record_finding(st, "assembly_dropped", "evidence.x.y",
                     detail={"reason": "not frame-backed"})
    L.record_finding(st, "principle", "model separation violated",
                     detail={"principle": "separation"})
    L.record_finding(st, "conformance", "authored ship conformance",
                     detail={"symbol": "SOLUSDT", "venue": "futures",
                             "hard_misses": ["b"], "conflicts": [],
                             "quality_findings": [], "dropped": ["evidence.x.y"],
                             "correction_used": False})

    st["plan"] = {"directive": {"kind": "hypothesis"},
                  "task_plan": {"kind": "hypothesis"},
                  "workflow": ["a"], "seeded": ["b"]}
    st["acquisition"] = {"wake": {"id": "w"},
                         "capture_status": {"state": "ok"},
                         "forecast_result": {"status": "validated"}}
    st["scenario_facts"] = {"contract": {"target_price": "104"},
                            "evaluation": {"verdict": "reachable"},
                            "digest": {"d": 1}}
    st["understanding"] = {"comprehension": {"c": 1}, "receipt": {"r": 1}}
    st["statistical_chain"] = {"chain_halt": None,
                               "base_summary": {"n": 1}}
    return st


class LedgerKindWriterTests(unittest.TestCase):
    """A kind nothing writes is a lie the ledger tells about itself."""

    def test_every_ledger_kind_has_a_write_or_read_site(self):
        from market_service.nooa_harness.engine.ledger import LedgerKind

        blob = "\n".join(
            p.read_text(encoding="utf-8")
            for p in _sources(_MARKET)
            if p != _LEDGER
        )
        missing = [
            kind.name for kind in LedgerKind
            if f"LedgerKind.{kind.name}" not in blob
            and f'"{kind.value}"' not in blob
        ]
        self.assertEqual(
            missing, [],
            "declared ledger kinds with no writer/reader anywhere: "
            f"{missing} — delete the member or give it a home",
        )

    def test_scenario_kind_is_not_declared(self):
        """Scenario facts live on deterministic_state['scenario_facts']."""
        from market_service.nooa_harness.engine.ledger import LedgerKind

        self.assertNotIn("SCENARIO", {kind.name for kind in LedgerKind})


class ArtifactProjectionTests(unittest.TestCase):
    """The projection layer is derived state, never a writable authority."""

    def test_projection_is_idempotent(self):
        from market_service.nooa_harness.engine.projections import (
            build_artifact_state,
        )

        once = build_artifact_state(_rich_state(), None)
        twice = build_artifact_state(once, None)
        self.assertEqual(
            twice, once,
            "re-projecting an already-projected state changed it: the "
            "projection is reading its own output",
        )

    def test_projected_keys_are_recomputed_not_honoured(self):
        from market_service.nooa_harness.engine.projections import (
            build_artifact_state,
        )

        once = build_artifact_state(_rich_state(), None)
        once["validation_findings"] = ["TAMPERED — hand-written legacy key"]
        once["chain_gaps"] = ["TAMPERED"]
        once["conformance"] = {"hard_misses": ["TAMPERED"]}

        rebuilt = build_artifact_state(once, None)
        self.assertEqual(rebuilt["validation_findings"],
                         ["citation not frame-backed"])
        self.assertEqual(rebuilt["chain_gaps"],
                         ["calc.forward.join refused: no events"])
        self.assertEqual(rebuilt["conformance"]["hard_misses"], ["b"])

    def test_findings_and_verdicts_never_enter_narration_context(self):
        """Prompt isolation: the ledger renders context kinds only."""
        from market_service.nooa_harness.engine.ledger import render_ledger

        rendered = render_ledger(_rich_state())
        self.assertIn("[understanding]", rendered)
        for section in ("[verdict]", "[finding]"):
            self.assertNotIn(section, rendered)


class ConformanceParityTests(unittest.TestCase):
    """One conformance value, two carriers — they must not drift."""

    def test_writer_derives_both_carriers_from_the_same_state(self):
        src = _normalised(_OUTPUT)
        self.assertIn(
            'conformance=deterministic_state.get("conformance")', src,
            "the conformance column must be the same value the run record "
            "carries (deterministic_state['conformance'])",
        )
        self.assertIn('"deterministic_state": dict(deterministic_state)', src,
                      "the run record must carry the same state dict")

    def test_wire_round_trip_carries_conformance_in_both_places(self):
        from market_service.nooa_harness.engine.core.answer import (
            answer_inputs_from_record,
        )
        from market_service.runtime.contracts import CanonicalOutput

        conf = {"hard_misses": ["missing scenario rationale"],
                "conflicts": [], "quality_findings": ["thin evidence"],
                "dropped": ["evidence.x.y"], "correction_used": True}
        record = CanonicalOutput.create(
            symbol="SOLUSDT", venue="futures",
            generated_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:01:00+00:00",
            status="validated", window_minutes=30, interval_seconds=10,
            input_hash="h", model_version="v",
            conformance=dict(conf),
            run_record={"deterministic_state": {"conformance": dict(conf)}},
        )
        wire = record.to_dict()
        self.assertEqual(wire["conformance"], conf)
        self.assertEqual(
            wire["run_record"]["deterministic_state"]["conformance"], conf)

        # The answer surface reads the run-record copy; both must agree or an
        # operator and the CLI would report different conformance for one run.
        conformance_in = answer_inputs_from_record(wire)[2]
        self.assertEqual(conformance_in["hard_misses"], conf["hard_misses"])
        self.assertEqual(conformance_in["quality_findings"],
                         conf["quality_findings"])

    def test_dropped_evidence_is_carried_on_the_column(self):
        """``dropped`` is recorded; it must at least survive the write."""
        from market_service.nooa_harness.engine.projections import (
            build_artifact_state,
        )

        projected = build_artifact_state(_rich_state(), None)
        self.assertEqual(projected["conformance"]["dropped"], ["evidence.x.y"])


if __name__ == "__main__":
    unittest.main()
