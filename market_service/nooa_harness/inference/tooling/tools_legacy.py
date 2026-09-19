"""Legacy Route A/B plane — OFI/AD/OLS/scenario.evaluate (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry
from .history_adapter import (
    _depth_fit_from_dict,
    _price_fit_from_dict,
    _tool_fit_beta,
)
from .replay_adapter import _read_tape_payloads
from .registry import _bounded
from .tick_guard import _frozen_tick

async def dispatch_calc_ofi_intervals(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_ms: int = 10_000, window_minutes: int = 30,
    postgres: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: calc.ofi.intervals — deterministic OFI per interval (no AD)."""
    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.ofi_intervals"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_ms": interval_ms, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
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
    postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.depth.average — AD per block, separate from OFI (paper AD_i)."""
    from decimal import Decimal

    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.ad_average"]
    scope = {"symbol": symbol.upper(), "venue": venue, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
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
    postgres: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: calc.observation.build — join OFI+AD+ΔP → observations (ΔP ticks vs OFI)."""
    from decimal import Decimal

    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.observation_build"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_seconds": interval_seconds, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
        events, dropped = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_seconds*1000)
        from market_service.microstructure.tick import resolve_tick_size
        observations, excluded = fm.build_observations(intervals, tick_size=resolve_tick_size(symbol, venue))
        proj = [{"ofi": str(o.ofi), "delta_ticks": str(o.delta_ticks), "average_depth": str(o.average_depth) if o.average_depth else None, "quality": o.quality} for o in observations[:50]]
        return proj, capability_log_entry(cap.name, scope, "ok", detail={"n_observations": len(observations), "excluded": excluded, "note": "ΔP = α+β·OFI observations ready for calc.fit.price_impact"})
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_fit_price_impact(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_seconds: int = 10, window_minutes: int = 30,
    postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.fit.price_impact — OLS ΔP=α+β·OFI (HC0), takes split observations."""
    from decimal import Decimal

    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.fit_price_impact"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_seconds": interval_seconds}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
        events, _ = fm.replay_events_from_payloads(payloads)
        windowed = [e for e in events if e.current.exchange_ts_ms >= (events[-1].current.exchange_ts_ms - window_minutes*60_000)] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_seconds*1000)
        from market_service.microstructure.tick import resolve_tick_size
        fit, _ = fm.fit_price_impact(intervals, symbol=symbol, venue=venue, tick_size=resolve_tick_size(symbol, venue), interval_seconds=interval_seconds)
        return fit.to_dict(), capability_log_entry(cap.name, scope, "ok", detail={"beta": str(fit.beta), "status": fit.status})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_fit_depth_scaling(
    store: RedisRuntimeStore, symbol: str, venue: str,
    postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.fit.depth_scaling — lnβ = ln c - λ ln AD, needs ≥3 blocks. Derived diagnostic."""
    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.fit_depth_scaling"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        # For split discipline, we can only fit depth scaling from history — single window gives n_blocks=1 → insufficient by design
        payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
        events, _ = fm.replay_events_from_payloads(payloads)
        intervals = fm.replay_intervals(events, interval_ms=10_000)
        from market_service.microstructure.tick import resolve_tick_size
        fit, _ = fm.fit_price_impact(intervals, symbol=symbol, venue=venue, tick_size=resolve_tick_size(symbol, venue), interval_seconds=10) if intervals else (None,None)
        if fit is None:
            return None, capability_log_entry(cap.name, scope, "ok", detail={"status": "insufficient", "reason": "no intervals"})
        depth_fit = fm.fit_depth_scaling([fit], symbol=symbol, venue=venue)
        return depth_fit.to_dict(), capability_log_entry(cap.name, scope, "ok", detail={"status": depth_fit.status, "n_blocks": depth_fit.n_blocks, "note": "derived, not prediction"})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


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

    import market_service.microstructure as fm
    from market_service.microstructure.tick import resolve_tick_size

    cap = CAPABILITIES["calc.derived_diagnostic"]
    scope = {"symbol": symbol.upper(), "venue": venue,
              "interval_seconds": interval_seconds,
              "window_minutes": window_minutes, "ofi": ofi}
    try:
        cap.validate_scope(symbol, venue)
        evidence_dict, _fit_log = await _tool_fit_beta(
            store, symbol, venue,
            {"interval_seconds": interval_seconds,
             "window_minutes": window_minutes,
             "tick_size": str(resolve_tick_size(symbol, venue))},
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
            payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
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
        tick_size = Decimal(str(evidence_dict.get("tick_size") or _frozen_tick(symbol, venue)))
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
    tick_size: str | None = None, postgres: Any | None = None,
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

    import market_service.microstructure as fm
    from market_service.runtime import read_paths

    cap = CAPABILITIES["calc.scenario.evaluate"]
    scope = {"symbol": symbol.upper(), "venue": venue,
              "interval_seconds": interval_seconds,
              "window_minutes": window_minutes, "target_price": target_price,
              "horizon": horizon}
    try:
        cap.validate_scope(symbol, venue)
        tick_size = _frozen_tick(symbol, venue, tick_size)
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
                detail={"status": "refused",
                        "reason": "no_intervals: no evidence window for the fitted models"},
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
        payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
        events, _dropped = fm.replay_events_from_payloads(payloads)
        intervals = fm.replay_intervals(events, interval_ms=interval_seconds * 1_000)
        tick_dec = Decimal(_frozen_tick(symbol, venue, tick_size))
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
        result["forecast_result"] = fm.assemble_forecast_result(
            symbol=symbol, venue=venue,
            generated_at_ms=events[-1].current.exchange_ts_ms if events else 0,
            scenario=result,
        ).to_dict()
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

