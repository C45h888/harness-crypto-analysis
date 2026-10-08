"""Pinned pre-extraction outputs and ordered effects for deterministic cycles."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_governance import _engine, _wake, _staged_narration
from tests.test_engine import (
    _TRACK_ASSEMBLE, _TRACK_DISCIPLINE, _TRACK_HYPOTHESIZE,
    _TRACK_INTERPRET, _track_tools,
)
from market_service.nooa_harness.engine import core

FIXTURE = Path(__file__).with_name("fixtures") / "cycle_characterization.json"


async def capture(case, stage_trace=None):
    # The engine now runs an output composition pass (core/output.py) which
    # makes one more LLM call. The success case needs 6 turns: 5 stage +
    # 1 output composition. Other cases terminate before output or fail.
    # Mirrors tests/test_engine's proven staged walk: evidence turns gather
    # the read-plane substrates, then the three hard-track reasoning
    # positions walk in order, discipline closes, and the last P6 is the
    # non-agentic output composition pass.
    # Repair-path note: the FIRST final (P6 after the track walk) is rejected
    # on the missing discipline citation — the repair turn dispatches
    # calc.discipline.audit in the validation home, then the final passes.
    # The spare trailing P6 is the output-composition pass's scripted turn.
    # The agent-first fetch turn opens every scripted cycle: the agent pulls
    # the gate reads itself (floor fills whatever is absent). All cases below
    # prepend it; the gate case refuses on the fetched thin tape (llm_calls
    # paid once — approved Option-2 cut, zero-LLM doctrine retired).
    fetch_turn = _staged_narration("P1", tools=[{"name": "micro.capture_status"},
                                           {"name": "micro.fit_beta"},
                                           {"name": "market.read"}])
    success_turns = [
        fetch_turn,
        _staged_narration("P1", tools=[{"name": "calc.ofi.intervals"},
                                       {"name": "micro.ofi_intervals"}]),
        _staged_narration("P2", tools=[{"name": "calc.depth.average"},
                                       {"name": "market.derivatives"}]),
        _staged_narration("P5", tools=[{"name": "memory.recall_paper"},
                                       {"name": "calc.price.delta"}]),
        _staged_narration("P6", final=True),
        _track_tools(*_TRACK_ASSEMBLE),
        _track_tools(*_TRACK_INTERPRET),
        _track_tools(*_TRACK_HYPOTHESIZE),
        _staged_narration("P6", final=True),
        _track_tools(*_TRACK_DISCIPLINE),
        _staged_narration("P6", final=True),
        _staged_narration("P6", final=True),
    ]
    if case == "repair":
        # Repair flow: the first final is rejected on the missing discipline
        # citation; the repair turn dispatches the audit in the validation
        # home; the next final passes. Mirrors the proven staged walk — an
        # early P6-final cannot be scripted first (final-ness only binds at
        # validation, and an unwalked track is not repairable mid-recovery).
        turns = [
            fetch_turn,
            _staged_narration("P1", tools=[{"name": "calc.ofi.intervals"},
                                           {"name": "micro.ofi_intervals"}]),
            _staged_narration("P2", tools=[{"name": "calc.depth.average"},
                                           {"name": "market.derivatives"}]),
            _staged_narration("P5", tools=[{"name": "memory.recall_paper"},
                                           {"name": "calc.price.delta"}]),
            _staged_narration("P6", final=True),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _staged_narration("P6", final=True),
            _track_tools(*_TRACK_DISCIPLINE),
            _staged_narration("P6", final=True),
            _staged_narration("P6", final=True),
            _staged_narration("P6", final=True),
        ]
    elif case == "parse":
        # Fetch degrades to full floor on the unparseable turn; narrate#1
        # then fails to parse on the second unparseable turn.
        turns = ["not json", "not json"]
    elif case == "transport":
        turns = []
    elif case == "gate":
        # The agent pulls the reads; the fake store reports thin tape for
        # the gate case and the DATA gate refuses (llm_calls == 1).
        turns = [fetch_turn]
    elif case == "budget":
        turns = [fetch_turn] + [_staged_narration("P6", final=True)] * 12
    else:
        turns = success_turns
    engine, store, postgres, memory = _engine(llm_responses=turns)
    effects = []

    async def execute(store, name, args, **kwargs):
        effects.append(["tool", name, args])
        if name == "micro.capture_status":
            result = {"state": "starting" if case == "gate" else "running", "sequence_gaps": 0}
        elif name == "micro.fit_beta":
            result = {"input_hash": "fixture", "price_impact_fit": {
                "status": "validated", "n_observations": 40, "beta": "0.001",
            }, "coverage": {"events_in_window": 5000}}
        else:
            result = {"value": "fixture"}
        return result, {"capability": name, "scope": {}, "result": "ok"}

    responses = iter(turns)
    async def llm(prompt):
        effects.append(["llm", prompt])
        try:
            return next(responses)
        except StopIteration:
            raise AssertionError("LLM called more times than scripted") from None

    if stage_trace is not None:
        def _wrap_stage(label, original):
            async def _spy(*args, **kwargs):
                stage_trace.append(label)
                result = await original(*args, **kwargs)
                if label == "run_output":
                    ctx = args[1]
                    assert ctx.controller.terminal.value == result[1]["terminal"]
                return result
            def _sync_spy(*args, **kwargs):
                stage_trace.append(label)
                return original(*args, **kwargs)
            if label == "run_wake":
                return _sync_spy
            return _spy
        _stage_wraps = [
            (core.wake, "run_wake"),
            (core.gather, "run_agent_fetch"),
            (core.gather, "run_data_gate"),
            (core.wake, "run_comprehension"),
            (core.gather, "run_plan_bound_acquisition"),
            (core.reasoning, "run_evidence"),
            (core.reasoning, "run_reasoning"),
            (core.reasoning, "run_validation"),
            (core.output, "run_output"),
        ]

    async def persist_pg(artifact):
        effects.append(["postgres", artifact.to_dict()])
        return True

    async def persist_redis(artifact):
        effects.append(["redis", artifact.to_dict()])
        return "fixture-id"

    remember = memory.remember
    async def remember_spy(*args, **kwargs):
        effects.append(["memory", args, kwargs])
        return await remember(*args, **kwargs)

    _extra_patchers = []
    if stage_trace is not None:
        for _mod, _name in _stage_wraps:
            _orig = getattr(_mod, _name)
            _extra_patchers.append(
                patch.object(_mod, _name, side_effect=_wrap_stage(_name, _orig))
            )
    for _p in _extra_patchers:
        _p.start()
    try:
        with patch.object(core.context, "execute_tool", side_effect=execute), patch.object(
        core.context, "_utc_now_iso", return_value="2026-01-01T00:00:00+00:00",
    ), patch("market_service.runtime.contracts.uuid.uuid4", return_value="00000000-0000-0000-0000-000000000001"), patch.object(
        engine, "_call_llm", side_effect=llm,
    ), patch.object(postgres, "insert_inference_artifact", side_effect=persist_pg), patch.object(
        store, "publish_inference_artifact", side_effect=persist_redis,
    ), patch.object(memory, "remember", side_effect=remember_spy):
            artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"}, task="Explain the evidence")
    finally:
        for _p in reversed(_extra_patchers):
            _p.stop()
    return json.loads(json.dumps({"artifact": artifact.to_dict(), "meta": meta, "effects": effects}, default=str))


@pytest.mark.parametrize("case", ["success", "repair", "gate", "parse", "transport", "budget"])
def test_cycle_characterization(case):
    expected = json.loads(FIXTURE.read_text())
    assert asyncio.run(capture(case)) == expected[case]


def test_staged_cycle_converges_and_preserves_placement_order():
    stages = []
    result = asyncio.run(capture("success", stages))
    # run_validation appears TWICE: the first pass rejects the final on the
    # missing discipline citation, the recovery turn dispatches the audit,
    # and validation re-runs to accept the repaired final. That two-phase
    # entry is the repair mechanics the settlement depends on.
    assert stages == ["run_wake", "run_agent_fetch", "run_data_gate", "run_comprehension",
                    "run_plan_bound_acquisition", "run_evidence",
                    "run_reasoning", "run_validation", "run_validation", "run_output"]
    assert result["meta"]["terminal"] == "settled"
    assert result["meta"]["final_validation"]["passed"]
    # Understanding + plan placement (two memory writes, at the
    # comprehension→evidence handoff) plus the end-of-CONTEXT handoff
    # package precede the two-phase settlement side effects: pending
    # projection, memory disposition, final projection, final-trace refresh.
    assert [effect[0] for effect in result["effects"] if effect[0] in ("postgres", "redis", "memory")] == [
        "memory", "memory", "memory", "postgres", "redis", "memory", "redis", "redis",
    ]


def test_gate_refusal_stops_stage_execution():
    stages = []
    result = asyncio.run(capture("gate", stages))
    assert stages == ["run_wake", "run_agent_fetch", "run_data_gate"]
    assert result["meta"]["llm_calls"] == 1
