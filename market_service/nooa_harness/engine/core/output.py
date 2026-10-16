"""OUTPUT stage (FINALIZE seam) — non-agentic composition + placement."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from market_service.runtime.contracts import InferenceArtifact
from . import context
from ..fsm import GovernanceEvent, GovernanceEventKind
from ..kb import build_task_workflow, chain_status
from ..ledger import LedgerKind, ledger_rows
from ..loop_states import (
    LoopReceipt,
    LoopStep,
    NestedLoop,
    ReceiptDisposition,
    SubLoop,
)
from .. import receipts

log = logging.getLogger(__name__)


def _assemble_final(
    composed: dict[str, Any], staged: dict[str, Any],
    deterministic_state: dict[str, Any],
    accumulated: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic assembly post-pass: narration from ledger (pure).

    - Evidence entries whose path resolves nowhere (accumulated proof,
      deterministic_state, ledger, digest) are dropped — narration carries
      zero numbers the ledger cannot back.
    - Null/empty sections (summary, evidence, hypothesis,
      hypothesis_probabilities) fall back to the staged final so a thin
      composition never erases judged content.
    - The weighed choice is aligned to the recorded `verdict-chosen` row
      when present (decision receipt authority over composition words).
    Returns (final, dropped_paths).
    """
    try:
        final = dict(composed)
        dropped: list[str] = []
        allowed_state = set(deterministic_state.keys()) | {"ledger", "digest"}

        def _resolves(_path: str) -> bool:
            _head = str(_path or "").split("→")[0].strip()
            if not _head:
                return False
            if _head in accumulated:
                return True
            if _head.split(".")[0] in ("deterministic_state", "ledger", "digest"):
                return True
            if _head in allowed_state:
                return True
            return False
        ev = final.get("evidence")
        if isinstance(ev, list):
            kept = []
            for entry in ev:
                if not isinstance(entry, dict) or not str(entry.get("path") or "").strip():
                    dropped.append(str((entry or {}).get("path") or "<empty>"))
                    continue
                if not _resolves(entry.get("path")):
                    dropped.append(str(entry.get("path")))
                    continue
                if not str(entry.get("interpretation") or "").strip():
                    dropped.append(str(entry.get("path")))
                    continue
                kept.append(entry)
            final["evidence"] = kept
        for _key in ("summary", "hypothesis", "hypothesis_probabilities"):
            _v = final.get(_key)
            _empty = _v is None or _v == [] or _v == {} or (
                isinstance(_v, str) and not _v.strip())
            if _empty and staged.get(_key) is not None:
                final[_key] = staged.get(_key)
        try:
            from ..ledger import LedgerKind as _LK
            from ..ledger import ledger_rows as _lr
            _vc = [r for r in _lr(
                deterministic_state, (_LK.OBSERVATION,))
                if (r.get("payload") or {}).get("stage") == "verdict-chosen"]
            if _vc:
                _choice = ((_vc[-1].get("payload") or {}).get("chosen"))
                _hp = final.get("hypothesis_probabilities")
                if _choice in ("H0", "H1") and isinstance(_hp, dict) \
                        and _hp.get("chosen") != _choice:
                    _hp = dict(_hp)
                    _hp["chosen"] = _choice
                    final["hypothesis_probabilities"] = _hp
                    dropped.append(f"<choice realigned to ledger verdict-chosen:{_choice}>")
        except Exception:
            pass
        return final, dropped
    except Exception:
        log.exception("assembly post-pass failed open; keeping composed")
        return dict(composed), []


async def run_output(
    engine: Any, ctx: "_CycleContext", st: Any,
) -> tuple[InferenceArtifact, dict[str, Any]]:
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
    # The agent reasons over the vectors here (collapse object, then final
    # from collapse + full material); failures are FSM-visible and degrade
    # to the legacy single call. Tiers accrue on st via record_tier.
    from .synthesize import synthesize_final
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
    try:
        maybe_composed = await synthesize_final(
            engine, ctx, st,
            directive_block=directive_block,
            synthesis_material=_material,
            accumulated_keys=list(accumulated_tool_results),
            traversal=controller.loop_coverage(),
            chain_line=_chain_line,
        )
        if isinstance(maybe_composed, dict):
            # Narration-from-ledger: deterministic assembly over the derived
            # object before it becomes the final (drops surface here).
            maybe_composed, _dropped = _assemble_final(
                maybe_composed, staged_final,
                deterministic_state, accumulated_tool_results,
            )
            for _d in _dropped:
                log.warning("assembly dropped: %s", _d[:160])
            _assembly_prev = deterministic_state.get("assembly_dropped") or []
            deterministic_state["assembly_dropped"] = list(_assembly_prev) + _dropped[:8]
            parsed_final = maybe_composed
    except Exception:
        log.exception("output synthesis failed; keeping staged final")
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
    # Scenario block rides the interpretation JSON (no migration): the agent's
    # verdict/probability/rationale over the deterministic requirement.
    if scenario is not None and isinstance(parsed_final.get("scenario"), dict):
        interpretation["scenario"] = parsed_final["scenario"]
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
        hypothesis = {"H0": f"β ≈ {betastr} ticks/OFI per OFI calculation, AD separately validated", "evidence_refs": ["calc.ofi.intervals","calc.depth.average"]}
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
    # after the durable update receipt is known.
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
    receipts.append_receipt(deterministic_state, LoopReceipt(
        loop=NestedLoop.OUTPUT, sub_loop=SubLoop.MEMORY,
        disposition=(ReceiptDisposition.COMPLETED
                     if durable_ok
                     else ReceiptDisposition.REFUSED),
        steps=(LoopStep.ASSEMBLE, LoopStep.GROUND,
               LoopStep.UNDERSTAND_SURFACE, LoopStep.LOCATE_OUTPUT,
               LoopStep.PERSIST, LoopStep.CONFIRM,
               LoopStep.PROPOSE, LoopStep.RESOLVE, LoopStep.SETTLE),
        payload={"durable_ok": bool(durable_ok)},
        evidence_refs=(pending.artifact_id,),
    ))
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )

    final_terminal = "settled" if durable_ok else "infra_failed"
    deterministic_state["terminal"] = final_terminal
    deterministic_state["persistence"] = persistence
    deterministic_state["loop_traversal"] = controller.loop_coverage()
    deterministic_state["congruence"] = controller.congruence()
    final_errors = list(pending.errors)
    if not durable_ok:
        final_errors.append({"source": "persistence", "error": "durable artifact receipt missing"})
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
            context.failure_event("infra_failed")
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
    }
    return artifact, cycle_meta


