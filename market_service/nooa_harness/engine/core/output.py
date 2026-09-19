"""OUTPUT stage (FINALIZE seam) — non-agentic composition + placement."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from market_service.runtime.contracts import InferenceArtifact
from . import context
from .. import narration as narration_mod
from ..fsm import GovernanceEvent, GovernanceEventKind
from ..kb import build_task_workflow, chain_status
from ..loop_states import NestedLoop, SubLoop

log = logging.getLogger(__name__)


async def run_output(
    engine: Any, ctx: "_CycleContext",
) -> tuple[InferenceArtifact, dict[str, Any]]:
    """OUTPUT loop (FINALIZE seam) — compose, persist, dispose memory."""
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
    compose_prompt = (
        "OUTPUT COMPOSITION PASS (no tools available — any tool_calls you "
        "return will be dropped): compose the FINAL artifact JSON strictly "
        "from this run's material below. Return ONLY one JSON object with "
        "EXACTLY {summary, evidence, confidence, limitations, "
        "model_separation, hypothesis, scenario, memory_proposals} — every "
        "numeric claim already cited; invent no values.\n\n"
        f"{directive_block}"
        "SYNTHESIS MATERIAL:\n"
        f"{json.dumps({'summary': staged_final.get('summary'), 'evidence': staged_final.get('evidence'), 'confidence': staged_final.get('confidence'), 'limitations': staged_final.get('limitations'), 'model_separation': staged_final.get('model_separation'), 'hypothesis': staged_final.get('hypothesis'), 'scenario': staged_final.get('scenario'), 'forecast_result': deterministic_state.get('forecast_result'), 'forward_scenario': deterministic_state.get('forward_scenario'), 'task_directive': deterministic_state.get('task_directive')}, default=str)[:30_000]}\n\n"
        "The canonical ForecastResult is deterministic evidence. Preserve its "
        "forecast_type, validation_state, probability_status, assumptions, and "
        "route disagreement in evidence/limitations; do not recreate numbers.\n"
        "DISPATCHED TOOL PATHS:\n"
        f"{json.dumps(sorted(accumulated_tool_results), default=str)[:4_000]}\n"
        "TRAVERSAL (loops walked this cycle — ground the composition in it):\n"
        f"{json.dumps(controller.loop_coverage(), default=str)[:2_000]}\n"
        f"{chain_status(task_workflow['chain'], list(accumulated_tool_results))}\n"
    )
    try:
        raw_compose = await engine._call_llm(compose_prompt)
        llm_calls += 1
        passes_per_loop["output"] = passes_per_loop.get("output", 0) + 1
        maybe_composed = narration_mod.extract_json_object(raw_compose)
        if isinstance(maybe_composed, dict):
            if maybe_composed.pop("tool_calls", None):
                log.warning("output pass: tool_calls dropped (non-agentic)")
            if maybe_composed.get("memory_proposals") is None:
                maybe_composed["memory_proposals"] = staged_final.get("memory_proposals")
            parsed_final = maybe_composed
    except Exception:
        log.exception("output composition pass failed; keeping staged final")
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
    if isinstance(deterministic_state.get("forecast_result"), dict):
        # Persist the deterministic object beside narration. The agent may
        # explain it, but this copy is never sourced from LLM text.
        interpretation["forecast_result"] = deterministic_state["forecast_result"]
    # Validator hardening: coerce confidence blends conservatively so the
    # persisted artifact never stores "low-medium" (contract enum only).
    try:
        from market_service.runtime.contracts import normalize_confidence as _norm_conf
        _coerced = _norm_conf(interpretation.get("confidence"))
        if interpretation.get("confidence") is not None and _coerced is not None:
            interpretation["confidence"] = _coerced
    except Exception:
        pass
    # If still null after forced final, fall back to first round's interpretation
    if interpretation["summary"] is None and parsed_1.get("summary") is not None:
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
    if isinstance(deterministic_state.get("forecast_result"), dict):
        interpretation["forecast_result"] = deterministic_state["forecast_result"]
    # Scenario block rides the interpretation JSON (no migration): the agent's
    # verdict/probability/rationale over the deterministic requirement.
    if scenario is not None and isinstance(parsed_final.get("scenario"), dict):
        interpretation["scenario"] = parsed_final["scenario"]
    # Pass C: hypothesis formed from the track's own evidence + calc.* tools
    # (memory node pruned from the track — transport retained),
    # final DeltaP is derived diagnostic heteroskedastic ν·OFI.
    # Scenario cycles ALWAYS take their verdict from the deterministic
    # tool payload — even when the agent formed no H0/H1 (live-proven:
    # budget exhaustion can strand tool calls in an undispatched final
    # turn, and the verdict must not depend on narration surviving).
    scenario_verdict: tuple[str, str] | None = None
    if scenario is not None:
        scenario_verdict = narration_mod.scenario_verdict(
            tool_results.get("calc.scenario.evaluate"), capability_log,
            gate_status, list(gate_reasons),
        )
    hypothesis = parsed_final.get("hypothesis")
    if not isinstance(hypothesis, dict) and hypothesis is not None:
        hypothesis = {"raw": hypothesis}
    beta = (evidence or {}).get("price_impact_fit", {}).get("beta") if isinstance(evidence, dict) else None
    betastr = str(beta)[:12] if beta is not None else "unknown"
    if hypothesis is None:
        hypothesis = {"H0": f"β ≈ {betastr} ticks/OFI per OFI calculation, AD separately validated", "paper_refs": ["Cont 1011.6402 OFI_k, AD_i, derived ΔP diagnostic"], "evidence_refs": ["calc.ofi.intervals","calc.depth.average"]}
        if scenario_verdict is not None:
            hypothesis_verdict, verdict_reason = (
                scenario_verdict[0],
                scenario_verdict[1] + " ; agent formed no H0/H1 this cycle",
            )
        else:
            hypothesis_verdict = "inconclusive"
            verdict_reason = "Agent did not explicitly form H0/H1; calculations split but hypothesis implicit"
    elif scenario_verdict is not None:
        # Scenario cycles: reachability verdict computed deterministically
        # from the accumulated scenario tool payload — never from LLM text.
        hypothesis_verdict, verdict_reason = scenario_verdict
        if isinstance(hypothesis, dict) and hypothesis.get("H0") \
                and hypothesis.get("H1"):
            verdict_reason += " ; H0/H1 reachability pair via calc.scenario.evaluate"
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
    # Read-plane state assembly: the artifact's calculations block is the
    # controller-classified calc-family outcomes the agent pulled during
    # the loop — no pre-gather spine exists anymore.
    calculations: dict[str, Any] = {
        k: v for k, v in accumulated_tool_results.items()
        if k.startswith("calc.") or k == "micro.fit_beta"
    }
    calculations["split_note"] = (
        "AD and OFI read as separate tools by the agent; final DeltaP is "
        "derived hypothesis, not shortcut — per Cont 1011.6402"
    )
    # --- GOVERNANCE: OUTPUT loop (FINALIZE stage) ---
    # Composition is already open.  Architecture, placement and memory are
    # entered explicitly; no visit is recorded for work that was denied or
    # never opened.
    if not finalize_now:
        controller = controller.advance(
            GovernanceEvent(GovernanceEventKind.BUDGET_EXHAUSTED)
        )
        terminal = controller.terminal.value if controller.terminal else "budget_exhausted"
        deterministic_state["terminal"] = terminal
        ctx.controller = controller
        return await engine._degraded_artifact(
            deterministic_state, capability_log,
            "output reached without a validated final",
        ), {"llm_calls": llm_calls, "terminal": terminal}

    # The composition call above opened ARCHITECTURE.  Opening the next
    # sibling is the membrane's ordered handoff; do not close first because
    # the current observation carries the sibling cursor.
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.PLACEMENT
    )

    # Persist a pending artifact first.  The final terminal is written only
    # after memory disposition and the durable update receipt are known.
    deterministic_state["terminal"] = "pending_settlement"
    deterministic_state["loop_traversal"] = controller.loop_coverage()
    deterministic_state["congruence"] = controller.congruence()
    pending = InferenceArtifact.create(
        symbol=engine.symbol, venue=engine.venue,
        generated_at=generated_at, completed_at=context._utc_now_iso(),
        status=gate_status, window_minutes=30, interval_seconds=10,
        deterministic_state=dict(deterministic_state),
        capability_log=capability_log,
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
        session_id=engine.session_id,
        hypothesis=hypothesis,
        hypothesis_verdict=hypothesis_verdict,
        verdict_reason=verdict_reason,
        calculations=calculations,
    )
    persistence = await engine._persist(pending)
    durable_ok = bool(
        persistence.get("postgres") if engine.postgres is not None
        else persistence.get("redis")
    )

    # --- MEMORY PROPOSAL RESOLUTION (LLM proposes; engine disposes) ---
    accepted, dispositions = engine.resolve_memory_proposals(
        parsed_final.get("memory_proposals"), artifact_status=gate_status,
    )
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.MEMORY
    )
    written = await engine._remember(
        accepted, {"artifact_id": pending.artifact_id},
    )
    memory_failed = any(
        isinstance(row, dict) and row.get("error") for row in written
    )
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )

    final_terminal = "settled" if durable_ok and not memory_failed else "infra_failed"
    deterministic_state["terminal"] = final_terminal
    deterministic_state["persistence"] = persistence
    deterministic_state["loop_traversal"] = controller.loop_coverage()
    deterministic_state["congruence"] = controller.congruence()
    final_errors = list(pending.errors)
    if not durable_ok:
        final_errors.append({"source": "persistence", "error": "durable artifact receipt missing"})
    if memory_failed:
        final_errors.append({"source": "memory", "error": "one or more memory writes failed"})
    artifact = replace(
        pending,
        deterministic_state=dict(deterministic_state),
        errors=tuple(final_errors),
    )
    final_persistence = await engine._finalize_persisted(artifact) if durable_ok else persistence
    persistence["final"] = final_persistence
    if durable_ok:
        if final_persistence.get("postgres") is False:
            final_terminal = "infra_failed"
            deterministic_state["terminal"] = final_terminal
            artifact = replace(
                artifact,
                deterministic_state=dict(deterministic_state),
                errors=tuple(list(artifact.errors) + [{
                    "source": "persistence", "error": "final durable update failed",
                }]),
            )
        elif final_terminal == "settled":
            # SETTLE is permitted only after the final durable receipt and
            # only when memory disposition also succeeded.  A durable row is
            # not enough to hide a failed side effect.
            controller, _ = context._must_govern(
                controller, GovernanceEventKind.SETTLE
            )
    if final_terminal != "settled" and controller.terminal is None:
        controller = controller.advance(
            GovernanceEvent(GovernanceEventKind.INFRA_FAILED)
        )
    terminal = controller.terminal.value if controller.terminal else final_terminal
    # Placement is complete only after the write/receipt path ran.  MEMORY
    # was completed above and cannot be claimed by a planned tuple.
    controller = controller.record_loop_visit(
        "output", passes_per_loop.get("output", 0),
        ("composition", "architecture", "placement", "memory"),
        completed=terminal == "settled",
    )
    ctx.controller = controller
    deterministic_state["terminal"] = terminal
    deterministic_state["loop_traversal"] = controller.loop_coverage()
    deterministic_state["governance_trace"] = controller.transition_trace()
    artifact = replace(artifact, deterministic_state=dict(deterministic_state))
    # Refresh the durable row once more with the terminal and complete FSM
    # trace.  The first finalization establishes the terminal; this pass
    # closes the small metadata gap created by SETTLE itself.
    if durable_ok:
        persistence["final_trace"] = await engine._finalize_persisted(artifact)
    cycle_meta = {
        "task": task[:200] if task else None,
        "scenario": scenario,
        "terminal": terminal,
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
        "persistence": persistence,
        "memory": {"accepted": accepted, "dispositions": dispositions,
                   "written": written},
    }
    return artifact, cycle_meta


