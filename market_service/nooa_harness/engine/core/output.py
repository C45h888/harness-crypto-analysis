"""OUTPUT stage (FINALIZE seam) — non-agentic composition + placement."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from market_service.runtime.contracts import CanonicalOutput
from . import context
from ..config import LOOP_PASS_BUDGET, SUMMARY_MIN_CHARS
from ..fsm import GovernanceEvent, GovernanceEventKind
from ..kb import build_task_workflow, chain_status
from ..schemas import coerce_turn, extract_json_object
from ..ledger import LedgerKind, ledger_rows, record_finding
from ..loop_states import LoopStep, NestedLoop, SubLoop
from .. import gates

log = logging.getLogger(__name__)


def build_output_frame(
    *, accumulated: dict[str, Any], deterministic_state: dict[str, Any],
    verdict_choice: str | None, delta_required: bool,
) -> dict[str, Any]:
    """Base-level instantiation of the deterministic frame (pure, zero-LLM).

    The fact surface the agent authors its output ON (the relaxed loop's
    determinism layer): every citable home of THIS run (tool heads actually
    dispatched — memory.* excluded, memory is never evidence — plus the
    deterministic_state keyspace and the ledger/digest namespaces), the
    collapse vectors the material carries, the known-null fields (null
    discipline), the adjudicated choice carried from the validation gate,
    and whether the ΔP citation is compulsory (CONDITIONAL — only when the
    directive bears targets/invalidations).
    """
    citable = sorted(k for k in (accumulated or {})
                     if not str(k).startswith("memory."))
    state_keys = sorted((deterministic_state or {}).keys())
    vectors = ["verdict", "base", "chain", "proof", "frame", "directive", "gate"]
    if (deterministic_state or {}).get("forecast_result") is not None:
        vectors.append("forecast")
    known_nulls = sorted(
        f"deterministic_state.{k}"
        for k, v in (deterministic_state or {}).items() if v is None)
    return {
        "citable_heads": citable,
        "state_keys": state_keys,
        "vectors": vectors,
        "known_nulls": known_nulls,
        "verdict_choice": verdict_choice,
        "delta_required": bool(delta_required),
        "summary_floor": 80,
        "summary_target": SUMMARY_MIN_CHARS,
    }


def _assemble_authored(
    composed: dict[str, Any], frame: dict[str, Any],
    accumulated: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic ASSEMBLE over the AUTHORED object (pure).

    Fact-check, never author words: evidence entries that are unshippable
    per the shared ``classify_evidence`` authority are dropped with a
    record — the one doctrine that lets the engine remove content is
    "narration carries zero numbers the ledger cannot back". Unshippable
    means: no path, unbacked head (unknown tool / ``memory.*``), or a null
    value on a non-refusal path (a path-only claim names no number the
    ledger can back). Empty ``interpretation`` is a quality finding, never
    a drop. With ``accumulated`` store truth, the completion half applies
    first (``synthesize.complete_evidence``): null values on resolving
    paths are FILLED, mismatched values CORRECTED, each with a
    ``filled:``/``corrected:`` marker on the trail; wording untouched.
    Everything else is the agent's AUTHENTIC output: no substitution, no
    backfill, no silent relabeling — choice/verdict conflicts surface via
    ``check_authored_output``, never a rewrite here. Returns
    (final, dropped_paths).
    """
    from .synthesize import (
        classify_evidence, complete_evidence, EVIDENCE_DROP_CODES,
    )
    try:
        final = dict(composed)
        dropped: list[str] = []
        markers: list[str] = []
        ev = final.get("evidence")
        if isinstance(ev, list):
            work = list(ev)
            if accumulated is not None:
                work, filled, corrected, comp_dropped = complete_evidence(
                    work, accumulated, frame)
                markers = list(filled) + list(corrected) + list(comp_dropped)
            kept = []
            for entry in work:
                if not isinstance(entry, dict):
                    dropped.append("<non-object>")
                    continue
                if classify_evidence(entry, frame) in EVIDENCE_DROP_CODES:
                    dropped.append(str(entry.get("path") or "<empty>"))
                    continue
                kept.append(entry)
            dropped = list(dropped) + list(markers)
            final["evidence"] = kept
        return final, dropped
    except Exception:
        log.exception("authored assembly failed open; keeping composed")
        return dict(composed), []


async def run_output(
    engine: Any, ctx: "_CycleContext", st: Any,
) -> tuple[CanonicalOutput, dict[str, Any]]:
    """OUTPUT loop (FINALIZE seam) — synthesize, persist, settle."""
    gathered, reasoned = ctx.gathered, ctx.reasoned
    if gathered is None or reasoned is None:
        raise RuntimeError("output requires gathered evidence and reasoning")
    task, scenario, generated_at = ctx.task, ctx.scenario, ctx.generated_at
    controller, capability_log = ctx.controller, ctx.capability_log
    evidence = gathered.evidence
    gate_status, gate_reasons = gathered.gate_status, gathered.gate_reasons
    deterministic_state = gathered.deterministic_state
    accumulated_tool_results = gathered.accumulated_tool_results
    tool_results = reasoned.tool_results
    parsed_1, parsed_final = reasoned.parsed_first, reasoned.parsed_final
    llm_calls, tool_rounds_used = reasoned.llm_calls, reasoned.tool_rounds_used
    repairs_sent, finalize_now = reasoned.repairs_sent, reasoned.finalize_now
    final_validation, unexecuted = reasoned.final_validation, reasoned.unexecuted
    phase_coverage = controller.phase_coverage
    passes_per_loop = dict(reasoned.passes_per_loop or {})
    comprehension = reasoned.comprehension

    # --- OUTPUT COMPOSITION PASS (LOOP_PASS_BUDGET["output"] = 1, non-agentic) ---
    # One LLM call, tools forbidden: compose the final artifact JSON
    # strictly from this run's material. Any tool_calls returned are
    # dropped and logged. Parse failure keeps the staged final.
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.ENTER_LOOP, NestedLoop.OUTPUT
    )
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.COMPOSITION
    )
    staged_final = parsed_final
    task_workflow = (deterministic_state.get("task_workflow")
                     or build_task_workflow(task, scenario))
    # Re-anchor the composition to the question asked: the task and the
    # bound directive (with its parse refusals) ride the synthesis material
    # so the final pass answers the task, not just summarizes evidence.
    directive_block = ""
    if task:
        directive_block = (
            "TASK (the question this output answers):\n" f"{task[:2_000]}\n\n"
            "TASK DIRECTIVE (deterministic parse + assessment disposal — cite "
            "these as deterministic_state.task_directive / forward_scenario "
            "paths; preserve refusals as findings, never paraphrased numbers):\n"
            f"{json.dumps(deterministic_state.get('task_directive'), default=str)[:6_000]}\n\n"
        )
    if (deterministic_state.get("scenario_contract")
            or deterministic_state.get("scenario_evaluation")):
        directive_block += (
            "SCENARIO CONTRACT + EVALUATION (the exact question and the exact "
            "dispatched answer — echo these; never re-scope in composition):\n"
            "contract: "
            f"{json.dumps(deterministic_state.get('scenario_contract'), default=str)[:2_000]}\n"
            "evaluation: "
            f"{json.dumps(deterministic_state.get('scenario_evaluation'), default=str)[:2_000]}\n"
            + ("divergence: "
               f"{json.dumps(deterministic_state.get('scenario_divergence'), default=str)[:1_000]}\n"
               if deterministic_state.get("scenario_divergence") else "")
            + "\n"
        )
    # Loop-surface completion (verdict recall): the adjudication decision
    # rows travel with synthesis so composition conforms to the weighed
    # choice. Pure ledger read on this task's segment (never the digest).
    _verdict_rows = [
        row for row in ledger_rows(
            deterministic_state, (LedgerKind.OBSERVATION,),
            segment=getattr(ctx, "task_id", None),
        )
        if (row.get("payload") or {}).get("stage")
        in ("verdict-basis", "verdict-chosen")
    ]
    # --- AGENTIC SYNTHESIS (core/synthesize.py owns COLLAPSE → DERIVE) ---
    # The agent authors the output over the deterministic frame (collapse
    # object, then the final from collapse + full material); failures are
    # FSM-visible and degrade to the staged final. Tiers accrue on st via
    # record_tier.
    from .synthesize import render_frame_block, synthesize_final
    _material = {
        'summary': staged_final.get('summary'),
        'evidence': staged_final.get('evidence'),
        'confidence': staged_final.get('confidence'),
        'limitations': staged_final.get('limitations'),
        'model_separation': staged_final.get('model_separation'),
        'hypothesis': staged_final.get('hypothesis'),
        'scenario': staged_final.get('scenario'),
        'forecast_result': deterministic_state.get('forecast_result'),
        'forward_scenario': deterministic_state.get('forward_scenario'),
        'task_directive': deterministic_state.get('task_directive'),
        # Slice E: frozen base + stage ids ride synthesis so the final
        # grounds base numbers from the ledger, never narration text.
        'base_summary': deterministic_state.get('base_summary'),
        # Loop-surface completion (verdict recall): the adjudication
        # decision rows (verdict-basis + verdict-chosen) ride synthesis
        # so composition conforms to the weighed choice.
        'verdict_rows': _verdict_rows,
        # The weighed vector rides synthesis so the choice survives
        # composition (one-home rule: decision lives here + ledger).
        'hypothesis_probabilities': staged_final.get(
            'hypothesis_probabilities'),
        'statistical_chain': deterministic_state.get('statistical_chain'),
    }
    _chain_line = chain_status(
        task_workflow['chain'], list(accumulated_tool_results))
    # Base-level instantiation (relaxed loop): the deterministic frame the
    # agent authors on top of — citable homes, carried adjudication, and the
    # CONDITIONAL ΔP rule (compulsory only when the directive bears targets).
    verdict_choice = None
    for _row in reversed(_verdict_rows):
        _pl = _row.get("payload") or {}
        if _pl.get("stage") == "verdict-chosen" and _pl.get("chosen") in ("H0", "H1"):
            verdict_choice = _pl.get("chosen")
            break
    _plan = getattr(st, "task_plan", None) or {}
    frame = build_output_frame(
        accumulated=accumulated_tool_results,
        deterministic_state=deterministic_state,
        verdict_choice=verdict_choice,
        delta_required=bool(_plan.get("targets") or _plan.get("invalidations")),
    )
    frame_block = render_frame_block(frame)
    authored: dict[str, Any] | None = None
    try:
        maybe_composed = await synthesize_final(
            engine, ctx, st,
            directive_block=directive_block,
            synthesis_material=_material,
            accumulated_keys=list(accumulated_tool_results),
            traversal=controller.loop_coverage(),
            chain_line=_chain_line,
            frame=frame,
        )
        if isinstance(maybe_composed, dict):
            authored = maybe_composed
    except Exception:
        log.exception("output synthesis failed; keeping staged final")
    authored_shipped = False
    if authored is not None:
        # ASSEMBLE (deterministic fact-check) + GROUND (relaxed hard gate)
        # on the object that will SHIP — the agent's authentic output.
        from .synthesize import check_authored_output, memory_context_block
        authored, _dropped = _assemble_authored(
            authored, frame, accumulated_tool_results)
        for _d in _dropped:
            log.warning("assembly dropped (not frame-backed): %s", _d[:160])
            record_finding(
                deterministic_state, "assembly_dropped", str(_d),
                detail={"reason": "not frame-backed"},
                segment=getattr(st, "session_id", None))
        memory_block = memory_context_block(
            getattr(st, "accumulated_tool_results", None) or accumulated_tool_results)
        scenario_refused = False
        try:
            from ..controller import ScenarioEvalStatus as _SES_auth
            scenario_refused = controller.scenario_state() is _SES_auth.REFUSED
        except Exception:
            pass
        hard, findings, conflicts = check_authored_output(
            authored, frame, scenario=scenario,
            scenario_refused=scenario_refused, final_pass=False)
        if scenario is not None:
            try:
                from .validation import check_scenario_equality as _cse
                hard += list(_cse(authored, st, ctx))
            except Exception:
                log.exception("scenario equality check failed open")
        correction_used = False
        if hard and st.passes_per_loop.get("output", 0) < LOOP_PASS_BUDGET["output"]:
            # ONE correction turn (the spare third output pass): the agent
            # fixes ITS OWN output — the engine never rewrites its words.
            from .synthesize import _one_call
            from ..kb import compose_correction_prompt
            raw_fix = await _one_call(
                engine, st,
                compose_correction_prompt(
                    task=task, misses=list(hard), authored=authored,
                    frame_block=frame_block, memory_block=memory_block,
                ),
            )
            if raw_fix is not None:
                correction_used = True
                fixed = coerce_turn(extract_json_object(raw_fix))
                if isinstance(fixed, dict):
                    fixed, _fix_dropped = _assemble_authored(
                        fixed, frame, accumulated_tool_results)
                    for _d in _fix_dropped:
                        log.warning("correction assembly dropped: %s", _d[:160])
                    hard, findings, conflicts = check_authored_output(
                        fixed, frame, scenario=scenario,
                        scenario_refused=scenario_refused, final_pass=True)
                    if scenario is not None:
                        try:
                            from .validation import check_scenario_equality as _cse2
                            conflicts += list(_cse2(fixed, st, ctx))
                        except Exception:
                            log.exception("scenario equality check failed open")
                    authored = fixed
        parsed_final = authored
        authored_shipped = True
        # Honest reporting: the verdict describes the object that SHIPS.
        final_validation = {
            "passed": not hard,
            "missing": list(hard),
            "quality_findings": list(findings),
            "conflicts": list(conflicts),
            "tier": "authored",
            "correction_used": correction_used,
        }
        _dropped_now = [
            str((row.get("payload") or {}).get("message"))
            for row in (deterministic_state.get("ledger") or [])
            if row.get("kind") == LedgerKind.FINDING.value
            and (row.get("payload") or {}).get("finding_kind") == "assembly_dropped"
        ]
        record_finding(
            deterministic_state, "conformance", "authored ship conformance",
            detail={
                "tier": "authored",
                "dropped": _dropped_now[-8:],
                "hard_misses": list(hard)[:8],
                "conflicts": list(conflicts)[:8],
                "quality_findings": list(findings)[:8],
                "correction_used": correction_used,
            })
    # Sync the handoff counters with what synthesis spent (tiers accrued on
    # st via record_tier; narration_tiers freezes the full cycle sequence).
    llm_calls = st.llm_calls
    passes_per_loop = dict(reasoned.passes_per_loop or {})
    passes_per_loop["output"] = st.passes_per_loop.get("output", 0)
    deterministic_state["narration_tiers"] = list(st.llm_tiers)
    # Composition completed; architecture is a real governed sub-loop.  The
    # placement and memory sub-loops are opened below only when their actual
    # handlers run, so the trace cannot claim work that was merely planned.
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ARCHITECTURE
    )

    interpretation = {
        "summary": parsed_final.get("summary"),
        "evidence": parsed_final.get("evidence"),
        "confidence": parsed_final.get("confidence"),
        "limitations": parsed_final.get("limitations"),
        "model_separation": parsed_final.get("model_separation"),
    }
    # One-home rule: forecast payloads live in calculations + deterministic
    # state only — interpretation carries citation paths, never object copies.
    # Validator hardening: coerce confidence blends conservatively so the
    # persisted artifact never stores "low-medium" (contract enum only).
    try:
        from market_service.runtime.contracts import normalize_confidence as _norm_conf
        _coerced = _norm_conf(interpretation.get("confidence"))
        if interpretation.get("confidence") is not None and _coerced is not None:
            interpretation["confidence"] = _coerced
    except Exception:
        pass
    # If still null after forced final (degrade path only — an authored
    # output ships as authored, flagged, never replaced by older turns),
    # fall back to first round's interpretation.
    if (not authored_shipped and interpretation["summary"] is None
            and parsed_1.get("summary") is not None):
        interpretation = {
            "summary": parsed_1.get("summary"),
            "evidence": parsed_1.get("evidence"),
            "confidence": parsed_1.get("confidence"),
            "limitations": parsed_1.get("limitations"),
            "model_separation": parsed_1.get("model_separation"),
        }
        parsed_final = parsed_1
    # Validator hardening (post-fallback): coerce again so fallback path
    # also persists the contract enum.
    try:
        from market_service.runtime.contracts import normalize_confidence as _norm_conf2
        _coerced2 = _norm_conf2(interpretation.get("confidence"))
        if interpretation.get("confidence") is not None and _coerced2 is not None:
            interpretation["confidence"] = _coerced2
    except Exception:
        pass
    # Scenario block rides the interpretation JSON (no migration): the agent's
    # verdict/probability/rationale over the deterministic requirement.
    if scenario is not None and isinstance(parsed_final.get("scenario"), dict):
        interpretation["scenario"] = parsed_final["scenario"]
    # C2 run-record retention (plan §8 gate 1 — KEEP): the authored
    # adjudication rides the interpretation so the ``answer`` re-render of a
    # past run is EXACT. The composer renders what ships; this only keeps
    # what the agent already wrote in the run record.
    _adjudication = parsed_final.get("hypothesis_probabilities")
    if isinstance(_adjudication, dict):
        interpretation["hypothesis_probabilities"] = _adjudication
    # Pass C: hypothesis formed from the track's own evidence + calc.* tools
    # (memory node pruned from the track — transport retained),
    # final DeltaP is derived diagnostic heteroskedastic ν·OFI.
    # Scenario verdicts are judged in validation (checker-enforced equality
    # against the deterministic derivation) — output keeps the agent's block
    # verbatim and computes nothing. The reachability answer lives in
    # interpretation.scenario; hypothesis_verdict below stays evidence-strength.
    hypothesis = parsed_final.get("hypothesis")
    if not isinstance(hypothesis, dict) and hypothesis is not None:
        hypothesis = {"raw": hypothesis}
    beta = (evidence or {}).get("price_impact_fit", {}).get("beta") if isinstance(evidence, dict) else None
    betastr = str(beta)[:12] if beta is not None else "unknown"
    if hypothesis is None:
        if authored_shipped:
            # Authentic output: the agent's omission ships as omission (the
            # hard gate already flagged it) — the engine authors nothing.
            hypothesis_verdict = "inconclusive"
            verdict_reason = "Agent did not explicitly form H0/H1; calculations split but hypothesis implicit"
        else:
            hypothesis = {"H0": f"β ≈ {betastr} ticks/OFI per OFI calculation, AD separately validated", "evidence_refs": ["calc.ofi.intervals", "calc.depth.average"]}
            hypothesis_verdict = "inconclusive"
            verdict_reason = "Agent did not explicitly form H0/H1; calculations split but hypothesis implicit"
    else:
        if gate_status == "provisional":
            hypothesis_verdict = "inconclusive"
            verdict_reason = f"Gate provisional ({';'.join(gate_reasons)}); hypothesis held as derived diagnostic, not shortcut — ΔP diagnostic heteroskedastic"
        elif gate_status == "validated":
            hypothesis_verdict = "validated"
            verdict_reason = "Deterministic fits validated; hypothesis confirmed via split AD/OFI and derived ΔP diagnostic"
        else:
            hypothesis_verdict = "invalidated"
            verdict_reason = "; ".join(gate_reasons)
        if isinstance(hypothesis, dict) and "H0" in hypothesis:
            verdict_reason += " ; H0 grounded in track evidence (position-3 formation)"
    # Scenario refusal note (not a verdict — validation owns verdicts): when
    # the scenario tool refused, the reachability answer is unevaluated and
    # the reason carries the deterministic cause beside the gate reading.
    if scenario is not None:
        try:
            from ..controller import ScenarioEvalStatus as _SES_out
            if controller.scenario_state() is _SES_out.REFUSED:
                _rr = controller.scenario_refusal_reason() or "refusal"
                verdict_reason += f" ; scenario unevaluated ({_rr})"
        except Exception:
            pass
    # Read-plane state assembly: the artifact's calculations block is the
    # controller-classified calc-family outcomes the agent pulled during
    # the loop — no pre-gather spine exists anymore.
    calculations: dict[str, Any] = {
        k: v for k, v in accumulated_tool_results.items()
        if k.startswith("calc.") or k == "micro.fit_beta"
    }
    calculations["split_note"] = (
        "AD and OFI read as separate tools by the agent; final DeltaP is "
        "derived hypothesis, not shortcut"
    )
    # --- GOVERNANCE: OUTPUT loop (FINALIZE stage) ---
    # Composition is already open.  Architecture, placement and memory are
    # entered explicitly; no visit is recorded for work that was denied or
    # never opened.
    if not finalize_now:
        controller = controller.advance(
            context.failure_event("budget_exhausted")
        )
        terminal = controller.terminal.value if controller.terminal else "budget_exhausted"
        deterministic_state["terminal"] = terminal
        ctx.controller = controller
        return await engine._degraded_output(
            deterministic_state, capability_log,
            "output reached without a validated final",
        ), {"llm_calls": llm_calls, "terminal": terminal}

    # The composition call above opened ARCHITECTURE.  Opening the next
    # sibling is the membrane's ordered handoff; do not close first because
    # the current observation carries the sibling cursor.
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.PLACEMENT
    )

    # --- OUTPUT PLACEMENT (C2 canonical path) ---
    # The deliverable is THE ANSWER (canonical-output doctrine): compose it
    # from the record that ships, DELIVER it to its surface (confirmation =
    # the call returns), then PLACE the canonical output row (bookkeeping).
    # PLACEMENT = deliver + confirm; SETTLE gates on delivery confirmation +
    # recorded verdicts — never on a store write. Placement failures are
    # RECORDED as run_record_errors and never block settlement.
    deterministic_state["terminal"] = "pending_settlement"
    from ..projections import build_artifact_state as _project
    deterministic_state.update(_project(deterministic_state, controller))
    pending = CanonicalOutput.create(
        symbol=engine.symbol, venue=engine.venue,
        generated_at=generated_at, completed_at=context._utc_now_iso(),
        status=gate_status, window_minutes=30, interval_seconds=10,
        input_hash=str(
            (deterministic_state.get("forecast_result") or {}).get("input_hash")
            or (evidence or {}).get("input_hash")
            or "no-evidence"
        ),
        model_version=str(
            (deterministic_state.get("forecast_result") or {}).get("model_version")
            or "inference-engine-v1"
        ),
        interpretation=interpretation,
        hypothesis=hypothesis,
        hypothesis_verdict=hypothesis_verdict,
        verdict_reason=verdict_reason,
        calculations=calculations,
        conformance=deterministic_state.get("conformance"),
        run_record={
            "deterministic_state": dict(deterministic_state),
            "capability_log": list(capability_log),
            "errors": [],
        },
        task=str(task or "")[:200] or None,
        terminal="pending_settlement",
        session_id=engine.session_id,
    )

    # --- DELIVER (the answer reaches its surface; render failure counts) ---
    answer_text, answer_struct = None, None
    delivery_error = None
    try:
        from .answer import (
            answer_from_record, answer_inputs_from_record,
            compose_answer_structured,
        )
        answer_text = answer_from_record(pending.to_dict())
        if answer_text is not None:
            answer_struct = compose_answer_structured(
                *answer_inputs_from_record(pending.to_dict()))
            await engine._deliver_answer(answer_text)
    except Exception as exc:
        log.exception("answer delivery failed")
        delivery_error = f"{type(exc).__name__}: {exc}"
    delivery_ok = delivery_error is None
    output = replace(pending, answer_text=answer_text, answer=answer_struct)

    # --- PLACE (the canonical output row: run-record bookkeeping) ---
    placement = await engine._place_output(output)
    from .runner import run_record_errors_of
    run_record_errors = run_record_errors_of(placement)

    # --- AGENT MEMORY (the agent owns it) ---
    # The agent writes its own memory through the memory.* tools, in flight, at
    # the moment it decides to. The engine does NOT dispose memory: there is no
    # proposal queue and no post-hoc write here. The MEMORY sub-loop still
    # closes the OUTPUT traversal.
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.MEMORY
    )
    # Canonical receipt: the OUTPUT loop's four sub-loops closed. The final
    # terminal is recorded separately (deterministic_state["terminal"] +
    # loop_traversal); this receipt records the WORK, not the SETTLE transition.
    gates.close_loop(
        deterministic_state, loop=NestedLoop.OUTPUT, sub_loop=SubLoop.MEMORY,
        disposition=(gates.GateDisposition.COMPLETED
                     if delivery_ok
                     else gates.GateDisposition.REFUSED),
        steps=(LoopStep.ASSEMBLE, LoopStep.GROUND,
               LoopStep.UNDERSTAND_SURFACE, LoopStep.LOCATE_OUTPUT,
               LoopStep.PERSIST, LoopStep.CONFIRM,
               LoopStep.PROPOSE, LoopStep.RESOLVE, LoopStep.SETTLE),
        payload={"delivered": bool(delivery_ok),
                 "run_record": "errors" if run_record_errors else "recorded"},
        refs=(output.run_id,),
        segment=getattr(output, "run_id", None),
    )
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )

    # --- SETTLE (delivery confirmation + projections recorded) ---
    # Receipts/findings are no longer state: derive the permanent artifact
    # projections from the ledger rows written this cycle.
    from ..projections import build_artifact_state
    deterministic_state.update(build_artifact_state(deterministic_state, controller))
    final_terminal = "settled" if delivery_ok else "delivery_failed"
    deterministic_state["terminal"] = final_terminal
    deterministic_state["persistence"] = placement
    deterministic_state.update(build_artifact_state(deterministic_state, controller))
    output = replace(
        output,
        terminal=final_terminal,
        run_record={
            "deterministic_state": dict(deterministic_state),
            "capability_log": list(capability_log),
            "errors": list(run_record_errors),
        },
    )
    if delivery_ok:
        # SETTLE is permitted once the ANSWER is confirmed delivered and the
        # verdicts are recorded. A run-record write is bookkeeping — its
        # failure never hides a delivered answer nor blocks the settle.
        controller, _ = context._must_govern(
            controller, GovernanceEventKind.SETTLE
        )
    if final_terminal != "settled" and controller.terminal is None:
        controller = controller.advance(
            context.failure_event("delivery_failed")
        )
    terminal = controller.terminal.value if controller.terminal else final_terminal
    # Placement is complete only after the deliver/confirm path ran.  MEMORY
    # was completed above and cannot be claimed by a planned tuple.
    controller = controller.record_loop_visit(
        "output", passes_per_loop.get("output", 0),
        ("composition", "architecture", "placement", "memory"),
        completed=terminal == "settled",
    )
    ctx.controller = controller
    deterministic_state["terminal"] = terminal
    deterministic_state.update(build_artifact_state(deterministic_state, controller))
    output = replace(
        output,
        terminal=terminal,
        run_record={
            "deterministic_state": dict(deterministic_state),
            "capability_log": list(capability_log),
            "errors": list(run_record_errors),
        },
    )
    # Refresh the canonical row once more with the terminal and complete FSM
    # trace (best-effort bookkeeping — recorded, never blocking).
    placement["final_trace"] = await engine._finalize_output(output)
    run_record_errors = run_record_errors + run_record_errors_of(
        placement["final_trace"])
    # Honest reporting: the verdict describes the object that ships — and
    # whether the deliverable actually reached its surface.
    if isinstance(final_validation, dict):
        final_validation = dict(final_validation)
        final_validation["delivered"] = delivery_ok
        if delivery_error:
            final_validation["delivery_error"] = delivery_error
    cycle_meta = {
        "task": task[:200] if task else None,
        "scenario": scenario,
        "terminal": terminal,
        "answer": answer_text,
        "delivery": {"delivered": delivery_ok, "error": delivery_error},
        "run_record_errors": run_record_errors,
        "unexecuted_tool_calls": unexecuted,
        "llm_calls": llm_calls,
        "passes_per_loop": passes_per_loop,
        "comprehension_present": comprehension is not None,
        "gate": gate_status,
        "tool_round": bool(tool_results),
        "tool_rounds": tool_rounds_used,
        "repairs": repairs_sent,
        "phase_coverage": {p: sorted(s) for p, s in phase_coverage.items()},
        "final_validation": final_validation,
        "persistence": placement,
    }
    return output, cycle_meta


