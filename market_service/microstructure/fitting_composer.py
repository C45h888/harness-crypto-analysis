"""Route bridge & evidence assembly — composes outputs from multiple routes.

This substrate does NOT fit any model itself. It takes fitted objects from
Route A, Route B, and Route C and composes them into:

- ``derive_price_delta`` — Produces Route A Direct + Route B Depth-scaled
  outputs with agreement (the ``agreement_{A,B} = |μ_A - μ_B|`` diagnostic).
- ``evaluate_scenario`` — Legacy OFI-exceedance scenario evaluation that
  answers "can price hit X?" using Route A (and optionally B) fits.
- ``assemble_evidence`` — Legacy ``MicrostructureEvidence`` (Route A + B).
- ``assemble_forecast_result`` — ``ForecastResult`` across all routes.
- ``assemble_evidence_v2`` — Unified ``MicrostructureEvidenceV2`` (Route C
  with optional Route A/B legacy fits).

The three routes remain separate estimands — they are never averaged, only
reported alongside their agreement/disagreement.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext
from typing import Any

from .contracts import (
    EVIDENCE_V2_VERSION,
    FIT_MODEL_VERSION,
    FORWARD_MODEL_VERSION,
    DepthScalingFit,
    FeatureVector,
    ForecastResult,
    ForwardFit,
    ForwardObservation,
    MicrostructureEvidence,
    MicrostructureEvidenceV2,
    OFIInterval,
    OrderBookEvent,
    PriceImpactFit,
)
from .fitting_common import (
    PRECISION,
    SCENARIO_HORIZONS,
    MIN_SCENARIO_WINDOWS,
    MAX_SCENARIO_INTERVALS,
    _BETA_ZERO_EPS,
    MIN_OBSERVATIONS,
    MIN_DEPTH_BLOCKS,
    _s,
    _norm_cdf,
    _scenario_required_ofi,
)
from .ofi import DEPTH_ESTIMATOR as _DEPTH_ESTIMATOR
from .fitting_route_a import fit_price_impact
from .fitting_route_b import fit_depth_scaling


# ---------------------------------------------------------------------------
# Route A + Route B bridge
# ---------------------------------------------------------------------------


def derive_price_delta(
    price_fit: PriceImpactFit,
    *,
    ofi: Decimal,
    tick_size: Decimal,
    depth_fit: DepthScalingFit | None = None,
    average_depth: Decimal | None = None,
) -> dict[str, Any]:
    """Derive ΔP (ticks + quote) from FITTED models for one OFI value — pure.

    Route A (direct, primary): ΔP = α + β·OFI with ±1.96·SE_β·|OFI| 95% band.
    Route B (depth-scaled): ΔP = α + (c·AD^−λ)·OFI, only when ``depth_fit``
    is validated/provisional with non-null c/λ and a positive AD.
    Raises ValueError on a gate-failed (``insufficient``) price fit —
    deriving from one would be fabrication. The ν·OFI heteroskedastic
    caveat is reported, never resolved: bands widen with |OFI|.
    """
    if price_fit.status not in ("validated", "provisional"):
        raise ValueError(
            f"cannot derive ΔP from {price_fit.status} price fit {price_fit.fit_id}"
        )
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        delta_a = price_fit.alpha + price_fit.beta * ofi
        band_a = (
            Decimal("1.96") * price_fit.stderr_beta * abs(ofi)
            if price_fit.stderr_beta is not None else None
        )
        route_a = {
            "delta_ticks": str(delta_a),
            "delta_quote": str(delta_a * tick_size),
            "band_95_ticks": str(band_a) if band_a is not None else None,
            "alpha": str(price_fit.alpha),
            "beta": str(price_fit.beta),
            "stderr_beta": (str(price_fit.stderr_beta)
                              if price_fit.stderr_beta is not None else None),
            "fit_id": price_fit.fit_id,
            "fit_status": price_fit.status,
            "r2": str(price_fit.r2) if price_fit.r2 is not None else None,
            "n_observations": price_fit.n_observations,
            "uncertainty_type": "slope_contribution_band",
            "prediction_interval": False,
        }
        route_b: dict[str, Any] = {
            "status": "unavailable",
            "reason": "depth scaling is "
                        f"{(depth_fit.status if depth_fit else 'missing')}",
        }
        agreement: str | None = None
        if (depth_fit is not None
                and depth_fit.status in ("validated", "provisional")
                and depth_fit.c is not None
                and depth_fit.lambda_ is not None):
            ad = average_depth
            if ad is not None and ad > 0 and depth_fit.c > 0:
                beta_implied = depth_fit.c * (-depth_fit.lambda_ * ad.ln()).exp()
                delta_b = price_fit.alpha + beta_implied * ofi
                agreement = str(abs(delta_a - delta_b))
                route_b = {
                    "status": "derived_ok",
                    "delta_ticks": str(delta_b),
                    "delta_quote": str(delta_b * tick_size),
                    "beta_implied": str(beta_implied),
                    "c": str(depth_fit.c),
                    "lambda": str(depth_fit.lambda_),
                    "ad": str(ad),
                    "fit_id": depth_fit.fit_id,
                    "fit_status": depth_fit.status,
                    "n_blocks": depth_fit.n_blocks,
                    "uncertainty_status": "not_propagated",
                }
            else:
                route_b = {
                    "status": "unavailable",
                    "reason": "non-positive c or AD: log-log undefined",
                }
    return {
        "route_a_direct": route_a,
        "route_b_depth_scaled": route_b,
        "agreement_ticks": agreement,
        "heteroskedasticity_flag": price_fit.heteroskedasticity_flag,
    }


# ---------------------------------------------------------------------------
# Legacy scenario evaluation (Route A/B: OFI exceedance)
# ---------------------------------------------------------------------------


def evaluate_scenario(
    target_price: Decimal,
    current_price: Decimal,
    tick_size: Decimal,
    price_fit: PriceImpactFit,
    intervals: list[OFIInterval],
    *,
    interval_seconds: int,
    horizon: str,
    depth_fit: DepthScalingFit | None = None,
    average_depth: Decimal | None = None,
) -> dict[str, Any]:
    """Evaluate a price-target scenario against FITTED models + tape — pure.

    Requirement from the fit, probability from the tape: required horizon
    flow ``OFI_req = (Δ_req − n·α)/β`` vs the empirical distribution of
    rolling ``n``-interval OFI sums (``n = horizon/interval_seconds``).
    Exceedance is one-sided on the target's direction. The 95% band
    (β±1.96·SE) yields a requirement range + exceedance range, never a point.
    Raises ValueError on gate-failed fits, degenerate inputs, or thin
    tapes — deriving from those would be fabrication.
    """
    if price_fit.status not in ("validated", "provisional"):
        raise ValueError(
            f"cannot evaluate scenario from {price_fit.status} price fit {price_fit.fit_id}"
        )
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if horizon not in SCENARIO_HORIZONS:
        raise ValueError(
            f"bad_horizon: {horizon!r}; expected one of {sorted(SCENARIO_HORIZONS)}"
        )
    horizon_s = SCENARIO_HORIZONS[horizon]
    if horizon_s % interval_seconds != 0:
        raise ValueError(
            f"horizon {horizon} is not a multiple of {interval_seconds}s intervals"
        )
    if not intervals:
        raise ValueError("no_intervals: empty OFI tape")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        delta_req = (target_price - current_price) / tick_size
        if delta_req == 0:
            raise ValueError("target_eq_current: target price equals current price")
        direction = "up" if delta_req > 0 else "down"
        n = horizon_s // interval_seconds
        drift = Decimal(n) * price_fit.alpha
        required = _scenario_required_ofi(delta_req, drift, price_fit.beta)
        if required is None:
            raise ValueError(
                "beta_zero: fitted β indistinguishable from zero — "
                "no finite flow reaches the target (H0 holds)"
            )
        # Empirical distribution: rolling n-interval OFI sums, step 1.
        # Only all-exact_feed windows score; degraded windows are counted.
        bounded = intervals[-MAX_SCENARIO_INTERVALS:]
        ofis = [iv.ofi for iv in bounded]
        quals = [iv.quality for iv in bounded]
        usable: list[Decimal] = []
        degraded = 0
        for start in range(len(ofis) - n + 1):
            window_q = quals[start:start + n]
            if all(q == "exact_feed" for q in window_q):
                usable.append(sum(ofis[start:start + n], Decimal(0)))
            else:
                degraded += 1
        if len(usable) < MIN_SCENARIO_WINDOWS:
            raise ValueError(
                f"insufficient_windows: {len(usable)} usable horizon windows "
                f"(need ≥{MIN_SCENARIO_WINDOWS}, {degraded} degraded excluded)"
            )
        total = Decimal(len(usable))
        if direction == "up":
            hits = sum(1 for s in usable if s >= required)
        else:
            hits = sum(1 for s in usable if s <= required)
        exceedance = Decimal(hits) / total
        # Band: requirement + exceedance at each β±1.96·SE edge.
        required_range: list[str] | None = None
        exceedance_range: list[str] | None = None
        band_note: str | None = None
        if price_fit.stderr_beta is not None:
            half = Decimal("1.96") * price_fit.stderr_beta
            edges: list[Decimal] = []
            for beta_edge in (price_fit.beta - half, price_fit.beta + half):
                req_edge = _scenario_required_ofi(delta_req, drift, beta_edge)
                if req_edge is not None:
                    edges.append(req_edge)
            if len(edges) == 2:
                lo, hi = (edges[0], edges[1]) if edges[0] <= edges[1] else (edges[1], edges[0])
                required_range = [_s(lo), _s(hi)]
                exc_lo = sum(1 for s in usable if (s >= lo if direction == "up" else s <= lo))
                exc_hi = sum(1 for s in usable if (s >= hi if direction == "up" else s <= hi))
                exceedance_range = [_s(Decimal(exc_lo) / total), _s(Decimal(exc_hi) / total)]
            else:
                band_note = "band edge crosses β=0: requirement range undefined on one side"
        else:
            band_note = "stderr_beta null: no band range"
        # Route B cross-check on the same distribution.
        route_b: dict[str, Any] = {"status": "unavailable", "reason": "depth scaling insufficient"}
        if (depth_fit is not None
                and depth_fit.status in ("validated", "provisional")
                and depth_fit.c is not None
                and depth_fit.lambda_ is not None):
            ad = average_depth
            if ad is not None and ad > 0 and depth_fit.c > 0:
                beta_implied = depth_fit.c * (-depth_fit.lambda_ * ad.ln()).exp()
                required_b = _scenario_required_ofi(delta_req, drift, beta_implied)
                if required_b is not None:
                    hits_b = sum(1 for s in usable
                                 if (s >= required_b if direction == "up" else s <= required_b))
                    route_b = {
                        "status": "derived_ok",
                        "beta_implied": _s(beta_implied),
                        "required_ofi": _s(required_b),
                        "exceedance": _s(Decimal(hits_b) / total),
                        "fit_id": depth_fit.fit_id,
                        "fit_status": depth_fit.status,
                        "uncertainty_status": "not_propagated",
                    }
                else:
                    route_b = {"status": "unavailable", "reason": "implied β indistinguishable from zero"}
            else:
                route_b = {"status": "unavailable", "reason": "non-positive c or AD: log-log undefined"}
        return {
            "direction": direction,
            "current_price": str(current_price),
            "target_price": str(target_price),
            "delta_req_ticks": _s(delta_req),
            "horizon": horizon,
            "n_intervals": n,
            "drift_ticks": _s(drift),
            "required_ofi": _s(required),
            "required_ofi_range": required_range,
            "exceedance": _s(exceedance),
            "exceedance_range": exceedance_range,
            "exceedance_hits": hits,
            "n_windows_usable": len(usable),
            "n_windows_degraded": degraded,
            "band_note": band_note,
            "route_b": route_b,
            "fit_id": price_fit.fit_id,
            "fit_status": price_fit.status,
            "r2": str(price_fit.r2) if price_fit.r2 is not None else None,
            "heteroskedasticity_flag": price_fit.heteroskedasticity_flag,
            "tick_size": str(tick_size),
            "forecast_type": "scenario_extrapolation",
            "horizon_regime": "long",
            "probability_semantics": "empirical_ofi_exceedance",
            "assumptions": {
                "linear_impact_scaling": True,
                "scale_invariance_proven": False,
                "native_forward_target_used": False,
            },
            "scale_assumption": ("β fitted per base interval applied linearly "
                                   "over the horizon (Δ_req = n·α + β·OFI_total); "
                                   "impact scale-invariance assumed, not proven"),
        }


# ---------------------------------------------------------------------------
# Legacy evidence assembly (Route A + B)
# ---------------------------------------------------------------------------


def assemble_evidence(
    intervals: list[OFIInterval],
    *,
    symbol: str,
    venue: str,
    tick_size: Decimal,
    interval_seconds: int,
    evidence_id: str,
    generated_at_ms: int,
    events: list[OrderBookEvent] | None = None,
    prior_block_fits: list[PriceImpactFit] | None = None,
    coverage: dict[str, Any] | None = None,
    min_observations: int = MIN_OBSERVATIONS,
    min_depth_blocks: int = MIN_DEPTH_BLOCKS,
) -> MicrostructureEvidence:
    """Assemble the immutable evidence object from one block's intervals.

    Runs the primary β fit, the sensitivity β fit (requires raw events), and
    the depth-scaling fit over prior block fits (may be ``insufficient``).
    The result is what NOOA reads — both models with independent diagnostics.
    """
    price_fit, _observations = fit_price_impact(
        intervals, symbol=symbol, venue=venue, tick_size=tick_size,
        interval_seconds=interval_seconds, min_observations=min_observations,
    )
    sensitivity_fit: PriceImpactFit | None = None
    if events:
        sensitivity_fit, _ = fit_price_impact(
            intervals, symbol=symbol, venue=venue, tick_size=tick_size,
            interval_seconds=interval_seconds, min_observations=min_observations,
            sensitivity=True, events=events,
        )
    blocks = list(prior_block_fits or []) + [price_fit]
    depth_fit = fit_depth_scaling(
        blocks, symbol=symbol, venue=venue, min_blocks=min_depth_blocks,
    )
    first, last = intervals[0], intervals[-1]
    status = price_fit.status
    evidence = MicrostructureEvidence(
        symbol=symbol.upper(), venue=venue, evidence_id=evidence_id,
        generated_at_ms=generated_at_ms, interval_seconds=interval_seconds,
        window_start_ms=first.start_ts_ms, window_end_ms=last.end_ts_ms,
        tick_size=tick_size, depth_estimator=_DEPTH_ESTIMATOR,
        input_hash=price_fit.input_hash, model_version=FIT_MODEL_VERSION,
        price_impact_fit=price_fit, sensitivity_fit=sensitivity_fit,
        depth_scaling_fit=depth_fit, block_average_depth=price_fit.mean_ad,
        coverage=coverage or {}, status=status,
    )
    return evidence


# ---------------------------------------------------------------------------
# Forecast result (all-route composer)
# ---------------------------------------------------------------------------


def assemble_forecast_result(
    *,
    symbol: str,
    venue: str,
    generated_at_ms: int,
    x: FeatureVector | None = None,
    fit: ForwardFit | None = None,
    distribution: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
    legacy_result: dict[str, Any] | None = None,
    scenario: dict[str, Any] | None = None,
) -> ForecastResult:
    """Compose one deterministic forecast object without merging estimands."""
    from .fitting_route_c import predict_distribution

    legacy = legacy_result or {}
    route_a = legacy.get("route_a_direct")
    route_b = legacy.get("route_b_depth_scaled")
    relative_difference = None
    if (route_a and route_b and route_b.get("status") == "derived_ok"
            and route_a.get("delta_ticks") is not None
            and route_b.get("delta_ticks") is not None):
        try:
            a = Decimal(str(route_a["delta_ticks"]))
            b = Decimal(str(route_b["delta_ticks"]))
            if a != 0:
                relative_difference = format(abs(a - b) / abs(a), "f")
        except (TypeError, ValueError, ArithmeticError):
            relative_difference = None
    agreement: dict[str, Any] = {
        "route_a_vs_route_b": {
            "status": "diagnostic" if route_a and route_b and
            route_b.get("status") == "derived_ok" else "unavailable",
            "absolute_difference_ticks": legacy.get("agreement_ticks"),
            "relative_difference": relative_difference,
        },
        "route_a_vs_multivariate": {
            "status": "not_comparable",
            "reason": "different_estimands",
        },
    }
    if scenario is not None:
        forecast_type = str(scenario.get("forecast_type") or "scenario_extrapolation")
        horizon_regime = str(scenario.get("horizon_regime") or "long")
        assumptions = dict(scenario.get("assumptions") or {
            "linear_impact_scaling": True,
            "scale_invariance_proven": False,
        })
        validation_state = str(scenario.get("fit_status") or "unvalidated")
        multivariate = {"scenario": scenario}
        horizon_ms = None
        model_version = str(scenario.get("model_version") or FIT_MODEL_VERSION)
        input_hash = str(scenario.get("fit_id") or "")
    else:
        forecast_type = "native_forecast"
        horizon_regime = "native"
        assumptions = {
            "gaussian_probability": True,
            "linear_impact_scaling": False,
        }
        if fit is None:
            validation_state = "unvalidated"
            multivariate = None
            horizon_ms = None
            model_version = FORWARD_MODEL_VERSION
            input_hash = ""
        else:
            # Guard: only predict from validated/provisional fits.
            # An insufficient fit is a finding, not an error — the composer
            # returns a ForecastResult with multivariate=None and the fit's
            # own status as validation_state. This avoids the ValueError
            # from predict_distribution which would turn a clean refusal
            # into a tool.error (breaking the null-discipline contract).
            if fit.status in ("validated", "provisional"):
                dist = distribution or predict_distribution(fit, x, calibration=calibration) if x else distribution or {}
                multivariate = dict(dist or {})
                if calibration is not None:
                    multivariate["calibration"] = calibration
                    multivariate["probability_status"] = (
                        "validated" if calibration.get("status") == "passed" else "refused"
                    )
            else:
                multivariate = None
            validation_state = str(fit.validation_status or fit.status)
            horizon_ms = fit.horizon_ms
            model_version = fit.model_version
            input_hash = fit.input_hash
        if x is not None:
            horizon_ms = fit.horizon_ms if fit is not None else horizon_ms
    information_set: dict[str, Any] = {
        "vector_version": x.vector_version if x else None,
        "feature_keys": list(x.feature_keys) if x else [],
        "feature_schema_hash": x.feature_schema_hash if x else None,
        "fields": dict(x.fields) if x else {},
        "quality": x.quality if x else None,
    }
    diagnostics: dict[str, Any] = {}
    if fit is not None:
        diagnostics.update({
            "heteroskedasticity": fit.hetero_flag,
            "n_observations": fit.n_obs,
            "n_train": fit.n_train,
            "n_oos": fit.n_oos,
            "oos_skill": fit.oos_skill,
            "oos_r2": fit.oos_r2,
            "baseline_oos_r2": fit.baseline_oos_r2,
            "calibration_status": (calibration or {}).get("status", "not_run"),
        })
    if scenario is not None:
        diagnostics.update({
            "calibration_status": "not_applicable",
            "probability_semantics": scenario.get("probability_semantics"),
        })
    # Steady-track receipt: ordered A → B → Multivariate → Compare links so
    # the agentic loop can VERIFY the statistical chain structurally without
    # re-running math. Statuses only — never a re-estimation, never a merge.
    chain_trace = [
        {"step": "run_a", "tool": "calc.fit.price_impact",
         "status": (route_a.get("status") if isinstance(route_a, dict) else None)
         or "unavailable"},
        {"step": "run_b", "tool": "calc.fit.depth_scaling",
         "status": (route_b.get("status") if isinstance(route_b, dict) else None)
         or "unavailable"},
        {"step": "run_multivariate",
         "tool": "calc.forward.fit",
         "status": (fit.status if fit is not None
                      else ("evaluated_ok" if scenario is not None else "unavailable")),
         "probability_status": ((multivariate or {}).get("probability_status")
                                 if isinstance(multivariate, dict) else None)},
        {"step": "compare", "tool": "fitting_composer.assemble",
         "status": "compared",
         "route_a_vs_route_b": (agreement.get("route_a_vs_route_b") or {}).get("status"),
         "route_a_vs_multivariate": (agreement.get("route_a_vs_multivariate") or {}).get("status")},
    ]
    diagnostics["chain_trace"] = chain_trace
    digest_payload = {
        "symbol": symbol.upper(), "venue": venue,
        "generated_at_ms": generated_at_ms,
        "x": x.input_hash if x else None,
        "fit": fit.fit_id if fit else None,
        "scenario": scenario,
    }
    digest = hashlib.sha256(json.dumps(
        digest_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return ForecastResult(
        symbol=symbol.upper(), venue=venue, generated_at_ms=generated_at_ms,
        horizon_ms=horizon_ms, forecast_type=forecast_type,
        horizon_regime=horizon_regime, information_set=information_set,
        route_a=route_a, route_b=route_b, multivariate=multivariate,
        agreement=agreement, diagnostics=diagnostics, assumptions=assumptions,
        validation_state=validation_state, model_version=model_version,
        input_hash=digest,
    )


# ---------------------------------------------------------------------------
# D6 scenario path comparison
# ---------------------------------------------------------------------------


def compare_scenario_paths(legacy: dict[str, Any], fwd: dict[str, Any]) -> dict[str, Any]:
    """D6 agreement/disagreement table: legacy flow-requirement vs horizon-native."""
    agreements: list[str] = []
    disagreements: list[str] = []
    if legacy.get("direction") == "up" and fwd.get("targets"):
        agreements.append("both paths frame upside as one-sided exceedance")
    if str(legacy.get("horizon", "")) not in {str(fwd.get("horizon_ms")), "15m", "1h", "4h"}:
        disagreements.append(
            f"horizon mismatch: legacy {legacy.get('horizon')} vs fwd {fwd.get('horizon_ms')}ms"
        )
    else:
        disagreements.append(
            f"assumption differs: legacy scale-invariance ({legacy.get('horizon')}) "
            f"vs fwd normal-approx ({fwd.get('horizon_ms')}ms)"
        )
    disagreements.append("legacy answers required-flow; fwd answers conditional probability")
    return {
        "agreement": "; ".join(agreements) if agreements else "none (different questions)",
        "disagreement": disagreements,
        "note": "different horizons/assumptions; neither is ground truth",
    }


# ---------------------------------------------------------------------------
# D10 unified evidence assembly
# ---------------------------------------------------------------------------


def assemble_evidence_v2(
    *,
    symbol: str, venue: str, evidence_id: str, generated_at_ms: int,
    tick_size: Decimal,
    x: FeatureVector | None = None,
    fit: ForwardFit | None = None,
    distribution: dict[str, Any] | None = None,
    scenario: dict[str, Any] | None = None,
    hypothesis: Any | None = None,
    events: list[dict[str, Any]] | None = None,
    legacy_fit: dict[str, Any] | None = None,
) -> MicrostructureEvidenceV2:
    """D10 compose-only assembly (never fits; unproven fields stay NULL)."""
    import hashlib as _hl
    import json as _js
    digest = _hl.sha256(_js.dumps(
        {"evidence_id": evidence_id, "fit": fit.fit_id if fit else None,
         "x": x.input_hash if x else None,
         "h": scenario.get("horizon_ms") if scenario else (fit.horizon_ms if fit else None)},
        sort_keys=True).encode()).hexdigest()
    dist = distribution or {}
    scen = scenario or {}
    hyp = hypothesis.to_dict() if hypothesis is not None and hasattr(hypothesis, "to_dict") else (hypothesis or None)
    return MicrostructureEvidenceV2(
        symbol=symbol.upper(), venue=venue, evidence_id=evidence_id,
        generated_at_ms=generated_at_ms, tick_size=format(tick_size, "f"),
        depth_estimator=_DEPTH_ESTIMATOR, input_hash=digest,
        model_version=EVIDENCE_V2_VERSION,
        vector_version=x.vector_version if x else None,
        x_t=x.to_dict() if x else None,
        horizon_ms=scen.get("horizon_ms") if scen else (fit.horizon_ms if fit else None),
        expected_dP_ticks=dist.get("expected_ticks"),
        variance_ticks=dist.get("variance_ticks"),
        se=None,
        interval_lo_95=dist.get("interval_lo_95"),
        interval_hi_95=dist.get("interval_hi_95"),
        p_positive=dist.get("p_positive"),
        p_target={"curves": scen.get("targets")} if scen.get("targets") else None,
        p_invalidation={"curves": scen.get("invalidations")} if scen.get("invalidations") else None,
        hypothesis=(hyp.get("hypothesis_id") if isinstance(hyp, dict) else None),
        effect=(hyp.get("effect") if isinstance(hyp, dict) else None),
        evidence=hyp,
        n=(hyp.get("n") if isinstance(hyp, dict) else (fit.n_obs if fit else None)),
        split=(hyp.get("split") if isinstance(hyp, dict) else None),
        multiplicity_adj=(hyp.get("multiplicity_adj") if isinstance(hyp, dict) else None),
        oos_info={"oos_skill": fit.oos_skill, "fit_id": fit.fit_id} if fit else None,
        events=events if events is not None else [{"note": "no_events"}],
        legacy_fit=legacy_fit,
    )