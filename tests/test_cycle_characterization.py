"""Pinned pre-extraction outputs and ordered effects for deterministic cycles."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_governance import _engine, _wake, _staged_narration
from market_service.nooa_harness.engine import core

FIXTURE = Path(__file__).with_name("fixtures") / "cycle_characterization.json"


async def capture(case, stage_trace=None):
    turns = [
        _staged_narration("P1", tools=[{"name": "calc.ofi.intervals"}]),
        _staged_narration("P2", tools=[{"name": "calc.depth.average"}]),
        _staged_narration("P3", tools=[{"name": "market.derivatives"}]),
        _staged_narration("P5", tools=[{"name": "memory.recall_paper"}, {"name": "calc.price.delta"}]),
        _staged_narration("P6", final=True),
    ]
    if case == "repair":
        turns.insert(0, _staged_narration("P6", final=True))
    elif case == "parse":
        turns = ["not json"]
    elif case in ("transport", "gate"):
        turns = []
    elif case == "budget":
        turns = [_staged_narration("P6", final=True)] * 12
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
        wake_stage = engine.run_wake
        def wake_spy(*args, **kwargs):
            stage_trace.append("run_wake")
            return wake_stage(*args, **kwargs)
        engine.run_wake = wake_spy
        def _wrap_stage(label, original):
            async def _spy(*args, **kwargs):
                stage_trace.append(label)
                result = await original(*args, **kwargs)
                if label == "run_output":
                    ctx = args[1]
                    assert ctx.controller.terminal.value == result[1]["terminal"]
                return result
            return _spy
        _stage_wraps = [
            (core.gather, "run_gather"),
            (core.reasoning, "run_comprehension"),
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
    assert stages == ["run_wake", "run_gather", "run_comprehension", "run_evidence",
                    "run_reasoning", "run_validation", "run_output"]
    assert result["meta"]["terminal"] == "settled"
    assert result["meta"]["final_validation"]["passed"]
    # Pending projection, memory disposition, final projection, and the
    # final-trace refresh are explicit side effects of two-phase settlement.
    assert [effect[0] for effect in result["effects"] if effect[0] in ("postgres", "redis", "memory")] == [
        "postgres", "redis", "memory", "redis", "redis",
    ]


def test_gate_refusal_stops_stage_execution():
    stages = []
    result = asyncio.run(capture("gate", stages))
    assert stages == ["run_wake", "run_gather"]
    assert result["meta"]["llm_calls"] == 0
