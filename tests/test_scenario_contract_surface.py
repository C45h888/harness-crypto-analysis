"""Scenario contract surface — contract, carry, redundancy, validation, prompts.

The 104.80 trace made a runtime object necessary: one frozen question, one
declaration path, one oracle. These tests pin the surface from both planes:

* contract/carry/redundancy (controller),
* validation (effective bound, no any-match scan, refusal-of-wrong-target),
* prompt rendering (opening contract block, live status block, digest).
"""

from types import SimpleNamespace

import pytest

from market_service.nooa_harness.engine.controller import (
    CycleController,
    ScenarioEvalStatus,
)
from market_service.nooa_harness.engine.core.validation import (
    check_scenario_equality,
    _record_scenario_evaluation,
)
from market_service.nooa_harness.engine.scenario_contract import (
    build_contract,
    canonical_number,
    render_contract_block,
    render_status_block,
)


TARGET = "104.80"
HORIZON = "1h"


def _contract(task=None, scenario=None):
    return build_contract(
        scenario or {"target_price": TARGET, "horizon": HORIZON}, task)


def _controller(contract, **kwargs):
    return CycleController(
        contract.to_scenario_dict() if contract is not None else None,
        scenario_contract=contract,
        **kwargs,
    )


def _log(target=TARGET, horizon=HORIZON, *, refused=False, reason=None):
    detail = ({"status": "refused", "reason": reason or "thin tape"}
              if refused else {"status": "evaluated_ok"})
    return {
        "capability": "calc.scenario.evaluate",
        "scope": {"symbol": "SOLUSDT", "venue": "spot",
                  "target_price": target, "horizon": horizon},
        "result": "ok",
        "detail": detail,
    }


def _payload(target=TARGET, horizon=HORIZON, exceedance="0"):
    return {
        "target_price": target, "horizon": horizon, "direction": "up",
        "exceedance": exceedance, "exceedance_range": [exceedance, exceedance],
        "n_windows_usable": 50, "fit_status": "validated", "r2": "0.5",
    }


def _evaluated(controller, target=TARGET, horizon=HORIZON, exceedance="0"):
    args = {"symbol": "SOLUSDT", "venue": "spot",
            "target_price": target, "horizon": horizon}
    return controller.record_outcome(
        "calc.scenario.evaluate",
        _log(target, horizon),
        _payload(target, horizon, exceedance),
        args=args,
    )


def _refused(controller, target=TARGET, horizon=HORIZON):
    args = {"symbol": "SOLUSDT", "venue": "spot",
            "target_price": target, "horizon": horizon}
    return controller.record_outcome(
        "calc.scenario.evaluate",
        _log(target, horizon, refused=True),
        None,
        args=args,
    )


def _ctx(contract):
    return SimpleNamespace(
        scenario=contract.to_scenario_dict() if contract is not None else None)


def _state(controller, accumulated=None):
    return SimpleNamespace(
        controller=controller,
        capability_log=[],
        accumulated_tool_results=accumulated or {},
        deterministic_state={"gate": {"status": "validated", "reasons": []}},
    )


def _judged(target=TARGET, horizon=HORIZON, verdict="not_reachable",
            divergence=None):
    block = {
        "target_price": target, "horizon": horizon, "direction": "up",
        "required_ofi": "50000", "exceedance": "0",
        "fit_status": "validated", "n_windows_usable": 50,
        "probability": "low", "verdict": verdict,
        "rationale": ("required flow sits outside the observed regime on a "
                      "validated fit over 50 usable windows; verdict follows "
                      "the deterministic derivation"),
    }
    if divergence is not None:
        block["divergence"] = divergence
    return {"scenario": block}


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

def test_contract_canonicalizes_and_forbids_by_default():
    contract = _contract()
    assert contract.asked_target == TARGET
    assert contract.target_canonical == canonical_number(TARGET)
    assert contract.divergence == "forbidden"
    assert contract.contract_id


def test_contract_prompt_comparison_permits_declared_divergence():
    contract = _contract(task="compare the 104.80 target with the 100.00 level")
    assert contract.divergence == "declared"
    assert contract.divergence_basis


def test_contract_none_without_scenario():
    assert build_contract(None, "a task") is None


def test_allowed_targets_union_plan_targets():
    contract = _contract()
    refined = contract.with_allowed_targets(["100.00"])
    assert canonical_number("100.00") in refined.allowed_targets
    assert refined.permits("100.00")


# ---------------------------------------------------------------------------
# Carry
# ---------------------------------------------------------------------------

def test_carry_fills_target_and_horizon_independently():
    controller = _controller(_contract())
    args, finding = controller.carry_scenario(
        "calc.scenario.evaluate", {"symbol": "SOLUSDT"})
    assert args["target_price"] == TARGET
    assert args["horizon"] == HORIZON
    assert finding and finding["source"] == "directive"


def test_carry_fills_horizon_when_target_is_explicit():
    controller = _controller(_contract())
    args, finding = controller.carry_scenario(
        "calc.scenario.evaluate", {"target_price": TARGET})
    assert args["target_price"] == TARGET
    assert args["horizon"] == HORIZON
    assert finding is not None


def test_carry_replaces_invalid_explicit_target():
    controller = _controller(_contract())
    args, finding = controller.carry_scenario(
        "calc.scenario.evaluate", {"target_price": 0, "horizon": HORIZON})
    assert args["target_price"] == TARGET
    assert finding["ignored_invalid_explicit"] == "0"


def test_carry_does_not_mask_valid_explicit_target():
    controller = _controller(_contract())
    args, finding = controller.carry_scenario(
        "calc.scenario.evaluate",
        {"target_price": "100.00", "horizon": HORIZON})
    assert args["target_price"] == "100.00"
    assert finding is None


# ---------------------------------------------------------------------------
# Declarations (engine-disposed)
# ---------------------------------------------------------------------------

def test_revision_rejected_when_prompt_forbids_it():
    controller = _controller(_contract())
    successor = controller.with_scenario_intent({
        "mode": "revision", "target_price": "100.00", "horizon": HORIZON,
        "reason": "a different level looks more informative here",
        "basis_path": "deterministic_state.task",
    })
    assert successor.scenario_intent is None
    assert successor.scenario_intent_rejects
    assert successor.effective_scenario() == (TARGET, HORIZON)


def test_revision_accepted_when_prompt_permits_it():
    controller = _controller(
        _contract(task="compare the 104.80 target with the 100.00 level"))
    successor = controller.with_scenario_intent({
        "mode": "revision", "target_price": "100.00", "horizon": HORIZON,
        "reason": "the comparison prompt names the second level explicitly",
        "basis_path": "deterministic_state.task",
    })
    assert successor.scenario_intent["mode"] == "revision"
    assert successor.effective_scenario() == (canonical_number("100.00"), HORIZON)


def test_revision_requires_reason_and_basis():
    controller = _controller(
        _contract(task="compare the 104.80 target with the 100.00 level"))
    successor = controller.with_scenario_intent({
        "mode": "revision", "target_price": "100.00"})
    assert successor.scenario_intent is None
    reasons = " ".join(e["reason"] for e in successor.scenario_intent_rejects)
    assert "reason" in reasons and "basis_path" in reasons


# ---------------------------------------------------------------------------
# Redundancy
# ---------------------------------------------------------------------------

def test_redundancy_is_numeric_not_textual():
    controller = _refused(_controller(_contract()))
    assert controller.is_redundant(
        "calc.scenario.evaluate",
        {"symbol": "SOLUSDT", "venue": "spot",
         "target_price": "104.8", "horizon": HORIZON})


def test_declared_revision_is_new_work_not_redundant():
    contract = _contract(task="compare the 104.80 target with the 100.00 level")
    controller = _refused(_controller(contract))
    controller = controller.with_scenario_intent({
        "mode": "revision", "target_price": "100.00", "horizon": HORIZON,
        "reason": "the comparison prompt names the second level explicitly",
        "basis_path": "deterministic_state.task",
    })
    args, _ = controller.carry_scenario(
        "calc.scenario.evaluate", {"symbol": "SOLUSDT", "venue": "spot"})
    assert args["target_price"] == canonical_number("100.00")
    assert not controller.is_redundant("calc.scenario.evaluate", args)


# ---------------------------------------------------------------------------
# Validation oracle
# ---------------------------------------------------------------------------

def test_validation_passes_for_bound_evaluation():
    contract = _contract()
    controller = _evaluated(_controller(contract))
    assert check_scenario_equality(_judged(), _state(controller), _ctx(contract)) == []


def test_validation_passes_numeric_equivalent_formatting():
    contract = _contract()
    controller = _evaluated(_controller(contract), target="104.8")
    assert check_scenario_equality(_judged(), _state(controller), _ctx(contract)) == []


def test_validation_blocks_different_evaluated_target():
    contract = _contract()
    controller = _evaluated(_controller(contract), target="100.00",
                            exceedance="1")
    missing = check_scenario_equality(
        _judged(target="100.00", verdict="reachable"),
        _state(controller), _ctx(contract))
    assert any("!= bound target" in m for m in missing), missing


def test_validation_any_match_scan_is_gone():
    """Evaluate the asked target, then a different one: latest wins."""
    contract = _contract()
    controller = _controller(contract)
    controller = _evaluated(controller, target=TARGET, exceedance="0")
    controller = _evaluated(controller, target="100.00", exceedance="1")
    missing = check_scenario_equality(
        _judged(target="100.00", verdict="reachable"),
        _state(controller), _ctx(contract))
    assert any("!= bound target" in m for m in missing), missing


def test_validation_blocks_refusal_of_wrong_target():
    contract = _contract()
    controller = _refused(_controller(contract), target="100.00")
    missing = check_scenario_equality(
        {"scenario": {"verdict": "unevaluable"}},
        _state(controller), _ctx(contract))
    assert any("refused" in m and "bound target" in m for m in missing), missing


def test_validation_allows_refusal_of_bound_target():
    contract = _contract()
    controller = _refused(_controller(contract))
    assert check_scenario_equality(
        {"scenario": {"verdict": "unevaluable"}},
        _state(controller), _ctx(contract)) == []


def test_validation_blocks_horizon_mismatch():
    contract = _contract()
    controller = _evaluated(_controller(contract), horizon="4h")
    missing = check_scenario_equality(_judged(), _state(controller), _ctx(contract))
    assert any("bound target" in m for m in missing), missing


def test_validation_final_block_must_echo_bound_target():
    contract = _contract()
    controller = _evaluated(_controller(contract))
    missing = check_scenario_equality(
        _judged(target="100.00"), _state(controller), _ctx(contract))
    assert any("scenario.target_price" in m for m in missing), missing


def test_declared_revision_requires_divergence_block():
    contract = _contract(task="compare the 104.80 target with the 100.00 level")
    controller = _controller(contract).with_scenario_intent({
        "mode": "revision", "target_price": "100.00", "horizon": HORIZON,
        "reason": "the comparison prompt names the second level explicitly",
        "basis_path": "deterministic_state.task",
    })
    controller = _evaluated(controller, target="100.0", exceedance="0")
    missing = check_scenario_equality(
        _judged(target="100.0"), _state(controller), _ctx(contract))
    assert any("scenario.divergence" in m for m in missing), missing
    passed = check_scenario_equality(
        _judged(target="100.0", divergence={
            "asked_target": TARGET,
            "reason": "comparison prompt named the alternative level",
        }),
        _state(controller), _ctx(contract))
    assert passed == []


def test_legacy_plane_preserved_without_contract_or_outcome():
    # Direct construction (no contract/outcome): old verdict equality path.
    controller = CycleController()
    state = _state(controller, accumulated={
        "calc.scenario.evaluate": _payload(exceedance="0")})
    ctx = SimpleNamespace(scenario={"target_price": TARGET, "horizon": HORIZON})
    assert check_scenario_equality(_judged(), state, ctx) == []


def test_evaluation_root_records_the_bound_dispatch():
    contract = _contract()
    controller = _evaluated(_controller(contract))
    st = _state(controller)
    _record_scenario_evaluation(st)
    evaluation = st.deterministic_state["scenario_facts"]["evaluation"]
    assert evaluation["contract_id"] == contract.contract_id
    assert evaluation["target_price"] == TARGET
    assert evaluation["horizon"] == HORIZON
    assert evaluation["state"] == "evaluated"


# ---------------------------------------------------------------------------
# Prompt surfaces
# ---------------------------------------------------------------------------

def test_opening_contract_block_names_question_and_policy():
    block = render_contract_block(_contract())
    assert TARGET in block and HORIZON in block
    assert "FORBIDDEN" in block
    assert "scenario_intent" in block
    assert "calc.scenario.evaluate" in block


def test_contract_block_names_declared_permission():
    block = render_contract_block(
        _contract(task="compare the 104.80 target with the 100.00 level"))
    assert "DECLARED" in block
    assert "revision" in block


def test_status_block_is_model_visible_and_empty_without_scenario():
    controller = _evaluated(_controller(_contract()))
    block = render_status_block(_contract(), controller)
    assert "SCENARIO STATUS" in block
    assert "evaluated" in block
    assert TARGET in block
    assert render_status_block(None, CycleController()) == ""


def test_envelope_digest_carries_contract():
    from market_service.nooa_harness.engine.kb import envelope_digest
    contract = _contract()
    digest = envelope_digest({
        "scenario_contract": contract.to_dict(),
        "gate": {"status": "validated"},
    })
    assert TARGET in digest
    assert contract.contract_id in digest


def test_followup_prompt_renders_scenario_status():
    from market_service.nooa_harness.engine.kb import compose_followup_prompt
    controller = _evaluated(_controller(_contract()))
    prompt = compose_followup_prompt(
        controller=controller, loop="evidence", sub_loop="acquisition",
        passes_spent=1, pass_budget=4, dispatches_left=2,
        chain=["calc.scenario.evaluate"], accumulated={}, round_results={},
        task_reminder="", scenario_reminder="",
        scenario_contract_block=render_status_block(_contract(), controller),
        phase_guidance={"P5": "do P5"}, next_phase="P5",
    )
    assert "SCENARIO STATUS" in prompt
    assert "evaluated" in prompt


def test_repair_prompt_renders_scenario_steer_and_status():
    from market_service.nooa_harness.engine.kb import compose_repair_prompt
    controller = _refused(_controller(_contract()), target="100.00")
    scenario_steer = controller.scenario_steer()
    prompt = compose_repair_prompt(
        controller=controller, loop="validation", sub_loop="recovery",
        passes_spent=1, pass_budget=2, dispatches_left=1,
        missing=["scenario refused a different target"],
        accumulated={}, task_reminder="", scenario_reminder="",
        scenario_steer=scenario_steer,
        scenario_contract_block=render_status_block(_contract(), controller),
        phase_guidance={"P5": "do P5"}, next_phase="P5",
    )
    assert "SCENARIO REFUSED A DIFFERENT TARGET" in prompt
    assert "SCENARIO STATUS" in prompt


def test_forced_final_prompt_renders_status():
    from market_service.nooa_harness.engine.kb import compose_forced_final_prompt
    controller = _evaluated(_controller(_contract()))
    prompt = compose_forced_final_prompt(
        task="can SOL hit 104.80?", missing=["fix it"],
        evidence_digest="{}",
        scenario={"target_price": TARGET, "horizon": HORIZON},
        scenario_contract_block=render_status_block(_contract(), controller),
    )
    assert "SCENARIO STATUS" in prompt


def test_unevaluable_requires_a_real_refusal_not_silence():
    from market_service.nooa_harness.engine.controller import ScenarioEvalStatus
    from market_service.nooa_harness.engine.core.validation import (
        validate_final_turn,
    )
    passed, missing = validate_final_turn(
        {"summary": "x" * 250, "confidence": "medium",
         "hypothesis": {"H0": "h", "H1": "h1"},
         "evidence": [{"path": "calc.price.delta → route_a_direct.delta_ticks",
                       "value": "0.1", "interpretation": "derived"}],
         "scenario": {"target_price": TARGET, "horizon": HORIZON,
                      "verdict": "unevaluable", "probability": "low",
                      "required_ofi": "1", "exceedance": "0",
                      "fit_status": "validated", "n_windows_usable": 50,
                      "rationale": "x" * 90}},
        {"P1": {"x"}, "P2": {"x"}, "P3": {"x"}, "P4": {"d"},
         "P5": {"calc.price.delta"}, "P6": {"d"}},
        scenario={"target_price": TARGET, "horizon": HORIZON},
        scenario_status=ScenarioEvalStatus.NOT_CALLED,
    )
    assert not passed
    assert any("unevaluable" in m and "refusal" in m for m in missing), missing


def test_opening_loop_binds_contract_and_renders_block():
    """WAKE + COMPREHENSION: the opening loop owns the contract surface."""
    import asyncio
    from tests.test_governance import _engine, _wake
    from market_service.nooa_harness.engine.core.wake import (
        run_comprehension,
        run_wake,
    )

    engine, _store, _postgres, _memory = _engine(llm_responses=[])
    ctx = run_wake(
        engine, _wake(), {"decision": "fire"}, task=None,
        scenario={"target_price": TARGET, "horizon": HORIZON},
    )
    assert ctx.scenario_contract is not None
    assert ctx.scenario_contract.asked_target == TARGET
    # ctx.scenario is the compat projection of the same authority.
    assert ctx.scenario["target_price"] == TARGET

    st, finished = asyncio.run(run_comprehension(engine, ctx))
    assert finished is None
    assert st.scenario_contract.asked_target == TARGET
    assert "SCENARIO CONTRACT" in st.scenario_block
    assert TARGET in st.scenario_block
    assert st.deterministic_state["scenario_facts"]["contract"]["asked_target"] == TARGET
    assert st.controller.scenario_contract is st.scenario_contract


def test_verdict_without_dispatch_is_fabrication():
    contract = _contract()
    controller = _controller(contract)  # NOT_CALLED
    missing = check_scenario_equality(
        _judged(verdict="reachable"), _state(controller), _ctx(contract))
    assert any("never dispatched" in m for m in missing), missing
    missing = check_scenario_equality(
        {"scenario": {"verdict": "unevaluable"}},
        _state(controller), _ctx(contract))
    assert any("never dispatched" in m for m in missing), missing
