"""REASON stage — one function per loop over a shared pass carrier.

Evidence gathers (narrate#1 + budgeted follow-ups); reasoning tests and
compares; validation gates with one bounded retry then emits to the FSM
terminal. COMPREHENSION / WAKE lives in engine.core.wake (canonical
owner); this module re-exports its run_comprehension for backward
compatibility. Per-pass mechanics live next to their owning modules:

  CycleRuntimeState, _open_once, _mark_declared, freeze_chain,
  run_runtime_failure, run_validation_terminal
        → engine.core.context
  _dispatch_turn, _followup_turn, _name_pending
        → InferenceEngine (engine.core.driver)
  run_wake, run_comprehension (+ WAKE completion predicates)
        → engine.core.wake
  seed_evidence_plan, default_comprehension, prompt composers
        → engine.kb

This module owns the three run_* REASON loop bodies; run_comprehension
is a re-export kept purely for backward-compatible imports.
"""

from __future__ import annotations

import logging
from typing import Any

from . import context
from ..schemas import coerce_turn, extract_json_object
from ..controller import next_uncovered_phase
from ..config import (
    SUMMARY_MIN_CHARS,
    AGENTIC_MAX_LLM_TURNS,
    LOOP_PASS_BUDGET,
    MAX_DISPATCHES_PER_PASS,
    VALIDATION_RETRY_PASSES,
)
from .context import (
    CycleRuntimeState,
    _ReasonedCycle,
    _open_once,
    _mark_declared,
    freeze_chain,
    run_runtime_failure,
    run_validation_terminal,
)
from ..fsm import GovernanceEvent, GovernanceEventKind
from .. import kb
from ..kb import (
    build_output_format,
    compose_narrate1_prompt,
    compose_repair_prompt,
)
from .chain import (
    FORWARD_SCENARIO_TOOL,
    POSITION_ORDER,
    POSITION_TOOLS,
    TOOL_POSITION,
    chain_completion,
    chain_halt_for,
    chain_steer,
    position_status,
    position_steer,
)
from ..loop_states import NestedLoop, SubLoop, TaskIntent
from ..principles import evaluate_principles as _evaluate_principles

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Re-exports for backward compatibility with call sites that historically
# imported the carrier / FSM helpers / terminal emitters from this module.
# The three run_* REASON loop bodies are the public surface tested by the
# cycle characterization suite; run_comprehension is re-exported from its
# canonical owner (core.wake); everything else is an internal carrier
# helper that lives in context now.
# --------------------------------------------------------------------------
__all__ = [
    "CycleRuntimeState",
    "run_evidence",
    "run_reasoning",
    "run_validation",
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
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ACQUISITION
    )
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
        memory_block=st.memory_block,
        output_format=engine._output_format(),
        workflow_block=st.workflow_block,
        traversal=st.controller.loop_coverage(),
        gate=st.deterministic_state.get("gate"),
    )
    try:
        raw_1 = await engine._call_llm(user_prompt_1)
        context.record_tier(st)
    except Exception as exc:
        log.exception("narration#1 failed")
        st.controller = st.controller.advance(
            GovernanceEvent(GovernanceEventKind.NARRATION_FAILED)
        )
        terminal = (
            st.controller.terminal.value if st.controller.terminal
            else "narration_failed"
        )
        ctx.controller = st.controller
        st.deterministic_state["terminal"] = terminal
        return await engine._degraded_artifact(
            st.deterministic_state, st.capability_log,
            f"narration_failed: {type(exc).__name__}: {exc}",
        ), {"llm_calls": st.llm_calls, "terminal": terminal}
    st.llm_calls += 1
    st.passes_per_loop["evidence"] += 1
    st.dispatched_in_pass = 0
    parsed_1 = coerce_turn(extract_json_object(raw_1))
    if parsed_1 is None:
        st.controller = st.controller.advance(
            GovernanceEvent(GovernanceEventKind.PARSE_FAILED)
        )
        terminal = (
            st.controller.terminal.value if st.controller.terminal
            else "parse_failed"
        )
        ctx.controller = st.controller
        st.deterministic_state["terminal"] = terminal
        return await engine._degraded_artifact(
            st.deterministic_state, st.capability_log,
            "narration_parse_failed: no JSON object in output",
        ), {"llm_calls": st.llm_calls, "terminal": terminal}
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
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )
    # STORE_HANDOFF (state redistribution): the end-of-CONTEXT package for
    # the reasoning loop — what was gathered, what the prompt asked, and what
    # reasoning still needs. Written to the task segment so the next turn
    # reasons over data, never from scratch. Best-effort: memory-less runs
    # skip silently (transport pattern).
    try:
        if engine.memory is not None:
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
            import json as _json
            await engine.memory.remember(
                engine.session_id, "handoff",
                _json.dumps(_handoff, default=str),
                segment=getattr(ctx, "task_id", None),
                importance=2.0,
                tags=("wake", "handoff", engine.symbol.lower()),
                evidence_refs=(ctx.wake.wake_id,),
            )
    except Exception:
        log.exception("handoff placement failed")
    # The CONTEXT loop visit is recorded once, at the loop's true close,
    # covering all six sub-loops (its deterministic passes + the evidence pass).
    st.controller = st.controller.record_loop_visit(
        "context",
        st.passes_per_loop.get("comprehension", 0)
        + st.passes_per_loop.get("evidence", 0),
        ("intake", "interpretation", "framing", "sourcing",
         "acquisition", "verification"),
        completed=True,
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
                st.deterministic_state["chain_halt"] = dict(halt)
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
                        st.deterministic_state["chain_halt"] = None
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
                st.reason_position += 1
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
    return None


async def run_validation(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple | None:
    """VALIDATION loop: gate + one bounded retry, else FSM terminal."""
    st.loop_tag = "validation"
    if not st.validation_entered:
        st.validation_entered = True
        st.passes_per_loop["validation"] = st.passes_per_loop.get("validation", 0) + 1
        st.controller, _ = context._must_govern(
            st.controller, GovernanceEventKind.ENTER_LOOP, NestedLoop.VALIDATION
        )
        st.controller, _ = context._must_govern(
            st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.GATE
        )
    elif (
        st.controller.observation is not None
        and st.controller.observation.sub_loop is None
    ):
        # A re-entry after a completed recovery starts a fresh gate without
        # pretending that the parent loop was re-entered.
        st.controller, _ = context._must_govern(
            st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.GATE
        )
    # Judge the MERGED interpretation across the cycle's turns: the staged
    # loop legitimately ends on bare declaration turns (the contract allows
    # null interpretation fields while tools are pending), and content the
    # model produced earlier in THIS cycle must not be zeroed out because the
    # last turn only declared a phase.
    judged = merge_interpretation(st.turn_log)
    passed, missing = validate_final_turn(
        judged, st.controller.phase_coverage, scenario=ctx.scenario,
        scenario_status=st.controller.scenario_state(),
        scenario_refusal_reason=st.controller.scenario_refusal_reason(),
    )
    # Steady-track gate (core-owned): required-but-never-attempted chain
    # links join missing[] and ride the existing repair path. Refused links
    # are findings — they never block, and the controller already
    # suppresses their re-dispatch structurally.
    st_chain = chain_completion(st.controller, task=ctx.task, scenario=ctx.scenario)
    freeze_chain(st, ctx.task, ctx.scenario)
    missing = list(missing)
    chain_gaps: list[str] = []
    for tool in st_chain["missing"]:
        if tool == "calc.discipline.audit":
            # The audit is validation-homed: the repair turn can pull it.
            missing.append(
                "statistical chain: calc.discipline.audit never attempted — "
                "call it now (validation home) before finalizing"
            )
        else:
            # Every other chain link lives in an earlier loop and cannot be
            # pulled from validation: record it as a GAP (findings preserved,
            # artifact carries it) instead of a blocking miss. Demanding an
            # uncallable tool from the repair turn guaranteed a
            # validation_failed terminal for cycles that were otherwise
            # complete — the track gap rides the artifact, it does not kill
            # the cycle.
            chain_gaps.append(
                f"statistical chain gap: {tool} never attempted in its loop — "
                "recorded unwalked; cited as a limitation, not a failure"
            )
    if chain_gaps:
        st.deterministic_state["chain_gaps"] = list(chain_gaps)
    # Principles at the gate: advisory findings land beside the verdict —
    # they inform the artifact, they never join missing[] (guards, not gates).
    for _finding in _evaluate_principles(
        controller=st.controller, plan=st.task_plan, turn=judged,
        reason_position=st.reason_position,
    ):
        st.deterministic_state.setdefault("principle_findings", []).append(
            {"principle": _finding.principle, "detail": _finding.detail})
    # Task-conformance (core-owned): a target-bearing directive must surface
    # its verdict — the band membership / scenario citation or its refusal —
    # before the gate passes. This closes the prompt→answer loop: the final
    # must answer the question the directive parsed, not merely cite tools.
    if (st.task_plan is not None
            and (st.task_plan.get("targets") or st.task_plan.get("invalidations"))
            and not has_directive_verdict(judged)):
        missing.append(
            "task directive verdict not cited — cite the deterministic "
            "directive/scenario paths (deterministic_state.task_directive "
            "or forward_scenario/calc.forward.scenario) with the band "
            "verdict or its refusal before finalizing"
        )
    # Halted track (core-owned): a refused required link stopped reasoning
    # for the FSM retry decision. The halt never clears itself here — the
    # missing entry below forces the validation_failed terminal with the
    # halt preserved on the artifact; only an FSM retry grant resumes it.
    halt = st.chain_halt or st.deterministic_state.get("chain_halt")
    halted = isinstance(halt, dict) and bool(halt.get("tool"))
    if halted:
        missing.append(
            f"statistical chain halted at {halt.get('position')} on "
            f"{halt.get('tool')} ({halt.get('reason')}) — FSM retry "
            "decision required before the track can resume; do not re-call "
            "the halted tool"
        )
    # The chain merge above only matters if it can actually hold the gate:
    # an unwalked track or an undecided halt fails validation even when the
    # phase checks pass (refused links stay findings — only never-attempted
    # links and halts block here).
    passed = passed and not st_chain["missing"] and not halted
    if passed:
        st.final_validation = {"passed": True, "missing": []}
        st.finalize_now = True
        if st.controller.observation is not None and st.controller.observation.sub_loop is not None:
            st.controller, _ = context._must_govern(
                st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
            )
        st.controller = st.controller.record_loop_visit(
            "validation", st.passes_per_loop.get("validation", 0),
            ("gate",) if st.validation_retries_used == 0
            else ("gate", "recovery"),
            completed=True,
        )
        ctx.controller = st.controller
        ctx.reasoned = st.to_reasoned()
        return None
    if (st.validation_retries_used >= VALIDATION_RETRY_PASSES
            and not st.forced_final_sent):
        # FORCED FINAL — the missing link (config budget comment promised
        # "narrate#1 + tool follow-ups + repairs + forced-final" but no such
        # turn existed). Live probe 2026-09-28: 12 in-loop turns returned
        # tool_calls only — summary/evidence/hypothesis empty in EVERY turn,
        # while the same model with a dedicated finalize request returns the
        # complete final object. So spend one tool-free finalize turn (small
        # context, field shapes inline) and let this gate judge it.
        st.forced_final_sent = True
        if await _forced_final_turn(engine, ctx, st, list(missing)) is not None:
            return await run_validation(engine, ctx, st)
    if st.validation_retries_used >= VALIDATION_RETRY_PASSES:
        return await run_validation_terminal(engine, ctx, st, list(missing))
    st.repairs_sent += 1
    st.validation_retries_used += 1
    # The RECOVERY sub-loop is opened ONCE per validation: further bounded
    # retries ride the already-open sub-loop. Re-opening it is a governance
    # denial ("no next sub-loop to open") that used to surface as an
    # infra_failed terminal the moment VALIDATION_RETRY_PASSES went above 1.
    if (st.controller.observation is None
            or st.controller.observation.sub_loop != SubLoop.RECOVERY):
        st.controller, _ = context._must_govern(
            st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.RECOVERY
        )
    scenario_steer = st.controller.scenario_steer()
    repair_prompt = compose_repair_prompt(
        controller=st.controller,
        loop="validation", sub_loop="recovery",
        passes_spent=st.passes_per_loop.get("validation", 0),
        pass_budget=LOOP_PASS_BUDGET["validation"],
        dispatches_left=MAX_DISPATCHES_PER_PASS,
        missing=list(missing),
        accumulated=st.accumulated_tool_results,
        task_reminder=st.task_reminder if ctx.task else "",
        scenario_reminder=st.scenario_reminder if ctx.scenario else "",
        scenario_steer=scenario_steer,
        phase_guidance=st.phase_guidance,
        next_phase=next_uncovered_phase(st.controller.phase_coverage),
    )
    try:
        raw_repair = await engine._call_llm(repair_prompt)
        context.record_tier(st)
    except Exception as exc:
        log.exception("repair turn failed; ending validation")
        st.failure_kind = "narration_failed"
        st.failure_detail = f"{type(exc).__name__}: {exc}"
        return await run_runtime_failure(engine, ctx, st)
    st.llm_calls += 1
    st.passes_per_loop["validation"] = st.passes_per_loop.get("validation", 0) + 1
    parsed_repair = coerce_turn(extract_json_object(raw_repair))
    if parsed_repair is None:
        log.warning("repair parse failed, ending validation")
        st.failure_kind = "parse_failed"
        st.failure_detail = "no JSON object in repair narration"
        return await run_runtime_failure(engine, ctx, st)
    st.parsed_current = parsed_repair
    st.turn_log.append(parsed_repair)
    st.controller = _mark_declared(st.controller, parsed_repair)
    st.dispatched_in_pass = 0
    await engine._dispatch_turn(ctx, st)
    # A repair that only repeats a deterministically refused/suppressed tool
    # already contains the candidate final.  Do not spend an unbounded extra
    # narration turn merely to report that no work executed.
    if getattr(st, "_round_had_execution", False):
        if await engine._followup_turn(ctx, st) is None:
            if st.failure_kind:
                return await run_runtime_failure(engine, ctx, st)
            log.warning("repair follow-up unavailable; judging repair turn as it stands")
    return await run_validation(engine, ctx, st)


async def _forced_final_turn(
    engine: Any, ctx: Any, st: CycleRuntimeState, missing: list[str],
) -> dict[str, Any] | None:
    """One tool-free FINALIZE turn (the config's promised 'forced-final').

    Small context, field shapes inline, every missing item named, bounded
    digest of this run's tool evidence. BOUNDED TO TWO ATTEMPTS: a response
    that parses but carries no interpretation content is discarded and the
    strict retry says so (live 2026-09-28: one-shot final returned an empty
    shell and the cycle died with an empty judged view). Returns the parsed
    turn (appended to ``st.turn_log``) or ``None`` when nothing usable came
    back — the caller then falls through to the validation-failed terminal.
    """
    from ..llm import bounded_envelope_view

    digest = bounded_envelope_view(
        st.accumulated_tool_results, roof=14_000, list_cap=8)
    last_parsed: dict[str, Any] | None = None
    for attempt in (1, 2):
        prompt = kb.compose_forced_final_prompt(
            task=ctx.task, missing=missing, evidence_digest=digest,
            scenario=ctx.scenario, strict=(attempt > 1),
        )
        try:
            raw_final = await engine._call_llm(prompt)
            context.record_tier(st)
        except Exception as exc:
            log.warning("forced final turn (attempt %d) failed: %s", attempt, exc)
            return last_parsed
        st.llm_calls += 1
        parsed_final = coerce_turn(
            extract_json_object(raw_final))
        if parsed_final is None:
            log.warning("forced final turn (attempt %d) unparseable", attempt)
            continue
        st.parsed_current = parsed_final
        st.turn_log.append(parsed_final)
        st.controller = _mark_declared(st.controller, parsed_final)
        last_parsed = parsed_final
        if _has_interpretation(parsed_final):
            return parsed_final
        log.warning(
            "forced final turn (attempt %d) returned an empty interpretation; "
            "%s", attempt, "retrying strictly" if attempt == 1 else "judging")
    return last_parsed


def _has_interpretation(parsed: dict[str, Any]) -> bool:
    """Does a parsed turn carry ANY interpretation content? (deterministic)"""
    summary = parsed.get("summary")
    if isinstance(summary, str) and summary.strip():
        return True
    evidence = parsed.get("evidence")
    if isinstance(evidence, list) and evidence:
        return True
    return isinstance(parsed.get("hypothesis"), dict) and bool(parsed.get("hypothesis"))


# ---------------------------------------------------------------------------
# Turn-folding + final acceptance — owned HERE (VALIDATION loop), moved
# from engine/narration.py (Pass-C redistribution). Cross-evidence
# properties describe the whole cycle, so they live with the loop that
# judges it. narration.py re-exports these names until its deletion pass.
# ---------------------------------------------------------------------------


def merge_interpretation(turns: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Merged interpretation view over a cycle's parsed turns (deterministic).

    The staged loop legitimately leaves interpretation fields null while
    tools are pending (the contract says so) and the last turn of a cycle is
    often a bare phase declaration. Judging only that turn zeroes out content
    the model already produced earlier in the SAME cycle. This view merges
    what was actually said — and a revision supersedes: every singleton
    field resolves to the LAST non-null statement, so an agent that refines
    its hypothesis mid-cycle is judged on the refined one, never the stale
    first draft:

      summary       — longest non-empty across turns (P4 explanation)
      hypothesis    — LAST non-null dict (revision wins)
      evidence      — union, order-preserving, deduped on (path, value)
      confidence    — LAST non-null (revision wins)
      limitations   — union of strings
      model_separation — LAST non-null (revision wins)
      scenario / forward_scenario / hypothesis_evidence — LAST non-null
                      (most recent statement wins)

    Pure function: no state, no I/O. ``validate_final_turn`` receives the
    merged view; per-turn shape violations are still repaired per turn.
    """
    merged: dict[str, Any] = {
        "summary": "",
        "evidence": [],
        "confidence": None,
        "limitations": [],
        "model_separation": None,
        "hypothesis": None,
        "scenario": None,
        "forward_scenario": None,
        "hypothesis_evidence": None,
    }
    seen_evidence: set[tuple[str, str]] = set()
    seen_limits: set[str] = set()
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        summary = turn.get("summary")
        if isinstance(summary, str) and len(summary.strip()) > len(merged["summary"]):
            merged["summary"] = summary
        hypothesis = turn.get("hypothesis")
        if isinstance(hypothesis, dict):
            merged["hypothesis"] = hypothesis
        evidence = turn.get("evidence")
        if isinstance(evidence, list):
            for entry in evidence:
                if not isinstance(entry, dict):
                    continue
                key = (str(entry.get("path") or ""), str(entry.get("value") or ""))
                if key in seen_evidence:
                    continue
                seen_evidence.add(key)
                merged["evidence"].append(entry)
        if turn.get("confidence") is not None:
            merged["confidence"] = turn.get("confidence")
        limitations = turn.get("limitations")
        if isinstance(limitations, list):
            for item in limitations:
                text = str(item)
                if text not in seen_limits:
                    seen_limits.add(text)
                    merged["limitations"].append(text)
        if turn.get("model_separation"):
            merged["model_separation"] = turn.get("model_separation")
        for key in ("scenario", "forward_scenario", "hypothesis_evidence"):
            if turn.get(key) is not None:
                merged[key] = turn[key]
    return merged



def has_directive_verdict(parsed: dict[str, Any] | None) -> bool:
    """Task-conformance read (pure): did the final surface the verdict?

    True when the final's evidence cites the task directive or a scenario
    verdict path (forward scenario / legacy exceedance) — the deterministic
    directive/scenario surface the plan bound. Used by the validation gate
    for target-bearing directives; the repair steer names the citation.
    """
    evidence = (parsed or {}).get("evidence") or []
    for entry in evidence if isinstance(evidence, list) else []:
        path = (str((entry or {}).get("path", ""))
                if isinstance(entry, dict) else str(entry or ""))
        if ("task_directive" in path or "forward_scenario" in path
                or "calc.forward.scenario" in path
                or "calc.scenario.evaluate" in path):
            return True
    return False



def validate_final_turn(
    parsed: dict[str, Any],
    coverage: dict[str, set[str]],
    scenario: dict[str, Any] | None = None,
    *,
    scenario_status: "ScenarioEvalStatus | None" = None,
    scenario_refusal_reason: str | None = None,
) -> tuple[bool, list[str]]:
    """Phase-aware final gate: depth is structural, not advisory.

    A FINAL turn passes only when every required phase family has at least
    one executed tool (P1 OFI, P2 AD/fits, P3 market correlation, P5 paper
    + derived ΔP), the hypothesis carries H0, the P4 explanation meets the
    length floor, confidence is the contract enum, every evidence entry
    carries a non-empty interpretation, and evidence cites at least two
    distinct roots with at least one fresh tool result (not just
    deterministic_state). Returns (passed, missing[]) — missing drives
    the repair prompt.

    Scenario semantics come from the CONTROLLER's tri-state
    (``ScenarioEvalStatus``), never re-derived here:
      - EVALUATED (or legacy None → treated as evaluated/unknown): the
        evidence must cite a calc.scenario.evaluate → … data path.
      - REFUSED: the tool refused deterministically — the citation
        requirement is satisfied by a refusal FINDING (path
        ``calc.scenario.evaluate → refusal``), scenario.verdict must be
        ``unevaluable`` (reachable/not_reachable are forbidden without
        tool data), and the numeric echo requirements are waived (nothing
        to echo). The LLM cannot override null discipline.
      - NOT_CALLED: the citation demand stands unchanged; the repair
        steer (controller-owned) tells the model to call the tool.
    """
    from market_service.nooa_harness.inference import _REQUIRED_PHASES
    from market_service.runtime.contracts import normalize_confidence

    missing: list[str] = []
    for phase in _REQUIRED_PHASES:
        if not coverage.get(phase):
            missing.append(
                f"phase {phase} uncovered: execute its tools before finalizing "
                f"(covered so far: {sorted(coverage.get(phase) or [])})"
            )
    if not coverage.get("P6"):
        missing.append(
            "P6 output-generation turn required: synthesize the primary inference "
            "output from this run's reasoning (declare phase P6, no tools)"
        )
    hypothesis = parsed.get("hypothesis")
    if not isinstance(hypothesis, dict) or not str(hypothesis.get("H0") or "").strip():
        missing.append("hypothesis.H0 required: frame H0/H1 grounded in recalled paper facts")
    summary = parsed.get("summary") or ""
    if not isinstance(summary, str) or len(summary.strip()) < SUMMARY_MIN_CHARS:
        missing.append(
            f"P4 explanation too thin ({len(summary.strip())}/{SUMMARY_MIN_CHARS} chars): "
            "say what the fits show AND why it is happening now"
        )
    confidence = parsed.get("confidence")
    if confidence is not None and normalize_confidence(confidence) is None:
        missing.append(
            f"confidence must be one of low|medium|high (got {confidence!r}); "
            "hyphenated blends coerce conservatively (low-medium → low)"
        )
    roots: set[str] = set()
    evidence = parsed.get("evidence")
    if isinstance(evidence, list):
        for idx, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                missing.append(f"evidence[{idx}] must be an object with path + interpretation")
                continue
            if not str(entry.get("interpretation") or "").strip():
                missing.append(
                    f"evidence[{idx}] missing interpretation: say what "
                    f"{entry.get('path')!r} shows (paths+values cited, readings empty is a violation)"
                )
            path = str(entry.get("path") or "")
            head = path.split("→")[0].strip()
            # Root identity: the TOOL head (calc.ofi.intervals, market.read, …)
            # is the root — splitting on "." collapsed every calc.* tool into
            # the single root "calc", so a perfectly-cited cycle could never
            # show "≥2 distinct roots". deterministic_state paths collapse to
            # their namespace (they are not fresh tool results).
            if head.startswith("deterministic_state"):
                root = "deterministic_state"
            else:
                root = head
            if root:
                roots.add(root)
    if len(roots) < 2 or not (roots - {"deterministic_state"}):
        missing.append(
            "evidence must cite ≥2 distinct roots including ≥1 fresh tool result "
            f"(got roots: {sorted(roots) or 'none'}" + ")"
        )
    delta_paths = [
        str(entry.get("path") or "")
        for entry in (evidence if isinstance(evidence, list) else [])
        if isinstance(entry, dict)
    ]
    if not any(
        head in ("calc.price.delta", "calc.derived_diagnostic")
        for path in delta_paths
        for head in [path.split("→")[0].strip()]
    ):
        missing.append(
            "evidence must cite the derived ΔP via a calc.price.delta → … path"
        )
    if scenario is not None:
        from ..controller import ScenarioEvalStatus  # local import: avoids cycle
        scen = parsed.get("scenario")
        if scenario_status is ScenarioEvalStatus.REFUSED:
            # Controller says the tool refused deterministically. The
            # citation requirement is satisfied by a refusal FINDING;
            # verdict must be unevaluable; numeric echoes are waived.
            scen_ok = isinstance(scen, dict)
            if not scen_ok:
                missing.append(
                    "scenario block required: the scenario tool refused — "
                    "return the scenario object with verdict=unevaluable "
                    "and the refusal cited"
                )
            else:
                verdict = str(scen.get("verdict") or "")
                if verdict not in ("unevaluable",):
                    missing.append(
                        "scenario.verdict must be 'unevaluable': the tool "
                        f"refused deterministically ({scenario_refusal_reason or 'refusal'}) "
                        "— reachable/not_reachable are forbidden without tool data"
                    )
                rationale = scen.get("rationale")
                reason_stub = str(scenario_refusal_reason or "refused")[:20].lower()
                names_cause = (
                    isinstance(rationale, str)
                    and (reason_stub in rationale.lower()
                         or "refus" in rationale.lower())
                )
                if (not isinstance(rationale, str)
                        or len(rationale.strip()) < 80
                        or not names_cause):
                    missing.append(
                        "scenario.rationale required (≥80 chars) naming the "
                        "refusal reason — a bare verdict without the "
                        "deterministic cause is rejected"
                    )
            if not any(
                head == "calc.scenario.evaluate"
                and "refusal" in path
                for path in delta_paths
                for head in [path.split("→")[0].strip()]
            ):
                missing.append(
                    "evidence must cite the refusal as "
                    "`calc.scenario.evaluate → refusal` (a finding, not a "
                    "re-call demand — the tool will refuse again)"
                )
        else:
            if not isinstance(scen, dict):
                missing.append(
                    "scenario block required: a scenario was given — return the "
                    "scenario{target_price,horizon,direction,required_ofi,exceedance,"
                    "probability,verdict,rationale} object evaluated via calc.scenario.evaluate"
                )
            else:
                if str(scen.get("verdict") or "") not in (
                        "reachable", "not_reachable", "unevaluable"):
                    missing.append(
                        "scenario.verdict required: reachable|not_reachable|unevaluable"
                    )
                if normalize_confidence(scen.get("probability")) is None:
                    missing.append(
                        "scenario.probability required: low|medium|high "
                        "(qualitative read of the exceedance + band + tape quality)"
                    )
                for field in ("required_ofi", "exceedance"):
                    if not str(scen.get(field) or "").strip():
                        missing.append(
                            f"scenario.{field} required: echo the calc.scenario.evaluate "
                            f"→ … value, never compute it yourself"
                        )
                # Thin-tape context travels WITH the verdict: a 0% exceedance
                # without fit_status + window count reads as "impossible" when
                # it means "the tape couldn't speak". Both are deterministic
                # echoes, so the check is structural, never semantic.
                for field in ("fit_status", "n_windows_usable"):
                    if not str(scen.get(field) or "").strip():
                        missing.append(
                            f"scenario.{field} required: echo the calc.scenario.evaluate "
                            f"→ … value so the verdict carries its tape context"
                        )
                rationale = scen.get("rationale")
                if not isinstance(rationale, str) or len(rationale.strip()) < 80:
                    missing.append(
                        "scenario.rationale required (≥80 chars): name fit_status + "
                        "usable windows + r2 beside the verdict — a bare verdict "
                        "without its tape context is rejected"
                    )
            if not any(
                head == "calc.scenario.evaluate"
                for path in delta_paths
                for head in [path.split("→")[0].strip()]
            ):
                missing.append(
                    "evidence must cite the scenario evaluation via a "
                    "calc.scenario.evaluate → … path"
                )
        hypothesis_sc = parsed.get("hypothesis")
        if (not isinstance(hypothesis_sc, dict)
                or not str(hypothesis_sc.get("H0") or "").strip()
                or not str(hypothesis_sc.get("H1") or "").strip()):
            missing.append(
                "scenario cycles frame TWO hypotheses: H0 (target NOT reachable) "
                "and H1 (target reachable), both non-empty"
            )
    return (not missing), missing


