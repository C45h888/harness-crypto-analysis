"""GATHER stage — split across the comprehension boundary (Pass B).

run_bootstrap: pre-gate reads + hard gate (needs no understanding).
run_plan_bound_acquisition: forecast + scenario pre-acquisition driven by
the FROZEN understanding receipt (post-comprehension). The split cures the
ordering inversion: plan-bound work executes under the receipt that
authorizes it, never before it exists.
"""

from __future__ import annotations

import logging
from typing import Any

from ..gates import evaluate_data_gate
from market_service.runtime.contracts import InferenceArtifact
from . import context
from .context import _CycleContext, _GatheredEvidence
from ..fsm import GovernanceEvent, GovernanceEventKind
from ..loop_states import NestedLoop, SubLoop, TaskIntent

log = logging.getLogger(__name__)


async def run_gather(
    engine: Any, ctx: "_CycleContext",
) -> tuple[InferenceArtifact, dict[str, Any]] | None:
    """Compat entry — runs bootstrap only. The runner calls the split
    phases explicitly (bootstrap → comprehension → plan-bound
    acquisition); direct callers get bootstrap semantics."""
    return await run_bootstrap(engine, ctx)


async def run_bootstrap(
    engine: Any, ctx: "_CycleContext",
) -> tuple[InferenceArtifact, dict[str, Any]] | None:
    """Bootstrap phase — pre-gate reads + hard gate (no understanding needed)."""
    # Canonical read: task/scenario flow from the injected PromptVariable
    # (normalized once in wake.run_wake); raw ctx.task/scenario are the
    # compat fallback for callers that bypass run_wake.
    _prompt = getattr(ctx, "prompt", None)
    wake = ctx.wake
    if _prompt is not None:
        task, scenario = _prompt.raw, _prompt.scenario
    else:
        task, scenario = ctx.task, ctx.scenario
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
        controller, nested_loop=NestedLoop.CONTEXT, bootstrap=True,
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
        controller, nested_loop=NestedLoop.CONTEXT, bootstrap=True,
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
    # Window-scoped degraded-span count (status-transition ledger). The
    # cumulative ``sequence_gaps`` counter is transport telemetry only and
    # must never gate data quality — one reconnect must not poison every
    # future cycle.
    from market_service.nooa_harness.inference.tooling.replay_adapter import degraded_spans_in_window
    _spine_window_ms = 30 * 60_000
    try:
        _degraded = await degraded_spans_in_window(
            engine.store, engine.symbol, engine.venue, window_ms=_spine_window_ms,
        )
    except Exception:
        _degraded = 0
    # Gate 1 (DATA) runs through the deterministic gate base layer: the
    # verdict is a frozen data contract, not inline ad-hoc logic. The
    # resolved trichotomy is preserved verbatim (behavior-identical); the
    # full verdict rides deterministic_state so a refusal cites its facts.
    data_gate = evaluate_data_gate(
        n_observations=n_observations,
        min_observations=30,
        fit_status=fit_status,
        capture_state=status_obj.get("state"),
        events_in_window=events_in_window,
        degraded_spans_in_window=_degraded,
    )
    gate_status = str(data_gate.facts_dict()["resolved_status"])
    gate_reasons = list(data_gate.reasons)

    # --- TASK DIRECTIVE (record-only provisional — acquisition moved out)
    # The bootstrap runs the canonical understand() entry so the gate
    # record (including gate-refusal artifacts) carries the directive
    # context. These keys are PROVISIONAL and record-only: the plan-bound
    # acquisition below runs post-receipt off the FINAL disposed plan, and
    # run_comprehension overwrites these keys as final authority. One
    # methodology, declared author; no acquisition executes pre-receipt.
    from ..task_directive import understand as _understand
    from .wake import PromptVariable as _PromptVariable
    _pv = _prompt if _prompt is not None else _PromptVariable.from_cli(task, scenario)
    task_directive, task_plan, _ = _understand(_pv)

    # Plan-bound acquisition lives in run_plan_bound_acquisition (below) —
    # it executes AFTER the comprehension receipt freezes, driven by the
    # final disposed plan. Placeholders below preserve the deterministic
    # key order; acquisition overwrites values in place (never re-inserts).
    forecast_result = None
    forecast_log: dict[str, Any] | None = None
    forward_scenario_result: Any = None
    forward_scenario_log: dict[str, Any] | None = None

    # --- READ-PLANE SHAPE (envelope-driven) ---
    # The controller receives the envelope and hands it to the agent;
    # the agent then READS AS IT PLEASES via the tool base to complete
    # its task. The canonical forward ForecastResult is now acquired before
    # interpretation; lower-level calc.* tools remain available as diagnostics
    # but are not the authority for combining statistical paths.
    deterministic_state: dict[str, Any] = {
        "task": task,
        "scenario": scenario,
        "task_directive": task_directive.to_dict(),
        "task_plan": task_plan,
        "wake": {
            "trigger_source": wake.trigger_source,
            "predicates_fired": wake.predicates_fired,
        },
        "capture_status": status,
        "microstructure_evidence": evidence,
        "forecast_result": forecast_result,
        "forward_scenario": forward_scenario_result,
        "forward_scenario_note": ((forward_scenario_log or {}).get("detail")
                                  if isinstance(forward_scenario_log, dict) else "not_run"),
        "gate": {"status": gate_status, "reasons": list(gate_reasons)},
        "data_gate": data_gate.to_dict(),
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
            "sequence_gaps_note": ("cumulative transport telemetry only — NOT a "
                                    "data-quality gate; see degraded_spans_in_window"),
            "degraded_spans_in_window": _degraded,
            "heteroskedasticity_flag": ((evidence or {}).get("price_impact_fit") or {}).get("heteroskedasticity_flag"),
            "fit_status": fit_status,
            "n_observations": n_observations,
            "spine": {"interval_seconds": 10, "window_minutes": 30},
            "note": ("provisional persists while degraded capture spans overlap the "
                     "window or hetero=true; "
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
        _pv = _prompt if _prompt is not None else None
        _preview = (_pv.preview_200 if _pv is not None
                    else (task[:200] if task else None))
        return artifact, {"task": _preview,
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



async def run_plan_bound_acquisition(
    engine: Any, ctx: "_CycleContext", st: Any,
) -> None:
    """Plan-bound acquisition — forecast + scenario AFTER the receipt.

    Driven by the FINAL disposed plan (``st.task_plan``), never the
    provisional one: Phase-B bound horizons/targets steer acquisition for
    the first time. Executes under the COMPREHENSION observation (the
    receipt authorizes its own acquisition) and merges in place into the
    carrier (``st`` shares its state dicts with ``ctx.gathered`` by
    reference, so the denial-handler path sees the merged view).
    """
    task_plan = st.task_plan or {}
    deterministic_state = st.deterministic_state
    accumulated_tool_results = st.accumulated_tool_results
    tool_results = st.tool_results
    capability_log = st.capability_log
    controller = st.controller

    # Directive-driven horizon: the plan binds the horizon the task asked
    # about. Native horizons fit directly; long horizons (15m/1h/4h) are
    # answered through the deterministic long-horizon bridge (native fit
    # projected to H — sigma scaled, skill decayed). Anything outside the
    # vocabulary is refused by the tool with the supported set attached.
    _planned_horizon = task_plan.get("forecast_horizon_ms")
    _forecast_horizon_ms = int(_planned_horizon) if _planned_horizon else 5_000
    _forecast_args = {"symbol": engine.symbol, "venue": engine.venue,
                      "horizon_ms": _forecast_horizon_ms, "window_minutes": 30}
    context._must_authorize_work(
        controller, nested_loop=NestedLoop.CONTEXT,
    )
    forecast_result, forecast_log = await context.execute_tool(
        engine.store, "calc.forward.forecast",
        _forecast_args,
        postgres=engine.postgres, memory=engine.memory, settings=engine.settings,
    )
    capability_log.append(forecast_log)
    controller = controller.record_outcome(
        "calc.forward.forecast", forecast_log, forecast_result,
        args=_forecast_args,
    )
    accumulated_tool_results["calc.forward.forecast"] = forecast_result
    tool_results["calc.forward.forecast"] = forecast_result
    forward_status = (
        forecast_result.get("validation_state")
        if isinstance(forecast_result, dict) else None
    )
    gate_status = ctx.gathered.gate_status if ctx.gathered is not None else "provisional"
    gate_reasons = list(ctx.gathered.gate_reasons) if ctx.gathered is not None else []
    if gate_status == "validated" and forward_status != "validated":
        gate_status = "provisional"
        gate_reasons = list(gate_reasons) + [
            "forward ForecastResult is not OOS-validated"
            if forward_status is not None
            else "forward ForecastResult unavailable"
        ]

    # Directive-driven scenario pre-acquisition: a target-bearing native
    # task gets its P(T)/P(S) curves computed deterministically under the
    # receipt (same authority pattern as the canonical forecast).
    forward_scenario_result: Any = None
    forward_scenario_log: dict[str, Any] | None = None
    if (task_plan.get("horizon_regime") == "native"
            and (task_plan.get("targets") or task_plan.get("invalidations"))):
        _scenario_args = {"symbol": engine.symbol, "venue": engine.venue,
                          "horizon_ms": _forecast_horizon_ms,
                          "targets": [t["value"] for t in task_plan.get("targets")],
                          "invalidations": [s["value"] for s in task_plan.get("invalidations")],
                          "window_minutes": 30}
        context._must_authorize_work(
            controller, nested_loop=NestedLoop.CONTEXT,
        )
        forward_scenario_result, forward_scenario_log = await context.execute_tool(
            engine.store, "calc.forward.scenario",
            _scenario_args,
            postgres=engine.postgres, memory=engine.memory, settings=engine.settings,
        )
        capability_log.append(forward_scenario_log)
        controller = controller.record_outcome(
            "calc.forward.scenario", forward_scenario_log, forward_scenario_result,
            args=_scenario_args,
        )
        accumulated_tool_results["calc.forward.scenario"] = forward_scenario_result
        tool_results["calc.forward.scenario"] = forward_scenario_result

    # In-place merge (key order preserved — placeholders set by bootstrap).
    deterministic_state["forecast_result"] = forecast_result
    deterministic_state["forward_scenario"] = forward_scenario_result
    deterministic_state["forward_scenario_note"] = (
        (forward_scenario_log or {}).get("detail")
        if isinstance(forward_scenario_log, dict) else "not_run")
    deterministic_state["gate"] = {"status": gate_status, "reasons": list(gate_reasons)}
    deterministic_state["forward_gate"] = {
        "status": ((forecast_result or {}).get("validation_state")
                   if isinstance(forecast_result, dict) else "unavailable"),
        "probability_status": (((forecast_result or {}).get("multivariate") or {}).get("probability_status")
                               if isinstance(forecast_result, dict) else "unavailable"),
        "reason": ((forecast_log or {}).get("detail")
                   if isinstance(forecast_log, dict) else "not_run"),
    }
    st.controller = controller
    ctx.controller = controller
