"""GATHER stage — agent-first fetch across the comprehension boundary.

run_agent_fetch: the agent owns acquisition from turn zero — one fetch turn
  on the micro-only tool window, then the deterministic floor fills whatever
  gate input is still absent (agent pull wins; floor never re-pulls thin data
  per the approved cut — no thin-fetch mitigation).
run_data_gate: DATA verdict on the fetched accumulation (zero-LLM doctrine
  ends here by approval: refusal now carries llm_calls >= 1).
run_plan_bound_acquisition: forecast + scenario pre-acquisition driven by
the FROZEN understanding receipt (post-comprehension). The split cures the
ordering inversion: fetch → gate → plan → acquisition, each under the
receipt that authorizes it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from market_service.nooa_harness.inference import (
    _normalize_tool_name,
    capability_log_entry,
)
from ..principles import evaluate_principles as _evaluate_principles
from ..principles import in_fetch_window

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
    """Compat entry — agent fetch then data gate. The runner calls the split
    phases explicitly (fetch → gate → comprehension → plan-bound
    acquisition); direct callers get the combined bootstrap semantics."""
    completed = await run_agent_fetch(engine, ctx)
    if completed is not None:
        return completed
    return await run_data_gate(engine, ctx)


@dataclass(frozen=True)
class _FetchedReads:
    """Agent-first fetch receipt: the agent's own gate-read pull plus
    floor fill, consumed by the DATA gate and seeded into comprehension."""

    accumulated_tool_results: dict[str, Any]
    tool_results: dict[str, Any]
    llm_calls: int
    unexecuted: list[str]
    fetch_note: str
    basis: dict[str, Any]
    llm_tiers: list[str] = field(default_factory=list)


# The fetch window lives in engine/principles.py (single vocabulary) —
# the dispatcher enforces it, the principle names the finding.


async def run_agent_fetch(
    engine: Any, ctx: "_CycleContext",
) -> tuple[InferenceArtifact, dict[str, Any]] | None:
    """Fetch phase — the agent commands the tape-quality reads itself.

    One fetch turn on the micro-only window (authorized as wake-bootstrap at
    the initial observation, exactly like the old floor reads — the membrane
    only grants bootstrap work while no sub-loop is open, so INTAKE opens
    AFTER dispatch). The deterministic floor then fills whatever gate input
    is still absent; agent pulls win and are never re-pulled. An
    unparseable/empty fetch degrades to full-floor, never a terminal — only
    transport failure ends the cycle here.
    """
    from ..config import MAX_DISPATCHES_PER_PASS
    from ..kb import compose_fetch_prompt
    from ..llm import last_tier
    from ..schemas import coerce_turn, extract_json_object

    _prompt = getattr(ctx, "prompt", None)
    wake = ctx.wake
    if _prompt is not None:
        task, scenario = _prompt.raw, _prompt.scenario
        task_block = _prompt.block_4000
    else:
        task, scenario = ctx.task, ctx.scenario
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
    wake_block = json.dumps(
        {"trigger_source": wake.trigger_source,
         "predicates": wake.predicates_fired},
        default=str,
    )
    controller, capability_log = ctx.controller, ctx.capability_log
    accumulated_tool_results: dict[str, Any] = {}
    tool_results: dict[str, Any] = {}
    unexecuted: list[str] = []
    llm_tiers: list[str] = []

    fetch_prompt = compose_fetch_prompt(
        controller=controller,
        task_block=task_block,
        scenario_block=scenario_block,
        wake_block=wake_block,
        dispatches_left=MAX_DISPATCHES_PER_PASS,
    )
    try:
        raw_fetch = await engine._call_llm(fetch_prompt)
        llm_calls = 1
        try:
            llm_tiers.append(last_tier() or "unknown")
        except Exception:
            llm_tiers.append("unknown")
        parsed_fetch = coerce_turn(extract_json_object(raw_fetch))
    except Exception:
        # Transport failure is not a fetch failure: the floor below fills
        # everything and the cycle continues agent-less-but-grounded. Only
        # a later turn's failure can still terminate the cycle.
        log.warning("fetch turn transport failed; floor fills defaults")
        llm_calls = 0
        parsed_fetch = None
    fetch_calls = (
        parsed_fetch.get("tool_calls")
        if isinstance(parsed_fetch, dict) else None
    )
    agent_pulled: list[str] = []
    if isinstance(fetch_calls, list) and fetch_calls:
        dict_calls = [c for c in fetch_calls if isinstance(c, dict)]
        for call in dict_calls[MAX_DISPATCHES_PER_PASS:]:
            raw_name = str(call.get("name") or call.get("tool") or "").strip()
            if raw_name:
                unexecuted.append(raw_name)
                log.warning("fetch ceiling: deferring %s to unexecuted", raw_name)
        for call in dict_calls[:MAX_DISPATCHES_PER_PASS]:
            raw_name = str(call.get("name") or call.get("tool") or "").strip()
            args = dict(call.get("args") or {})
            args.setdefault("symbol", engine.symbol)
            args.setdefault("venue", engine.venue)
            canonical = _normalize_tool_name(raw_name) or raw_name
            if not in_fetch_window(canonical):
                denied_log = capability_log_entry(
                    f"tool.denied:{canonical}",
                    {"symbol": engine.symbol, "venue": engine.venue},
                    "denied",
                    detail={"reason": "fetch_window_violation"},
                )
                capability_log.append(denied_log)
                controller = controller.record_outcome(
                    canonical, denied_log, None, raw_name=raw_name, args=args,
                )
                if raw_name and raw_name not in unexecuted:
                    unexecuted.append(raw_name)
                continue
            context._must_authorize_work(
                controller, nested_loop=NestedLoop.CONTEXT, bootstrap=True,
            )
            try:
                result, tool_log = await context.execute_tool(
                    engine.store, raw_name, args,
                    postgres=engine.postgres, memory=engine.memory,
                    settings=engine.settings,
                )
            except Exception as exc:
                log.exception("fetch: tool %s raised", raw_name)
                result, tool_log = None, capability_log_entry(
                    f"tool.error:{canonical}",
                    {"symbol": engine.symbol, "venue": engine.venue},
                    "error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            capability_log.append(tool_log)
            controller = controller.record_outcome(
                canonical, tool_log, result, raw_name=raw_name, args=args,
            )
            agent_pulled.append(canonical)
            if result is None:
                result_payload = None
            else:
                rendered = json.dumps(result, default=str)
                result_payload = json.loads(rendered)
            tool_results[canonical] = result_payload
            accumulated_tool_results[canonical] = result_payload
    # --- DETERMINISTIC FLOOR (liveness guarantee, never a re-pull) ---
    # Fills whatever gate input the agent did not supply. Agent pulls win:
    # present non-null payloads are never fetched twice. An unparseable or
    # empty fetch degrades to full floor here, not to a terminal.
    floor_filled: list[str] = []
    if accumulated_tool_results.get("micro.capture_status") is None:
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
        floor_filled.append("micro.capture_status")
    if accumulated_tool_results.get("micro.fit_beta") is None:
        context._must_authorize_work(
            controller, nested_loop=NestedLoop.CONTEXT, bootstrap=True,
        )
        evidence, fit_log = await context.execute_tool(
            engine.store, "micro.fit_beta",
            {"symbol": engine.symbol, "venue": engine.venue,
             "interval_seconds": 10, "window_minutes": 30},
            postgres=engine.postgres, memory=engine.memory,
            settings=engine.settings,
        )
        capability_log.append(fit_log)
        controller = controller.record_outcome(
            "micro.fit_beta", fit_log, evidence,
            args={"symbol": engine.symbol, "venue": engine.venue,
                  "interval_seconds": 10, "window_minutes": 30},
        )
        accumulated_tool_results["micro.fit_beta"] = evidence
        tool_results["micro.fit_beta"] = evidence
        floor_filled.append("micro.fit_beta")
    # INTAKE opens now that the bootstrap-authorized work is done: the
    # membrane grants bootstrap work only while no sub-loop is open, so the
    # fetch dispatches above ran at the initial observation and the loop
    # opens here for comprehension to continue (INTERPRETATION next).
    controller, _ = context._must_govern(
        controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE
    )
    if agent_pulled:
        fetch_note = (
            f"Fetch turn: you pulled {', '.join(agent_pulled)}"
            + (f"; floor filled {', '.join(floor_filled)}" if floor_filled else "")
            + ". Do not re-pull gate reads."
        )
    else:
        fetch_note = (
            "Fetch turn supplied no usable reads — floor pulled defaults"
            f" ({', '.join(floor_filled) or 'none' }). Do not re-pull gate reads."
        )
    basis = {
        "status_source": (
            "agent" if "micro.capture_status" in agent_pulled else "floor"
        ),
        "fit_source": (
            "agent" if "micro.fit_beta" in agent_pulled else "floor"
        ),
        "agent_pulled": list(agent_pulled),
        "floor_filled": list(floor_filled),
        "unexecuted": list(unexecuted),
        "window_findings": [
            f.detail for f in _evaluate_principles(
                controller=None, plan=None,
                turn={"tool_calls": [
                    {"name": name} for name in unexecuted
                    if not in_fetch_window(name)
                ]},
                fetch_turn=True,
            )
        ],
    }
    ctx.controller = controller
    ctx.fetched = _FetchedReads(
        accumulated_tool_results=accumulated_tool_results,
        tool_results=tool_results,
        llm_calls=llm_calls,
        unexecuted=list(unexecuted),
        fetch_note=fetch_note,
        basis=basis,
        llm_tiers=list(llm_tiers),
    )
    return None


async def run_data_gate(
    engine: Any, ctx: "_CycleContext",
) -> tuple[InferenceArtifact, dict[str, Any]] | None:
    """DATA gate on the fetched accumulation (agent pull + floor fill).

    Verdicts the fetch: insufficient tape refuses the cycle (now carrying
    the fetch turn's LLM cost, per the approved Option-2 cut), otherwise
    freezes the gathered evidence comprehension plans against."""
    # Canonical read: task/scenario flow from the injected PromptVariable
    # (normalized once in wake.run_wake); raw ctx.task/scenario are the
    # compat fallback for callers that bypass run_wake.
    fetched = ctx.fetched
    if fetched is None:
        raise RuntimeError("data gate requires fetched reads")
    _prompt = getattr(ctx, "prompt", None)
    wake = ctx.wake
    if _prompt is not None:
        task, scenario = _prompt.raw, _prompt.scenario
    else:
        task, scenario = ctx.task, ctx.scenario
    generated_at = ctx.generated_at
    controller, capability_log = ctx.controller, ctx.capability_log
    # The accumulation is the agent's pull plus the floor fill (owned by
    # run_agent_fetch); the gate judges it, it never re-fetches here.
    accumulated_tool_results = dict(fetched.accumulated_tool_results)
    tool_results = dict(fetched.tool_results)
    status = accumulated_tool_results.get("micro.capture_status")
    evidence = accumulated_tool_results.get("micro.fit_beta")

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
    # The gate runs the canonical understand() entry so the gate record
    # (including gate-refusal artifacts) carries the directive context. These keys are PROVISIONAL and record-only: the plan-bound
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
        "bootstrap_basis": fetched.basis,
        "fetch_note": fetched.fetch_note,
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
                          "llm_calls": fetched.llm_calls, "gate": gate_status,
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
