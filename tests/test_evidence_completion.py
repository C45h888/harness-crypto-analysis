"""Evidence completion — engine completes, never kills, model-derived state.

``synthesize.complete_evidence`` fills null values and corrects mismatched
values from store truth for resolving paths, drops only unbacked entries,
and passes refusal findings through. Salvage prefers fill over drop, so the
live 4-in-53 case settles with numbers preserved, not deleted.
"""

from __future__ import annotations

ACCUM = {
    "calc.forward.distribution": {
        "tool": "calc.forward.distribution",
        "data": {"p_ge_theta": 0.42, "status": "ok"},
    },
    "calc.forward.fit": {"tool": "calc.forward.fit", "data": {"status": "ok"}},
}

COVERAGE = {
    "P1": {"x"}, "P2": {"x"}, "P3": {"x"},
    "P4": set(), "P5": {"x"}, "P6": {"d"},
}


def _entry(path, value, reading="reading"):
    return {"path": path, "value": value, "interpretation": reading}


class TestCompleteEvidence:
    def test_null_backed_value_is_filled(self):
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        kept, filled, corrected, dropped = complete_evidence(
            [_entry("calc.forward.distribution → p_ge_theta", None)], ACCUM)
        assert len(kept) == 1 and kept[0]["value"] == 0.42
        assert len(filled) == 1 and not corrected and not dropped

    def test_wrong_backed_value_is_corrected(self):
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        kept, filled, corrected, dropped = complete_evidence(
            [_entry("calc.forward.distribution → p_ge_theta", 0.41)], ACCUM)
        assert kept[0]["value"] == 0.42
        assert len(corrected) == 1 and not filled and not dropped

    def test_format_variants_correct_to_canonical_store_form(self):
        # Gate parity: validation compares Decimal text form, so "0.420"
        # mismatches store truth 0.42 there too — completion corrects to
        # the canonical store form rather than preserving formatting.
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        kept, filled, corrected, dropped = complete_evidence(
            [_entry("calc.forward.distribution → p_ge_theta", "0.420")], ACCUM)
        assert kept[0]["value"] == 0.42 and len(corrected) == 1

    def test_valued_unresolvable_is_preserved(self):
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        kept, filled, corrected, dropped = complete_evidence(
            [_entry("calc.price.delta → route_a.delta_ticks", "+0.53")], ACCUM)
        assert len(kept) == 1 and not filled and not corrected and not dropped

    def test_null_unresolvable_is_dropped(self):
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        kept, filled, corrected, dropped = complete_evidence(
            [_entry("calc.price.delta → route_a.delta_ticks", None)], ACCUM)
        assert not kept and len(dropped) == 1

    def test_refusal_passes_through(self):
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        entry = _entry("calc.scenario.evaluate → refusal", None, "refused: x")
        kept, filled, corrected, dropped = complete_evidence([entry], ACCUM)
        assert kept == [entry] and not filled and not corrected and not dropped

    def test_empty_path_and_non_dict_dropped(self):
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        kept, filled, corrected, dropped = complete_evidence(
            [_entry("", 1), "nope", None], ACCUM)
        assert not kept and len(dropped) == 3

    def test_wording_never_touched(self):
        from market_service.nooa_harness.engine.core.synthesize import (
            complete_evidence,
        )
        kept, _, _, _ = complete_evidence(
            [_entry("calc.forward.distribution → p_ge_theta", None, "my words")],
            ACCUM)
        assert kept[0]["interpretation"] == "my words"
        assert kept[0]["path"] == "calc.forward.distribution → p_ge_theta"


class TestSalvagePrefersFillOverDrop:
    def _judged(self, evidence):
        return {
            "summary": "x" * 250,
            "confidence": "medium",
            "hypothesis": {"H0": "h0", "H1": "h1"},
            "evidence": list(evidence),
            "scenario": None,
            "hypothesis_probabilities": {
                "chosen": "H0",
                "H0": {"probability": "medium",
                       "basis_path": "calc.forward.fit → status"},
                "H1": {"probability": "low",
                       "basis_path": "calc.forward.fit → status"},
            },
        }

    def test_backed_nulls_fill_and_settle_with_numbers_preserved(self):
        from market_service.nooa_harness.engine.core.validation import (
            salvage_entry_shape_misses,
            validate_final_turn,
        )
        evidence = [
            _entry("calc.ofi.intervals → data[0].ofi", 12.5),
            _entry("market.read → data.mid", 100.0),
            _entry("calc.price.delta → data.delta_ticks", 0.5),
            _entry("calc.forward.distribution → p_ge_theta", None),
        ]
        judged = self._judged(evidence)
        passed, missing = validate_final_turn(
            judged, COVERAGE, accumulated=ACCUM, findings=[])
        assert not passed and len(missing) == 1
        salvaged, remaining, records = salvage_entry_shape_misses(
            judged, missing, coverage=COVERAGE, scenario=None,
            scenario_status=None, scenario_refusal_reason=None,
            accumulated=ACCUM)
        assert salvaged is not None and remaining == []
        assert len(salvaged["evidence"]) == 4
        assert salvaged["evidence"][3]["value"] == 0.42
        assert any(r.get("action") == "filled" for r in records)

    def test_salvage_records_corrections(self):
        from market_service.nooa_harness.engine.core.validation import (
            salvage_entry_shape_misses,
            validate_final_turn,
        )
        evidence = [
            _entry("calc.ofi.intervals → data[0].ofi", 12.5),
            _entry("market.read → data.mid", 100.0),
            _entry("calc.price.delta → data.delta_ticks", 0.5),
            _entry("calc.forward.distribution → p_ge_theta", 0.99),
        ]
        judged = self._judged(evidence)
        passed, missing = validate_final_turn(
            judged, COVERAGE, accumulated=ACCUM, findings=[])
        assert not passed
        salvaged, remaining, records = salvage_entry_shape_misses(
            judged, missing, coverage=COVERAGE, scenario=None,
            scenario_status=None, scenario_refusal_reason=None,
            accumulated=ACCUM)
        assert salvaged is not None and remaining == []
        assert salvaged["evidence"][3]["value"] == 0.42
        assert any(r.get("action") == "corrected" for r in records)
