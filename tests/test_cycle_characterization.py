"""Pinned pre-extraction outputs and ordered effects for deterministic cycles."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_governance import _engine, _wake, _staged_narration
from tests.test_engine import (
    _collapse_turn,
    _TRACK_ASSEMBLE, _TRACK_DISCIPLINE, _TRACK_HYPOTHESIZE,
    _TRACK_FORECAST,
    _TRACK_INTERPRET, _track_tools,
)
from market_service.nooa_harness.engine import core

FIXTURE = Path(__file__).with_name("fixtures") / "cycle_characterization.json"

#: The canonical answer-turn response pinned by the fixture: the agent
#: speaks finally FROM the gated foundation (reword, never re-decide).
#: Cases that ship an authored output script it as their last turn; the
#: fallback path is pinned separately by the output-authoring suite.
_ANSWER_PIN = json.dumps({
    "phase": "P6",
    "summary": ("OFI tape stays one-sided into the close and depth absorbs "
                "the aggression, so the distribution keeps pointing up — "
                "held as the gate's diagnostic reading, never a shortcut."),
    "scenario_rationale": "no scenario was bound this cycle",
    "model_separation": "nu*OFI stays heteroskedastic; never merged.",
    "limitations": ["synthetic window"],
    "confidence": "medium",
    "tool_calls": [],
})


async def capture(case, stage_trace=None):
    # Phase R7 (fetch semantic removed): narrate#1 IS the P1 evidence read
    # (the agent pulls the read-plane substrates directly). The staged walk
    # mirrors tests/test_engine's proven turns: P1 read + P2 + P5 follow-ups
    # at the evidence budget, the three hard-track reasoning positions,
    # discipline close, and the output-composition pass. The success case
    # needs 11 turns (3 evidence incl. narrate#1 + 3 track + 3 finals +
    # 1 discipline + 1 composition); other cases terminate early or fail.
    # Repair-path note: the FIRST final (P6 after the track walk) is
    # rejected on the missing discipline citation — the repair turn
    # dispatches calc.discipline.audit in the validation home, then the
    # final passes; the spare trailing P6 is the composition pass.
    p1_read = _staged_narration("P1", tools=[{"name": "micro.capture_status"},
                                            {"name": "micro.fit_beta"},
                                            {"name": "market.read"}])
    success_turns = [
        p1_read,
        _staged_narration("P2", tools=[{"name": "calc.depth.average"},
                                       {"name": "market.derivatives"}]),
        _staged_narration("P5", tools=[{"name": "calc.price.delta"}]),
        _track_tools(*_TRACK_ASSEMBLE),
        _track_tools(*_TRACK_INTERPRET),
        _track_tools(*_TRACK_FORECAST),
        _track_tools(*_TRACK_HYPOTHESIZE),
        _staged_narration("P6", final=True),
        _track_tools(*_TRACK_DISCIPLINE),
        _staged_narration("P6", final=True),
        _staged_narration("P6", final=True),
    ]
    if case == "repair":
        # Repair flow: the first final is rejected on the missing discipline
        # citation; the repair turn dispatches the audit in the validation
        # home; the next final passes.
        turns = success_turns + [_staged_narration("P6", final=True)]
    elif case == "authored":
        # Clean authored ship (the canonical output of record): a valid
        # COLLAPSE object turn, then the derive turn — the answer renders
        # from what ships and the ledger pins it.
        turns = success_turns[:10] + [
            _collapse_turn(), _staged_narration("P6", final=True)]
    elif case == "parse":
        # Narrate#1 fails to parse on the first turn.
        turns = ["not json"]
    elif case == "transport":
        turns = []
    elif case == "gate":
        # Phase R7: there is no DATA gate — the cycle runs comprehension ->
        # plan-bound acquisition and dies at narrate#1 (llm_calls == 1).
        turns = [p1_read]
    elif case == "budget":
        turns = [_staged_narration("P6", final=True)] * 12
    else:
        turns = success_turns
    if case in ("success", "repair", "authored"):
        # Canonical completion: the answer turn speaks finally from the
        # gated foundation (unused script is harmless — llm() pops on call).
        # Success walks one turn deeper before output (validation recovery
        # consumes the spare final), so it scripts its own derive spare.
        turns = list(turns)
        if case == "success":
            turns.append(_staged_narration("P6", final=True))
        turns.append(_ANSWER_PIN)
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
        elif name == "calc.ofi.intervals":
            result = [{"start_ts_ms": 1, "end_ts_ms": 2, "ofi": "12.5",
                       "event_count": 10, "quality": "exact_feed"}]
        elif name == "calc.depth.average":
            result = {"window_minutes": 30, "n_intervals": 4,
                      "ad_per_interval": ["10", "11"], "mean_ad": "10.5",
                      "depth_estimator": "test"}
        elif name == "calc.forward.join":
            result = {"n_pairs": 20, "exclusion_log": {}, "sample": []}
        elif name == "calc.forward.fit":
            result = {"status": "validated", "r2": "0.31", "train_n": 100,
                      "horizon_ms": 5000}
        elif name == "calc.forward.distribution":
            result = {"horizon_ms": 5000, "expected_delta": "0.5",
                      "pi_95_lo": "-1.0", "pi_95_hi": "2.0",
                      "status": "validated"}
        elif name == "calc.decay.report":
            result = {"finalized_horizons": [5000]}
        elif name == "calc.forward.scenario":
            result = {"horizon_ms": 5000, "targets": [], "fit_id": "fx"}
        elif name == "calc.hypothesis.test":
            result = {"hypothesis_id": "H-track", "p_value": "0.03",
                      "status": "ok"}
        elif name == "calc.discipline.audit":
            result = {"go": True, "memo": "fixture"}
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
            (core.wake, "run_comprehension"),
            (core.gather, "run_plan_bound_acquisition"),
            (core.reasoning, "run_evidence"),
            (core.reasoning, "run_reasoning"),
            (core.validation, "run_validation"),
            (core.output, "run_output"),
        ]

    async def place_pg(output):
        effects.append(["postgres", output.to_dict()])
        return True

    async def update_pg(output):
        effects.append(["postgres", output.to_dict()])
        return True

    async def publish_redis(output):
        effects.append(["redis", output.to_dict()])
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
    ), patch.object(postgres, "insert_canonical_output", side_effect=place_pg), patch.object(
        postgres, "update_canonical_output", side_effect=update_pg,
    ), patch.object(store, "publish_canonical_output", side_effect=publish_redis,
    ), patch.object(memory, "remember", side_effect=remember_spy):
            output, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"}, task="Explain the evidence")
    finally:
        for _p in reversed(_extra_patchers):
            _p.stop()
    return json.loads(json.dumps({"artifact": output.to_dict(), "meta": meta, "effects": effects}, default=str))


@pytest.mark.parametrize("case", ["success", "repair", "authored", "gate", "parse", "transport", "budget"])
def test_cycle_characterization(case):
    expected = json.loads(FIXTURE.read_text())
    assert asyncio.run(capture(case)) == expected[case]


def test_staged_cycle_converges_and_preserves_placement_order():
    stages = []
    result = asyncio.run(capture("success", stages))
    # Phase R7 stage table: run_validation appears TWICE — the first pass
    # rejects the final on the missing discipline citation, the recovery
    # turn dispatches the audit, and validation re-runs to accept the
    # repaired final. The fetch/data-gate stages no longer exist.
    assert stages == ["run_wake", "run_comprehension",
                    "run_plan_bound_acquisition", "run_evidence",
                    "run_reasoning", "run_validation", "run_validation", "run_output"]
    assert result["meta"]["terminal"] == "settled"
    assert result["meta"]["final_validation"]["passed"]
    # P1 central ledger + retirement: engine memory is deterministic state (NOT
    # MemoryNode rows) AND the engine no longer disposes the agent's memory.
    # This scripted walk never calls memory.remember, so NO memory write occurs
    # — the agent's memory is written only through the memory.* tools.
    # Canonical-output placement side effects (BY DESIGN under the canonical
    # path): durable insert + live projection, then the post-SETTLE row
    # refresh + its projection. The run record is bookkeeping; the answer is
    # the deliverable.
    assert [effect[0] for effect in result["effects"] if effect[0] in ("postgres", "redis", "memory")] == [
        "postgres", "redis", "postgres", "redis",
    ]


def test_gate_refusal_stops_stage_execution():
    stages = []
    result = asyncio.run(capture("gate", stages))
    # Phase R7: no DATA gate — the cycle runs comprehension and dies at
    # narrate#1 (the scripted turn is consumed there). The stage table
    # stops at evidence (narrate#1 lives inside run_evidence).
    assert stages == ["run_wake", "run_comprehension",
                    "run_plan_bound_acquisition", "run_evidence"]
    assert result["meta"]["llm_calls"] == 1
