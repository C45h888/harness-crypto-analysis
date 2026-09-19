"""Forward Route C plane — feature/join/fit/distribution/scenario/forecast (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry
from .history_adapter import (
    _depth_fit_from_dict,
    _price_fit_from_dict,
    _tool_fit_beta,
)
from .replay_adapter import (
    _forward_replay_inputs,
    _read_windowed_events,
)
from .tick_guard import _frozen_tick


async def _forward_core(
    store: Any, symbol: str, venue: str, *, horizon_ms: int,
    window_minutes: int, tick_size: str | None, theta_ticks: Any | None = None,
    postgres: Any | None = None,
) -> dict[str, Any]:
    """Single-replay forward core: one tape read → pairs → fit → x → calibration.

    Owned by the forward plane so join/fit/distribution/scenario/forecast
    share one deterministic input window. Raises ValueError on refusal
    (no window / no fit) — callers project to their tool's refusal shape.
    """
    from decimal import Decimal

    import market_service.microstructure as fm

    frozen = _frozen_tick(symbol, venue, tick_size)
    windowed, vectors, _mids, pairs, join_log = await _forward_replay_inputs(
        store, symbol, venue, window_minutes=window_minutes, tick_size=frozen,
        postgres=postgres)
    if not windowed or not vectors:
        raise ValueError("no forward observation window")
    fit, _used = fm.fit_forward_ols(
        pairs, symbol=symbol, venue=venue, horizon_ms=horizon_ms)
    x = vectors[-1]
    theta = Decimal(str(theta_ticks)) if theta_ticks is not None else None
    calibration = fm.calibration_report(fit, pairs, theta_ticks=theta)
    return {"windowed": windowed, "vectors": vectors, "pairs": pairs,
            "join_log": join_log, "fit": fit, "x": x,
            "calibration": calibration, "theta": theta,
            "tick_size": frozen}


async def dispatch_calc_feature_build(
    store: RedisRuntimeStore, symbol: str, venue: str, *, window_minutes: int = 30,
    postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.feature.build — xt-v2 vector from the latest event window."""
    from decimal import Decimal

    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.feature.build"]
    scope = {"symbol": symbol.upper(), "venue": venue, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        events, windowed, _start, _end, _dropped = await _read_windowed_events(
            store, symbol, venue, window_minutes=window_minutes, postgres=postgres)
        if not events:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no events in ledger"})
        intervals = fm.replay_intervals(windowed, interval_ms=10_000)
        last_iv = intervals[-1] if intervals else None
        quote = windowed[-1].current
        vec = fm.build_feature_vector(symbol=symbol, venue=venue, ts_ms=quote.exchange_ts_ms,
            ofi=last_iv.ofi if last_iv else None,
            average_depth=last_iv.average_depth if last_iv else None,
            quote=quote, quality=last_iv.quality if last_iv else "exact_feed")
        return vec.to_dict(), capability_log_entry(cap.name, scope, "ok",
            detail={"vector_version": vec.vector_version, "fields": sorted(vec.fields)})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_forward_join(
    store: RedisRuntimeStore, symbol: str, venue: str, *, window_minutes: int = 30,
    tick_size: str | None = None, postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.forward.join — (X_t, Y(h)) pairs + exclusion log."""
    from decimal import Decimal

    import market_service.microstructure as fm
    from market_service.microstructure.ofi import _mid
    cap = CAPABILITIES["calc.forward.join"]
    scope = {"symbol": symbol.upper(), "venue": venue, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        tick_size = _frozen_tick(symbol, venue, tick_size)
        windowed, vecs, mids, pairs, log = await _forward_replay_inputs(
            store, symbol, venue, window_minutes=window_minutes, tick_size=tick_size,
            postgres=postgres)
        if not windowed:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no events in ledger"})
        return {"n_pairs": len(pairs), "exclusion_log": log,
                "sample": [p.to_dict() for p in pairs[:5]]}, capability_log_entry(
            cap.name, scope, "ok", detail={"n_pairs": len(pairs), **log})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_forward_fit(
    store: RedisRuntimeStore, symbol: str, venue: str, *, window_minutes: int = 30,
    horizon_ms: int = 5_000, tick_size: str | None = None, postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.forward.fit — per-horizon OLS + comparator + OOS."""
    from decimal import Decimal

    import market_service.microstructure as fm
    from market_service.microstructure.ofi import _mid
    cap = CAPABILITIES["calc.forward.fit"]
    scope = {"symbol": symbol.upper(), "venue": venue, "horizon_ms": horizon_ms}
    try:
        cap.validate_scope(symbol, venue)
        tick_size = _frozen_tick(symbol, venue, tick_size)
        windowed, vecs, mids, pairs, _join_log = await _forward_replay_inputs(
            store, symbol, venue, window_minutes=window_minutes, tick_size=tick_size,
            postgres=postgres)
        if not windowed:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no events in ledger"})
        try:
            fit, _ = fm.fit_forward_ols(pairs, symbol=symbol, venue=venue, horizon_ms=horizon_ms)
        except ValueError as vex:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex)})
        return fit.to_dict(), capability_log_entry(cap.name, scope, "ok",
            detail={"status": fit.status, "horizon_ms": horizon_ms, "oos_skill": fit.oos_skill})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_forward_distribution(
    store: RedisRuntimeStore, symbol: str, venue: str, *, horizon_ms: int = 5_000,
    theta_ticks: Any | None = None, window_minutes: int = 30, tick_size: str | None = None,
    postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.forward.distribution — E[Y(h)] + PI + P(>0)/P(>theta)."""
    from decimal import Decimal

    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.forward.distribution"]
    scope = {"symbol": symbol.upper(), "venue": venue, "horizon_ms": horizon_ms}
    try:
        cap.validate_scope(symbol, venue)
        try:
            core = await _forward_core(
                store, symbol, venue, horizon_ms=horizon_ms,
                window_minutes=window_minutes, tick_size=tick_size,
                theta_ticks=theta_ticks, postgres=postgres)
        except ValueError as vex:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": f"no forward fit: {vex}"})
        fit, x, calibration = core["fit"], core["x"], core["calibration"]
        try:
            out = fm.predict_distribution(
                fit, x, theta_ticks=core["theta"], calibration=calibration)
        except ValueError as vex:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex), "fit_id": fit.fit_id})
        return out, capability_log_entry(cap.name, scope, "ok",
            detail={"fit_id": fit.fit_id, "horizon_ms": horizon_ms})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_forward_scenario(
    store: RedisRuntimeStore, symbol: str, venue: str, *, horizon_ms: int = 5_000,
    targets: list[Any] | None = None, invalidations: list[Any] | None = None,
    window_minutes: int = 30, tick_size: str | None = None,
    postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.forward.scenario — horizon-native P(T)/P(S) with bands."""
    from decimal import Decimal

    import market_service.microstructure as fm
    from market_service.runtime import read_paths
    cap = CAPABILITIES["calc.forward.scenario"]
    scope = {"symbol": symbol.upper(), "venue": venue, "horizon_ms": horizon_ms}
    try:
        cap.validate_scope(symbol, venue)
        try:
            core = await _forward_core(
                store, symbol, venue, horizon_ms=horizon_ms,
                window_minutes=window_minutes, tick_size=tick_size,
                postgres=postgres)
        except ValueError as vex:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": f"no distribution: {vex}"})
        fit, x = core["fit"], core["x"]
        try:
            dist = fm.predict_distribution(fit, x, calibration=core["calibration"])
        except ValueError as vex:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex), "fit_id": fit.fit_id})
        payload = await read_paths.read_collated(store, symbol.upper())
        snap = read_paths.market_snapshot(payload) if payload is not None else {}
        spot_raw = snap.get("mark_price") or snap.get("last_price")
        if spot_raw is None:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no_market_price"})
        try:
            out = fm.evaluate_forward_scenario(fit, x, spot_price=Decimal(str(spot_raw)),
                targets=[Decimal(str(t)) for t in (targets or [])],
                invalidations=[Decimal(str(s)) for s in (invalidations or [])],
                tick_size=Decimal(str(core["tick_size"])),
                calibration=dist.get("calibration"))
        except ValueError as vex:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex), "fit_id": fit.fit_id})
        return out, capability_log_entry(cap.name, scope, "ok",
            detail={"fit_id": fit.fit_id, "horizon_ms": horizon_ms})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_forward_forecast(
    store: RedisRuntimeStore, symbol: str, venue: str, *,
    horizon_ms: int = 5_000, window_minutes: int = 30,
    tick_size: str | None = None, postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Canonical deterministic forward ForecastResult composition."""
    from decimal import Decimal
    import market_service.microstructure as fm
    cap = CAPABILITIES["calc.forward.forecast"]
    scope = {"symbol": symbol.upper(), "venue": venue,
             "horizon_ms": horizon_ms, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        tick_size = _frozen_tick(symbol, venue, tick_size)
        windowed, vectors, _mids, pairs, join_log = await _forward_replay_inputs(
            store, symbol, venue, window_minutes=window_minutes, tick_size=tick_size,
            postgres=postgres)
        if not windowed or not vectors:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no forward observation window"},
            )
        fit, _used = fm.fit_forward_ols(
            pairs, symbol=symbol, venue=venue, horizon_ms=horizon_ms)
        x = vectors[-1]
        calibration = fm.calibration_report(fit, pairs)
        distribution: dict[str, Any] | None = None
        if fit.status in ("validated", "provisional"):
            try:
                distribution = fm.predict_distribution(
                    fit, x, calibration=calibration)
            except ValueError as vex:
                distribution = {
                    "status": "refused", "reason": str(vex),
                    "probability_status": "refused",
                }
        legacy_result: dict[str, Any] | None = None
        try:
            evidence_dict, _ = await _tool_fit_beta(
                store, symbol, venue,
                {"interval_seconds": 10, "window_minutes": window_minutes,
                 "tick_size": tick_size}, postgres=postgres)
            if isinstance(evidence_dict, dict):
                price_fit = _price_fit_from_dict(
                    evidence_dict.get("price_impact_fit") or {})
                depth_fit_dict = evidence_dict.get("depth_scaling_fit")
                depth_fit = (_depth_fit_from_dict(depth_fit_dict)
                             if isinstance(depth_fit_dict, dict) else None)
                if price_fit is not None and "ofi_10s" in x.fields:
                    legacy_result = fm.derive_price_delta(
                        price_fit, ofi=Decimal(x.fields["ofi_10s"]),
                        tick_size=Decimal(str(tick_size)), depth_fit=depth_fit,
                        average_depth=(Decimal(x.fields["ad_10s"])
                                      if "ad_10s" in x.fields else price_fit.mean_ad),
                    )
        except (ValueError, TypeError, ArithmeticError):
            legacy_result = None
        result = fm.assemble_forecast_result(
            symbol=symbol, venue=venue, generated_at_ms=windowed[-1].current.exchange_ts_ms,
            x=x, fit=fit, distribution=distribution, calibration=calibration,
            legacy_result=legacy_result,
        ).to_dict()
        result["diagnostics"]["forward_join"] = join_log
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": result.get("validation_state"),
                    "fit_status": fit.status,
                    "probability_status": result.get("multivariate", {}).get("probability_status")
                    if isinstance(result.get("multivariate"), dict) else "unavailable"},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}",
        )

