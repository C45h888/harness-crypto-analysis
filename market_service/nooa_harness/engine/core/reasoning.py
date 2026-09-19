"""REASON stage — one function per loop over a shared pass carrier.

Comprehension understands once; evidence gathers (narrate#1 + budgeted
follow-ups); reasoning tests and compares; validation gates with one bounded
retry then emits to the FSM terminal. Per-pass mechanics (dispatch round,
follow-up turn, name-pending, FSM-string helpers, terminal emitters, and
the cycle carrier itself) live next to their owning modules:

  CycleRuntimeState, _open_once, _mark_declared, freeze_chain,
  run_runtime_failure, run_validation_terminal
        → engine.core.context
  _dispatch_turn, _followup_turn, _name_pending
        → InferenceEngine (engine.core.driver)
  seed_evidence_plan, default_comprehension, prompt composers
        → engine.kb

This module owns only the four run_* loop bodies; everything else is a
re-export kept on this module purely for backward-compatible imports.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import context
from .. import narration as narration_mod
from ..config import (
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
    build_task_workflow,
    compose_narrate1_prompt,
    compose_repair_prompt,
    default_comprehension,
    seed_evidence_plan,
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

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Re-exports for backward compatibility with call sites that historically
# imported the carrier / FSM helpers / terminal emitters from this module.
# The four run_* loop bodies are the public surface tested by the cycle
# characterization suite; everything else is an internal carrier helper
# that lives in context now.
# --------------------------------------------------------------------------
__all__ = [
    "CycleRuntimeState",
    "run_comprehension",
    "run_evidence",
    "run_reasoning",
    "run_validation",
]



async def run_comprehension(
    engine: Any, ctx: Any,
) -> tuple[CycleRuntimeState | None, tuple | None]:
    """COMPREHENSION loop (1 pass, no tools). Returns (st, None) or (None, finished)."""
    gathered = ctx.gathered
    if gathered is None:
        raise RuntimeError("reason/check requires gathered evidence")
    wake, task, scenario = ctx.wake, ctx.task, ctx.scenario
    st = CycleRuntimeState(
        controller=ctx.controller,
        capability_log=ctx.capability_log,
        deterministic_state=gathered.deterministic_state,
        accumulated_tool_results=gathered.accumulated_tool_results,
        tool_results=gathered.tool_results,
        parsed_current={},
    )

    # --- CONTEXT: memory node pruned from the track (transport retained on
    # the driver for later use) — no recall read feeds the prompts; H0/H1
    # grounding comes from the track's own evidence.

    wake_block = json.dumps(
        {"trigger_source": wake.trigger_source,
         "predicates": wake.predicates_fired},
        default=str,
    )
    task_block = (
        f"TASK (interactive-plane directive — frame H0/H1 to ANSWER this; "
        f"cite it in hypothesis.evidence_refs as 'task'):\n{task[:4_000]}\n\n"
        if task else ""
    )
    scenario_block = (
        f"SCENARIO (price-target question — evaluate with the "
        f"calc.scenario.evaluate tool at the given horizon; cite its "
        f"→ … paths, never compute the requirement yourself):\n"
        f"{json.dumps(scenario)}\n\n"
        if scenario else ""
    )
    st.task_reminder = (
        f"TASK reminder (answer this): {task[:500]}\n"
        if task else ""
    )
    st.task_block = task_block  # type: ignore[attr-defined]
    st.scenario_block = scenario_block  # type: ignore[attr-defined]
    st.wake_block = wake_block  # type: ignore[attr-defined]
    st.memory_block = ""  # type: ignore[attr-defined]
    st.prior_note = ""  # type: ignore[attr-defined]
    st.scenario_reminder = (
        f"SCENARIO reminder (evaluate with calc.scenario.evaluate at the "
        f"given horizon; frame H0/H1 as not-reachable/reachable): "
        f"{json.dumps(scenario)}\n"
        if scenario else ""
    )
    st.phase_guidance = dict(narration_mod.PHASE_GUIDANCE)
    if scenario:
        st.phase_guidance["P5"] = (
            st.phase_guidance["P5"]
            + f" SCENARIO GIVEN ({json.dumps(scenario)}): call "
            "calc.scenario.evaluate with that target_price/horizon IN ADDITION "
            "to price.delta — the scenario verdict is the primary output. "
            "The final scenario block must echo fit_status + n_windows_usable + r2 "
            "and its rationale (≥80 chars) must name them beside the verdict."
        )
    user_prompt_1 = (
        f"{task_block}"
        f"{scenario_block}"
        f"WAKE: {wake_block}\n\n"
        "ENVELOPE STATE (gate reads + wake identity — READ-PLANE; pull "
        "everything else yourself via tools; never recompute values, "
        "COMMAND the read tools and cite their paths):\n"
        f"{json.dumps(st.deterministic_state, default=str)[:60_000]}\n\n"
    )
    st.user_prompt_1 = user_prompt_1  # type: ignore[attr-defined]

    # --- COMPREHENSION PASS (1 LLM call, no tools) ---
    # --- TASK DIRECTIVE (Phase A reparse + Phase B assessment) ---
    # Phase A already ran in gather (deterministic, drove the pre-acquired
    # forecast horizon + scenario). Here the LLM gets ONE bounded assessment
    # turn at the primitive intake stage: it assesses the prompt and
    # PROPOSES amendments strictly within the frozen vocabularies; the
    # engine disposes — validated proposals bind, rejects are recorded, the
    # track is never amended by them (controller doctrine).
    from ..task_directive import (
        assessment_prompt, build_plan, dispose_assessment,
        parse_task_directive,
    )
    directive = parse_task_directive(task, scenario)
    assessment_note: str | None = None
    # Phase B trigger (budget guardrail): only prompts that carry directive-
    # relevant language the deterministic parse could not fully bind —
    # recorded refusals, or general-kind text hinting at targets/hypotheses.
    # A fully-bound directive (or a plain "explain" task) skips the turn;
    # placement (primitive intake) and disposal semantics are unchanged.
    from ..kb import PRICE_TARGET_HINTS
    from ..task_directive import _HYPOTHESIS_WORDS
    _task_blob = (task or "").lower()
    _hinted = any(h in _task_blob for h in PRICE_TARGET_HINTS) or any(
        w in _task_blob for w in _HYPOTHESIS_WORDS)
    _assessment_due = bool(task and task.strip()) and (
        bool(directive.refusals)
        or (directive.kind == "general" and _hinted))
    if _assessment_due:
        try:
            raw_assessment = await engine._call_llm(
                assessment_prompt(task, directive))
            st.llm_calls += 1
            parsed_assessment = narration_mod.coerce_turn(
                narration_mod.extract_json_object(raw_assessment))
            if parsed_assessment is None:
                assessment_note = "assessment_unparseable; deterministic parse stands"
            else:
                directive, rejects = dispose_assessment(directive, parsed_assessment)
                if rejects:
                    assessment_note = (
                        "assessment rejects: "
                        + "; ".join(f"{r['field']}: {r['reason']}" for r in rejects))
        except Exception as exc:
            assessment_note = f"assessment_unavailable: {type(exc).__name__}"
    plan = build_plan(directive)
    st.task_directive = directive.to_dict()
    st.task_plan = plan
    st.deterministic_state["task_directive"] = st.task_directive
    st.deterministic_state["task_plan"] = plan
    st.deterministic_state["assessment_note"] = assessment_note

    st.seed_plan = seed_evidence_plan(task, scenario)
    st.deterministic_state["seeded_plan"] = st.seed_plan
    st.task_workflow = build_task_workflow(task, scenario,
                                            kind_override=plan["kind"])
    st.deterministic_state["task_workflow"] = st.task_workflow
    workflow_block = (
        f"REQUIRED WORKFLOW FOR THIS TASK (kind: {st.task_workflow['kind']} — "
        "walk it in order; this is the shape a complete answer takes):\n"
        + "\n".join(f"- {step}" for step in st.task_workflow["steps"])
        + f"\n{st.task_workflow['handoff']}\n"
    )
    st.workflow_block = workflow_block  # type: ignore[attr-defined]
    # COMPREHENSION is a real governed traversal, but it is deterministic:
    # envelope normalization, task classification and evidence framing do
    # not spend an additional model turn before the agent's first proposal.
    # This keeps the hard-gate and narration budgets unambiguous.
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTERPRETATION
    )
    st.passes_per_loop["comprehension"] += 1
    st.comprehension = default_comprehension(
        task, scenario, st.seed_plan, "deterministic",
    )
    st.deterministic_state["comprehension"] = st.comprehension
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.FRAMING
    )
    # Framing is deterministic in the current runtime: the task workflow and
    # acceptance material have been constructed above.  Close it explicitly;
    # leaving the observation open would make the next ENTER_LOOP illegal.
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )
    st.controller = st.controller.record_loop_visit(
        "comprehension", 1,
        ("intake", "interpretation", "framing"), True,
    )
    return st, None


async def run_evidence(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple | None:
    """EVIDENCE loop: narrate#1 + budgeted follow-ups. Ends early on empty
    calls or a failed round (candidate stands; validation decides)."""
    st.loop_tag = "evidence"
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.ENTER_LOOP, NestedLoop.EVIDENCE
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.SOURCING
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ACQUISITION
    )
    # First-turn prompt: composed once by kb.compose_narrate1_prompt.
    user_prompt_1 = kb.compose_narrate1_prompt(
        controller=st.controller,
        loop="evidence", sub_loop="acquisition",
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
    parsed_1 = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_1))
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
    st.controller = st.controller.record_loop_visit(
        "evidence", st.passes_per_loop["evidence"],
        ("sourcing", "acquisition", "verification"),
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
    passed, missing = narration_mod.validate_final_turn(
        st.parsed_current, st.controller.phase_coverage, scenario=ctx.scenario,
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
    for tool in st_chain["missing"]:
        if tool == "calc.discipline.audit":
            # The audit is validation-homed: the repair turn can pull it.
            missing.append(
                "statistical chain: calc.discipline.audit never attempted — "
                "call it now (validation home) before finalizing"
            )
        else:
            # Every other chain link lives in an earlier loop and cannot be
            # pulled from validation: record the unwalked link honestly so
            # the repair budget terminates instead of demanding uncallable
            # tools from the agent.
            missing.append(
                f"statistical chain: {tool} never attempted in its loop — "
                "recorded unwalked; the track cannot pass this cycle"
            )
    # Task-conformance (core-owned): a target-bearing directive must surface
    # its verdict — the band membership / scenario citation or its refusal —
    # before the gate passes. This closes the prompt→answer loop: the final
    # must answer the question the directive parsed, not merely cite tools.
    if (st.task_plan is not None
            and (st.task_plan.get("targets") or st.task_plan.get("invalidations"))
            and not narration_mod.has_directive_verdict(st.parsed_current)):
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
    if st.validation_retries_used >= VALIDATION_RETRY_PASSES:
        return await run_validation_terminal(engine, ctx, st, list(missing))
    st.repairs_sent += 1
    st.validation_retries_used += 1
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
        next_phase=narration_mod.next_uncovered_phase(st.controller.phase_coverage),
    )
    try:
        raw_repair = await engine._call_llm(repair_prompt)
    except Exception as exc:
        log.exception("repair turn failed; ending validation")
        st.failure_kind = "narration_failed"
        st.failure_detail = f"{type(exc).__name__}: {exc}"
        return await run_runtime_failure(engine, ctx, st)
    st.llm_calls += 1
    st.passes_per_loop["validation"] = st.passes_per_loop.get("validation", 0) + 1
    parsed_repair = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_repair))
    if parsed_repair is None:
        log.warning("repair parse failed, ending validation")
        st.failure_kind = "parse_failed"
        st.failure_detail = "no JSON object in repair narration"
        return await run_runtime_failure(engine, ctx, st)
    st.parsed_current = parsed_repair
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
