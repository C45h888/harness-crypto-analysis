"""V6 carry-over + evidence cap + structural gates (no steering)."""

import asyncio


def _fake_store():
    class _Redis:
        async def get(self, *a, **k):
            return None

    class _Store:
        prefix = "test"
        redis = _Redis()

    return _Store()


def _carry_controller(scenario):
    from market_service.nooa_harness.engine.controller import CycleController
    # Frozen dataclass: bypass init (constructor needs full wiring).
    controller = CycleController.__new__(CycleController)
    object.__setattr__(controller, "scenario", scenario)
    return controller


def test_v6_null_target_carried_from_directive():
    controller = _carry_controller({"target_price": "104.80", "horizon": "1h"})
    args, finding = controller.carry_scenario(
        "calc.scenario.evaluate", {"symbol": "SOLUSDT"})
    assert args["target_price"] == "104.80"
    assert args["horizon"] == "1h"
    assert finding is not None and finding["source"] == "directive"


def test_v6_explicit_target_wins_over_directive():
    controller = _carry_controller({"target_price": "104.80", "horizon": "1h"})
    args, finding = controller.carry_scenario(
        "calc.scenario.evaluate", {"symbol": "SOLUSDT", "target_price": "100.00"})
    # Explicit target is untouched; the ABSENT horizon is still carried from
    # the directive, and the finding names the contract (never silent).
    assert args["target_price"] == "100.00"
    assert args["horizon"] == "1h"
    assert finding is not None and finding["source"] == "directive"


def test_v6_no_directive_carries_nothing():
    controller = _carry_controller(None)
    args, finding = controller.carry_scenario(
        "calc.scenario.evaluate", {"symbol": "SOLUSDT"})
    assert "target_price" not in args
    assert finding is None


def test_v6_other_tools_untouched():
    controller = _carry_controller({"target_price": "104.80", "horizon": "1h"})
    args, finding = controller.carry_scenario(
        "market.read", {"symbol": "SOLUSDT"})
    assert "target_price" not in args
    assert finding is None


def test_evidence_cap_bounds_turn_growth():
    from market_service.nooa_harness.engine.schemas import coerce_turn
    turn = {"evidence": [
        {"path": f"t{i}", "value": str(i), "interpretation": "x"} for i in range(40)
    ], "tool_calls": []}
    out = coerce_turn(turn)
    assert len(out["evidence"]) == 16
    assert out["evidence"][0]["path"] == "t0"


def test_parse_gate_flags_pathless_evidence():
    from market_service.nooa_harness.engine.core.validation import (
        validate_final_turn,
    )
    passed, missing = validate_final_turn(
        {"summary": "x" * 250, "confidence": "medium",
         "hypothesis": {"H0": "h"}, "evidence": [{"interpretation": "naked number"}]},
        {"P1": {"x"}, "P2": {"x"}, "P3": {"x"}, "P4": set(), "P5": {"x"}, "P6": {"d"}})
    assert not passed
    assert any("needs path+value" in m for m in missing)


def test_refusal_finding_exempt_from_value_gate():
    from market_service.nooa_harness.engine.core.validation import (
        validate_final_turn,
    )
    passed, missing = validate_final_turn(
        {"summary": "x" * 250, "confidence": "medium",
         "hypothesis": {"H0": "h"},
         "evidence": [{"path": "calc.scenario.evaluate → refusal",
                       "interpretation": "refused: thin tape"}]},
        {"P1": {"x"}, "P2": {"x"}, "P3": {"x"}, "P4": set(), "P5": {"x"}, "P6": {"d"}})
    assert not any("needs path+value" in m for m in missing)


def test_thin_summary_is_finding_not_gate():
    from market_service.nooa_harness.engine.core.validation import (
        validate_final_turn,
    )
    findings: list[str] = []
    validate_final_turn(
        {"summary": "thin", "confidence": "medium",
         "hypothesis": {"H0": "h"}, "evidence": []},
        {"P1": {"x"}, "P2": {"x"}, "P3": {"x"}, "P4": set(), "P5": {"x"}, "P6": {"d"}},
        findings=findings)
    assert any("thin" in f for f in findings)
