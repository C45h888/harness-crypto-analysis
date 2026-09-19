"""History adapter — PG prior fits + legacy evidence assembly (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry
from .replay_adapter import _read_tape_payloads
from .tick_guard import _frozen_tick

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
            tick_size=_dec("tick_size") or _legacy_tick(data),
            input_hash=str(data.get("input_hash") or ""),
            model_version=str(data.get("model_version") or ""),
            sensitivity=bool(data.get("sensitivity", False)),
            status=str(data.get("status") or "insufficient"),
        )
    except (ValueError, TypeError, ArithmeticError, KeyError):
        return None


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

    import market_service.microstructure as fitting_mod

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
        tick = Decimal(_frozen_tick(symbol, venue, args.get("tick_size")) or "0")

        payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
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

