"""P0 alignment invariants for the canonical agentic runtime.

These tests intentionally target authority and mutation boundaries rather than
legacy call counts.  The old engine characterization suite contains assertions
for the removed stage runner and pre-refactor persistence shape.
"""

from __future__ import annotations

import pytest

from market_service.nooa_harness.engine.controller import (
    CycleController,
    ScenarioEvalStatus,
)
from market_service.nooa_harness.engine.fsm import (
    GOVERNANCE_MEMBRANE,
    GovernanceEvent,
    GovernanceEventKind,
)
from market_service.nooa_harness.engine.kb import build_loop_state_block
from market_service.nooa_harness.engine.loop_states import (
    NestedLoop,
    SubLoop,
)
from market_service.nooa_harness.inference.dispatch import tool_home, tool_homes


def _controller() -> CycleController:
    return (
        CycleController()
        .with_membrane(GOVERNANCE_MEMBRANE)
        .with_observation(GOVERNANCE_MEMBRANE.initial())
    )


def test_denied_transition_is_side_effect_free_and_traced() -> None:
    controller = _controller()
    successor, verdict = controller.transition(
        GovernanceEvent(
            GovernanceEventKind.OPEN_SUBLOOP,
            SubLoop.ACQUISITION,
        )
    )

    assert not verdict.allowed
    assert successor.observation == controller.observation
    assert successor.terminal is None
    assert successor.transitions[-1].allowed is False
    assert successor.transitions[-1].after_sub_loop is None


def test_fsm_authorizes_bootstrap_only_at_initial_comprehension() -> None:
    controller = _controller()

    assert controller.authorize_work(
        nested_loop=NestedLoop.COMPREHENSION, bootstrap=True,
    ).allowed
    assert not controller.authorize_work(
        nested_loop=NestedLoop.EVIDENCE, bootstrap=True,
    ).allowed

    opened, verdict = controller.transition(
        GovernanceEvent(
            GovernanceEventKind.OPEN_SUBLOOP,
            SubLoop.INTAKE,
        )
    )
    assert verdict.allowed
    assert opened.observation.sub_loop is SubLoop.INTAKE
    assert not opened.authorize_work(
        nested_loop=NestedLoop.EVIDENCE,
        sub_loop=SubLoop.ACQUISITION,
    ).allowed


def test_controller_classifies_refusal_without_coverage_credit() -> None:
    controller = _controller()
    log = {
        "capability": "calc.scenario.evaluate",
        "result": "ok",
        "detail": {"status": "refused", "reason": "thin tape"},
    }
    successor = controller.record_outcome(
        "calc.scenario.evaluate", log, None,
    )

    assert successor.scenario_state() is ScenarioEvalStatus.REFUSED
    assert not successor.phase_coverage["P5"]
    assert successor.scenario_refusal_reason() == "thin tape"
    assert successor.is_redundant("calc.scenario.evaluate")


def test_prompt_state_cannot_override_controller_observation() -> None:
    controller = _controller()
    opened, _ = controller.transition(
        GovernanceEvent(GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE)
    )

    with pytest.raises(AssertionError):
        build_loop_state_block(
            controller=opened,
            loop="evidence",
            sub_loop="acquisition",
            intent="infer_order_flow",
        )

    block = build_loop_state_block(controller=opened)
    assert "inside comprehension / intake" in block
    assert "[understand_task]" in block


def test_tool_home_registry_declares_multi_loop_work_explicitly() -> None:
    assert tool_home("calc.price.delta") == ("reasoning", "analysis")
    assert tool_homes("calc.price.delta") == (
        ("evidence", "acquisition"),
        ("reasoning", "analysis"),
    )
    assert tool_homes("calc.hypothesis.test") == (("reasoning", "analysis"),)
    assert tool_homes("calc.discipline.audit") == (("validation", "gate"),)
