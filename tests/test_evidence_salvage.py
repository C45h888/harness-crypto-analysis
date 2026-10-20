"""Evidence authority unification — salvage, revision dedupe, cycle-level cap.

Regression suite for the live 2026-10-20 cycle: 4 null-valued citations out
of 53 killed a cycle the engine's own doctrine could have settled by removing
them. Covers the gate/assembly agreement, the removal-only salvage edge, the
path-keyed revision merge, and the per-turn cap bypass on finalize turns.
"""

from __future__ import annotations

import pytest

COVERAGE = {
    "P1": {"x"}, "P2": {"x"}, "P3": {"x"},
    "P4": set(), "P5": {"x"}, "P6": {"d"},
}

# Adjudication needs a resolvable basis_path; nothing else here reads it.
ACCUM = {"calc.forward.fit": {"tool": "calc.forward.fit",
                              "data": {"status": "ok"}}}


def _entry(path: str, value, reading: str = "reading") -> dict:
    return {"path": path, "value": value, "interpretation": reading}


def _base_evidence() -> list[dict]:
    return [
        _entry("calc.ofi.intervals → data[0].ofi", 12.5),
        _entry("market.read → data.mid", 100.0),
        _entry("calc.price.delta → data.delta_ticks", 0.5),
    ]


def _judged(evidence: list[dict]) -> dict:
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


def _salvage(judged, missing, *, task_plan=None):
    from market_service.nooa_harness.engine.core.validation import (
        salvage_entry_shape_misses,
    )
    return salvage_entry_shape_misses(
        judged, missing, coverage=COVERAGE, scenario=None,
        scenario_status=None, scenario_refusal_reason=None,
        accumulated=ACCUM, task_plan=task_plan,
    )


def _gate(judged):
    from market_service.nooa_harness.engine.core.validation import (
        validate_final_turn,
    )
    return validate_final_turn(judged, COVERAGE, accumulated=ACCUM,
                               findings=[])


# ---------------------------------------------------------------------------
# Salvage: the 4/53 case settles
# ---------------------------------------------------------------------------


def test_four_null_citations_out_of_53_salvage_and_settle():
    evidence = _base_evidence() + [
        _entry("micro.fit_beta → data.beta", None) for _ in range(4)
    ]
    # Distinct paths: null entries with the same path would collapse (see
    # merge tests) — this mirrors the live dump, 4 distinct fields.
    for i, e in enumerate(evidence):
        if e["value"] is None:
            e["path"] = f"micro.fit_beta → data.beta[{i}]"
    judged = _judged(evidence)
    passed, missing = _gate(judged)
    assert not passed
    assert len(missing) == 4
    assert all("needs path+value" in m for m in missing)

    salvaged, remaining, dropped = _salvage(judged, missing)
    assert salvaged is not None
    assert remaining == []
    assert len(salvaged["evidence"]) == 3
    assert len(dropped) == 4
    assert {d["index"] for d in dropped} == {3, 4, 5, 6}


def test_salvage_refuses_when_delta_citation_is_dropped():
    # The only ΔP citation is null-valued: removing it re-opens the ΔP gate.
    evidence = [
        _entry("calc.ofi.intervals → data[0].ofi", 12.5),
        _entry("market.read → data.mid", 100.0),
        _entry("calc.price.delta → data.delta_ticks", None),
    ]
    judged = _judged(evidence)
    passed, missing = _gate(judged)
    assert not passed
    salvaged, remaining, dropped = _salvage(judged, missing)
    assert salvaged is None
    assert dropped == []
    assert remaining == missing


def test_salvage_refuses_on_structural_miss():
    evidence = _base_evidence() + [_entry("micro.fit_beta → data.beta", None)]
    judged = _judged(evidence)
    passed, missing = _gate(judged)
    assert not passed
    # Inject a structural miss alongside the entry-shape miss.
    missing = list(missing) + ["phase P3 uncovered: execute its tools"]
    salvaged, remaining, _ = _salvage(judged, missing)
    assert salvaged is None
    assert remaining == missing


def test_salvage_refuses_when_directive_verdict_would_disappear():
    # has_directive_verdict counts a path even when its value is null, so the
    # gate can pass while the only directive citation is the salvage target.
    evidence = _base_evidence() + [
        _entry("deterministic_state.task_directive → band", None),
    ]
    judged = _judged(evidence)
    passed, missing = _gate(judged)
    assert not passed and len(missing) == 1
    salvaged, remaining, _ = _salvage(
        judged, missing, task_plan={"targets": [1.0]})
    assert salvaged is None
    assert remaining == missing


def test_salvage_is_bounded():
    from market_service.nooa_harness.engine.config import MAX_SALVAGE_ENTRIES

    evidence = _base_evidence() + [
        _entry(f"micro.fit_beta → data.beta[{i}]", None)
        for i in range(MAX_SALVAGE_ENTRIES + 1)
    ]
    judged = _judged(evidence)
    passed, missing = _gate(judged)
    assert not passed
    salvaged, remaining, _ = _salvage(judged, missing)
    assert salvaged is None
    assert remaining == missing


def test_salvage_records_ledger_and_state():
    from market_service.nooa_harness.engine.core.validation import (
        _record_evidence_salvage,
    )

    class _Wake:
        wake_id = "wake-1"

    class _Ctx:
        task_id = "task-1"
        wake = _Wake()

    class _St:
        deterministic_state: dict = {}
        ledger_stage_ids: list = []

    st = _St()
    _record_evidence_salvage(
        st, _Ctx(), [{"index": 3, "path": "micro.fit_beta → data.beta"}])
    assert st.deterministic_state["evidence_salvaged"] == [
        {"index": 3, "path": "micro.fit_beta → data.beta"}]
    rows = st.deterministic_state["ledger"]
    assert rows[-1]["payload"]["stage"] == "evidence-salvage"
    assert rows[-1]["payload"]["dropped"][0]["index"] == 3
    assert st.ledger_stage_ids[-1].startswith("evidence-salvage#")


# ---------------------------------------------------------------------------
# Merge: a repair can actually fix an entry (null -> value, in place)
# ---------------------------------------------------------------------------


def test_repair_null_to_value_supersedes_in_place():
    from market_service.nooa_harness.engine.core.validation import (
        merge_interpretation,
    )

    merged = merge_interpretation([
        {"evidence": [_entry("calc.ofi.intervals → cvd", None)]},
        {"evidence": [_entry("calc.ofi.intervals → cvd", -12.3)]},
    ])
    assert len(merged["evidence"]) == 1
    assert merged["evidence"][0]["value"] == -12.3


def test_zero_value_does_not_collide_with_null():
    from market_service.nooa_harness.engine.core.validation import (
        merge_interpretation,
    )

    merged = merge_interpretation([
        {"evidence": [_entry("calc.ofi.intervals → cvd", None)]},
        {"evidence": [_entry("calc.ofi.intervals → cvd", 0)]},
    ])
    assert len(merged["evidence"]) == 1
    assert merged["evidence"][0]["value"] == 0


def test_distinct_non_null_values_stay_visible():
    from market_service.nooa_harness.engine.core.validation import (
        merge_interpretation,
    )

    merged = merge_interpretation([
        {"evidence": [_entry("calc.ofi.intervals → cvd", 1.0)]},
        {"evidence": [_entry("calc.ofi.intervals → cvd", 2.0)]},
    ])
    # A contradiction must remain visible to the echo gate.
    assert len(merged["evidence"]) == 2


def test_merged_evidence_is_cycle_bounded():
    from market_service.nooa_harness.engine.config import MAX_MERGED_EVIDENCE
    from market_service.nooa_harness.engine.core.validation import (
        merge_interpretation,
    )

    turns = [
        {"evidence": [_entry(f"tool{i}.read → field", i)
                      for i in range(MAX_MERGED_EVIDENCE + 10)]},
    ]
    merged = merge_interpretation(turns)
    assert len(merged["evidence"]) == MAX_MERGED_EVIDENCE


# ---------------------------------------------------------------------------
# coerce_turn: finalize turns (no tool_calls key) are capped too
# ---------------------------------------------------------------------------


def test_turn_cap_applies_without_tool_calls_key():
    from market_service.nooa_harness.engine.schemas import coerce_turn

    turn = {"summary": "x", "evidence": [
        {"path": f"t{i}", "value": str(i), "interpretation": "x"}
        for i in range(53)]}
    assert "tool_calls" not in turn
    out = coerce_turn(turn)
    assert len(out["evidence"]) == 16


def test_turn_cap_applies_when_tool_calls_null():
    from market_service.nooa_harness.engine.schemas import coerce_turn

    out = coerce_turn({
        "tool_calls": None,
        "evidence": [{"path": f"t{i}", "value": str(i)} for i in range(30)],
    })
    assert len(out["evidence"]) == 16


# ---------------------------------------------------------------------------
# Gate/assembly share one classifier
# ---------------------------------------------------------------------------


def test_assembly_drops_null_value_citations():
    from market_service.nooa_harness.engine.core.output import _assemble_authored

    frame = {"citable_heads": ["calc.ofi.intervals", "market.read"],
             "state_keys": []}
    final, dropped = _assemble_authored({"evidence": [
        {"path": "calc.ofi.intervals → ofi", "value": "12.5"},
        {"path": "market.read → mid", "value": None},
    ]}, frame)
    assert [e["path"] for e in final["evidence"]] == ["calc.ofi.intervals → ofi"]
    assert dropped == ["market.read → mid"]


def test_gate_and_assembly_agree_on_shape():
    from market_service.nooa_harness.engine.core.output import _assemble_authored
    from market_service.nooa_harness.engine.core.synthesize import (
        classify_evidence,
    )

    frame = {"citable_heads": ["calc.ofi.intervals"], "state_keys": []}
    entry = {"path": "calc.ofi.intervals → ofi", "value": None,
             "interpretation": "reading"}
    assert classify_evidence(entry, frame) == "null_value"
    assert classify_evidence(entry) == "null_value"
    final, dropped = _assemble_authored({"evidence": [entry]}, frame)
    assert final["evidence"] == [] and dropped == ["calc.ofi.intervals → ofi"]
