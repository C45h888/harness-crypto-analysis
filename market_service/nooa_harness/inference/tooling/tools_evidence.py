"""Evidence plane — hypothesis/events/decay/discipline (read-only) (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry
from .replay_adapter import _forward_replay_inputs, _read_windowed_events
from .tick_guard import _frozen_tick
from .tools_forward import dispatch_calc_forward_fit

async def dispatch_calc_hypothesis_test(
    store: RedisRuntimeStore, symbol: str, venue: str, *, hypothesis_id: str,
    horizon_ms: int = 5_000, m_tests: int = 1, window_minutes: int = 30,
    tick_size: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.hypothesis.test — independent post-fit test (never auto-called)."""
    from decimal import Decimal

    import market_service.microstructure as fm
    from market_service.microstructure.hypothesis import test_hypothesis
    from market_service.microstructure.ofi import _mid
    cap = CAPABILITIES["calc.hypothesis.test"]
    scope = {"symbol": symbol.upper(), "venue": venue, "hypothesis_id": hypothesis_id}
    try:
        cap.validate_scope(symbol, venue)
        tick_size = _frozen_tick(symbol, venue, tick_size)
        if not hypothesis_id:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": "hypothesis_id required (pre-registration)"})
        # Shared interval-attached replay (same semantic as forecast/fit).
        _windowed, _vecs, _mids, pairs, _join_log = await _forward_replay_inputs(
            store, symbol, venue, window_minutes=window_minutes, tick_size=tick_size)
        fit_dict, fit_log = await dispatch_calc_forward_fit(
            store, symbol, venue, window_minutes=window_minutes, horizon_ms=horizon_ms, tick_size=tick_size)
        if not fit_dict:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": f"no forward fit: {(fit_log.get('detail') or {})}",})
        from market_service.microstructure.contracts import ForwardFit
        fit = ForwardFit.from_dict(fit_dict)
        try:
            ev = test_hypothesis(fit, pairs, hypothesis_id=hypothesis_id, m_tests=int(m_tests or 1))
        except ValueError as vex:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex)})
        return ev.to_dict(), capability_log_entry(cap.name, scope, "ok",
            detail={"hypothesis_id": hypothesis_id, "p_value": ev.p_value, "status": ev.status})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_events(
    store: RedisRuntimeStore, symbol: str, venue: str, *, kind: str,
    window_minutes: int = 30,
) -> tuple[Any, dict[str, Any]]:
    """Tools: calc.events.absorption / calc.events.walls — typed detectors."""
    import market_service.microstructure as fm
    from market_service.microstructure.events import detect_absorption, detect_walls, replay_agreement
    cap = CAPABILITIES[f"calc.events.{kind}"]
    scope = {"symbol": symbol.upper(), "venue": venue, "kind": kind}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, _ = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        if kind == "absorption":
            found, log = detect_absorption(windowed, symbol=symbol, venue=venue)
        else:
            found, log = detect_walls(windowed, symbol=symbol, venue=venue)
        agr = replay_agreement(windowed, symbol=symbol, venue=venue) if windowed else {"agreement": True}
        return {"events": [f.to_dict() for f in found], "log": log, "replay_agreement": agr}, \
            capability_log_entry(cap.name, scope, "ok", detail={"n": len(found), **log})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_decay_report(
    store: RedisRuntimeStore, symbol: str, venue: str, *, window_minutes: int = 30,
    tick_size: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.decay.report — skill-decay + finalized horizons."""
    from decimal import Decimal

    import market_service.microstructure as fm
    from market_service.microstructure.ofi import _mid
    cap = CAPABILITIES["calc.decay.report"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        tick_size = _frozen_tick(symbol, venue, tick_size)
        from market_service.microstructure.contracts import ForwardFit
        fits = {}
        pairs_ref = None
        for h in fm.FORWARD_HORIZONS_MS:
            fit_dict, _ = await dispatch_calc_forward_fit(
                store, symbol, venue, window_minutes=window_minutes, horizon_ms=h, tick_size=tick_size)
            if not fit_dict:
                continue
            fits[h] = ForwardFit.from_dict(fit_dict)
        if not fits:
            return None, capability_log_entry(cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no forward fits"})
        # Pairs for counts: one shared interval-attached replay (not per-horizon).
        _windowed, _vecs, _mids, pairs_ref, _join_log = await _forward_replay_inputs(
            store, symbol, venue, window_minutes=window_minutes, tick_size=tick_size)
        pairs_ref = pairs_ref or []
        rep = fm.skill_decay_report(pairs_ref, fits)
        return rep, capability_log_entry(cap.name, scope, "ok",
            detail={"finalized": rep.get("finalized_horizons")})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_calc_discipline_audit(
    store: RedisRuntimeStore, symbol: str, venue: str, *, window_minutes: int = 30,
    tick_size: str | None = None, cost_statement: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.discipline.audit — 9-lock audit -> go/no-go memo."""
    from decimal import Decimal

    import market_service.microstructure as fm
    from market_service.microstructure.discipline import discipline_audit
    from market_service.microstructure.ofi import _mid
    cap = CAPABILITIES["calc.discipline.audit"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        tick_size = _frozen_tick(symbol, venue, tick_size)
        from market_service.microstructure.contracts import ForwardFit
        _windowed, _vecs, _mids, pairs, _join_log = await _forward_replay_inputs(
            store, symbol, venue, window_minutes=window_minutes, tick_size=tick_size)
        pairs = pairs or []
        fits = {}
        for h in fm.FORWARD_HORIZONS_MS:
            fit_dict, _ = await dispatch_calc_forward_fit(
                store, symbol, venue, window_minutes=window_minutes, horizon_ms=h, tick_size=tick_size)
            if not fit_dict:
                continue
            fits[h] = ForwardFit.from_dict(fit_dict)
        out = discipline_audit(forward_pairs=pairs, fits=fits, cost_statement=cost_statement)
        return out, capability_log_entry(cap.name, scope, "ok", detail={"verdict": out["verdict"]})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

