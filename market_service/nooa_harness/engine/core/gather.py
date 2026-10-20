"""GATHER stage — plan-bound acquisition under the frozen receipt.

Phase R7 (fetch semantic removed): the agent-first fetch turn, the DATA
gate on model-chosen cadence data, and the wake-bootstrap authorization
are GONE. The stage is now one thing: ``run_plan_bound_acquisition`` —
forecast + scenario pre-acquisition driven by the FROZEN understanding
receipt (post-comprehension), at the CONTROLLER-adopted plan cadence.

The old semantics dissolve rather than relocate:
  - run_wake is the single deterministic INTAKE (normalization, no tools).
  - run_comprehension is the canonical first AGENTIC step (INTERPRETATION
    → FRAMING, cadence adopted + logged there).
  - every read after the receipt is agent-pulled under the normal FSM
    authorization (evidence loop); nothing pre-dates the observation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..fsm import GovernanceEventKind
from ..loop_states import NestedLoop
from market_service.runtime.contracts import InferenceArtifact  # noqa: F401 (compat type)
from . import context
from .. import gates
from .context import _CycleContext, _GatheredEvidence

log = logging.getLogger(__name__)


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

    Phase R6 (cadence-into-plan): interval/window/horizon come from the
    CONTROLLER-adopted cadence (comprehension adoption, sole semantic
    authority) — never inline defaults. Unadopted controllers (legacy
    tests/direct construction) fall back to the resolver defaults.
    """
    task_plan = st.task_plan or {}
    deterministic_state = st.deterministic_state
    accumulated_tool_results = st.accumulated_tool_results
    tool_results = st.tool_results
    capability_log = st.capability_log
    controller = st.controller

    from ..cadence import (
        DEFAULT_FORECAST_MS,
        DEFAULT_INTERVAL_SECONDS,
        DEFAULT_WINDOW_MINUTES,
    )
    # Cadence floor transparency (G1): adopted cadence wins; any fallback
    # to deterministic defaults is an explicit floor, logged as a finding
    # — never a silent default masquerading as a binding.
    _adopted_interval = controller.cadence_value("interval_seconds")
    _adopted_window = controller.cadence_value("window_minutes")
    _adopted_horizon = controller.cadence_value("horizon_ms")
    _interval = _adopted_interval or DEFAULT_INTERVAL_SECONDS
    _window = _adopted_window or DEFAULT_WINDOW_MINUTES
    _planned_horizon = task_plan.get("forecast_horizon_ms")
    _forecast_horizon_ms = int(_planned_horizon) if _planned_horizon else (
        _adopted_horizon or DEFAULT_FORECAST_MS)
    if _adopted_interval is None or _adopted_window is None or _adopted_horizon is None:
        capability_log.append({
            "capability": "engine.cadence.floor",
            "scope": {"symbol": engine.symbol, "venue": engine.venue},
            "result": "ok",
            "detail": {
                "interval_seconds": int(_interval),
                "window_minutes": int(_window),
                "horizon_ms": int(_forecast_horizon_ms),
                "adopted": {
                    "interval_seconds": _adopted_interval,
                    "window_minutes": _adopted_window,
                    "horizon_ms": _adopted_horizon,
                },
            },
            "governance": "deterministic floor (unadopted controller) — not a binding",
        })
    _forecast_args = {"symbol": engine.symbol, "venue": engine.venue,
                      "horizon_ms": _forecast_horizon_ms,
                      "window_minutes": int(_window)}
    context._must_authorize_work(
        controller, nested_loop=NestedLoop.CONTEXT,
    )
    # Digest authority (not agent proof): the plan-bound forecast is
    # computed for the GATE + envelope + artifact digest, but it confers
    # NO chain/position/phase credit and is NEVER placed in accumulated
    # (agent-visible). The agent earns the multivariate truth with its own
    # 2b call; on a failed 2b call the digest is revealed once as a bounded
    # failure-substrate fallback (driver, st.digest_revealed). This ends
    # the double-see: digest status travels, digest numbers do not — unless
    # the agent fails, exactly once.
    forecast_result, forecast_log = await context.execute_tool(
        engine.store, "calc.forward.forecast",
        _forecast_args,
        postgres=engine.postgres, memory=engine.memory, settings=engine.settings,
        session_id=engine.session_id, segment=getattr(ctx, "task_id", None),
    )
    capability_log.append(forecast_log)
    capability_log.append({
        "capability": "engine.forecast.digest",
        "scope": {"symbol": engine.symbol, "venue": engine.venue},
        "result": "ok",
        "detail": {
            "authority": "receipt_digest_not_proof",
            "note": ("plan-bound digest for gate/envelope only; "
                       "agent proof requires a 2b calc.forward.forecast call"),
        },
    })
    forward_status = (
        forecast_result.get("validation_state")
        if isinstance(forecast_result, dict) else None
    )
    gate_status = (
        ctx.gathered.gate_status if ctx.gathered is not None else "provisional"
    )
    gate_reasons = list(ctx.gathered.gate_reasons) if ctx.gathered is not None else []
    # DATA gate: the status is adjudicated by the gate registry, not written
    # inline. Policy is unchanged (provisional unless insufficient).
    _data_verdict = gates.evaluate(gates.GateKind.DATA, {
        "status": gate_status, "reasons": list(gate_reasons)})
    gates.record_verdict(_data_verdict, st,
                         segment=getattr(ctx, "task_id", None))
    gate_status = str(_data_verdict.facts_dict().get("status") or gate_status)
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
                          "window_minutes": int(_window)}
        context._must_authorize_work(
            controller, nested_loop=NestedLoop.CONTEXT,
        )
        forward_scenario_result, forward_scenario_log = await context.execute_tool(
            engine.store, "calc.forward.scenario",
            _scenario_args,
            postgres=engine.postgres, memory=engine.memory, settings=engine.settings,
            session_id=engine.session_id, segment=getattr(ctx, "task_id", None),
        )
        capability_log.append(forward_scenario_log)
        controller = controller.record_outcome(
            "calc.forward.scenario", forward_scenario_log, forward_scenario_result,
            args=_scenario_args,
        )
        accumulated_tool_results["calc.forward.scenario"] = forward_scenario_result
        tool_results["calc.forward.scenario"] = forward_scenario_result

    # Long-horizon scenario pre-acquisition (plan alignment): build_plan
    # advertises calc.scenario.evaluate for long-horizon targets; honor it
    # under the SAME receipt authority as the forecast digest — computed for
    # the envelope/gate, NEVER placed in accumulated (agent proof still
    # requires its own 2b call). Without this the plan advertised work the
    # runtime silently ignored.
    scenario_digest: Any = None
    if (task_plan.get("horizon_regime") == "long"
            and (task_plan.get("targets") or task_plan.get("invalidations"))):
        _digest_targets = [t["value"] for t in task_plan.get("targets") or []]
        _digest_horizon = str(task_plan.get("scenario_horizon") or "")
        if _digest_targets and _digest_horizon:
            _digest_args = {
                "symbol": engine.symbol, "venue": engine.venue,
                "target_price": _digest_targets[0],
                "horizon": _digest_horizon,
                "interval_seconds": int(_interval),
                "window_minutes": int(_window),
            }
            try:
                scenario_digest, _digest_log = await context.execute_tool(
                    engine.store, "calc.scenario.evaluate", _digest_args,
                    postgres=engine.postgres, memory=engine.memory,
                    settings=engine.settings, session_id=engine.session_id,
                    segment=getattr(ctx, "task_id", None),
                )
                # ONE digest row, never the raw tool log: the agent's own
                # dispatch must remain the only calc.scenario.evaluate entry
                # in the operator ledger (proof vs digest stays legible).
                capability_log.append({
                    "capability": "engine.scenario.digest",
                    "scope": {"symbol": engine.symbol, "venue": engine.venue},
                    "result": (str((_digest_log or {}).get("result") or "ok")),
                    "detail": {
                        "authority": "receipt_digest_not_proof",
                        "target_price": _digest_targets[0],
                        "horizon": _digest_horizon,
                        "tool_detail": (_digest_log or {}).get("detail"),
                        "note": ("plan-bound scenario digest for envelope only; "
                                 "agent proof requires its own "
                                 "calc.scenario.evaluate call"),
                    },
                })
            except Exception as exc:  # never kill the cycle on the digest
                capability_log.append({
                    "capability": "engine.scenario.digest",
                    "scope": {"symbol": engine.symbol, "venue": engine.venue},
                    "result": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                })

    # In-place merge (key order preserved — placeholders set by bootstrap).
    _acq = deterministic_state.setdefault("acquisition", {})
    _acq["forecast_result"] = forecast_result
    _acq["forward_scenario"] = forward_scenario_result
    _sfacts = deterministic_state.setdefault("scenario_facts", {})
    _sfacts["digest"] = scenario_digest
    _acq["forward_scenario_note"] = (
        (forward_scenario_log or {}).get("detail")
        if isinstance(forward_scenario_log, dict) else "not_run")
    _acq["forward_gate"] = {
        "status": ((forecast_result or {}).get("validation_state")
                   if isinstance(forecast_result, dict) else "unavailable"),
        "probability_status": (((forecast_result or {}).get("multivariate") or {}).get("probability_status")
                               if isinstance(forecast_result, dict) else "unavailable"),
        "reason": ((forecast_log or {}).get("detail")
                   if isinstance(forecast_log, dict) else "not_run"),
    }
    st.controller = controller
    ctx.controller = controller