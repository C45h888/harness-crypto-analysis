"""Reasoning dispatch guard: an undispatched contract scenario cannot close silently.

While a scenario contract exists but the tri-state is still NOT_CALLED,
every reasoning followup carries the engine-issued dispatch directive —
the validation home cannot dispatch it (loop-work boundary), so an
uninstructed close guarantees a fabrication terminal. Sticky until the
dispatch executes (no extra rounds, no budget spent); one capability-log
entry per cycle.
"""

from __future__ import annotations

import asyncio
import json

from tests.test_engine import (
    _staged_narration,
    _track_tools,
    _TRACK_ASSEMBLE,
    _TRACK_DISCIPLINE,
    _TRACK_FORECAST,
    _TRACK_HYPOTHESIZE,
    _TRACK_INTERPRET,
)
from tests.test_governance import _engine, _wake


SCENARIO = {"target_price": "104.80", "horizon": "1h"}


class TestScenarioDispatchDue:
    def test_no_contract_never_due(self):
        from market_service.nooa_harness.engine.controller import CycleController
        assert CycleController().scenario_dispatch_due() is False
        assert CycleController(None).scenario_dispatch_due() is False

    def test_dict_scenario_without_dispatch_is_due(self):
        from market_service.nooa_harness.engine.controller import CycleController
        assert CycleController(dict(SCENARIO)).scenario_dispatch_due() is True

    def test_contract_without_dispatch_is_due(self):
        from market_service.nooa_harness.engine.controller import (
            CycleController,
        )
        from market_service.nooa_harness.engine.scenario_contract import (
            build_contract,
        )
        contract = build_contract(dict(SCENARIO), "Can it hit it?")
        ctrl = CycleController(
            contract.to_scenario_dict(), scenario_contract=contract)
        assert ctrl.scenario_dispatch_due() is True

    def test_dispatched_scenario_not_due(self):
        from market_service.nooa_harness.engine.controller import CycleController
        from market_service.nooa_harness.engine.scenario_contract import (
            build_contract,
        )
        contract = build_contract(dict(SCENARIO), "Can it hit it?")
        ctrl = CycleController(
            contract.to_scenario_dict(), scenario_contract=contract)
        ctrl = ctrl.record_outcome(
            "calc.scenario.evaluate",
            {"capability": "calc.scenario.evaluate",
             "scope": {}, "result": "ok",
             "detail": {"status": "evaluated"}},
            {"exceedance": 0.1},
            args={"target_price": "104.80", "horizon": "1h"},
        )
        assert ctrl.scenario_dispatch_due() is False


def _run_cycle(turns):
    from market_service.nooa_harness.engine import core
    from unittest.mock import patch
    engine, store, postgres, memory = _engine(llm_responses=list(turns))
    prompts: list[str] = []

    async def llm(prompt):
        prompts.append(prompt)
        return turns.pop(0) if turns else _staged_narration("P6", final=True)

    async def execute(store, name, args, **kwargs):
        return {"value": "fixture"}, {"capability": name,
                                           "scope": {}, "result": "ok"}

    async def _run():
        with patch.object(engine, "_call_llm", side_effect=llm), \
                patch.object(core.context, "execute_tool",
                             side_effect=execute):
            return await engine.narrate_cycle(
                _wake(), {"decision": "fire"},
                task="Can SOL hit 104.80 within 1h?",
                scenario=dict(SCENARIO))
    output, meta = asyncio.run(_run())
    return output, meta, prompts


class TestReasoningExitVeto:
    def test_veto_forces_the_dispatch_turn(self):
        p1_read = _staged_narration("P1", tools=[
            {"name": "micro.capture_status", "args": {}},
            {"name": "micro.fit_beta", "args": {}},
            {"name": "market.read", "args": {}}])
        turns = [
            p1_read,
            _staged_narration("P2", tools=[
                {"name": "calc.depth.average", "args": {}}]),
            _staged_narration("P5", tools=[
                {"name": "calc.price.delta", "args": {}}]),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_FORECAST),
            _track_tools(*_TRACK_HYPOTHESIZE),
            # Veto round: the scripted agent still does not dispatch —
            # the veto fires, the cycle continues, validation decides.
            _staged_narration("P5"),
            _staged_narration("P6", final=True),
            _track_tools(*_TRACK_DISCIPLINE),
            _staged_narration("P6", final=True),
            _staged_narration("P6", final=True),
            _staged_narration("P6", final=True),
            _staged_narration("P6", final=True),
        ]
        output, meta, prompts = _run_cycle(turns)
        veto_prompts = [p for p in prompts if "STRUCTURAL DIRECTIVE" in p]
        assert len(veto_prompts) >= 1
        assert all("104.80" in p and "1h" in p for p in veto_prompts)
        assert all("calc.scenario.evaluate" in p for p in veto_prompts)
        # The veto does not bypass the gate: without a dispatch the honest
        # terminal still fires downstream.
        assert meta["terminal"] == "validation_failed"

    def test_no_veto_without_scenario(self):
        from market_service.nooa_harness.engine import core
        from unittest.mock import patch
        engine, store, postgres, memory = _engine(llm_responses=[
            _staged_narration("P6", final=True),
        ])
        prompts: list[str] = []

        async def llm(prompt):
            prompts.append(prompt)
            return _staged_narration("P6", final=True)

        async def _run():
            with patch.object(engine, "_call_llm", side_effect=llm):
                return await engine.narrate_cycle(
                    _wake(), {"decision": "fire"}, task="Explain the evidence")
        asyncio.run(_run())
        assert not any("STRUCTURAL DIRECTIVE" in p for p in prompts)
