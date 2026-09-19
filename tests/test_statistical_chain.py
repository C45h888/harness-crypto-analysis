"""Steady-track chain tests — core rail, no LLM, no I/O.

Covers chain_completion tri-state semantics (EVALUATED satisfies,
REFUSED is a finding that never blocks, NOT_CALLED-required steers)
and the chain_trace receipt in the assembled ForecastResult.
"""

from market_service.nooa_harness.engine.controller import CycleController
from market_service.nooa_harness.engine.core.chain import (
    CHAIN_LINKS,
    chain_completion,
    chain_steer,
)


def _ok(payload=None):
    return {"result": "ok", "detail": {}}, ({"v": 1} if payload is None else payload)


def _refused(reason):
    return {"result": "ok", "detail": {"status": "refused", "reason": reason}}, None


def test_empty_cycle_requires_forecast_and_discipline_only():
    out = chain_completion(CycleController(), task="general check", scenario=None)
    assert out["missing"] == ["calc.forward.forecast", "calc.discipline.audit"]
    assert out["refused"] == []
    assert out["complete"] is False
    assert len(out["links"]) == len(CHAIN_LINKS) == 5
    assert [lnk["step"] for lnk in out["links"]] == [
        "run_forecast", "run_scenario", "run_hypothesis", "run_decay",
        "check_validation",
    ]


def test_refused_required_link_is_finding_not_missing():
    c = CycleController()
    log, payload = _refused("no events in ledger")
    c = c.record_outcome("calc.forward.forecast", log, payload)
    out = chain_completion(c, task="general check", scenario=None)
    assert "calc.forward.forecast" not in out["missing"]
    assert out["refused"] == [
        {"tool": "calc.forward.forecast", "reason": "no events in ledger"}]
    # The chain continues past the refusal; only discipline still steers.
    assert out["missing"] == ["calc.discipline.audit"]


def test_evaluated_forecast_pulls_decay_in():
    c = CycleController()
    log, payload = _ok({"validation_state": "validated"})
    c = c.record_outcome("calc.forward.forecast", log, payload)
    out = chain_completion(c, task="general check", scenario=None)
    assert "calc.decay.report" in out["missing"]
    assert "calc.forward.scenario" not in out["missing"]
    assert "calc.hypothesis.test" not in out["missing"]


def test_horizon_language_requires_scenario_and_hypothesis():
    out = chain_completion(
        CycleController(),
        task="will price hit the target at the 1h horizon?",
        scenario=None,
    )
    assert "calc.forward.scenario" in out["missing"]
    assert "calc.hypothesis.test" not in out["missing"]


def test_hypothesis_language_requires_test():
    out = chain_completion(
        CycleController(), task="test H0 that OFI has no effect", scenario=None)
    assert "calc.hypothesis.test" in out["missing"]
    assert "calc.forward.scenario" not in out["missing"]


def test_complete_track():
    c = CycleController()
    for tool in ("calc.forward.forecast", "calc.forward.scenario",
                 "calc.hypothesis.test", "calc.decay.report",
                 "calc.discipline.audit"):
        log, payload = _ok()
        c = c.record_outcome(tool, log, payload)
    out = chain_completion(
        c, task="target at 1h horizon, test H0", scenario={"target_price": "1"})
    assert out["complete"] is True
    assert out["missing"] == []
    assert chain_steer(out) == ""


def test_steer_names_missing_and_cites_refused():
    c = CycleController()
    log, payload = _refused("no_market_price")
    c = c.record_outcome("calc.forward.scenario", log, payload)
    out = chain_completion(c, task="target check", scenario={"target_price": "1"})
    steer = chain_steer(out)
    assert "calc.forward.forecast" in steer  # missing → call steer
    assert "Do NOT\nre-call" not in steer
    assert "Do NOT re-call it" in steer  # refused → citation steer
    assert "calc.forward.scenario → refusal" in steer


def test_forecast_result_carries_chain_trace():
    import market_service.microstructure as fm

    result = fm.assemble_forecast_result(
        symbol="BTCUSDT", venue="spot", generated_at_ms=1)
    trace = result.to_dict()["diagnostics"]["chain_trace"]
    assert [s["step"] for s in trace] == [
        "run_a", "run_b", "run_multivariate", "compare"]
    assert trace[-1]["status"] == "compared"


def test_chain_bound_to_loop_vocabulary():
    from market_service.nooa_harness.engine.loop_states import (
        CHAIN_SUBLOOP,
        SUBLOOP_SPECS,
        SubLoop,
    )

    # Every enforced link executes in exactly one (sub-loop, step).
    assert CHAIN_SUBLOOP["calc.forward.forecast"][0] is SubLoop.ANALYSIS
    assert CHAIN_SUBLOOP["calc.decay.report"][0] is SubLoop.ANALYSIS
    assert CHAIN_SUBLOOP["calc.discipline.audit"][0] is SubLoop.GATE
    assert CHAIN_SUBLOOP["output.compose"][0] is SubLoop.COMPOSITION
    for tool, (sub_loop, step) in CHAIN_SUBLOOP.items():
        assert step in SUBLOOP_SPECS[sub_loop].steps, tool
    # ANALYSIS cannot exit with the track unwalked (text gate).
    assert "statistical chain" in SUBLOOP_SPECS[SubLoop.ANALYSIS].exit_condition


def test_forecast_reachable_from_both_track_positions():
    from market_service.nooa_harness.inference.tooling.registry import tool_homes

    homes = tool_homes("calc.forward.forecast")
    assert ("evidence", "acquisition") in homes
    assert ("reasoning", "analysis") in homes


def test_assembly_substrates_reachable_in_reasoning():
    from market_service.nooa_harness.inference.tooling.registry import tool_homes

    for tool in ("calc.ofi.intervals", "calc.depth.average",
                 "calc.observation.build", "calc.fit.price_impact",
                 "calc.fit.depth_scaling"):
        assert ("reasoning", "analysis") in tool_homes(tool), tool


def _ok_outcome(controller, tool, payload=None):
    log, result = _ok(payload)
    return controller.record_outcome(tool, log, result)


def _refused_outcome(controller, tool, reason):
    log, result = _refused(reason)
    return controller.record_outcome(tool, log, result)


def _denied_outcome(controller, tool):
    return controller.record_outcome(
        tool, {"result": "denied", "detail": "later_track_position"}, None)


def test_assemble_position_predicate():
    from market_service.nooa_harness.engine.core.chain import position_status

    c = CycleController()
    assert position_status(c.outcomes, "assemble")["complete"] is False
    for tool in ("calc.ofi.intervals", "calc.depth.average",
                 "calc.forward.join"):
        c = _ok_outcome(c, tool)
    status = position_status(c.outcomes, "assemble")
    assert status["complete"] is True
    assert status["missing"] == []


def test_denied_outcomes_never_satisfy_positions():
    from market_service.nooa_harness.engine.core.chain import position_status

    c = CycleController()
    for tool in ("calc.ofi.intervals", "calc.depth.average",
                 "calc.forward.join"):
        c = _denied_outcome(c, tool)
    status = position_status(c.outcomes, "assemble")
    assert status["complete"] is False
    assert status["refused"] == []  # denials are neither findings nor halts
    assert len(status["missing"]) == 3


def test_refused_assembly_link_halts():
    from market_service.nooa_harness.engine.core.chain import chain_halt_for

    c = CycleController()
    c = _ok_outcome(c, "calc.ofi.intervals")
    c = _refused_outcome(c, "calc.depth.average", "no depth blocks")
    halt = chain_halt_for(c.outcomes, "assemble")
    assert halt == {"position": "assemble", "tool": "calc.depth.average",
                     "reason": "no depth blocks"}
    assert chain_halt_for(c.outcomes, "interpret") is None


def test_interpret_and_hypothesize_positions():
    from market_service.nooa_harness.engine.core.chain import position_status

    c = CycleController()
    for tool in ("calc.forward.fit", "calc.forward.distribution",
                 "calc.decay.report"):
        c = _ok_outcome(c, tool)
    assert position_status(c.outcomes, "interpret")["complete"] is True
    c = _ok_outcome(c, "calc.hypothesis.test")
    hypo = position_status(c.outcomes, "hypothesize",
                            task="test H0", scenario=None)
    assert hypo["complete"] is True
    # Scenario link joins the hypothesize position only with horizons.
    hypo_plain = position_status(c.outcomes, "hypothesize",
                                  task="general check", scenario=None)
    assert hypo_plain["complete"] is True


def test_tool_position_ordering():
    from market_service.nooa_harness.engine.core.chain import (
        POSITION_ORDER,
        REASONING_MIN_POSITIONS,
        TOOL_POSITION,
    )

    assert POSITION_ORDER == ("assemble", "interpret", "hypothesize")
    assert REASONING_MIN_POSITIONS == 3
    assert TOOL_POSITION["calc.ofi.intervals"] == "assemble"
    assert TOOL_POSITION["calc.forward.distribution"] == "interpret"
    assert TOOL_POSITION["calc.hypothesis.test"] == "hypothesize"
    assert TOOL_POSITION["calc.forward.scenario"] == "hypothesize"


def test_budgets_fit_the_hard_floor():
    from market_service.nooa_harness.engine.config import (
        AGENTIC_MAX_LLM_TURNS,
        AGENTIC_MAX_TOOL_ROUNDS,
        LOOP_PASS_BUDGET,
    )
    from market_service.nooa_harness.engine.core.chain import (
        REASONING_MIN_POSITIONS,
    )

    assert LOOP_PASS_BUDGET["reasoning"] >= REASONING_MIN_POSITIONS + 2
    assert AGENTIC_MAX_LLM_TURNS >= sum(LOOP_PASS_BUDGET.values()) + 1
    assert AGENTIC_MAX_TOOL_ROUNDS >= REASONING_MIN_POSITIONS + 6


def _refusal_log_for(tool, reason="thin tape"):
    from market_service.nooa_harness.inference import capability_log_entry
    return capability_log_entry(
        tool, {}, "ok", detail={"status": "refused", "reason": reason})


def test_suppression_is_args_aware():
    from market_service.nooa_harness.engine.controller import CycleController

    c = CycleController()
    c = c.record_outcome(
        "calc.ofi.intervals",
        _refusal_log_for("calc.ofi.intervals", "tape gap"), None,
        args={"symbol": "BTCUSDT", "venue": "spot"})
    # Identical inputs: still suppressed (legacy shape preserved).
    assert c.is_redundant(
        "calc.ofi.intervals", {"symbol": "BTCUSDT", "venue": "spot"}) is True
    assert c.is_redundant("calc.ofi.intervals") is False  # no-args ≠ args
    # Reformulated inputs: new work, suppression lifts.
    assert c.is_redundant(
        "calc.ofi.intervals",
        {"symbol": "BTCUSDT", "venue": "spot", "window_minutes": 60}) is False
    # A different tool is never suppressed by this refusal.
    assert c.is_redundant("calc.depth.average", {"symbol": "BTCUSDT"}) is False


def test_no_args_refusal_suppresses_no_args_recall():
    from market_service.nooa_harness.engine.controller import CycleController

    c = CycleController()
    c = c.record_outcome(
        "calc.scenario.evaluate", _refusal_log_for("calc.scenario.evaluate"), None)
    assert c.is_redundant("calc.scenario.evaluate") is True
    assert c.is_redundant(
        "calc.scenario.evaluate", {"target_price": "1"}) is False


def test_halt_steer_names_reformulation():
    from market_service.nooa_harness.engine.core.chain import halt_steer

    text = halt_steer({"position": "assemble", "tool": "calc.ofi.intervals",
                       "reason": "tape gap"})
    assert "calc.ofi.intervals" in text
    assert "different args" in text
    assert "tool_calls=[]" in text
