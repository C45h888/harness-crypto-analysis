"""Tool base — the agent's commandable calculation surface.

Tools are named, scope-validated registry entries the LLM can COMMAND in
its narration call (it never recomputes; it dispatches). Every dispatch is
deterministic, produces a capability_log audit entry, and returns
JSON-transportable output. Two families:

  T1 micro   — the Pass-3 paper-derived microstructure stack
  T2 market  — the canonical pipeline's calculation groups + ledger reads

Tool dispatch may run at most ONE round per narration cycle (spec §4);
results are cited evidence, never memory.

Moved verbatim from the inference.py monolith (decomposition Phase 0).
This module is openai-free (the client is injected via ``backends.build_llm``).
"""

from __future__ import annotations

from typing import Any

from market_service.microstructure import fitting
from market_service.runtime.redis_store import RedisRuntimeStore

from .capability import (
    CAPABILITIES,
    CapabilityDenied,
    capability_log_entry,
)

# ---------------------------------------------------------------------------
# Pass-A dispatchers (T1 micro core)
# ---------------------------------------------------------------------------


async def dispatch_read_capture_status(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Capability: redis.read_capture_status."""
    cap = CAPABILITIES["redis.read_capture_status"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        status = await store.read_microstructure_status(venue, symbol.upper())
        return status, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_events(
    store: RedisRuntimeStore, symbol: str, venue: str, *, count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Capability: redis.read_events."""
    cap = CAPABILITIES["redis.read_events"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(
            venue, symbol.upper(), count=count,
        )
        return payloads, capability_log_entry(
            cap.name, scope, "ok", detail={"entries": len(payloads)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


def dispatch_replay(
    event_payloads: list[dict[str, Any]], *, symbol: str, venue: str, interval_ms: int,
) -> tuple[list[Any], list[Any], int, dict[str, Any]]:
    """Capability: fitting.replay — pure, synchronous.

    Returns (events, intervals, dropped_count, log_entry).
    """
    cap = CAPABILITIES["fitting.replay"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_ms": interval_ms}
    try:
        cap.validate_scope(symbol, venue)
        events, dropped = fitting.replay_events_from_payloads(event_payloads)
        intervals = fitting.replay_intervals(events, interval_ms=interval_ms)
        return events, intervals, dropped, capability_log_entry(
            cap.name, scope, "ok",
            detail={"events": len(events), "intervals": len(intervals), "dropped": dropped},
        )
    except CapabilityDenied as exc:
        return [], [], 0, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


def dispatch_assemble_evidence(
    intervals: list[Any], *, symbol: str, venue: str, tick_size: Any,
    interval_seconds: int, evidence_id: str, generated_at_ms: int,
    events: list[Any] | None = None, coverage: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Capability: fitting.assemble_evidence — pure, synchronous.

    Returns (evidence_or_None, log_entry). A CapabilityDenied scope refusal
    yields (None, denied-entry) without touching the fitter.
    """
    cap = CAPABILITIES["fitting.assemble_evidence"]
    scope = {"symbol": symbol.upper(), "venue": venue,
             "interval_seconds": interval_seconds}
    try:
        cap.validate_scope(symbol, venue)
        evidence = fitting.assemble_evidence(
            intervals, symbol=symbol, venue=venue, tick_size=tick_size,
            interval_seconds=interval_seconds, evidence_id=evidence_id,
            generated_at_ms=generated_at_ms, events=events, coverage=coverage,
        )
        return evidence, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


# ---------------------------------------------------------------------------
# Tool registry (Pass B2)
# ---------------------------------------------------------------------------

TOOL_NAMES: dict[str, str] = {
    # T1 — microstructure (paper stack) — split AD/OFI per Pass C
    "micro.capture_status": "redis.read_capture_status",
    "micro.events": "redis.read_events",
    "micro.ofi_intervals": "redis.read_intervals",
    "micro.replay": "fitting.replay",
    "micro.fit_beta": "fitting.assemble_evidence",
    "micro.evidence": "redis.read_evidence",
    # T1 split: AD/OFI separate tools, final fit is hypothesis validation
    "calc.ofi.intervals": "calc.ofi_intervals",
    "calc.depth.average": "calc.ad_average",
    "calc.observation.build": "calc.observation_build",
    "calc.fit.price_impact": "calc.fit_price_impact",
    "calc.fit.depth_scaling": "calc.fit_depth_scaling",
    "calc.derived_diagnostic": "calc.derived_diagnostic",
    "calc.price.delta": "calc.derived_diagnostic",
    "calc.scenario.evaluate": "calc.scenario.evaluate",
    "memory.recall_paper": "memory.recall_paper",
    # T2 — market correlation (canonical pipeline seams)
    "market.read": "market.read",
    "market.derivatives": "market.read_derivatives",
    "market.keystone_history": "market.read_keystone_history",
    "market.wall_history": "market.read_wall_history",
    # T3 — substrate worker plane (tool-first invocation; replaces run_cycle)
    "substrate.read": "substrate.read",
    "substrate.invoke": "substrate.invoke",
    "substrate.anchors": "substrate.anchors",
    "substrate.density": "substrate.density",
    "substrate.delta": "substrate.delta",
    "substrate.ladders": "substrate.ladders",
    "substrate.large_print": "substrate.large_print",
    "substrate.migration": "substrate.migration",
    "substrate.oi": "substrate.oi",
    "substrate.signals": "substrate.signals",
    "substrate.tape": "substrate.tape",
    "substrate.technicals": "substrate.technicals",
    "substrate.tiers": "substrate.tiers",
    "substrate.volume_profile": "substrate.volume_profile",
}

# Phase map for the staged inference cycle (engine drives P1→P5).
# Credit is by TOOL FAMILY actually executed, not by the phase the model
# declares — robust to mislabeled turns. P4 (explanation) needs no tools;
# it is validated through summary/evidence quality at finalization.
TOOL_PHASE: dict[str, str] = {
    # P1 — OFI / tape quality
    "micro.capture_status": "P1",
    "micro.events": "P1",
    "micro.ofi_intervals": "P1",
    "micro.replay": "P1",
    "calc.ofi.intervals": "P1",
    # P2 — AD / observations / fits
    "micro.fit_beta": "P2",
    "micro.evidence": "P2",
    "calc.depth.average": "P2",
    "calc.observation.build": "P2",
    "calc.fit.price_impact": "P2",
    "calc.fit.depth_scaling": "P2",
    # P3 — market correlation (Redis plane)
    "market.read": "P3",
    "market.derivatives": "P3",
    "market.keystone_history": "P3",
    "market.wall_history": "P3",
    # P3 — substrate worker plane (tool-first; replaces run_cycle)
    "substrate.read": "P3",
    "substrate.invoke": "P3",
    "substrate.anchors": "P3",
    "substrate.density": "P3",
    "substrate.delta": "P3",
    "substrate.ladders": "P3",
    "substrate.large_print": "P3",
    "substrate.migration": "P3",
    "substrate.oi": "P3",
    "substrate.signals": "P3",
    "substrate.tape": "P3",
    "substrate.technicals": "P3",
    "substrate.tiers": "P3",
    "substrate.volume_profile": "P3",
    # P5 — paper grounding + derived ΔP
    "memory.recall_paper": "P5",
    "calc.derived_diagnostic": "P5",
    "calc.price.delta": "P5",
    "calc.scenario.evaluate": "P5",
}
_PHASE_ORDER = ("P1", "P2", "P3", "P4", "P5")
_REQUIRED_PHASES = ("P1", "P2", "P3", "P5")


def _bounded(values: list[Any], cap: int) -> list[Any]:
    """Hard output bound for tool payloads (context-budget discipline)."""
    return values[:cap]


async def dispatch_read_intervals(
    store: RedisRuntimeStore, symbol: str, venue: str, *, count: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: micro.ofi_intervals — completed OFI interval rows."""
    cap = CAPABILITIES["redis.read_intervals"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        rows = await store.read_microstructure_intervals(
            venue, symbol.upper(), count=count,
        )
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_evidence(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: micro.evidence — latest immutable evidence object."""
    cap = CAPABILITIES["redis.read_evidence"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        evidence = await store.read_microstructure_evidence(venue, symbol.upper())
        return evidence, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_market_read(
    store: RedisRuntimeStore, symbol: str, *, mode: str = "snapshot",
    venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.read — latest collated market run, raw from Redis.

    Post-envelope deviation: no dataclass round trip. One GET, one
    json.loads, one schema-version guard (read_paths), then a bounded
    projection. ``mode`` selects the agent-facing shape:

      snapshot  — headline scalars + CVD multi-window sign series (default;
                  small by construction, the primary inference view)
      inventory — section key inventory + the snapshot
      full      — the raw collated payload (explicit deep-dive; the
                  engine's 40k tool-result gate is the bound)

    A schema-version mismatch is a structured ``error`` payload, never a
    coercion — the writer is on a different contract and must escalate.
    """
    from market_service.runtime import read_paths

    cap = CAPABILITIES["market.read"]
    scope = {"symbol": symbol.upper(), "venue": venue, "mode": mode}
    try:
        cap.validate_scope(symbol, venue)
        if mode not in ("snapshot", "inventory", "full"):
            raise CapabilityDenied(f"unknown market.read mode: {mode!r}")
        payload = await read_paths.read_collated(store, symbol.upper())
        if payload is None:
            return None, capability_log_entry(
                cap.name, scope, "ok", detail={"status": "no_run_persisted"},
            )
        if mode == "full":
            result: dict[str, Any] = payload
        elif mode == "inventory":
            result = read_paths.market_inventory(payload)
        else:
            result = read_paths.market_snapshot(payload)
        # NaN-safe at the tool seam: the stored payload passed the write
        # gate, but projections traverse live pipeline dicts that may hold
        # raw float('nan') (pipeline flow math). The engine's json.loads
        # round trip would choke on a bare NaN token.
        result = read_paths.json_safe(result)
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"schema_version": payload.get("schema_version")},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except ValueError as exc:
        # Schema guard breach — stale writer on a different contract.
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


async def dispatch_read_derivatives(
    store: RedisRuntimeStore, symbol: str, *, venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.derivatives — cached funding/OI/cross-asset evidence."""
    cap = CAPABILITIES["market.read_derivatives"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        payload = await store.read_derivative_evidence(symbol.upper())
        return payload, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_keystone_history(
    store: RedisRuntimeStore, symbol: str, *, count: int = 100,
    venue: str = "spot",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: market.keystone_history — bounded cross-cycle keystone series."""
    cap = CAPABILITIES["market.read_keystone_history"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        rows = await store.read_keystone_history(symbol.upper(), count=count)
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_wall_history(
    store: RedisRuntimeStore, symbol: str, *, count: int = 100,
    venue: str = "spot",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: market.wall_history — bounded cross-cycle wall series."""
    cap = CAPABILITIES["market.read_wall_history"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        rows = await store.read_wall_history(symbol.upper(), count=count)
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


# ------------------------------------------------------------------
# T3 — substrate worker plane (tool-first invocation; replaces run_cycle)
# ------------------------------------------------------------------

async def dispatch_substrate_read(
    store: RedisRuntimeStore, symbol: str, *, substrate: str | None = None,
    mode: str = "compact", venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: substrate.read — always-fresh worker projections.

    The warm-plane companion of ``market.read``: same compact-by-default
    discipline, ``available:false`` for missing workers (never errors),
    ``json_safe`` at the seam, schema mismatch as structured error.
    """
    from market_service.runtime import read_paths
    from market_service.substrate_worker import tools as substrate_tools

    cap = CAPABILITIES["substrate.read"]
    scope = {"symbol": symbol.upper(), "venue": venue, "substrate": substrate, "mode": mode}
    try:
        cap.validate_scope(symbol, venue)
        if mode not in ("compact", "full"):
            raise CapabilityDenied(f"unknown substrate.read mode: {mode!r}")
        result = await substrate_tools.read_state(
            store, symbol.upper(),
            substrates=[substrate] if substrate else None, mode=mode)
        result = read_paths.json_safe(result)
        return result, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except ValueError as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


async def dispatch_substrate_invoke(
    store: RedisRuntimeStore, symbol: str, substrate: str | None,
    *, postgres: Any | None = None, venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: substrate.<name> / substrate.invoke — bounded worker ticks.

    The engine decides WHEN a calculation must run; this dispatches that
    decision to the calculation container, which decides whether the tick
    actually fires (its cooldown gates are unchanged) and owns the durable
    ledger write. A named worker gets one fire-tick; ``substrate.invoke``
    with no substrate covers every registered worker. Each invocation audits
    under its own capability, exactly as before.

    The request goes over the control plane rather than building a worker
    here: worker identity is ``(substrate, symbol)`` and never the process,
    so an in-process fire would overwrite the live container's supervisor
    heartbeat (and the fire-dedupe high-water state sharing that key) and
    consume its raw-stream entries with ``noack=True``. ``postgres`` is
    accepted for signature compatibility but unused — the calculation
    container holds its own ledger handle.

    An unreachable plane is an ``error`` audit the engine reports as a
    finding; it never falls back to in-process invocation.
    """
    from market_service.substrate_worker.control_client import (
        CalcPlaneRejected,
        CalcPlaneUnreachable,
        request_invoke,
    )

    target = (substrate or "").lower() or None
    cap_name = f"substrate.{target}" if target else "substrate.invoke"
    if cap_name not in CAPABILITIES:
        return None, capability_log_entry(
            "substrate.invoke", {"symbol": symbol.upper(), "substrate": substrate},
            "denied", detail=f"unknown substrate worker {substrate!r}")
    cap = CAPABILITIES[cap_name]
    scope = {"symbol": symbol.upper(), "venue": venue, "substrate": target}
    try:
        cap.validate_scope(symbol, venue)
        report = await request_invoke(
            symbol.upper(), [target] if target else None)
        if target is None:
            return report, capability_log_entry(
                cap.name, scope, "ok",
                detail={"invoked": report.get("invoked"),
                        "fired": report.get("fired")})
        result = _single_report(report, target)
        if not result.get("invoked"):
            return None, capability_log_entry(
                cap.name, scope, "denied", detail=result.get("error"))
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"fired": result.get("fired"),
                    "trigger_source": result.get("trigger_source")})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except CalcPlaneRejected as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except CalcPlaneUnreachable as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


def _single_report(report: dict[str, Any], target: str) -> dict[str, Any]:
    """Unwrap the plane's per-worker report so the tool shape is unchanged.

    ``POST /invoke`` always answers in the ``invoke_many`` shape; a single
    named worker's caller expects the ``invoke`` shape it got in-process.
    """
    for entry in report.get("reports") or []:
        if isinstance(entry, dict) and entry.get("substrate") == target:
            return entry
    return {
        "substrate": target, "symbol": report.get("symbol"), "invoked": False,
        "error": f"calculation plane returned no report for {target!r}",
    }


# ------------------------------------------------------------------
# Pass C split: AD/OFI separate tools + derived diagnostic
# The final formula ΔP = α + c·OFI/AD^λ + (ν·OFI+ε) remains DERIVED
# hypothesis (heteroskedastic ν·OFI), never shortcut calculation.
# ------------------------------------------------------------------

async def dispatch_calc_ofi_intervals(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_ms: int = 10_000, window_minutes: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: calc.ofi.intervals — deterministic OFI per interval (no AD)."""
    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.ofi_intervals"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_ms": interval_ms, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_ms)
        # Return OFI-only projection (paper Cont OFI_k), AD stripped for split discipline
        projected = [{"start_ts_ms": i.start_ts_ms, "end_ts_ms": i.end_ts_ms, "ofi": str(i.ofi), "event_count": i.event_count, "quality": i.quality} for i in intervals]
        return _bounded(projected, 200), capability_log_entry(cap.name, scope, "ok", detail={"intervals": len(intervals), "dropped": dropped, "note": "AD excluded — use calc.depth.average separately"})
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_ad_average(
    store: RedisRuntimeStore, symbol: str, venue: str, *, window_minutes: int = 30,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.depth.average — AD per block, separate from OFI (paper AD_i)."""
    from decimal import Decimal

    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.ad_average"]
    scope = {"symbol": symbol.upper(), "venue": venue, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=10_000)
        # AD per block via DepthAverager semantics: mean (qB+qA)/2
        ads = [str(i.average_depth) if i.average_depth is not None else None for i in intervals]
        valid_ads = [a for a in ads if a is not None]
        mean_ad = str(sum(Decimal(a) for a in valid_ads) / len(valid_ads)) if valid_ads else None
        result = {"window_minutes": window_minutes, "n_intervals": len(intervals), "ad_per_interval": _bounded(ads, 200), "mean_ad": mean_ad, "depth_estimator": fm.DEPTH_ESTIMATOR, "note": "OFI excluded — use calc.ofi.intervals separately; ν·OFI heteroskedastic"}
        return result, capability_log_entry(cap.name, scope, "ok", detail={"mean_ad": mean_ad, "n": len(valid_ads)})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_observation_build(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_seconds: int = 10, window_minutes: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: calc.observation.build — join OFI+AD+ΔP → observations (ΔP ticks vs OFI)."""
    from decimal import Decimal

    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.observation_build"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_seconds": interval_seconds, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_seconds*1000)
        observations, excluded = fm.build_observations(intervals, tick_size=Decimal("0.01"))
        proj = [{"ofi": str(o.ofi), "delta_ticks": str(o.delta_ticks), "average_depth": str(o.average_depth) if o.average_depth else None, "quality": o.quality} for o in observations[:50]]
        return proj, capability_log_entry(cap.name, scope, "ok", detail={"n_observations": len(observations), "excluded": excluded, "note": "ΔP = α+β·OFI observations ready for calc.fit.price_impact"})
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_fit_price_impact(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_seconds: int = 10, window_minutes: int = 30,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.fit.price_impact — OLS ΔP=α+β·OFI (HC0), takes split observations."""
    from decimal import Decimal

    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.fit_price_impact"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_seconds": interval_seconds}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, _ = fm.replay_events_from_payloads(payloads)
        windowed = [e for e in events if e.current.exchange_ts_ms >= (events[-1].current.exchange_ts_ms - window_minutes*60_000)] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_seconds*1000)
        fit, _ = fm.fit_price_impact(intervals, symbol=symbol, venue=venue, tick_size=Decimal("0.01"), interval_seconds=interval_seconds)
        return fit.to_dict(), capability_log_entry(cap.name, scope, "ok", detail={"beta": str(fit.beta), "status": fit.status})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_fit_depth_scaling(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.fit.depth_scaling — lnβ = ln c - λ ln AD, needs ≥3 blocks. Derived diagnostic."""
    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.fit_depth_scaling"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        # For split discipline, we can only fit depth scaling from history — single window gives n_blocks=1 → insufficient by design
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, _ = fm.replay_events_from_payloads(payloads)
        intervals = fm.replay_intervals(events, interval_ms=10_000)
        fit, _ = fm.fit_price_impact(intervals, symbol=symbol, venue=venue, tick_size=fm.DECIMAL("0.01") if hasattr(fm,"DECIMAL") else __import__("decimal").Decimal("0.01"), interval_seconds=10) if intervals else (None,None)
        if fit is None:
            return None, capability_log_entry(cap.name, scope, "ok", detail={"status": "insufficient", "reason": "no intervals"})
        depth_fit = fm.fit_depth_scaling([fit], symbol=symbol, venue=venue)
        return depth_fit.to_dict(), capability_log_entry(cap.name, scope, "ok", detail={"status": depth_fit.status, "n_blocks": depth_fit.n_blocks, "note": "derived, not prediction"})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

def _depth_fit_from_dict(data: dict[str, Any]) -> Any | None:
    """Reconstruct a DepthScalingFit from a persisted dict (PG history or fresh evidence)."""
    try:
        from decimal import Decimal

        from market_service.microstructure.contracts import DepthScalingFit

        def _dec(key: str) -> Decimal | None:
            value = data.get(key)
            return Decimal(str(value)) if value is not None else None

        return DepthScalingFit(
            fit_id=str(data.get("fit_id") or "hist-depth-unknown"),
            symbol=str(data.get("symbol") or "").upper(),
            venue=str(data.get("venue") or ""),
            c=_dec("c"),
            lambda_=_dec("lambda"),
            stderr_lambda=_dec("stderr_lambda"),
            n_blocks=int(data.get("n_blocks") or 0),
            r2=_dec("r2"),
            fit_ids=tuple(str(f) for f in (data.get("fit_ids") or ())),
            depth_estimator=str(data.get("depth_estimator") or ""),
            model_version=str(data.get("model_version") or ""),
            status=str(data.get("status") or "insufficient"),
        )
    except (ValueError, TypeError, ArithmeticError, KeyError):
        return None


async def dispatch_calc_derived_diagnostic(
    store: RedisRuntimeStore, symbol: str, venue: str,
    *, interval_seconds: int = 10, window_minutes: int = 30,
    ofi: Any | None = None, postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.price.delta (alias calc.derived_diagnostic) — NUMERIC derived ΔP.

    P5 derivation endpoint: an OFI scenario value (or the latest closed
    interval's OFI by default) is run through the FITTED models via
    ``fitting.derive_price_delta`` — route A direct plus route B
    depth-scaled when c/λ identify. Refuses (result None, never zero) on
    gate-failed fits, empty tapes, or unparseable inputs.
    """
    from decimal import Decimal

    from market_service.microstructure import fitting as fm

    cap = CAPABILITIES["calc.derived_diagnostic"]
    scope = {"symbol": symbol.upper(), "venue": venue,
              "interval_seconds": interval_seconds,
              "window_minutes": window_minutes, "ofi": ofi}
    try:
        cap.validate_scope(symbol, venue)
        evidence_dict, _fit_log = await _tool_fit_beta(
            store, symbol, venue,
            {"interval_seconds": interval_seconds,
             "window_minutes": window_minutes, "tick_size": "0.01"},
            postgres=postgres,
        )
        if not isinstance(evidence_dict, dict):
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no evidence window"},
            )
        price_fit = _price_fit_from_dict(evidence_dict.get("price_impact_fit") or {})
        if price_fit is None:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "unparseable price fit"},
            )
        dsf_dict = evidence_dict.get("depth_scaling_fit")
        depth_fit = (_depth_fit_from_dict(dsf_dict)
                     if isinstance(dsf_dict, dict) else None)
        if ofi is not None:
            try:
                ofi_dec = Decimal(str(ofi))
            except Exception:
                return None, capability_log_entry(
                    cap.name, scope, "ok",
                    detail={"status": "refused",
                            "reason": f"unparseable ofi scenario: {ofi!r}"},
                )
            ofi_source = "scenario_arg"
        else:
            payloads = await store.read_microstructure_events(venue, symbol.upper())
            events, _dropped = fm.replay_events_from_payloads(payloads)
            intervals = fm.replay_intervals(events, interval_ms=interval_seconds * 1_000)
            if not intervals:
                return None, capability_log_entry(
                    cap.name, scope, "ok",
                    detail={"status": "refused",
                            "reason": "no closed intervals for default OFI"},
                )
            ofi_dec = intervals[-1].ofi
            ofi_source = "latest_interval"
        tick_size = Decimal(str(evidence_dict.get("tick_size") or "0.01"))
        try:
            derived = fm.derive_price_delta(
                price_fit, ofi=ofi_dec, tick_size=tick_size,
                depth_fit=depth_fit, average_depth=price_fit.mean_ad,
            )
        except ValueError as vex:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex),
                        "fit_id": price_fit.fit_id},
            )
        route_b = derived.get("route_b_depth_scaled") or {}
        result = {
            "formula": "ΔP_k = α_i + c·OFI_k/AD_i^λ + (ν_i·OFI_k + ε_k)",
            "ofi": str(ofi_dec),
            "ofi_source": ofi_source,
            **derived,
            "units": {"price_unit": "ticks", "tick_size": str(tick_size)},
            "heteroskedasticity": {
                "flag": derived.get("heteroskedasticity_flag"),
                "warning": ("ν·OFI term: error variance grows with |OFI| — "
                              "bands widen on large flow; diagnostic, never a point prediction"),
            },
            "status": "derived_ok",
            "paper": "Cont 1011.6402 §3",
        }
        route_a = derived.get("route_a_direct") or {}
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": "derived_ok",
                    "delta_ticks": route_a.get("delta_ticks"),
                    "route_b": route_b.get("status")},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}",
        )

async def dispatch_calc_scenario_evaluate(
    store: RedisRuntimeStore, symbol: str, venue: str,
    *, target_price: Any | None = None, horizon: str = "1h",
    interval_seconds: int = 10, window_minutes: int = 30,
    tick_size: str = "0.01", postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.scenario.evaluate — price-target scenario vs tape — NUMERIC.

    Interaction-plane endpoint: a target price plus a horizon (15m|1h|4h)
    is run through the FITTED models (`fitting.evaluate_scenario`) —
    required horizon flow vs the empirical rolling-sum OFI distribution at
    that horizon, direction-matched exceedance, SE-band range, route-B
    cross-check. The current price is resolved INSIDE the tool — collated
    snapshot (mark ?? last) first, then the TTL-bounded derivatives cache
    (funding.mark_price); never agent-supplied, never recomputed in-prompt.
    The winning source is recorded on ``price_source``. Refuses (result
    None, never zero) on gate-failed fits, β≈0, missing price, unparseable
    inputs, or thin tapes.
    """
    from decimal import Decimal, InvalidOperation

    from market_service.microstructure import fitting as fm
    from market_service.runtime import read_paths

    cap = CAPABILITIES["calc.scenario.evaluate"]
    scope = {"symbol": symbol.upper(), "venue": venue,
              "interval_seconds": interval_seconds,
              "window_minutes": window_minutes, "target_price": target_price,
              "horizon": horizon}
    try:
        cap.validate_scope(symbol, venue)
        try:
            target_dec = Decimal(str(target_price))
        except (InvalidOperation, ValueError, TypeError):
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused",
                        "reason": f"unparseable target_price: {target_price!r}"},
            )
        if target_dec <= 0:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "target_price must be positive"},
            )
        evidence_dict, _fit_log = await _tool_fit_beta(
            store, symbol, venue,
            {"interval_seconds": interval_seconds,
             "window_minutes": window_minutes, "tick_size": tick_size},
            postgres=postgres,
        )
        if not isinstance(evidence_dict, dict):
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no evidence window"},
            )
        price_fit = _price_fit_from_dict(evidence_dict.get("price_impact_fit") or {})
        if price_fit is None:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "unparseable price fit"},
            )
        dsf_dict = evidence_dict.get("depth_scaling_fit")
        depth_fit = (_depth_fit_from_dict(dsf_dict)
                     if isinstance(dsf_dict, dict) else None)
        # Current price: tool-resolved. Collated snapshot first (mark ?? last),
        # then the TTL-bounded derivatives cache (funding.mark_price — same
        # exchange, Redis-expiry enforced, so a stale quote can never sneak in).
        payload = await read_paths.read_collated(store, symbol.upper())
        snap = read_paths.market_snapshot(payload) if payload is not None else {}
        current_raw = snap.get("mark_price") or snap.get("last_price")
        price_source = ("snapshot:" + ("mark_price" if snap.get("mark_price")
                                         else "last_price")) if current_raw else None
        if current_raw is None:
            deriv = await store.read_derivative_evidence(symbol.upper())
            futures = (deriv or {}).get("futures") or {}
            funding = futures.get("funding") or {}
            current_raw = funding.get("mark_price")
            price_source = "derivatives:funding.mark_price" if current_raw else None
        try:
            current_dec = Decimal(str(current_raw))
        except (InvalidOperation, ValueError, TypeError):
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused",
                        "reason": "no_market_price: collated snapshot and derivatives cache carry no usable price"},
            )
        if current_dec <= 0:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "snapshot price non-positive"},
            )
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, _dropped = fm.replay_events_from_payloads(payloads)
        intervals = fm.replay_intervals(events, interval_ms=interval_seconds * 1_000)
        tick_dec = Decimal(str(tick_size or "0.01"))
        try:
            result = fm.evaluate_scenario(
                target_dec, current_dec, tick_dec, price_fit, intervals,
                interval_seconds=interval_seconds, horizon=horizon,
                depth_fit=depth_fit, average_depth=price_fit.mean_ad,
            )
        except ValueError as vex:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex),
                        "fit_id": price_fit.fit_id},
            )
        result["price_source"] = price_source
        result["units"] = {"price_unit": "ticks", "tick_size": str(tick_dec)}
        result["status"] = "evaluated_ok"
        result["paper"] = "Cont 1011.6402 §3"
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": "evaluated_ok",
                    "direction": result["direction"],
                    "required_ofi": result["required_ofi"],
                    "exceedance": result["exceedance"],
                    "route_b": (result.get("route_b") or {}).get("status")},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}",
        )


async def dispatch_memory_recall_paper(
    symbol: str, venue: str, *, query: str = "Cont OFI AD beta",
    memory: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: memory.recall_paper — recall paper facts through the ENGINE's own
    MemoryNode (two-plane boundary pass, 2026-09-04).

    The node and the paper-KB session are injected; this dispatcher never
    constructs stores and never re-reads the environment. The paper session
    UUID comes from the single source of truth ``memory.paper_kb_session_id``
    shared with scripts/seed_paper_kb.py. With no memory node (Redis-only
    deployment) the result is an explicit null payload — never a fabricated
    recall and never a second connection pool.
    """
    from market_service.nooa_harness.memory import paper_kb_session_id

    cap = CAPABILITIES["memory.recall_paper"]
    scope = {"symbol": symbol.upper(), "venue": venue, "query": query}
    try:
        cap.validate_scope(symbol, venue)
        if memory is None:
            return [], capability_log_entry(
                cap.name, scope, "ok",
                detail={"facts": 0, "reason": "memory_node_not_configured"},
            )
        mems = await memory.recall(paper_kb_session_id(), query=query, limit=8)
        projected = [{"content": m.content[:600], "tags": list(m.tags),
                      "importance": m.importance} for m in mems]
        return projected, capability_log_entry(
            cap.name, scope, "ok",
            detail={"facts": len(projected), "query": query,
                    "session": "paper-kb (injected MemoryNode)"},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


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
        return await dispatch_calc_ofi_intervals(store, symbol, venue, interval_ms=int(args.get("interval_ms") or args.get("interval_seconds", 10)*1000 if "interval_seconds" in args else 10_000), window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.depth.average":
        return await dispatch_calc_ad_average(store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.observation.build":
        return await dispatch_calc_observation_build(store, symbol, venue, interval_seconds=int(args.get("interval_seconds") or 10), window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.fit.price_impact":
        return await dispatch_calc_fit_price_impact(store, symbol, venue, interval_seconds=int(args.get("interval_seconds") or 10), window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.fit.depth_scaling":
        return await dispatch_calc_fit_depth_scaling(store, symbol, venue)
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
            tick_size=str(args.get("tick_size") or "0.01"),
            postgres=postgres,
        )
    if name == "memory.recall_paper":
        return await dispatch_memory_recall_paper(
            symbol, venue, query=str(args.get("query") or "Cont OFI AD beta"),
            memory=memory,
        )
    return None, capability_log_entry(
        "tool.unrouted", {"name": name}, "denied", detail="no dispatch path",
    )


# Alias table for LLM-supplied tool names — lookup is by fully-normalized
# form (every "_" treated as "."), so exact keys, all-underscore forms,
# and MIXED forms (``calc.ofi_intervals``) all resolve. Plus explicit
# truncations. Built once from TOOL_NAMES so new tools inherit it.
def _norm_tool_key(value: str) -> str:
    return value.replace("_", ".")


_TOOL_ALIASES: dict[str, str] = {
    _norm_tool_key(_alias_key): _alias_key for _alias_key in TOOL_NAMES
}
_TOOL_ALIASES.update({
    "micro.ofi": "micro.ofi_intervals",
    "micro.fit": "micro.fit_beta",
    "micro.status": "micro.capture_status",
    "micro.capture": "micro.capture_status",
    "calc.ofi": "calc.ofi.intervals",
    "calc.ad": "calc.depth.average",
    "calc.depth": "calc.depth.average",
    "calc.observations": "calc.observation.build",
    "calc.observation": "calc.observation.build",
    "calc.derived": "calc.derived_diagnostic",
    "market.history": "market.keystone_history",
})


def _normalize_tool_name(name: Any) -> str | None:
    """Resolve an LLM-supplied tool name to its canonical registry key.

    Accepts the exact key plus separator variants (``calc.ofi_intervals`` /
    ``calc.ofi.intervals``) and a small explicit alias map for truncated
    names. Returns None when nothing matches — the caller denies with the
    attempted payload attached for debuggability.
    """
    if not isinstance(name, str):
        return None
    cleaned = name.strip().lower()
    normalized = _norm_tool_key(cleaned)
    if normalized in _TOOL_ALIASES:
        return _TOOL_ALIASES[normalized]
    return None


def _price_fit_from_dict(data: dict[str, Any]) -> Any | None:
    """Reconstruct a PriceImpactFit from a persisted PG-history dict.

    History rows were serialized with ``default=str`` so every Decimal
    arrives as a string; anything unparseable yields None (skipped, never
    fabricated).
    """
    try:
        from decimal import Decimal

        from market_service.microstructure.contracts import PriceImpactFit

        def _dec(key: str) -> Decimal | None:
            value = data.get(key)
            return Decimal(str(value)) if value is not None else None

        beta = _dec("beta")
        if beta is None:
            return None
        return PriceImpactFit(
            fit_id=str(data.get("fit_id") or "hist-unknown"),
            symbol=str(data.get("symbol") or "").upper(),
            venue=str(data.get("venue") or ""),
            window_start_ms=int(data.get("window_start_ms") or 0),
            window_end_ms=int(data.get("window_end_ms") or 0),
            interval_seconds=int(data.get("interval_seconds") or 0),
            alpha=_dec("alpha") or Decimal(0),
            beta=beta,
            stderr_beta=_dec("stderr_beta"),
            robust_se_method=str(data.get("robust_se_method") or "HC0"),
            n_observations=int(data.get("n_observations") or 0),
            excluded_observations=int(data.get("excluded_observations") or 0),
            r2=_dec("r2"),
            residual_std=_dec("residual_std"),
            heteroskedasticity_flag=bool(data.get("heteroskedasticity_flag", False)),
            mean_ad=_dec("mean_ad"),
            price_unit=str(data.get("price_unit") or "ticks"),
            tick_size=_dec("tick_size") or Decimal("0.01"),
            input_hash=str(data.get("input_hash") or ""),
            model_version=str(data.get("model_version") or ""),
            sensitivity=bool(data.get("sensitivity", False)),
            status=str(data.get("status") or "insufficient"),
        )
    except (ValueError, TypeError, ArithmeticError, KeyError):
        return None


async def _load_prior_block_fits(
    postgres: Any | None, symbol: str, venue: str,
    *, interval_seconds: int, limit: int = 8,
) -> list[Any]:
    """Load prior-cycle price-impact fits so depth scaling is identified.

    Without history every cycle fits depth scaling from a single block
    (n_blocks=1 → insufficient by design). Priors come from the durable PG
    ledger — same symbol/venue/interval, validated-or-provisional,
    non-sensitivity, distinct fit_ids. Empty on any failure (fit degrades
    to single-block, never fabricates).
    """
    if postgres is None:
        return []
    try:
        rows = await postgres.read_recent_inference_artifacts(
            symbol, venue=venue, limit=limit,
        )
    except Exception:
        return []
    fits: list[Any] = []
    seen: set[str] = set()
    for row in rows or []:
        state = (row or {}).get("deterministic_state") or {}
        micro = state.get("microstructure_evidence") or {}
        fit_dict = micro.get("price_impact_fit")
        if not isinstance(fit_dict, dict):
            continue
        fit = _price_fit_from_dict(fit_dict)
        if fit is None or fit.fit_id in seen:
            continue
        if fit.venue != venue or fit.interval_seconds != interval_seconds:
            continue
        if fit.sensitivity or fit.status not in ("validated", "provisional"):
            continue
        seen.add(fit.fit_id)
        fits.append(fit)
    return fits


async def _tool_fit_beta(
    store: RedisRuntimeStore, symbol: str, venue: str, args: dict[str, Any],
    *, postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: micro.fit_beta — replay + fit over the windowed event ledger.

    Reads events, replays deterministically, assembles evidence (primary β +
    sensitivity + depth-scaling-over-prior-evidence). Window anchoring is the
    last captured event (reproducible), mirroring the CLI fit path.
    """
    from decimal import Decimal

    from market_service.microstructure import fitting as fitting_mod

    cap = CAPABILITIES["fitting.assemble_evidence"]
    scope = {
        "symbol": symbol.upper(), "venue": venue,
        "interval_seconds": int(args.get("interval_seconds") or 10),
        "window_minutes": int(args.get("window_minutes") or 30),
    }
    try:
        cap.validate_scope(symbol, venue)
        interval_s = int(args.get("interval_seconds") or 10)
        window_m = int(args.get("window_minutes") or 30)
        tick = Decimal(str(args.get("tick_size") or "0.01"))

        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fitting_mod.replay_events_from_payloads(payloads)
        if len(events) < 2:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "insufficient", "reason": "fewer than 2 events"},
            )
        window_ms = window_m * 60_000
        end_ts = events[-1].current.exchange_ts_ms
        windowed = [
            e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms
        ]
        intervals = fitting_mod.replay_intervals(windowed, interval_ms=interval_s * 1_000)
        if intervals and intervals[-1].end_ts_ms > end_ts:
            intervals = intervals[:-1]
        if not intervals:
            return None, capability_log_entry(cap.name, scope, "ok", detail={"status": "insufficient", "reason": "no closed intervals", "events": len(windowed)})
        fit_config = {
            "symbol": symbol, "venue": venue, "tick_size": str(tick),
            "interval_seconds": interval_s, "window_minutes": window_m,
        }
        evidence_id = f"ev-{fitting_mod.input_hash(intervals, fit_config)[:16]}"
        prior_fits = await _load_prior_block_fits(
            postgres, symbol, venue, interval_seconds=interval_s,
        )
        evidence = fitting_mod.assemble_evidence(
            intervals, symbol=symbol, venue=venue, tick_size=tick,
            interval_seconds=interval_s,
            evidence_id=evidence_id,
            generated_at_ms=end_ts,
            events=windowed,
            prior_block_fits=prior_fits,
            coverage={
                "events_total": len(events),
                "events_in_window": len(windowed),
                "events_dropped_on_decode": dropped,
                "intervals_closed": len(intervals),
            },
        )
        return evidence.to_dict(), capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": evidence.status, "intervals": len(intervals)},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
