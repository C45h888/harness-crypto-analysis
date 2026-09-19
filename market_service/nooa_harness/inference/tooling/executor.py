"""Tool router — single entry point for the narration loop (extracted verbatim)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry
from .history_adapter import _tool_fit_beta
from .registry import TOOL_NAMES, _normalize_tool_name
from .replay_adapter import _forward_replay_inputs  # noqa: F401 (shared core)
from .tick_guard import _resolved_tick_for_dispatch
from .tools_evidence import (
    dispatch_calc_decay_report,
    dispatch_calc_discipline_audit,
    dispatch_calc_events,
    dispatch_calc_hypothesis_test,
)
from .tools_forward import (
    dispatch_calc_feature_build,
    dispatch_calc_forward_distribution,
    dispatch_calc_forward_fit,
    dispatch_calc_forward_forecast,
    dispatch_calc_forward_join,
    dispatch_calc_forward_scenario,
)
from .tools_legacy import (
    dispatch_calc_ad_average,
    dispatch_calc_derived_diagnostic,
    dispatch_calc_fit_depth_scaling,
    dispatch_calc_fit_price_impact,
    dispatch_calc_observation_build,
    dispatch_calc_ofi_intervals,
    dispatch_calc_scenario_evaluate,
)
from .tools_market import (
    dispatch_assemble_evidence,
    dispatch_market_read,
    dispatch_memory_recall_paper,
    dispatch_read_capture_status,
    dispatch_read_derivatives,
    dispatch_read_events,
    dispatch_read_evidence,
    dispatch_read_intervals,
    dispatch_read_keystone_history,
    dispatch_read_wall_history,
    dispatch_replay,
    dispatch_substrate_invoke,
    dispatch_substrate_read,
)

async def execute_tool(
    store: RedisRuntimeStore, name: str, args: dict[str, Any],
    *, postgres: Any | None = None, memory: Any | None = None,
    settings: Any | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Execute one tool call by public name with scope validation + audit.

    This is the single entry point the narration loop uses for the LLM's
    ``tool_calls``. Unknown tool names and out-of-scope dispatches return a
    structured ``denied`` result — never an exception to the caller.

    Injected dependencies (two-plane boundary pass, 2026-09-04):
    - ``store``    — the engine's own Redis connection (shared, never rebuilt)
    - ``postgres`` — the engine's durable store, for fit tools' prior cycles
    - ``memory``   — the engine's MemoryNode, for memory.recall_paper
    - ``settings`` — the operator Settings (tolerated by every tool).
    """
    canonical = _normalize_tool_name(name)
    if canonical is None:
        attempted = {
            "name": name,
            "arg_keys": sorted(args.keys()) if isinstance(args, dict) else None,
        }
        return None, capability_log_entry(
            "tool.unknown", {"name": name}, "denied",
            detail={"attempted": attempted, "allowed": sorted(TOOL_NAMES)},
        )
    name = canonical
    symbol = str(args.get("symbol", "")).upper()
    venue = str(args.get("venue", "spot"))
    if name == "micro.capture_status":
        return await dispatch_read_capture_status(store, symbol, venue)
    if name == "micro.events":
        return await dispatch_read_events(
            store, symbol, venue, count=int(args.get("count") or 200),
        )
    if name == "micro.ofi_intervals":
        return await dispatch_read_intervals(
            store, symbol, venue, count=int(args.get("count") or 200),
        )
    if name == "micro.replay":
        payloads = list(args.get("event_payloads") or [])
        return dispatch_replay(
            payloads, symbol=symbol, venue=venue,
            interval_ms=int(args.get("interval_ms") or 10_000),
        )
    if name == "micro.fit_beta":
        return await _tool_fit_beta(store, symbol, venue, args, postgres=postgres)
    if name == "micro.evidence":
        return await dispatch_read_evidence(store, symbol, venue)
    if name == "market.read":
        return await dispatch_market_read(
            store, symbol, mode=str(args.get("mode") or "snapshot"),
            venue=venue,
        )
    if name == "market.derivatives":
        return await dispatch_read_derivatives(store, symbol, venue=venue)
    if name == "market.keystone_history":
        return await dispatch_read_keystone_history(
            store, symbol, count=int(args.get("count") or 100), venue=venue,
        )
    if name == "market.wall_history":
        return await dispatch_read_wall_history(
            store, symbol, count=int(args.get("count") or 100), venue=venue,
        )
    if name == "substrate.read":
        return await dispatch_substrate_read(
            store, symbol,
            substrate=args.get("substrate"),
            mode=str(args.get("mode") or "compact"), venue=venue,
        )
    if name == "substrate.invoke" or (
        name.startswith("substrate.") and name not in ("substrate.read",)
    ):
        # ``substrate.invoke`` means "every registered worker" — target stays
        # None unless the caller names one. Deriving it from the tool name
        # would yield the literal "invoke", which is not a registered worker
        # and gets denied.
        if name == "substrate.invoke":
            target = str(args["substrate"]) if args.get("substrate") else None
        else:
            target = str(args.get("substrate") or name.split(".", 1)[1])
        return await dispatch_substrate_invoke(
            store, symbol, target, postgres=postgres, venue=venue,
        )
    if name == "calc.ofi.intervals":
        return await dispatch_calc_ofi_intervals(store, symbol, venue, interval_ms=int(args.get("interval_ms") or args.get("interval_seconds", 10)*1000 if "interval_seconds" in args else 10_000), window_minutes=int(args.get("window_minutes") or 30), postgres=postgres)
    if name == "calc.depth.average":
        return await dispatch_calc_ad_average(store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30), postgres=postgres)
    if name == "calc.observation.build":
        return await dispatch_calc_observation_build(store, symbol, venue, interval_seconds=int(args.get("interval_seconds") or 10), window_minutes=int(args.get("window_minutes") or 30), postgres=postgres)
    if name == "calc.fit.price_impact":
        return await dispatch_calc_fit_price_impact(store, symbol, venue, interval_seconds=int(args.get("interval_seconds") or 10), window_minutes=int(args.get("window_minutes") or 30), postgres=postgres)
    if name == "calc.fit.depth_scaling":
        return await dispatch_calc_fit_depth_scaling(store, symbol, venue, postgres=postgres)
    if name in ("calc.derived_diagnostic", "calc.price.delta"):
        return await dispatch_calc_derived_diagnostic(
            store, symbol, venue,
            interval_seconds=int(args.get("interval_seconds") or 10),
            window_minutes=int(args.get("window_minutes") or 30),
            ofi=args.get("ofi"), postgres=postgres,
        )
    if name == "calc.scenario.evaluate":
        return await dispatch_calc_scenario_evaluate(
            store, symbol, venue,
            target_price=args.get("target_price"),
            horizon=str(args.get("horizon") or "1h"),
            interval_seconds=int(args.get("interval_seconds") or 10),
            window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args),
            postgres=postgres,
        )
    if name == "calc.feature.build":
        return await dispatch_calc_feature_build(
            store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30), postgres=postgres)
    if name == "calc.forward.join":
        return await dispatch_calc_forward_join(
            store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args), postgres=postgres)
    if name == "calc.forward.fit":
        return await dispatch_calc_forward_fit(
            store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30),
            horizon_ms=int(args.get("horizon_ms") or 5000),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args), postgres=postgres)
    if name == "calc.forward.distribution":
        theta = args.get("theta_ticks")
        return await dispatch_calc_forward_distribution(
            store, symbol, venue, horizon_ms=int(args.get("horizon_ms") or 5000),
            theta_ticks=theta, window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args), postgres=postgres)
    if name == "calc.forward.scenario":
        return await dispatch_calc_forward_scenario(
            store, symbol, venue, horizon_ms=int(args.get("horizon_ms") or 5000),
            targets=list(args.get("targets") or []), invalidations=list(args.get("invalidations") or []),
            window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args), postgres=postgres)
    if name == "calc.forward.forecast":
        return await dispatch_calc_forward_forecast(
            store, symbol, venue, horizon_ms=int(args.get("horizon_ms") or 5000),
            window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args), postgres=postgres)
    if name == "calc.hypothesis.test":
        return await dispatch_calc_hypothesis_test(
            store, symbol, venue, hypothesis_id=str(args.get("hypothesis_id") or ""),
            horizon_ms=int(args.get("horizon_ms") or 5000),
            m_tests=int(args.get("m_tests") or 1),
            window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args), postgres=postgres)
    if name in ("calc.events.absorption", "calc.events.walls"):
        kind = "absorption" if name.endswith("absorption") else "walls"
        return await dispatch_calc_events(
            store, symbol, venue, kind=kind,
            window_minutes=int(args.get("window_minutes") or 30), postgres=postgres)
    if name == "calc.decay.report":
        return await dispatch_calc_decay_report(
            store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args), postgres=postgres)
    if name == "calc.discipline.audit":
        return await dispatch_calc_discipline_audit(
            store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30),
            tick_size=_resolved_tick_for_dispatch(symbol, venue, args),
            cost_statement=args.get("cost_statement"), postgres=postgres)
    if name == "memory.recall_paper":
        return await dispatch_memory_recall_paper(
            symbol, venue, query=str(args.get("query") or "Cont OFI AD beta"),
            memory=memory,
        )
    return None, capability_log_entry(
        "tool.unrouted", {"name": name}, "denied", detail="no dispatch path",
    )

