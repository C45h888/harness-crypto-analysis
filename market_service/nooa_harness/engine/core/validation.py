"""VALIDATION loop — gate + one bounded retry, then the FSM terminal.

Owns the ``NestedLoop.VALIDATION`` body (GATE + RECOVERY) and the
deterministic checks that judge a cycle's merged interpretation:
``merge_interpretation`` folds the cycle's turns, ``validate_final_turn``
enforces the structural gate, ``check_scenario_equality`` and
``has_directive_verdict`` enforce scenario/task conformance, and
``_record_verdict_chosen`` freezes the adjudication decision on the ledger.

Extracted from ``core.reasoning`` (semantic separation by loop state):
``reasoning.py`` keeps EVIDENCE + REASONING; this module owns VALIDATION.
The public names are still re-exported from ``core.reasoning`` for
backward-compatible imports.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from . import context
from ..llm import narration_failure_kind
from ..schemas import coerce_turn, extract_json_object
from ..controller import next_uncovered_phase
from ..config import (
    SUMMARY_MIN_CHARS,
    LOOP_PASS_BUDGET,
    MAX_DISPATCHES_PER_PASS,
    MAX_SALVAGE_ENTRIES,
    VALIDATION_RETRY_PASSES,
)
from .context import (
    CycleRuntimeState,
    _mark_declared,
    freeze_chain,
    run_runtime_failure,
    run_validation_terminal,
)
from ..fsm import GovernanceEventKind
from .. import kb
from ..kb import compose_repair_prompt
from ..scenario_contract import render_status_block
from ..gates import (
    check_scenario_equality,
    has_directive_verdict,
    merge_interpretation,
    validate_final_turn,
)
from .. import gates
from ..ledger import LedgerKind, record_entry, record_finding, render_ledger
from ..loop_states import LoopStep, NestedLoop, SubLoop
from ..principles import evaluate_principles as _evaluate_principles
from .chain import chain_completion

log = logging.getLogger(__name__)

__all__ = [
    "run_validation",
    "merge_interpretation",
    "check_scenario_equality",
    "has_directive_verdict",
    "validate_final_turn",
]


def _record_verdict_chosen(
    st: CycleRuntimeState, ctx: Any, *, outcome: str,
    judged: dict[str, Any] | None = None,
) -> None:
    """Record the adjudication decision on the central ledger (best-effort).

    Reads the merged probabilities + frame basis and freezes the decision
    receipt (chosen, probabilities, basis ids, frame basis, terminal
    context) as OBSERVATION `verdict-chosen`. Gives cross-cycle verdict
    memory through the prior-artifact recall path and the artifact a
    machine-readable decision. Never blocks: judged may lack probabilities
    (unevaluable bypass) — then the record names the bypass instead.
    """
    try:
        from .chain import build_hypothesis_frame as _bhf_vc
        # A salvaged gate judges a REDUCED evidence view; record that same
        # view so the ledger never claims a settlement the gate did not
        # actually approve.
        if judged is None:
            judged = merge_interpretation(st.turn_log)
        probs = judged.get("hypothesis_probabilities")
        _dir = getattr(st, "task_directive", None)
        frame = _bhf_vc(
            ctx.task,
            _dir if isinstance(_dir, dict) else None,
            st.accumulated_tool_results,
        )
        if isinstance(probs, dict):
            payload = {
                "chosen": probs.get("chosen"),
                "probabilities": {
                    h: {"probability": (probs.get(h) or {}).get("probability"),
                        "p": (probs.get(h) or {}).get("p"),
                        "basis_path": (probs.get(h) or {}).get("basis_path")}
                    for h in ("H0", "H1")
                },
                "frame_basis": frame.get("basis"),
                "outcome": outcome,
            }
        else:
            payload = {"chosen": None, "outcome": outcome,
                       "note": "no probabilities judged (unevaluable bypass or empty final)",
                       "frame_basis": frame.get("basis")}
        segment = getattr(ctx, "task_id", None)
        wake_id = getattr(getattr(ctx, "wake", None), "wake_id", "") or ""
        entry = record_entry(
            st.deterministic_state, LedgerKind.OBSERVATION,
            {"stage": "verdict-chosen", **payload},
            evidence_refs=(wake_id,),
            segment=segment,
        )
        st.ledger_stage_ids.append(f"verdict-chosen#{entry.seq}")
    except Exception:
        log.exception("verdict-chosen record failed; continuing")

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
    # Adjudication recall (spec §4, stage-filtered): the decision rows
    # render FIRST so truncation can never evict them, then the stage
    # observations. Pure ledger read — the agent weighs recalled conclusions
    # plus accumulated proof (never tool re-calls: identical-args re-dispatch
    # is suppressed).
    try:
        _vseg = getattr(ctx, "task_id", None)
        _decision = render_ledger(
            st.deterministic_state, (LedgerKind.OBSERVATION,),
            segment=_vseg, stage=("verdict-basis", "verdict-chosen"),
            budget=2_000,
        )
        _stages = render_ledger(
            st.deterministic_state, (LedgerKind.OBSERVATION,),
            segment=_vseg, stage=("base", "refined"),
            budget=1_500,
        )
        st.adjudication_ledger_block = "\n".join(
            part for part in (_decision, _stages) if part
        )
        st.capability_log.append({
            "capability": "engine.ledger.adjudication_recall",
            "scope": {"symbol": engine.symbol, "venue": engine.venue},
            "result": "ok",
            "detail": {"segment": _vseg},
        })
    except Exception:
        log.exception("adjudication recall failed; weighing without ledger")
        st.adjudication_ledger_block = ""
    # Judge the MERGED interpretation across the cycle's turns: the staged
    # loop legitimately ends on bare declaration turns (the contract allows
    # null interpretation fields while tools are pending), and content the
    # model produced earlier in THIS cycle must not be zeroed out because the
    # last turn only declared a phase.
    judged = merge_interpretation(st.turn_log)
    _validation_findings: list[str] = []
    _segment = getattr(ctx, "task_id", None)
    _content_verdict = gates.evaluate(gates.GateKind.CONTENT, {
        "judged": judged,
        "coverage": st.controller.phase_coverage,
        "scenario": ctx.scenario,
        "scenario_status": st.controller.scenario_state(),
        "scenario_refusal_reason": st.controller.scenario_refusal_reason(),
        "accumulated": st.accumulated_tool_results,
        "findings": _validation_findings,
    })
    gates.record_verdict(_content_verdict, st, segment=_segment)
    passed = _content_verdict.passed
    missing = list(_content_verdict.facts_dict().get("missing") or ())
    # F9 relaxed: presentational notes ride the artifact as findings (same
    # pattern as principle_findings) — they inform, they never block.
    for _finding in _validation_findings:
        record_finding(st.deterministic_state, "validation", str(_finding),
                       segment=_segment)
    # Scenario equality (validation-owned): the agent's reachability verdict
    # obeys the deterministic derivation; output keeps the block verbatim.
    missing = list(missing) + check_scenario_equality(judged, st, ctx)
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
    for _gap in chain_gaps:
        record_finding(st.deterministic_state, "chain_gap", str(_gap),
                       segment=_segment)
    # Principles at the gate: advisory findings land beside the verdict —
    # they inform the artifact, they never join missing[] (guards, not gates).
    for _finding in _evaluate_principles(
        controller=st.controller, plan=st.task_plan, turn=judged,
        reason_position=st.reason_position,
    ):
        record_finding(
            st.deterministic_state, "principle", str(_finding.detail),
            detail={"principle": _finding.principle}, segment=_segment)
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
    _chain_verdict = gates.evaluate(gates.GateKind.CHAIN, {
        "missing": list(st_chain["missing"]),
        "gaps": list(st.deterministic_state.get("chain_gaps") or ()),
        "halted": halt if halted else None,
    })
    gates.record_verdict(_chain_verdict, st, segment=_segment)
    passed = passed and not st_chain["missing"] and not halted
    # FORCED FINAL — the missing link (config budget comment promised
    # "narrate#1 + tool follow-ups + repairs + forced-final" but no such turn
    # existed). Live probe 2026-09-28: 12 in-loop turns returned tool_calls
    # only — summary/evidence/hypothesis empty in EVERY turn, while the same
    # model with a dedicated finalize request returns the complete final
    # object. So spend one tool-free finalize turn (small context, field
    # shapes inline) and let this gate judge it. Runs BEFORE salvage and only
    # on a failed gate: a cycle that passed at the retry ceiling must not
    # pay an extra turn.
    if (not passed
            and st.validation_retries_used >= VALIDATION_RETRY_PASSES
            and not st.forced_final_sent):
        st.forced_final_sent = True
        if await _forced_final_turn(engine, ctx, st, list(missing)) is not None:
            return await run_validation(engine, ctx, st)
        # The finalize attempt yielded nothing usable; fall through to
        # salvage — the pre-existing turn log may still be salvable.
    # COMPLETE-THEN-DROP SALVAGE (terminal boundary). Doctrine: engine
    # completes, never kills, model-derived state — integral citations
    # (paths resolving to store truth) have nulls filled and mismatches
    # corrected; only unbacked entries are REMOVABLE, never fatal. The
    # repair budget and the forced-final turn are spent by now; if every
    # remaining miss is that one entry-shape class, complete those entries
    # deterministically and re-judge once. Any other miss
    # (phase/hypothesis/confidence/roots/ΔP/scenario/
    # directive/chain/halt) vetoes — salvage never papers over a real gap.
    # The re-judge is the safety net: completion can delete the ΔP
    # citation or the last distinct root, and then the terminal stands.
    salvaged: list[dict[str, Any]] = []
    if (not passed
            and st.validation_retries_used >= VALIDATION_RETRY_PASSES
            and st.forced_final_sent):
        _salvaged_view, _remaining, _dropped = salvage_entry_shape_misses(
            judged, missing,
            coverage=st.controller.phase_coverage,
            scenario=ctx.scenario,
            scenario_status=st.controller.scenario_state(),
            scenario_refusal_reason=st.controller.scenario_refusal_reason(),
            accumulated=st.accumulated_tool_results,
            task_plan=st.task_plan,
        )
        if _salvaged_view is not None:
            judged, missing, passed = _salvaged_view, _remaining, True
            salvaged = _dropped
            _record_evidence_salvage(st, ctx, salvaged)
    if passed:
        st.final_validation = {"passed": True, "missing": []}
        if salvaged:
            st.final_validation["salvaged"] = list(salvaged)
        st.finalize_now = True
        _record_scenario_evaluation(st)
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
        # Canonical receipt: the final passed the GATE sub-loop.
        gates.close_loop(
            st, loop=NestedLoop.VALIDATION, sub_loop=SubLoop.GATE,
            steps=(LoopStep.VALIDATE, LoopStep.ADJUDICATE,
                   LoopStep.CLASSIFY_FINDINGS),
            payload={"retries_used": st.validation_retries_used},
            segment=_segment,
        )
        _record_verdict_chosen(
            st, ctx,
            outcome="settled_salvaged" if salvaged else "settled",
            judged=judged)
        ctx.controller = st.controller
        ctx.reasoned = st.to_reasoned()
        return None
    if st.validation_retries_used >= VALIDATION_RETRY_PASSES:
        _record_verdict_chosen(st, ctx, outcome="validation_failed")
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
        # Canonical receipt: the gate REFUSED the candidate and recovery opened.
        gates.close_loop(
            st, loop=NestedLoop.VALIDATION, sub_loop=SubLoop.RECOVERY,
            disposition=gates.GateDisposition.REFUSED,
            steps=(LoopStep.DIAGNOSE, LoopStep.STEER, LoopStep.REVALIDATE),
            payload={"missing": [str(m)[:200] for m in list(missing)[:8]]},
            segment=_segment,
        )
    scenario_steer = st.controller.scenario_steer()
    # Adjudication layer (spec §5): the repair turn weighs before it fixes —
    # frame + recalled verdict-basis + deterministic data reads ride the steer.
    _adj_block = ""
    _dc_block = ""
    try:
        from .chain import build_hypothesis_frame as _bhf_rep
        from .chain import hypothesis_steer as _hs_rep
        from ..tool_schemas import build_data_check_block as _dc_rep
        _dir_rep = getattr(st, "task_directive", None)
        _adj_block = kb.compose_adjudication_block(
            frame_steer=_hs_rep(_bhf_rep(
                ctx.task,
                _dir_rep if isinstance(_dir_rep, dict) else None,
                st.accumulated_tool_results,
            )),
            memory_block=getattr(st, "adjudication_ledger_block", "") or "",
        )
        _dc_block = _dc_rep(st.accumulated_tool_results) or ""
    except Exception:
        log.exception("adjudication block build failed; repairing without it")
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
        adjudication_block=_adj_block,
        data_checks_block=_dc_block,
        scenario_contract_block=render_status_block(
            getattr(ctx, "scenario_contract", None), st.controller),
    )
    try:
        raw_repair = await engine._call_llm(repair_prompt)
        context.record_tier(st)
    except Exception as exc:
        log.exception("repair turn failed; dispatching to the failure node")
        # Cause-carrying kinds: Signal B stall verdicts route as their
        # stall kind (same prompt retry — repair prompts are already
        # missing-focused with a bounded digest, nothing to shrink).
        st.failure_kind = narration_failure_kind(exc)
        st.failure_detail = f"{type(exc).__name__}: {exc}"
        # Bounded RETRY: substrate dispatches the narration worker, which
        # re-attempts the repair call (worker owns the transport).
        raw_repair = await context.recover_narration_call(
            engine, ctx, st, repair_prompt,
        )
        if raw_repair is None:
            return await run_runtime_failure(engine, ctx, st)
        context.record_tier(st)
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
    # Adjudication layer rides the last-chance turn too (spec §5).
    _ff_adj = ""
    _ff_dc = ""
    try:
        from .chain import build_hypothesis_frame as _bhf_ff
        from .chain import hypothesis_steer as _hs_ff
        from ..tool_schemas import build_data_check_block as _dc_ff
        _dir_ff = getattr(st, "task_directive", None)
        _ff_adj = kb.compose_adjudication_block(
            frame_steer=_hs_ff(_bhf_ff(
                ctx.task,
                _dir_ff if isinstance(_dir_ff, dict) else None,
                st.accumulated_tool_results,
            )),
            memory_block=getattr(st, "adjudication_ledger_block", "") or "",
        )
        _ff_dc = _dc_ff(st.accumulated_tool_results) or ""
    except Exception:
        log.exception("forced-final adjudication build failed; finalizing without it")
    for attempt in (1, 2):
        prompt = kb.compose_forced_final_prompt(
            task=ctx.task, missing=missing, evidence_digest=digest,
            scenario=ctx.scenario, strict=(attempt > 1),
            adjudication_block=_ff_adj, data_checks_block=_ff_dc,
            scenario_contract_block=render_status_block(
                getattr(ctx, "scenario_contract", None), st.controller),
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


def _record_scenario_evaluation(st: CycleRuntimeState) -> None:
    """Freeze the contract-bound evaluation on the artifact state (idempotent).

    The SAME outcome validation bound to is what the artifact records — the
    target/horizon actually dispatched, the tri-state, the refusal reason,
    and (when declared) the divergence. Operators never have to reconstruct
    "which 104.80 was answered" from a capability-log scan.
    """
    controller = st.controller
    contract = getattr(controller, "scenario_contract", None)
    outcome = None
    read = getattr(controller, "scenario_outcome", None)
    if callable(read):
        try:
            outcome = read()
        except Exception:
            outcome = None
    if outcome is None and contract is None:
        return
    args = (outcome.args
            if outcome is not None and isinstance(outcome.args, Mapping)
            else {})
    try:
        state = controller.scenario_state().value
    except Exception:
        state = None
    st.deterministic_state["scenario_evaluation"] = {
        "contract_id": getattr(contract, "contract_id", None),
        "state": state,
        "target_price": args.get("target_price"),
        "horizon": args.get("horizon"),
        "refused": bool(getattr(outcome, "refused", False)),
        "refusal_reason": getattr(outcome, "refusal_reason", None),
    }
    intent = getattr(controller, "scenario_intent", None)
    if isinstance(intent, Mapping) and intent.get("mode") == "revision":
        st.deterministic_state["scenario_divergence"] = dict(intent)










#: The ONLY miss class salvage may consume: a citation that names no number
#: (path present, value null, non-refusal). Any other string vetoes.
_ENTRY_SHAPE_MISS = re.compile(r"^evidence\[(\d+)\] needs path\+value\b")
_EVIDENCE_VALUE_MISMATCH = re.compile(r"^evidence value mismatch on\b")


def salvage_entry_shape_misses(
    judged: dict[str, Any],
    missing: list[str],
    *,
    coverage: dict[str, set[str]],
    scenario: dict[str, Any] | None,
    scenario_status: Any,
    scenario_refusal_reason: str | None,
    accumulated: dict[str, Any] | None,
    task_plan: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
    """Complete-then-drop rescue at the validation terminal (pure).

    Fires ONLY when EVERY miss is an entry-shape miss — the cycle is
    complete in every structural, contract, scenario, chain and verdict
    sense and only unattributed citations block it. Integral entries (paths
    resolving to store truth) are COMPLETED first (nulls filled, mismatches
    corrected via ``synthesize.complete_evidence`` — engine completes,
    never kills, model-derived state); only unbacked entries are dropped.
    Then re-judges ONCE.

    Returns ``(reduced_view, remaining_missing, records)`` where records
    carry ``filled:``/``corrected:`` markers and dropped index/path rows
    for the audit trail. ``reduced_view`` is None when salvage is not
    permitted or the re-judge fails — the caller then keeps the terminal
    with the ORIGINAL missing list. Nothing worded is rewritten.
    """
    from .synthesize import complete_evidence
    indices: list[int] = []
    for item in missing:
        hit = _ENTRY_SHAPE_MISS.match(str(item))
        if hit is not None:
            indices.append(int(hit.group(1)))
            continue
        # Echo mismatches (path-correct, value-wrong) are completable too:
        # whole-list completion corrects them from store truth, so they
        # join the trigger without an index.
        if _EVIDENCE_VALUE_MISMATCH.match(str(item)) is not None:
            continue
        return None, list(missing), []
    if not indices and not any(
            _EVIDENCE_VALUE_MISMATCH.match(str(item)) is not None
            for item in missing):
        return None, list(missing), []
    if len(indices) > MAX_SALVAGE_ENTRIES:
        return None, list(missing), []
    evidence = judged.get("evidence")
    if not isinstance(evidence, list) or any(i >= len(evidence) for i in indices):
        return None, list(missing), []
    completed, filled, corrected, comp_dropped = complete_evidence(
        evidence, accumulated)
    records: list[dict[str, Any]] = (
        [{"action": "filled", "path": str(m).split("filled:", 1)[-1]}
         for m in filled]
        + [{"action": "corrected", "path": str(m).split("corrected:", 1)[-1]}
           for m in corrected]
    )
    for index in indices:
        entry = evidence[index]
        records.append({
            "index": index,
            "path": (str(entry.get("path") or "")
                     if isinstance(entry, dict) else "<non-object>"),
        })
    reduced = {**judged, "evidence": completed}
    # Task-conformance lives OUTSIDE validate_final_turn, so the re-judge
    # cannot see it: veto if the completion removed the directive verdict
    # (a null-valued citation still counts for has_directive_verdict).
    if (task_plan is not None
            and (task_plan.get("targets") or task_plan.get("invalidations"))
            and has_directive_verdict(judged)
            and not has_directive_verdict(reduced)):
        return None, list(missing), []
    _findings: list[str] = []
    passed, _remaining = validate_final_turn(
        reduced, coverage, scenario=scenario,
        scenario_status=scenario_status,
        scenario_refusal_reason=scenario_refusal_reason,
        accumulated=accumulated, findings=_findings)
    if not passed:
        # Completion broke another citation contract (ΔP, distinct
        # roots, directive, …): do not ship a half-salvaged view.
        return None, list(missing), []
    return reduced, [], records


def _record_evidence_salvage(
    st: CycleRuntimeState, ctx: Any, dropped: list[dict[str, Any]],
) -> None:
    """Audit-trail the complete-then-drop rescue (ledger OBSERVATION + state).

    Records carry ``filled:``/``corrected:`` markers and dropped index/path
    rows — what the engine completed vs removed, never model wording.
    Best-effort, like every ledger writer on this path: a record failure must
    never convert a settled cycle back into a terminal.
    """
    try:
        segment = getattr(ctx, "task_id", None)
        wake_id = getattr(getattr(ctx, "wake", None), "wake_id", "") or ""
        entry = record_entry(
            st.deterministic_state, LedgerKind.OBSERVATION,
            {"stage": "evidence-salvage",
             "reason": ("entry-shape rescue: integral values completed "
                          "from store truth, unbacked entries removed"),
             "dropped": list(dropped)},
            evidence_refs=(wake_id,), segment=segment)
        st.ledger_stage_ids.append(f"evidence-salvage#{entry.seq}")
        record_finding(
            st.deterministic_state, "evidence_salvaged",
            f"salvaged {len(dropped)} citation(s) at the validation terminal",
            detail={"dropped": list(dropped)}, segment=segment)
    except Exception:
        log.exception("evidence salvage record failed; continuing")


