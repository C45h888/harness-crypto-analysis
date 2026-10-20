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
    MAX_MERGED_EVIDENCE,
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
from ..scenario_contract import (
    numbers_equal,
    render_status_block,
    scenario_args_match,
)
from .. import receipts
from ..ledger import LedgerKind, record_entry, render_ledger
from ..loop_states import LoopStep, NestedLoop, SubLoop, ReceiptDisposition
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
    passed, missing = validate_final_turn(
        judged, st.controller.phase_coverage, scenario=ctx.scenario,
        scenario_status=st.controller.scenario_state(),
        scenario_refusal_reason=st.controller.scenario_refusal_reason(),
        accumulated=st.accumulated_tool_results,
        findings=_validation_findings,
    )
    # F9 relaxed: presentational notes ride the artifact as findings (same
    # pattern as principle_findings) — they inform, they never block.
    if _validation_findings:
        st.deterministic_state.setdefault("validation_findings", []).extend(
            [str(_f) for _f in _validation_findings])
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
    # REMOVAL-ONLY SALVAGE (terminal boundary). Doctrine: "narration carries
    # zero numbers the ledger cannot back" — so a citation with a null value
    # on a non-refusal path is REMOVABLE, not fatal. The repair budget and the
    # forced-final turn are spent by now; if every remaining miss is that one
    # entry-shape class, drop those entries deterministically and re-judge
    # once. Any other miss (phase/hypothesis/confidence/roots/ΔP/scenario/
    # directive/chain/halt) vetoes — salvage never papers over a real gap.
    # The re-judge is the safety net: removing an entry can delete the ΔP
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
        receipts.emit_receipt(
            st, loop=NestedLoop.VALIDATION, sub_loop=SubLoop.GATE,
            steps=(LoopStep.VALIDATE, LoopStep.ADJUDICATE,
                   LoopStep.CLASSIFY_FINDINGS),
            payload={"retries_used": st.validation_retries_used},
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
        receipts.emit_receipt(
            st, loop=NestedLoop.VALIDATION, sub_loop=SubLoop.RECOVERY,
            disposition=ReceiptDisposition.REFUSED,
            steps=(LoopStep.DIAGNOSE, LoopStep.STEER, LoopStep.REVALIDATE),
            payload={"missing": [str(m)[:200] for m in list(missing)[:8]]},
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
      evidence      — union, order-preserving, deduped BY PATH: a later
                      non-null value supersedes an earlier null (a repair
                      can finally fix an entry); distinct non-null values
                      stay visible as contradictions. Bounded by
                      MAX_MERGED_EVIDENCE (a per-turn cap cannot bound a
                      union across turns)
      confidence    — LAST non-null (revision wins)
      limitations   — union of strings
      model_separation — LAST non-null (revision wins)
      scenario / forward_scenario / hypothesis_evidence /
                      hypothesis_probabilities — LAST non-null
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
        "hypothesis_probabilities": None,
    }
    seen_evidence: dict[str, int] = {}
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
                path_key = str(entry.get("path") or "").strip()
                if not path_key:
                    # Pathless entries are gate material (they fail shape);
                    # never let them collide with one another.
                    merged["evidence"].append(entry)
                    continue
                idx = seen_evidence.get(path_key)
                if idx is None:
                    seen_evidence[path_key] = len(merged["evidence"])
                    merged["evidence"].append(entry)
                    continue
                prev = merged["evidence"][idx]
                prev_value, new_value = prev.get("value"), entry.get("value")
                if prev_value is None and new_value is not None:
                    # null -> value: a later turn REFINED the citation.
                    # Replace in place so a repair can actually fix an entry.
                    # The old (path, value) key appended a corrected twin and
                    # left the broken one unremovable, so the gate re-refused
                    # the same index on every pass (live 2026-10-20: 2 repairs
                    # could not clear 4 null-valued citations).
                    merged["evidence"][idx] = entry
                elif str(prev_value) == str(new_value):
                    # Same claim: a later reading wins when it has one.
                    if str(entry.get("interpretation") or "").strip():
                        merged["evidence"][idx] = entry
                else:
                    # Two DIFFERENT non-null values for one path is a real
                    # contradiction: keep both so the echo gate still sees it.
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
        for key in ("scenario", "forward_scenario", "hypothesis_evidence",
                    "hypothesis_probabilities"):
            if turn.get(key) is not None:
                merged[key] = turn[key]
    if len(merged["evidence"]) > MAX_MERGED_EVIDENCE:
        log.warning("capping merged evidence %d → %d entries",
                    len(merged["evidence"]), MAX_MERGED_EVIDENCE)
        merged["evidence"] = merged["evidence"][:MAX_MERGED_EVIDENCE]
    return merged

def _bound_from_controller(controller: Any, ctx: Any) -> tuple[Any, Any]:
    """Effective (target, horizon) with legacy fallbacks (pure)."""
    effective = getattr(controller, "effective_scenario", None)
    if callable(effective):
        try:
            return effective()
        except Exception:
            pass
    if isinstance(ctx.scenario, dict):
        return ctx.scenario.get("target_price"), ctx.scenario.get("horizon")
    return None, None


def _derive_verdict(
    payload: Any, st: CycleRuntimeState, det_verdict: Any,
) -> tuple[str, str]:
    gate = st.deterministic_state.get("gate") or {}
    return det_verdict(
        payload, st.capability_log,
        str(gate.get("status") or "provisional"),
        list(gate.get("reasons") or []),
    )


def check_scenario_equality(
    judged: dict[str, Any], st: CycleRuntimeState, ctx: Any,
) -> list[str]:
    """Scenario contract equality (validation-owned, deterministic).

    Two planes, ONE oracle:

    * Contract plane (a ScenarioContract and/or a real dispatch outcome
      exists): the LAST genuine dispatch must carry the EFFECTIVE bound
      target/horizon. A refusal only answers the asked question when it
      refuses THAT target — an explicit wrong-target refusal is a finding,
      not an unevaluable escape. The final block must echo the bound
      target/horizon; a declared revision must surface its divergence.
      Verdict derivation reads the SAME outcome payload that was bound.
    * Legacy plane (no contract/outcome — direct construction/tests): the
      pre-contract behavior (accumulated payload + log) is preserved so
      older callers keep working.

    Returns missing[]; output keeps the agent's block verbatim.
    """
    try:
        from ..controller import ScenarioEvalStatus as _SES
        from ..controller import scenario_verdict as _det_verdict
    except Exception:
        return []
    try:
        if ctx.scenario is None:
            return []
        controller = st.controller
        contract = getattr(controller, "scenario_contract", None)
        outcome = None
        outcome_read = getattr(controller, "scenario_outcome", None)
        if callable(outcome_read):
            try:
                outcome = outcome_read()
            except Exception:
                outcome = None
        state = controller.scenario_state()
        scen = judged.get("scenario")

        if contract is not None or outcome is not None:
            eff_t, eff_h = _bound_from_controller(controller, ctx)
            recorded_args = (outcome is not None
                             and isinstance(getattr(outcome, "args", None), Mapping)
                             and bool(outcome.args))
            if state is _SES.REFUSED:
                if (recorded_args
                        and not scenario_args_match(outcome.args, eff_t, eff_h)):
                    return [
                        "scenario refused "
                        f"{getattr(outcome, 'args', None)!r} != bound target "
                        f"{eff_t!r} @ {eff_h!r} — a refusal answers the asked "
                        "question only when it refuses the bound target "
                        "(dispatch the contract target exactly; a declared "
                        "revision is required to change it)"
                    ]
                return []
            if not isinstance(scen, dict):
                return []  # the structural check names the missing block
            if state is _SES.NOT_CALLED:
                # No dispatch, no data: a verdict (reachable, not_reachable,
                # OR unevaluable) is fabrication. The structural gate names
                # the missing block; this names the missing DISPATCH.
                return [
                    "scenario never dispatched: call calc.scenario.evaluate "
                    f"with the bound target {eff_t!r} @ {eff_h!r} before "
                    "declaring a scenario verdict"
                ]
            if (recorded_args
                    and not scenario_args_match(outcome.args, eff_t, eff_h)):
                return [
                    "scenario evaluated "
                    f"{getattr(outcome, 'args', None)!r} != bound target "
                    f"{eff_t!r} @ {eff_h!r} — evaluate the bound target "
                    "(or declare a permitted revision; answering a different "
                    "question is not a silent option)"
                ]
            if (scen.get("target_price") not in (None, "")
                    and not numbers_equal(scen.get("target_price"), eff_t)):
                return [
                    f"scenario.target_price {scen.get('target_price')!r} != "
                    f"bound target {eff_t!r} — echo the evaluated contract target"
                ]
            if (scen.get("horizon") not in (None, "")
                    and str(scen.get("horizon")).strip() != str(eff_h)):
                return [
                    f"scenario.horizon {scen.get('horizon')!r} != bound "
                    f"horizon {eff_h!r} — echo the contract horizon"
                ]
            intent = getattr(controller, "scenario_intent", None)
            if isinstance(intent, Mapping) and intent.get("mode") == "revision":
                divergence = scen.get("divergence")
                if (not isinstance(divergence, dict)
                        or not str(divergence.get("reason") or "").strip()):
                    return [
                        "scenario.divergence {asked_target, reason} required: "
                        "a declared revision must surface the asked target "
                        "and why the question changed"
                    ]
            verdict = str(scen.get("verdict") or "")
            if verdict == "unevaluable":
                return []  # refused-shape verdict with evaluated tooling is
                # caught by the structural unevaluable rules, not here
            payload = (outcome.payload if outcome is not None else
                       (st.accumulated_tool_results or {}).get(
                           "calc.scenario.evaluate"))
            derived, reason = _derive_verdict(payload, st, _det_verdict)
            want = {"invalidated": "not_reachable",
                    "validated": "reachable"}.get(derived)
            if want is None:
                if verdict not in ("reachable", "not_reachable"):
                    return [
                        "scenario verdict must be reachable|not_reachable: the "
                        f"tool evaluated ({reason}) — unevaluable is forbidden "
                        "with tool data"
                    ]
                return []
            if verdict != want:
                return [
                    f"scenario verdict must be {want!r}: deterministic "
                    f"derivation says {derived} ({reason}) — the agent's "
                    "block keeps its words, but the verdict obeys the derivation"
                ]
            return []

        # --- Legacy plane (no contract, no classified outcome) -------------
        if state is _SES.REFUSED:
            return []
        if not isinstance(scen, dict):
            return []
        verdict = str(scen.get("verdict") or "")
        if verdict == "unevaluable":
            return []
        derived, reason = _derive_verdict(
            (st.accumulated_tool_results or {}).get("calc.scenario.evaluate"),
            st, _det_verdict,
        )
        want = {"invalidated": "not_reachable",
                "validated": "reachable"}.get(derived)
        if want is None:
            if verdict not in ("reachable", "not_reachable"):
                return [
                    "scenario verdict must be reachable|not_reachable: the tool "
                    f"evaluated ({reason}) — unevaluable is forbidden with tool data"
                ]
            return []
        if verdict != want:
            return [
                f"scenario verdict must be {want!r}: deterministic derivation says "
                f"{derived} ({reason}) — the agent's block keeps its words, "
                "but the verdict obeys the derivation"
            ]
        return []
    except Exception:
        log.exception("scenario equality check failed open")
        return []

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


#: The ONLY miss class salvage may consume: a citation that names no number
#: (path present, value null, non-refusal). Any other string vetoes.
_ENTRY_SHAPE_MISS = re.compile(r"^evidence\[(\d+)\] needs path\+value\b")


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
        if hit is None:
            return None, list(missing), []
        indices.append(int(hit.group(1)))
    if not indices or len(indices) > MAX_SALVAGE_ENTRIES:
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
    # cannot see it: veto if the dropped entries were carrying the directive
    # verdict (a null-valued citation still counts for has_directive_verdict).
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
        # Dropping the entry broke another citation contract (ΔP, distinct
        # roots, directive, …): do not ship a half-salvaged view.
        return None, list(missing), []
    return reduced, [], dropped


def _record_evidence_salvage(
    st: CycleRuntimeState, ctx: Any, dropped: list[dict[str, Any]],
) -> None:
    """Audit-trail the removal-only rescue (ledger OBSERVATION + state).

    Best-effort, like every ledger writer on this path: a record failure must
    never convert a settled cycle back into a terminal.
    """
    try:
        st.deterministic_state["evidence_salvaged"] = list(dropped)
        segment = getattr(ctx, "task_id", None)
        wake_id = getattr(getattr(ctx, "wake", None), "wake_id", "") or ""
        entry = record_entry(
            st.deterministic_state, LedgerKind.OBSERVATION,
            {"stage": "evidence-salvage",
             "reason": "null value on non-refusal path; removal-only rescue",
             "dropped": list(dropped)},
            evidence_refs=(wake_id,), segment=segment)
        st.ledger_stage_ids.append(f"evidence-salvage#{entry.seq}")
    except Exception:
        log.exception("evidence salvage record failed; continuing")


def validate_final_turn(
    parsed: dict[str, Any],
    coverage: dict[str, set[str]],
    scenario: dict[str, Any] | None = None,
    *,
    scenario_status: "ScenarioEvalStatus | None" = None,
    scenario_refusal_reason: str | None = None,
    accumulated: dict[str, Any] | None = None,
    findings: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Phase-aware final gate: depth is structural, not advisory.

    A FINAL turn passes only when every required phase family has at least
    one executed tool (P1 OFI, P2 AD/fits, P3 market correlation, P5
    derived ΔP), the hypothesis carries H0, the P4 explanation meets the
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
        missing.append(
            "hypothesis.H0 required: frame H0/H1 in line with the hypothesis frame"
        )
    summary = parsed.get("summary") or ""
    if not isinstance(summary, str) or len(summary.strip()) < SUMMARY_MIN_CHARS:
        # F9 structural/relaxed: thin prose is a finding, never a gate. A
        # presentational repair re-rolls every number and manufactures
        # recompute opportunities; only citation shape + echo truth block.
        _note = (
            f"P4 explanation thin ({len(summary.strip())}/{SUMMARY_MIN_CHARS} chars): "
            "say what the fits show AND why it is happening now"
        )
        if findings is not None:
            findings.append(_note)
        else:
            missing.append(_note)
    confidence = parsed.get("confidence")
    if confidence is not None and normalize_confidence(confidence) is None:
        missing.append(
            f"confidence must be one of low|medium|high (got {confidence!r}); "
            "hyphenated blends coerce conservatively (low-medium → low)"
        )
    roots: set[str] = set()
    evidence = parsed.get("evidence")
    # ONE shape authority, shared with the shipping filter
    # (output._assemble_authored → synthesize.classify_evidence). The gate
    # judges shape only (no frame); backing is assembly's fact-check.
    from .synthesize import (
        EVIDENCE_NO_PATH,
        EVIDENCE_NO_READING,
        EVIDENCE_NULL_VALUE,
        classify_evidence,
    )
    if isinstance(evidence, list):
        for idx, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                missing.append(f"evidence[{idx}] must be an object with path + interpretation")
                continue
            # F9 structural parse gate (deterministic, pre-resolver): an
            # unattributed number can never reach the artifact. Missing path
            # or null value is a gate here — except deterministic refusal
            # findings (`→ refusal`), which carry no numeric value by null
            # discipline. The echo check below judges path-correct +
            # value-wrong. Interpretation stays required (the reading is
            # what keeps prose grounded to the cited field).
            _shape = classify_evidence(entry)
            if _shape in (EVIDENCE_NO_PATH, EVIDENCE_NULL_VALUE):
                missing.append(
                    f"evidence[{idx}] needs path+value — cite the tool+field "
                    "you read (unattributed numbers never reach the artifact)"
                )
                continue
            if _shape == EVIDENCE_NO_READING:
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
                if (scenario_status is ScenarioEvalStatus.NOT_CALLED
                        and str(scen.get("verdict") or "") == "unevaluable"):
                    missing.append(
                        "scenario.verdict 'unevaluable' is forbidden without a "
                        "deterministic refusal — call calc.scenario.evaluate with "
                        "the contract target/horizon; a refusal is the ONLY "
                        "unevaluable path")
                if normalize_confidence(scen.get("probability")) is None:
                    missing.append(
                        "scenario.probability required: low|medium|high "
                        "(qualitative read of the exceedance + band + tape quality)"
                    )
                for field in ("target_price", "horizon", "required_ofi",
                              "exceedance"):
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
    # Slice E — value-echo verify (path-correct + value-wrong = missing).
    # Resolves through chain.resolve_basis_path (the shared citation resolver):
    # field-segmented, comparator-disjoint, skip-when-unresolvable. See that
    # function for the run-6 lesson.
    try:
        from .chain import resolve_basis_path as _resolve

        def _norm(_v: Any) -> str | None:
            if _v is None:
                return None
            try:
                from decimal import Decimal
                return str(Decimal(str(_v)))
            except Exception:
                return str(_v).strip() or None
        _ev = parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else []
        for _entry in _ev:
            if not isinstance(_entry, dict):
                continue
            _path = str(_entry.get("path") or "")
            _val = _entry.get("value")
            if _val is None or not _path:
                continue
            _head, _fname, _truth = _resolve(
                _path, accumulated if isinstance(accumulated, dict) else {})
            if _fname is None or _truth is None:
                continue
            if _norm(_val) != _norm(_truth):
                missing.append(
                    f"evidence value mismatch on {_path!r}: cited {_val!r} "
                    f"!= {_head} field {_fname!r} payload {_truth!r} — cite the exact "
                    "tool+field you read (comparator.* is the univariate baseline, "
                    "top-level is the multivariate fit; never swap them)"
                )
    except Exception:
        pass
    # Adjudication coherence (spec §6): the agent weighs each live hypothesis
    # probabilistically and chooses. The checker verifies COHERENCE — choice
    # follows probabilities follows tools — never truth. A wrong-but-coherent
    # vector passes by explicit scope decision. Unevaluable (refused) paths
    # bypass weighing entirely.
    try:
        from ..controller import ScenarioEvalStatus as _SES
        _refused_scen = (scenario is not None
                         and scenario_status is _SES.REFUSED)
    except Exception:
        _refused_scen = False
    # Unevaluable bypass: a refused scenario tool leaves no distribution to
    # weigh — the verdict carries the refusal finding instead of probabilities.
    _weigh_exempt = bool(_refused_scen)
    try:
        _probs = parsed.get("hypothesis_probabilities")
        _hyp = parsed.get("hypothesis")
        _needs_weigh = ((isinstance(_hyp, dict) and bool(str(_hyp.get("H0") or "").strip()))
                        and not _weigh_exempt)
        if _needs_weigh and not isinstance(_probs, dict):
            missing.append(
                "hypothesis_probabilities required: weigh H0/H1 "
                "(low|medium|high + exact basis_path each, chosen = argmax) — "
                "probabilities inform, never execute"
            )
        elif isinstance(_probs, dict) and _needs_weigh:
            from .chain import resolve_basis_path as _resolve_basis
            _RANK = {"low": 0, "medium": 1, "high": 2}

            def _acc_data(_v: Any) -> Any:
                if isinstance(_v, dict) and "tool" in _v and "data" in _v:
                    return _v.get("data")
                return _v
            _a = accumulated if isinstance(accumulated, dict) else {}
            _fitA = _acc_data(_a.get("calc.forward.fit"))
            _fitA = _fitA if isinstance(_fitA, dict) else {}
            _distA = _acc_data(_a.get("calc.forward.distribution"))
            _distA = _distA if isinstance(_distA, dict) else {}

            def _basis_truth(_path: str) -> tuple[str | None, Any]:
                # Shared citation resolver (chain-owned): adjudication can
                # never disagree with echo on what a path means.
                _head, _field, _truth = _resolve_basis(_path, _a)
                if _field is None:
                    return None, None
                return _field, _truth

            def _num(_v: Any) -> float | None:
                try:
                    return float(str(_v))
                except Exception:
                    return None
            _keys = ("H0", "H1")
            _score: dict[str, tuple[int, float]] = {}
            for _hk in _keys:
                _entry = _probs.get(_hk)
                if not isinstance(_entry, dict):
                    missing.append(
                        f"hypothesis_probabilities.{_hk} required: object with "
                        "probability low|medium|high + exact basis_path"
                    )
                    continue
                _penum = str(_entry.get("probability") or "").strip().lower()
                if _penum not in _RANK:
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.probability must be "
                        f"low|medium|high (got {_entry.get('probability')!r})"
                    )
                    continue
                _bpath = str(_entry.get("basis_path") or "")
                # F9 structural parse gate: unattributed adjudication numbers
                # never reach the artifact; resolver judges wrong paths below.
                if not _bpath.strip():
                    missing.append(
                        f"hypothesis_probabilities.{_hk} needs basis_path — cite "
                        "the accumulated tool field the weighing stands on"
                    )
                    continue
                _bval = _entry.get("basis_value")
                _fk, _ft = _basis_truth(_bpath)
                if _fk is None:
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.basis_path must resolve "
                        "to an accumulated tool field (head + field segments; "
                        "comparator.* is the baseline, top-level the multivariate)"
                    )
                    continue
                if _bval is not None and _norm(_bval) != _norm(_ft):
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.basis_value mismatch: "
                        f"cited {_bval!r} != {_fk!r} payload {_ft!r}"
                    )
                _pnum = _num(_entry.get("p"))
                if _entry.get("p") is not None and _pnum is None:
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.p must be numeric or null"
                    )
                _score[_hk] = (_RANK[_penum], _pnum if _pnum is not None else 0.5)
            _ps = [_probs.get(_hk, {}).get("p") if isinstance(_probs.get(_hk), dict) else None
                   for _hk in _keys]
            _pnums = [_num(_v) for _v in _ps]
            if all(_v is not None for _v in _pnums):
                if abs(sum(_pnums) - 1.0) > 0.15:  # type: ignore[arg-type]
                    missing.append(
                        "hypothesis_probabilities numeric P must sum to ≈1 "
                        f"(got {sum(_pnums):.3f}) — or leave p null and weigh by enum"
                    )
            # Bucket coherence (coarse — rejects gross incoherence only).
            _fit_status = _fitA.get("status")
            _has_dist = any(_distA.get(_k) is not None
                            for _k in ("expected_delta", "p_ge_zero", "p_ge_theta"))
            _fore_ok = isinstance(_acc_data(_a.get("calc.forward.forecast")), dict)
            if _fit_status in ("insufficient", "refused") or not (_has_dist or _fore_ok):
                for _hk in _keys:
                    _en = _probs.get(_hk) if isinstance(_probs.get(_hk), dict) else {}
                    if str((_en or {}).get("probability") or "").lower() == "high":
                        missing.append(
                            f"hypothesis_probabilities.{_hk}=high with no usable "
                            "distribution/forecast truth — nothing is proven; "
                            "weigh low|medium until 2b truth exists"
                        )
                        break
            else:
                _pnode = _distA.get("p_ge_theta", _distA.get("p_ge_zero"))
                _pv = _num(_pnode)
                if _pv is not None:
                    _h1en = str((_probs.get("H1") or {}).get("probability") or "").lower() \
                        if isinstance(_probs.get("H1"), dict) else ""
                    if _h1en == "high" and _pv < 0.55:
                        missing.append(
                            "hypothesis_probabilities.H1=high beside "
                            f"distribution P={_pv:.3f} — high needs P≥0.55 or a "
                            "named refused-tool finding carrying the choice"
                        )
            # Choice rule: chosen == argmax(rank, p); ties need tiebreak;
            # deviation needs a cited refusal finding on the ledger.
            _chosen = str(_probs.get("chosen") or "").strip()
            if _chosen not in ("H0", "H1"):
                missing.append(
                    "hypothesis_probabilities.chosen must be H0|H1"
                )
            elif len(_score) == 2:
                _best = max(_score.items(), key=lambda _kv: (_kv[1][0], _kv[1][1]))
                _runner = min(_score.items(), key=lambda _kv: (_kv[1][0], _kv[1][1]))
                _tied = _best[1] == _runner[1]
                if _tied and not str(_probs.get("tiebreak") or "").strip():
                    missing.append(
                        "hypothesis_probabilities tied — tiebreak citing evidence required"
                    )
                if _chosen != _best[0]:
                    _ev_paths = [str((_e or {}).get("path") or "")
                                 for _e in (parsed.get("evidence") or [])
                                 if isinstance(_e, dict)]
                    if not any("refusal" in _p for _p in _ev_paths):
                        missing.append(
                            f"hypothesis_probabilities.chosen must be the argmax "
                            f"({_best[0]} outranks {_chosen}) unless a cited "
                            "refusal finding carries the deviation"
                        )
    except Exception:
        log.exception("adjudication check failed open; continuing without it")
    return (not missing), missing
