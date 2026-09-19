"""GATHER stage (EVIDENCE loop, fixed part) — pre-gate reads + hard gate."""

from __future__ import annotations

import logging
from typing import Any

from market_service.nooa_harness.inference import resolve_inference_status
from market_service.runtime.contracts import InferenceArtifact
from . import context
from .context import _CycleContext, _GatheredEvidence
from ..fsm import GovernanceEvent, GovernanceEventKind
from ..loop_states import NestedLoop, SubLoop, TaskIntent

log = logging.getLogger(__name__)


async def run_gather(
    engine: Any, ctx: "_CycleContext",
) -> tuple[InferenceArtifact, dict[str, Any]] | None:
    """EVIDENCE loop, fixed part — pre-gate reads + hard gate."""
    wake, task, scenario = ctx.wake, ctx.task, ctx.scenario
    generated_at = ctx.generated_at
    controller, capability_log = ctx.controller, ctx.capability_log

    # --- PRE-LLM DETERMINISTIC ACQUISITION ---
    # Two pure reads serve the zero-token gate contract: capture status
    # (capture_state) and the fit (fit_status + n_observations). Both are
    # routed through the controller (its first classified outcomes — the
    # controller RECEIVES the envelope and opens the ledger the agent
    # reads/cites) and mirrored into accumulated_tool_results so the
    # model can cite what it owns. Everything beyond these two reads is
    # the AGENT's to pull.
    accumulated_tool_results: dict[str, Any] = {}
    tool_results: dict[str, Any] = {}

    # The two gate reads are an explicit wake bootstrap.  They are
    # deterministic prerequisites for deciding whether the agentic loops may
    # spend any LLM budget, so they are authorized at the initial
    # COMPREHENSION observation rather than pretending that EVIDENCE was
    # already traversed.
    context._must_authorize_work(
        controller, nested_loop=NestedLoop.COMPREHENSION, bootstrap=True,
    )
    status, status_log = await context.execute_tool(
        engine.store, "micro.capture_status",
        {"symbol": engine.symbol, "venue": engine.venue},
        memory=engine.memory, settings=engine.settings,
    )
    capability_log.append(status_log)
    controller = controller.record_outcome(
        "micro.capture_status", status_log, status,
        args={"symbol": engine.symbol, "venue": engine.venue},
    )
    accumulated_tool_results["micro.capture_status"] = status
    tool_results["micro.capture_status"] = status

    context._must_authorize_work(
        controller, nested_loop=NestedLoop.COMPREHENSION, bootstrap=True,
    )
    evidence, fit_log = await context.execute_tool(
        engine.store, "micro.fit_beta",
        {"symbol": engine.symbol, "venue": engine.venue,
         "interval_seconds": 10, "window_minutes": 30},
        postgres=engine.postgres, memory=engine.memory, settings=engine.settings,
    )
    capability_log.append(fit_log)
    controller = controller.record_outcome(
        "micro.fit_beta", fit_log, evidence,
        args={"symbol": engine.symbol, "venue": engine.venue,
              "interval_seconds": 10, "window_minutes": 30},
    )
    accumulated_tool_results["micro.fit_beta"] = evidence
    tool_results["micro.fit_beta"] = evidence

    fit_status = None
    n_observations = 0
    if evidence is not None:
        pif = evidence.get("price_impact_fit") or {}
        fit_status = pif.get("status")
        n_observations = int(pif.get("n_observations") or 0)
    events_in_window = int((evidence or {}).get("coverage", {}).get(
        "events_in_window", 0) or 0)
    status_obj = status or {}
    gate_status, gate_reasons = resolve_inference_status(
        n_observations=n_observations,
        min_observations=30,
        fit_status=fit_status,
        capture_state=status_obj.get("state"),
        events_in_window=events_in_window,
        sequence_gaps=int(status_obj.get("sequence_gaps") or 0),
    )

    # Build the canonical forward ForecastResult before any interpretation.
    # This is deterministic evidence, not an agent-selected sequence of raw
    # statistical tools. A legacy-insufficient cycle still exits before this
    # call; provisional forward evidence remains readable and explicitly gated.
    forecast_result = None
    forecast_log: dict[str, Any] | None = None
    if gate_status != "insufficient":
        context._must_authorize_work(
            controller, nested_loop=NestedLoop.COMPREHENSION, bootstrap=True,
        )
        forecast_result, forecast_log = await context.execute_tool(
            engine.store, "calc.forward.forecast",
            {"symbol": engine.symbol, "venue": engine.venue,
             "horizon_ms": 5_000, "window_minutes": 30},
            postgres=engine.postgres, memory=engine.memory, settings=engine.settings,
        )
        capability_log.append(forecast_log)
        controller = controller.record_outcome(
            "calc.forward.forecast", forecast_log, forecast_result,
            args={"symbol": engine.symbol, "venue": engine.venue,
                  "horizon_ms": 5_000, "window_minutes": 30},
        )
        accumulated_tool_results["calc.forward.forecast"] = forecast_result
        tool_results["calc.forward.forecast"] = forecast_result
        forward_status = (
            forecast_result.get("validation_state")
            if isinstance(forecast_result, dict) else None
        )
        if gate_status == "validated" and forward_status != "validated":
            gate_status = "provisional"
            gate_reasons = list(gate_reasons) + [
                "forward ForecastResult is not OOS-validated"
                if forward_status is not None
                else "forward ForecastResult unavailable"
            ]

    # --- READ-PLANE SHAPE (envelope-driven) ---
    # The controller receives the envelope and hands it to the agent;
    # the agent then READS AS IT PLEASES via the tool base to complete
    # its task. The canonical forward ForecastResult is now acquired before
    # interpretation; lower-level calc.* tools remain available as diagnostics
    # but are not the authority for combining statistical paths.
    deterministic_state: dict[str, Any] = {
        "task": task,
        "scenario": scenario,
        "wake": {
            "trigger_source": wake.trigger_source,
            "predicates_fired": wake.predicates_fired,
        },
        "capture_status": status,
        "microstructure_evidence": evidence,
        "forecast_result": forecast_result,
        "gate": {"status": gate_status, "reasons": list(gate_reasons)},
        "forward_gate": {
            "status": ((forecast_result or {}).get("validation_state")
                       if isinstance(forecast_result, dict) else "unavailable"),
            "probability_status": (((forecast_result or {}).get("multivariate") or {}).get("probability_status")
                                   if isinstance(forecast_result, dict) else "unavailable"),
            "reason": ((forecast_log or {}).get("detail")
                       if isinstance(forecast_log, dict) else "not_run"),
        },
        "data_quality": {
            "capture_state": status_obj.get("state"),
            "sequence_gaps": int(status_obj.get("sequence_gaps") or 0),
            "heteroskedasticity_flag": ((evidence or {}).get("price_impact_fit") or {}).get("heteroskedasticity_flag"),
            "fit_status": fit_status,
            "n_observations": n_observations,
            "spine": {"interval_seconds": 10, "window_minutes": 30},
            "note": ("provisional persists while gaps>0 or hetero=true; "
                     "agent may recompute at interval 10/15/30s × window 15/30/60m via tool args"),
            "read_plane": ("the deterministic spine (OFI intervals, AD, "
                           "observations, ΔP) is NOT pre-computed — pull it "
                           "via your read tools during P1/P2/P5"),
        },
    }

    # --- HARD GATE ---
    if gate_status == "insufficient":
        # GOVERNANCE: the gate refusal is a first-class failure — report
        # the kind, the membrane returns the terminal, and it lands beside
        # the meta AND the deterministic state.
        controller = controller.advance(
            GovernanceEvent(GovernanceEventKind.GATE_REFUSED)
        )
        terminal = (
            controller.terminal.value if controller.terminal
            else "gate_refused"
        )
        ctx.controller = controller
        deterministic_state["terminal"] = terminal
        artifact = InferenceArtifact.create(
            symbol=engine.symbol, venue=engine.venue,
            generated_at=generated_at, completed_at=context._utc_now_iso(),
            status="insufficient", window_minutes=30, interval_seconds=10,
            deterministic_state=deterministic_state,
            capability_log=capability_log,
            input_hash=str((evidence or {}).get("input_hash") or "gate-refused"),
            model_version="inference-engine-v1",
            interpretation=None,
            session_id=engine.session_id,
            errors=[{"source": "gate", "error": "; ".join(gate_reasons)}],
        )
        await engine._persist(artifact)
        # Null discipline with learning: remember the quality observation
        # (deterministic write, not an LLM proposal).
        if engine.memory is not None:
            try:
                await engine.memory.remember(
                    engine.session_id, "observation",
                    f"Cycle refused by gate: {'; '.join(gate_reasons)}",
                    run_id=artifact.artifact_id, importance=2.0,
                    tags=("gate", "insufficient", engine.symbol.lower()),
                    evidence_refs=(artifact.artifact_id,),
                )
            except Exception:
                log.exception("gate-cycle memory write failed")
        return artifact, {"task": task[:200] if task else None,
                          "scenario": scenario,
                          "llm_calls": 0, "gate": gate_status,
                          "reasons": list(gate_reasons),
                          "terminal": terminal}

    ctx.controller = controller
    ctx.gathered = _GatheredEvidence(
        evidence=evidence, gate_status=gate_status,
        gate_reasons=list(gate_reasons), deterministic_state=deterministic_state,
        accumulated_tool_results=accumulated_tool_results,
        tool_results=tool_results,
    )
    return None

