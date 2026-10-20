"""REASON stage — EVIDENCE + REASONING loop bodies over the shared carrier.

Evidence gathers (narrate#1 + budgeted follow-ups); reasoning tests and
compares. VALIDATION now lives in engine.core.validation (semantic
separation by loop state); its public names are re-exported here for
backward compatibility. COMPREHENSION / WAKE lives in engine.core.wake
(canonical owner) and is likewise re-exported. Per-pass mechanics live
next to their owning modules:

  CycleRuntimeState, _open_once, _mark_declared, freeze_chain,
  run_runtime_failure
        → engine.core.context
  _dispatch_turn, _followup_turn, _name_pending
        → InferenceEngine (engine.core.driver)
  run_wake, run_comprehension (+ WAKE completion predicates)
        → engine.core.wake
  seed_evidence_plan, default_comprehension, prompt composers
        → engine.kb

This module owns the EVIDENCE and REASONING run_* loop bodies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import context
from ..schemas import coerce_turn, extract_json_object
from ..config import (
    AGENTIC_MAX_LLM_TURNS,
    LOOP_PASS_BUDGET,
    MAX_DISPATCHES_PER_PASS,
)
from .context import (
    CycleRuntimeState,
    _open_once,
    _mark_declared,
    freeze_chain,
    run_runtime_failure,
)
from ..llm import NarrationStalledError, narration_failure_kind
from ..fsm import GovernanceEventKind
from .. import kb
from .. import gates
from ..ledger import LedgerKind, ledger_rows, record_entry, render_ledger
from .chain import (
    POSITION_ORDER,
    base_summary as _base_summary,
    chain_halt_for,
    position_status,
)


async def _store_stage_observation(
    engine: Any, ctx: Any, st: CycleRuntimeState,
    stage: str, payload: dict[str, Any],
) -> None:
    """Record an inter-stage reading on the CENTRAL ENGINE LEDGER.

    2a close records the base reading; 2b close records the refined reading.
    The ledger is deterministic engine state (never the episodic memory node),
    and the LLM reads it as context. Pure state mutation — cannot fail the
    cycle.
    """
    segment = getattr(ctx, "task_id", None)
    wake_id = getattr(getattr(ctx, "wake", None), "wake_id", "") or ""
    entry = record_entry(
        st.deterministic_state, LedgerKind.OBSERVATION,
        {"stage": stage, **payload},
        evidence_refs=(wake_id,),
        segment=segment,
    )
    st.ledger_stage_ids.append(f"{stage}#{entry.seq}")
    st.capability_log.append({
        "capability": "engine.ledger.stage_record",
        "scope": {"symbol": engine.symbol, "venue": engine.venue},
        "result": "ok",
        "detail": {"stage": stage, "seq": entry.seq, "segment": segment},
    })


from ..loop_states import LoopStep, NestedLoop, SubLoop, TaskIntent

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Re-exports for backward compatibility with call sites that historically
# imported the carrier / FSM helpers / terminal emitters from this module.
# The EVIDENCE / REASONING run_* loop bodies are the public surface tested
# by the cycle characterization suite; run_comprehension is re-exported from
# its canonical owner (core.wake) and the VALIDATION loop from
# core.validation; everything else is an internal carrier helper that lives
# in context now.
# --------------------------------------------------------------------------
__all__ = [
    "CycleRuntimeState",
    "run_evidence",
    "run_reasoning",
]


from .wake import run_comprehension  # noqa: F401  # canonical owner: core.wake


async def run_evidence(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple | None:
    """EVIDENCE loop: narrate#1 + budgeted follow-ups. Ends early on empty
    calls or a failed round (candidate stands; validation decides)."""
    st.loop_tag = "evidence"
    # CONTEXT is a single merged loop: SOURCING follows FRAMING with no
    # ENTER_LOOP (same loop). The FSM cursor is still on FRAMING.
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.SOURCING
    )
    st.opened_subs.add(SubLoop.SOURCING.value)
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ACQUISITION
    )
    st.opened_subs.add(SubLoop.ACQUISITION.value)
    # Adopt the acquisition intent for the merged loop's evidence work (legal:
    # INFER_ORDER_FLOW is served by CONTEXT). This keeps the live observation
    # and the prompt label in agreement.
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.SET_TASK, TaskIntent.INFER_ORDER_FLOW
    )
    # First-turn prompt: composed once by kb.compose_narrate1_prompt.
    user_prompt_1 = kb.compose_narrate1_prompt(
        controller=st.controller,
        loop="context", sub_loop="acquisition",
        intent=TaskIntent.INFER_ORDER_FLOW.value,
        passes_spent=0, pass_budget=LOOP_PASS_BUDGET["evidence"],
        dispatches_left=MAX_DISPATCHES_PER_PASS,
        seeds=list(st.seed_plan["seeds"]),
        task_block=st.task_block,
        scenario_block=st.scenario_block,
        wake_block=st.wake_block,
        deterministic_state=st.deterministic_state,
        prior_note=st.prior_note,
        ledger_block=st.ledger_block,
        output_format=engine._output_format(),
        workflow_block=st.workflow_block,
        traversal=st.controller.loop_coverage(),
        gate=(gates.gate_facts(st.deterministic_state, gates.GateKind.DATA)
              or st.deterministic_state.get("gate")),
    )
    raw_1: str | None = None
    first_error = ""
    terminal_kind = "narration_failed"
    try:
        raw_1 = await engine._call_llm(user_prompt_1)
        context.record_tier(st)
    except Exception as exc:
        log.exception("narration#1 failed")
        first_error = f"{type(exc).__name__}: {exc}"
        # Bounded RETRY for TRANSIENT transport stalls: timeout-class plus
        # Signal B stall verdicts (cause-carrying kinds). Live 2026-10-19:
        # narration#1 stalled past the bound twice and the single-shot
        # terminal burned the cycle (provisional artifact, llm_calls 0,
        # validation never reached). Deterministic failures (auth, bugs,
        # scripted refusals) keep the instant terminal the
        # characterization fixtures pin. Same retry plane the validation
        # repair path uses — substrate dispatches the narration worker,
        # which re-attempts the call (worker owns the transport). Narrate#1
        # carries no shrinkable history (envelope digest is already ~4K),
        # so stall retries here are identical-prompt; the KIND still rides
        # the ledger for the audit. A recovered turn falls through to the
        # shared downstream path below.
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError,
                            NarrationStalledError)):
            terminal_kind = narration_failure_kind(exc)
            st.failure_kind = terminal_kind
            st.failure_detail = first_error
            raw_1 = await context.recover_narration_call(
                engine, ctx, st, user_prompt_1,
            )
            if raw_1 is not None:
                context.record_tier(st)
            elif st.failure_kind:
                return await run_runtime_failure(engine, ctx, st)
    if raw_1 is None:
        st.controller = st.controller.advance(
            context.failure_event(terminal_kind)
        )
        terminal = (
            st.controller.terminal.value if st.controller.terminal
            else terminal_kind
        )
        ctx.controller = st.controller
        context.set_terminal(st.deterministic_state, terminal)
        return await engine._degraded_artifact(
            st.deterministic_state, st.capability_log,
            f"{terminal_kind}: {first_error}",
        ), {"task": (ctx.task or "")[:200] or None,
            "scenario": ctx.scenario,
            "llm_calls": st.llm_calls, "terminal": terminal}
    st.llm_calls += 1
    st.passes_per_loop["evidence"] += 1
    st.dispatched_in_pass = 0
    parsed_1 = coerce_turn(extract_json_object(raw_1))
    if parsed_1 is None:
        st.controller = st.controller.advance(
            context.failure_event("parse_failed")
        )
        terminal = (
            st.controller.terminal.value if st.controller.terminal
            else "parse_failed"
        )
        ctx.controller = st.controller
        context.set_terminal(st.deterministic_state, terminal)
        return await engine._degraded_artifact(
            st.deterministic_state, st.capability_log,
            "narration_parse_failed: no JSON object in output",
        ), {"task": (ctx.task or "")[:200] or None,
            "scenario": ctx.scenario,
            "llm_calls": st.llm_calls, "terminal": terminal}
    st.parsed_1 = parsed_1
    st.parsed_current = parsed_1
    st.turn_log.append(parsed_1)
    st.controller = _mark_declared(st.controller, parsed_1)
    while (st.passes_per_loop["evidence"] < LOOP_PASS_BUDGET["evidence"]
           and st.llm_calls < AGENTIC_MAX_LLM_TURNS):
        if not (isinstance(st.parsed_current.get("tool_calls"), list)
                and st.parsed_current.get("tool_calls")):
            break  # candidate stands; reasoning may still pull
        st.dispatched_in_pass = 0
        await engine._dispatch_turn(ctx, st)
        if await engine._followup_turn(ctx, st) is None:
            if st.failure_kind:
                return await run_runtime_failure(engine, ctx, st)
            break
    engine._name_pending(st)
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.VERIFICATION
    )
    st.opened_subs.add(SubLoop.VERIFICATION.value)
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )
    # STORE_HANDOFF (central ledger): the end-of-CONTEXT package for the
    # reasoning loop — what was gathered, what the prompt asked, and what
    # reasoning still needs. Deterministic engine state; the LLM reads it.
    from .chain import chain_completion as _chain_completion
    _snap = _chain_completion(
        st.controller, task=ctx.task, scenario=ctx.scenario,
    )
    _coverage = st.controller.phase_coverage
    _handoff = {
        "prompt_preview": (ctx.task[:200] if ctx.task else None),
        "plan_kind": (st.task_plan or {}).get("kind"),
        "gathered": sorted(st.accumulated_tool_results.keys()),
        "chain_missing": list(_snap.get("missing") or []),
        "chain_refused": list(_snap.get("refused") or []),
        "phase_coverage": {
            p: sorted(s) for p, s in _coverage.items()
        },
        "reasoning_needs": sorted(
            list(_snap.get("missing") or [])
            + [f"phase {p} uncovered"
               for p in ("P1", "P2", "P3", "P5")
               if not _coverage.get(p)]
        ),
    }
    record_entry(
        st.deterministic_state, LedgerKind.HANDOFF, _handoff,
        evidence_refs=(getattr(getattr(ctx, "wake", None), "wake_id", "") or "",),
        segment=getattr(ctx, "task_id", None),
    )
    # Honest CONTEXT visit: only sub-loops actually opened this cycle are
    # claimed. The deterministic passes (wake) recorded intake/interpretation/
    # framing on st.opened_subs; evidence added sourcing/acquisition/
    # verification above. A path that skipped a handler cannot claim it.
    _ordered = ("intake", "interpretation", "framing", "sourcing",
                "acquisition", "verification")
    _visited = tuple(s for s in _ordered if s in st.opened_subs)
    st.controller = st.controller.record_loop_visit(
        "context",
        st.passes_per_loop.get("comprehension", 0)
        + st.passes_per_loop.get("evidence", 0),
        _visited,
        completed=len(_visited) == len(_ordered),
    )
    # Canonical receipt: the evidence half of CONTEXT closed. Only sub-loops
    # that actually opened are claimed (no invented work).
    gates.close_loop(
        st, loop=NestedLoop.CONTEXT, sub_loop=SubLoop.VERIFICATION,
        steps=(LoopStep.PLAN_READS, LoopStep.SELECT_TOOLS, LoopStep.READ,
               LoopStep.CALCULATE, LoopStep.OBSERVE, LoopStep.ASSESS,
               LoopStep.STORE_HANDOFF),
        payload={"passes": st.passes_per_loop.get("evidence", 0)},
        refs=(getattr(ctx.wake, "wake_id", "") or "",),
        segment=getattr(ctx, "task_id", None),
    )
    return None


async def run_reasoning(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple | None:
    """REASONING loop: hard-tracked positions assemble → interpret → hypothesize.

    Three forced positions, one per pass minimum: each needs ≥1 in-position
    dispatch and its predicate met, advancing at most one position per pass.
    Empty tool_calls before all positions complete steer-continue instead of
    breaking. A refused required link halts the track into chain_halt for
    the FSM retry decision (no synthesis, visit marked incomplete).
    """
    st.loop_tag = "reasoning"
    # FIRST SEMANTIC (reasoning entry): read the CENTRAL LEDGER for this task
    # segment — understanding + plan (comprehension close) + handoff (evidence
    # close). Deterministic: by kind and append order, never by similarity or
    # scoring. This is what the LLM reasons over, never from scratch.
    _segment = getattr(ctx, "task_id", None)
    _kinds = (LedgerKind.UNDERSTANDING, LedgerKind.PLAN, LedgerKind.HANDOFF)
    _rows = ledger_rows(st.deterministic_state, _kinds, segment=_segment)
    st.reasoning_ledger_block = render_ledger(
        st.deterministic_state, _kinds, segment=_segment, budget=3_000,
    )
    st.reasoning_ledger_ids = [
        f"{row.get('kind')}#{row.get('seq')}" for row in _rows
    ]
    st.capability_log.append({
        "capability": "engine.ledger.reasoning_recall",
        "scope": {"symbol": engine.symbol, "venue": engine.venue},
        "result": "ok",
        "detail": {
            "segment": _segment,
            "kinds": [k.value for k in _kinds],
            "entries": len(_rows),
        },
    })
    # Slice B frame capability (distinct data layer): the HYPOTHESIS prior
    # frame is logged at entry (prompt basis with empty data — truth fills
    # in at hypothesize). The artifact trace proves which basis governed H0.
    try:
        from .chain import build_hypothesis_frame as _bhf_entry
        _dir = getattr(st, "task_directive", None)
        _entry_frame = _bhf_entry(ctx.task, _dir if isinstance(_dir, dict) else None, {})
        st.capability_log.append({
            "capability": "engine.hypothesis.frame",
            "scope": {"symbol": engine.symbol, "venue": engine.venue},
            "result": "ok",
            "detail": {
                "basis": _entry_frame.get("basis"),
                "prompt_bearing": _entry_frame.get("prompt_bearing"),
                "H0": str(_entry_frame.get("H0") or "")[:200],
            },
        })
    except Exception:
        pass
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.ENTER_LOOP, NestedLoop.REASONING
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.HYPOTHESIS
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ANALYSIS
    )
    while (st.passes_per_loop["reasoning"] < LOOP_PASS_BUDGET["reasoning"]
           and st.llm_calls < AGENTIC_MAX_LLM_TURNS):
        if not (isinstance(st.parsed_current.get("tool_calls"), list)
                and st.parsed_current.get("tool_calls")):
            if st.reason_position >= len(POSITION_ORDER):
                break  # track complete; candidate stands; validation decides
            # Floor + retry-reserve guard (Phase 2): the floor steer must not
            # consume the validation reserve (repairs + forced-final). When
            # fewer than 4 LLM turns remain, close the track so validation
            # still has its bounded retries.
            if st.llm_calls >= AGENTIC_MAX_LLM_TURNS - 4:
                break
            # Floor: the track is unwalked — steer into another positioned
            # pass instead of breaking (bounded by the pass/turn ceilings).
            st.dispatched_in_pass = 0
            if await engine._followup_turn(ctx, st) is None:
                if st.failure_kind:
                    return await run_runtime_failure(engine, ctx, st)
                break
            continue
        st.dispatched_in_pass = 0
        await engine._dispatch_turn(ctx, st)
        if st.reason_position < len(POSITION_ORDER):
            active_position = POSITION_ORDER[st.reason_position]
            halt = chain_halt_for(
                st.controller.outcomes, active_position,
                task=ctx.task, scenario=ctx.scenario)
            if halt is not None and st.chain_halt is None:
                st.chain_halt = halt
                freeze_chain(st, ctx.task, ctx.scenario)
                # Bounded reformulation offer: one followup turn where ONLY
                # a materially-different re-call of the halted tool can move
                # the cycle (suppression lifts for new args; everything else
                # stays denied or frozen). An empty close exits halted.
                st.dispatched_in_pass = 0
                reform = await engine._followup_turn(ctx, st)

                if reform is None:
                    if st.failure_kind:
                        return await run_runtime_failure(engine, ctx, st)
                    break
                if isinstance(reform.get("tool_calls"), list) and reform.get("tool_calls"):
                    st.dispatched_in_pass = 0
                    await engine._dispatch_turn(ctx, st)
                    if chain_halt_for(
                        st.controller.outcomes, active_position,
                        task=ctx.task, scenario=ctx.scenario,
                    ) is None:
                        st.chain_halt = None
                        freeze_chain(st, ctx.task, ctx.scenario)
                        continue  # halt cleared by re-evaluation; resume track
                engine._name_pending(st)
                # Leaving the loop with a sub-loop open is illegal under
                # the membrane (ENTER_LOOP would be denied) — close ANALYSIS
                # explicitly. This closes the position, not the track: the
                # visit below is marked incomplete and validation records
                # the halt for the FSM retry decision.
                st.controller, _ = context._must_govern(
                    st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
                )
                st.controller = st.controller.record_loop_visit(
                    "reasoning", st.passes_per_loop["reasoning"],
                    ("hypothesis", "analysis", "synthesis"),
                    completed=False,
                )
                return None  # validation records the halt; FSM decides retry
            status = position_status(
                st.controller.outcomes, active_position,
                task=ctx.task, scenario=ctx.scenario)
            # Advance on no-missing: assemble refusals already halted above;
            # interpret/hypothesize refusals ride forward as findings.
            if not status["missing"] and st.position_hits.get(active_position, 0) >= 1:
                completed = active_position
                st.reason_position += 1
                # Phase 2 inter-stage carry: freeze + store at each 2a/2b
                # close so the next stage reasons over stored conclusions.
                if completed == "assemble":
                    try:
                        st.base_summary = dict(
                            _base_summary(st.accumulated_tool_results))
                    except Exception:
                        log.exception("base_summary freeze failed")
                    await _store_stage_observation(
                        engine, ctx, st, "base",
                        {"base_summary": dict(getattr(st, "base_summary", {})),
                         "task": (ctx.task or "")[:200] or None},
                    )
                elif completed == "interpret":
                    try:
                        from ..tool_schemas import build_data_check_block as _dc
                        _dc_text = _dc(st.accumulated_tool_results)
                    except Exception:
                        _dc_text = ""
                    await _store_stage_observation(
                        engine, ctx, st, "refined",
                        {"data_checks": _dc_text[:1500],
                         "base_summary": dict(getattr(st, "base_summary", {})),
                         "task": (ctx.task or "")[:200] or None},
                    )
                # Slice D: per-position progress marker — the FSM reads
                # progress without new enums; the final exit visit stays
                # the honest (hypothesis, analysis, synthesis) close.
                try:
                    st.controller = st.controller.record_loop_visit(
                        "reasoning", st.passes_per_loop.get("reasoning", 0),
                        ("analysis",), completed=False,
                    )
                except Exception:
                    pass
        if await engine._followup_turn(ctx, st) is None:
            if st.failure_kind:
                return await run_runtime_failure(engine, ctx, st)
            break
    engine._name_pending(st)
    _open_once(st, SubLoop.SYNTHESIS)
    # Freeze the steady-track reading at reasoning exit: the validator
    # judges this, not the agent's self-report.
    freeze_chain(st, ctx.task, ctx.scenario)
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )
    st.controller = st.controller.record_loop_visit(
        "reasoning", st.passes_per_loop["reasoning"],
        ("hypothesis", "analysis", "synthesis"),
        completed=True,
    )
    # Canonical receipt: the REASONING loop closed its full track.
    gates.close_loop(
        st, loop=NestedLoop.REASONING, sub_loop=SubLoop.SYNTHESIS,
        steps=(LoopStep.FRAME, LoopStep.STATE_PRIOR, LoopStep.TEST,
               LoopStep.COMPARE, LoopStep.INTERPRET, LoopStep.RECONCILE),
        payload={"reason_position": st.reason_position},
        segment=getattr(ctx, "task_id", None),
    )
    # Adjudication spine (spec §3): verdict-basis ledger record at reasoning
    # close — frame + primary truth + stage ids — so validation weighs
    # recalled conclusions. Best-effort ledger write (pure, cannot fail the
    # cycle); validation falls back to recomputing the frame when absent.
    try:
        from .chain import build_hypothesis_frame as _bhf_close
        _dir_close = getattr(st, "task_directive", None)
        _close_frame = _bhf_close(
            ctx.task,
            _dir_close if isinstance(_dir_close, dict) else None,
            st.accumulated_tool_results,
        )
    except Exception:
        _close_frame = {}
    await _store_stage_observation(
        engine, ctx, st, "verdict-basis",
        {"frame": _close_frame,
         "base_summary": dict(getattr(st, "base_summary", {})),
         "task": (ctx.task or "")[:200] or None},
    )
    return None


# ---------------------------------------------------------------------------
# Backward-compatible re-exports — the canonical owner is now core.validation
# (semantic separation by loop state). Kept so existing imports from
# ``core.reasoning`` keep resolving, mirroring the run_comprehension re-export.
# ---------------------------------------------------------------------------
from .validation import (  # noqa: E402,F401
    check_scenario_equality,
    has_directive_verdict,
    merge_interpretation,
    run_validation,
    validate_final_turn,
)
