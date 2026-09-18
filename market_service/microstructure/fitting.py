"""Deterministic Pass-3 inference: reproducible β, c, λ fitting.

Authority: this module is the ONLY producer of ``PriceImpactFit`` /
``DepthScalingFit`` / ``MicrostructureEvidence``. It is pure computation —
no I/O, no exchange access, no LLM. The ``nooa market microstructure``
command group is the only sanctioned caller, and NOOA agents are read-only
consumers of the resulting evidence.

Two models are fitted and reported SEPARATELY (paper hierarchy):

1. Empirical model, one estimation block ``i`` of ``k`` intervals::

       ΔP_k = α_i + β_i · OFI_k + ε_k

2. Depth scaling, across estimation blocks::

       β_i = c · AD_i^-λ + ν_i   fitted as   ln(β_i) = ln(c) − λ·ln(AD_i)

The substituted combined expression ``ΔP = α + c·OFI/AD^λ + (ν·OFI + ε)``
is deliberately never materialized: the ``ν·OFI`` term is heteroskedastic,
so the combined form is a derived diagnostic, not a point prediction.

Determinism: all arithmetic is ``Decimal`` under a fixed precision context;
the ``input_hash`` is a SHA-256 over the canonical serialization of the
consumed intervals plus configuration, so identical inputs always yield
identical outputs and identical fit ids.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from decimal import Decimal, localcontext
from typing import Any

from .contracts import (
    FIT_MODEL_VERSION,
    DepthScalingFit,
    MicrostructureEvidence,
    OFIInterval,
    OrderBookEvent,
    PriceImpactFit,
    PriceImpactObservation,
)
from .ofi import DEPTH_ESTIMATOR, OFIAggregator
from .orderbook import OrderBookReconstructor

# Quality gates (frozen defaults; overridable by the command surface only
# within the bounded request validator).
MIN_OBSERVATIONS = 30
MIN_DEPTH_BLOCKS = 3
PRECISION = 50


def input_hash(intervals: list[OFIInterval], config: dict[str, Any]) -> str:
    """SHA-256 over canonical interval bytes + configuration.

    Identical fixture bytes, configuration, and model version yield an
    identical hash — the reproducibility guarantee required before any fit
    may be labelled ``validated``.
    """
    payload = {
        "intervals": [interval.to_dict() for interval in intervals],
        "config": config,
        "model_version": FIT_MODEL_VERSION,
        "depth_estimator": DEPTH_ESTIMATOR,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_events_from_payloads(
    event_payloads: list[dict[str, Any]],
) -> tuple[list[OrderBookEvent], int]:
    """Rebuild typed OrderBookEvent objects from persisted transport dicts.

    Entries that fail to decode are skipped and counted, never fabricated.
    The result is ordered as stored; callers that need sequence-verified
    reconstruction should run the reconstructor instead.
    """
    events: list[OrderBookEvent] = []
    dropped = 0
    for payload in event_payloads:
        try:
            events.append(OrderBookEvent.from_dict(payload))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            dropped += 1
    return events, dropped


def replay_intervals(
    events: list[OrderBookEvent], *, interval_ms: int,
) -> list[OFIInterval]:
    """Deterministically re-aggregate events into closed OFI intervals.

    Pure replay: identical event bytes and interval_ms always produce
    identical interval rows (and therefore an identical input_hash). The
    final open interval is flushed so a window ending mid-interval still
    yields its partial measurement (marked by the aggregator's own logic).
    """
    aggregator = OFIAggregator(interval_ms)
    closed: list[OFIInterval] = []
    for event in events:
        closed.extend(aggregator.add(event))
    tail = aggregator.flush()
    if tail is not None:
        closed.append(tail)
    return closed


def reconstruct_events(
    snapshot: dict[str, Any], delta_payloads: list[dict[str, Any]], *,
    symbol: str, venue: str, received_ts_ms: int = 0,
) -> list[OrderBookEvent]:
    """Full book reconstruction path: apply raw depth deltas in sequence.

    Bootstraps from one REST snapshot, then applies each persisted delta
    through the ``OrderBookReconstructor``. Any sequence gap raises
    ``BookGapError`` — the caller must treat the window as insufficient and
    never bridge a gap with inferred events. This is the strongest replay
    guarantee and is used when the raw delta stream is retained.
    """
    from .contracts import DepthDelta

    reconstructor = OrderBookReconstructor(symbol, venue)
    reconstructor.bootstrap(snapshot, received_ts_ms=received_ts_ms)
    events: list[OrderBookEvent] = []
    for payload in delta_payloads:
        delta = DepthDelta.from_dict(payload)
        event = reconstructor.apply(delta)  # BookGapError propagates to the caller
        if event is not None:
            events.append(event)
    return events


def _fit_id(kind: str, digest: str) -> str:
    return f"{kind}-{digest[:16]}"


def build_observations(
    intervals: list[OFIInterval], *, tick_size: Decimal, sensitivity: bool = False,
    events: list[OrderBookEvent] | None = None,
) -> tuple[list[PriceImpactObservation], int]:
    """Join intervals to same-window mid-price changes (ΔP_k in ticks).

    Excludes intervals that cannot form an observation: quality other than
    ``exact_feed`` or missing boundary mids. Returns the observations plus
    the excluded count.

    ``sensitivity=True`` recomputes OFI per interval using ONLY queue events
    (price-changing events dropped, paper tautology caveat). It requires the
    raw ``events`` so contributions can be re-summed; ΔP_k keeps the full
    price change because the interval mid boundaries are unchanged.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")

    sens_ofi: dict[int, Decimal] | None = None
    if sensitivity:
        if events is None:
            raise ValueError("sensitivity observations require the raw events")
        interval_ms = (intervals[0].end_ts_ms - intervals[0].start_ts_ms) if intervals else 0
        if interval_ms <= 0:
            raise ValueError("cannot derive interval length for sensitivity OFI")
        sens_ofi = {}
        for event in events:
            if event.price_changed:
                continue
            bucket = (event.current.exchange_ts_ms // interval_ms) * interval_ms
            sens_ofi[bucket] = sens_ofi.get(bucket, Decimal(0)) + event.contribution

    observations: list[PriceImpactObservation] = []
    excluded = 0
    for interval in intervals:
        if interval.quality != "exact_feed" or interval.mid_start is None or interval.mid_end is None:
            excluded += 1
            continue
        ofi = interval.ofi if sens_ofi is None else sens_ofi.get(interval.start_ts_ms, Decimal(0))
        delta_quote = interval.mid_end - interval.mid_start
        with localcontext() as ctx:
            ctx.prec = PRECISION
            delta_ticks = delta_quote / tick_size
        observations.append(PriceImpactObservation(
            interval_start_ts_ms=interval.start_ts_ms,
            interval_end_ts_ms=interval.end_ts_ms,
            ofi=ofi,
            delta_ticks=delta_ticks,
            delta_quote=delta_quote,
            mid_start=interval.mid_start,
            mid_end=interval.mid_end,
            average_depth=interval.average_depth,
            quality=interval.quality,
        ))
    return observations, excluded


def fit_price_impact(
    intervals: list[OFIInterval],
    *,
    symbol: str,
    venue: str,
    tick_size: Decimal,
    interval_seconds: int,
    min_observations: int = MIN_OBSERVATIONS,
    sensitivity: bool = False,
    events: list[OrderBookEvent] | None = None,
) -> tuple[PriceImpactFit, list[PriceImpactObservation]]:
    """OLS of ΔP_k = α + β·OFI_k over one estimation block (Decimal, HC0 SE).

    Returns the fit plus the observations it consumed. The fit's ``status``
    is the trichotomy gate:

    - ``insufficient`` — fewer than ``min_observations`` usable intervals or
      zero OFI variance. Numeric fields are computed when possible but MUST
      NOT be interpreted.
    - ``provisional`` — fitted, but diagnostics are incomplete (n below
      twice the minimum, or heteroskedasticity detected). Never a signal.
    - ``validated`` — all gates pass.
    """
    if not intervals:
        raise ValueError("fit_price_impact requires at least one interval")
    first, last = intervals[0], intervals[-1]
    if (first.symbol, first.venue) != (symbol.upper(), venue):
        raise ValueError("intervals belong to a different instrument")
    if any(i.depth_estimator not in ("", DEPTH_ESTIMATOR) for i in intervals):
        raise ValueError(f"interval depth_estimator differs from frozen {DEPTH_ESTIMATOR}")

    observations, excluded = build_observations(
        intervals, tick_size=tick_size, sensitivity=sensitivity, events=events,
    )
    digest = input_hash(intervals, {
        "symbol": symbol.upper(), "venue": venue, "tick_size": str(tick_size),
        "interval_seconds": interval_seconds, "sensitivity": sensitivity,
        "min_observations": min_observations,
    })

    def _make(alpha: Decimal, beta: Decimal, stderr: Decimal | None, r2: Decimal | None,
              resid_std: Decimal | None, hetero: bool, mean_ad: Decimal | None,
              status: str, n_obs: int) -> PriceImpactFit:
        return PriceImpactFit(
            fit_id=_fit_id("sens" if sensitivity else "beta", digest),
            symbol=symbol.upper(), venue=venue,
            window_start_ms=first.start_ts_ms, window_end_ms=last.end_ts_ms,
            interval_seconds=interval_seconds,
            alpha=alpha, beta=beta, stderr_beta=stderr, robust_se_method="HC0",
            n_observations=n_obs, excluded_observations=excluded,
            r2=r2, residual_std=resid_std, heteroskedasticity_flag=hetero,
            mean_ad=mean_ad, price_unit="ticks", tick_size=tick_size,
            input_hash=digest, model_version=FIT_MODEL_VERSION,
            sensitivity=sensitivity, status=status,
        )

    n = len(observations)
    depths = [o.average_depth for o in observations if o.average_depth is not None]
    mean_ad = (
        sum(depths, Decimal(0)) / Decimal(len(depths)) if depths else None
    )

    with localcontext() as ctx:
        ctx.prec = PRECISION
        if n < 2:
            return _make(Decimal(0), Decimal(0), None, None, None, False,
                         mean_ad, "insufficient", n), observations

        xs = [o.ofi for o in observations]
        ys = [o.delta_ticks for o in observations]
        x_bar = sum(xs, Decimal(0)) / Decimal(n)
        y_bar = sum(ys, Decimal(0)) / Decimal(n)
        sxx = sum(((x - x_bar) ** 2 for x in xs), Decimal(0))
        sxy = sum(((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)), Decimal(0))
        sst = sum(((y - y_bar) ** 2 for y in ys), Decimal(0))

        if sxx == 0:
            return _make(y_bar, Decimal(0), None, None, None, False,
                         mean_ad, "insufficient", n), observations

        beta = sxy / sxx
        alpha = y_bar - beta * x_bar
        residuals = [y - alpha - beta * x for x, y in zip(xs, ys)]
        ssr = sum((e ** 2 for e in residuals), Decimal(0))
        r2 = (Decimal(1) - ssr / sst) if sst != 0 else None
        resid_std = (ssr / Decimal(n - 2)).sqrt() if n > 2 else None
        # White/HC0 variance of the slope for a single-regressor OLS.
        hc0_var = sum(((e ** 2) * ((x - x_bar) ** 2) for e, x in zip(residuals, xs)), Decimal(0))
        stderr_beta = (hc0_var.sqrt() / sxx) if sxx > 0 else None

        # Breusch-Pagan-style proxy: split residuals by |OFI| median and
        # compare squared-residual means. A crude but deterministic flag.
        ordered = sorted(zip(((x - x_bar).copy_abs() for x in xs), residuals))
        half = n // 2
        low_sq = sum((r ** 2 for _, r in ordered[:half]), Decimal(0)) / Decimal(max(half, 1))
        high_sq = sum((r ** 2 for _, r in ordered[half:]), Decimal(0)) / Decimal(max(n - half, 1))
        hetero = (high_sq > low_sq * 2) or (low_sq > high_sq * 2)

        if n < min_observations:
            status = "insufficient"
        elif n < 2 * min_observations or hetero or r2 is None:
            status = "provisional"
        else:
            status = "validated"

        return _make(alpha, beta, stderr_beta, r2, resid_std, hetero,
                     mean_ad, status, n), observations


def fit_depth_scaling(
    block_fits: list[PriceImpactFit],
    *,
    symbol: str,
    venue: str,
    min_blocks: int = MIN_DEPTH_BLOCKS,
) -> DepthScalingFit:
    """Log-log fit ln(β_i) = ln(c) − λ·ln(AD_i) across estimation blocks.

    A single 30-minute window produces ONE β — depth scaling is unidentified
    from one point. At least ``min_blocks`` blocks with positive β, positive
    AD, and at least two distinct AD values are required; otherwise the fit
    is returned with status ``insufficient`` and null coefficients (never a
    fabricated value).
    """
    usable = [
        f for f in block_fits
        if f.mean_ad is not None and f.mean_ad > 0 and f.beta > 0
        and f.status in ("validated", "provisional")
        and not f.sensitivity
    ]
    fit_ids = tuple(f.fit_id for f in block_fits)
    digest = hashlib.sha256(json.dumps({
        "fit_ids": sorted(fit_ids), "model_version": FIT_MODEL_VERSION,
        "min_blocks": min_blocks,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _make(c: Decimal | None, lam: Decimal | None, stderr: Decimal | None,
              r2: Decimal | None, n_blocks: int, status: str) -> DepthScalingFit:
        return DepthScalingFit(
            fit_id=_fit_id("depth", digest), symbol=symbol.upper(), venue=venue,
            c=c, lambda_=lam, stderr_lambda=stderr, n_blocks=n_blocks, r2=r2,
            fit_ids=fit_ids, depth_estimator=DEPTH_ESTIMATOR,
            model_version=FIT_MODEL_VERSION, status=status,
        )

    if len(usable) < min_blocks:
        return _make(None, None, None, None, len(usable), "insufficient")
    if len({f.mean_ad for f in usable}) < 2:
        return _make(None, None, None, None, len(usable), "insufficient")

    with localcontext() as ctx:
        ctx.prec = PRECISION
        # usable filter guarantees non-None positive mean_ad; materialize the
        # narrowed values explicitly so the type checker sees Decimal, not
        # Optional[Decimal].
        pairs: list[tuple[Decimal, Decimal]] = []
        for fit in usable:
            ad = fit.mean_ad
            assert ad is not None  # guaranteed by the usable filter above
            pairs.append((ad, fit.beta))
        xs = [ad.ln() for ad, _beta in pairs]
        ys = [beta.ln() for _ad, beta in pairs]
        n = len(usable)
        x_bar = sum(xs, Decimal(0)) / Decimal(n)
        y_bar = sum(ys, Decimal(0)) / Decimal(n)
        sxx = sum(((x - x_bar) ** 2 for x in xs), Decimal(0))
        sxy = sum(((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)), Decimal(0))
        sst = sum(((y - y_bar) ** 2 for y in ys), Decimal(0))
        if sxx == 0:
            return _make(None, None, None, None, n, "insufficient")
        slope = sxy / sxx          # = −λ
        intercept = y_bar - slope * x_bar  # = ln(c)
        lam = -slope
        c = intercept.exp()
        residuals = [y - intercept - slope * x for x, y in zip(xs, ys)]
        ssr = sum((e ** 2 for e in residuals), Decimal(0))
        r2 = (Decimal(1) - ssr / sst) if sst != 0 else None
        hc0_var = sum(((e ** 2) * ((x - x_bar) ** 2) for e, x in zip(residuals, xs)), Decimal(0))
        stderr_lambda = (hc0_var.sqrt() / sxx) if n > 2 else None
        status = "validated" if n >= 2 * min_blocks and r2 is not None else "provisional"
        return _make(c, lam, stderr_lambda, r2, n, status)


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


# Scenario evaluation — the interaction-plane question "can price hit X?".
# The fitted model supplies the REQUIREMENT (required horizon flow); the
# tape supplies the PROBABILITY (empirical exceedance over horizon-length
# OFI sums). Horizons live where the question lives (15m/1h/4h) — a 10s
# distribution is rejected by design: required flow for a real target always
# sits far outside 10s flow. β is fitted per base interval and applied
# linearly over the horizon (Δ_req = n·α + β·OFI_total) — scale-invariance
# is a stated assumption, recorded on every output.
SCENARIO_HORIZONS = {"15m": 900, "1h": 3600, "4h": 14400}
MIN_SCENARIO_WINDOWS = 30
MAX_SCENARIO_INTERVALS = 50_000
_BETA_ZERO_EPS = Decimal("1e-18")


def _s(value: Decimal) -> str:
    """Fixed-point string for scenario outputs (no exponent form in prompts)."""
    return format(value, "f")


def _scenario_required_ofi(delta_req: Decimal, drift: Decimal, beta: Decimal) -> Decimal | None:
    """Required total horizon OFI, or None when β is indistinguishable from zero."""
    if abs(beta) < _BETA_ZERO_EPS:
        return None
    try:
        return (delta_req - drift) / beta
    except Exception:
        return None


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
            "scale_assumption": ("β fitted per base interval applied linearly "
                                   "over the horizon (Δ_req = n·α + β·OFI_total); "
                                   "impact scale-invariance assumed, not proven"),
        }


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
        tick_size=tick_size, depth_estimator=DEPTH_ESTIMATOR,
        input_hash=price_fit.input_hash, model_version=FIT_MODEL_VERSION,
        price_impact_fit=price_fit, sensitivity_fit=sensitivity_fit,
        depth_scaling_fit=depth_fit, block_average_depth=price_fit.mean_ad,
        coverage=coverage or {}, status=status,
    )
    return evidence


# ---------------------------------------------------------------------------
# Track D (D2-D5) — additive only. Existing fits above are frozen.
# ---------------------------------------------------------------------------

from .contracts import (
    FEATURE_VECTOR_VERSION,
    FORWARD_MODEL_VERSION,
    BestQuoteState,
    FeatureVector,
    ForwardFit,
    ForwardObservation,
)

FORWARD_HORIZONS_MS: tuple[int, ...] = (1_000, 5_000, 30_000, 60_000)

# Frozen per-field definition labels for xt-v1.
FEATURE_DEF_VERSIONS: dict[str, str] = {
    "ofi_10s": "event_contribution-v1",
    "ad_10s": "event_mean_best_bid_ask_v1",
    "dmu": "microprice-v1",
    "spread_bps": "spread-v1",
    "obi_top": "obi-top-v1",
    "cvd_slope_60s": "cvd-slope-v1",
    "skew_bps": "skew-v1",
}


def feature_input_hash(
    *, symbol: str, venue: str, ts_ms: int,
    fields: dict[str, str], def_versions: dict[str, str],
) -> str:
    """SHA-256 over the canonical feature-vector bytes (replay guarantee)."""
    payload = {
        "symbol": symbol.upper(), "venue": venue, "ts_ms": ts_ms,
        "fields": fields, "def_versions": def_versions,
        "vector_version": FEATURE_VECTOR_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_feature_vector(
    *,
    symbol: str,
    venue: str,
    ts_ms: int,
    ofi: Decimal | None = None,
    average_depth: Decimal | None = None,
    quote: BestQuoteState | None = None,
    displacement: Decimal | None = None,
    cvd_slope: Decimal | None = None,
    quality: str = "exact_feed",
) -> FeatureVector:
    """Promote Decimal measurements into a versioned xt-v1 vector (pure).

    Floats from substrates/poller must NEVER be passed here — recompute in
    Decimal from raw events/trades first. Unavailable fields are omitted
    (NULL = not-provided, never zero-filled). ``displacement`` may be passed
    explicitly (D1 output) or derived from ``quote``; an explicit value wins.
    """
    if not symbol or not venue:
        raise ValueError("symbol and venue are required")
    if ts_ms <= 0:
        raise ValueError("ts_ms must be positive")
    for name, value in (("ofi", ofi), ("average_depth", average_depth),
                         ("displacement", displacement), ("cvd_slope", cvd_slope)):
        if value is not None and isinstance(value, float):
            raise TypeError(f"{name} must be Decimal, not float — recompute, never copy")
    fields: dict[str, str] = {}
    defs: dict[str, str] = {}

    def _put(key: str, value: Decimal | None) -> None:
        if value is not None:
            fields[key] = format(value, "f")
            defs[key] = FEATURE_DEF_VERSIONS[key]

    _put("ofi_10s", ofi)
    _put("ad_10s", average_depth)
    dmu = displacement
    if dmu is None and quote is not None:
        from .microprice import displacement as _dmu
        try:
            dmu = _dmu(quote)
        except ValueError:
            dmu = None
    _put("dmu", dmu)
    if quote is not None:
        quote.validate()
        with localcontext() as ctx:
            ctx.prec = PRECISION
            mid = (quote.bid_price + quote.ask_price) / Decimal(2)
            if mid > 0:
                _put("spread_bps", (quote.ask_price - quote.bid_price) / mid * Decimal(10000))
        queue = quote.bid_qty + quote.ask_qty
        _put("obi_top", (quote.bid_qty - quote.ask_qty) / queue if queue != 0 else None)
        with localcontext() as ctx:
            ctx.prec = PRECISION
            mu = ((quote.ask_price * quote.bid_qty + quote.bid_price * quote.ask_qty)
                  / queue) if queue != 0 else None
            if mu is not None and mid > 0:
                _put("skew_bps", (mu / mid - Decimal(1)) * Decimal(10000))
    _put("cvd_slope_60s", cvd_slope)
    digest = feature_input_hash(
        symbol=symbol, venue=venue, ts_ms=ts_ms, fields=fields, def_versions=defs,
    )
    return FeatureVector(
        symbol=symbol.upper(), venue=venue, ts_ms=ts_ms,
        vector_version=FEATURE_VECTOR_VERSION, fields=fields, def_versions=defs,
        quality=quality, input_hash=digest,
    )


def build_forward_observations(
    vectors: list[FeatureVector],
    mids: list[tuple[int, Decimal]],
    *,
    tick_size: Decimal,
    venue: str,
) -> tuple[list[ForwardObservation], dict[str, int]]:
    """Join event-grain X_t to forward mid changes Y_t(h) (pure, no lookahead).

    ``mids`` is a sorted (ts_ms, mid) series from microstructure events.
    Each horizon resolves independently: gaps/halts/end-of-window/venue
    mismatch yield NULL + a counted reason, never a bridged value.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if isinstance(tick_size, float):
        raise TypeError("tick_size must be Decimal, not float")
    if not vectors:
        return [], {"excluded_total": 0}
    series = sorted(mids, key=lambda row: row[0])
    stamps = [ts for ts, _ in series]

    def _at_or_after(t: int) -> Decimal | None:
        """First mid with ts >= t (bisect; identical to linear scan)."""
        i = bisect.bisect_left(stamps, t)
        return series[i][1] if i < len(series) else None
    log: dict[str, int] = {"excluded_total": 0}
    out: list[ForwardObservation] = []
    for vec in vectors:
        if vec.venue != venue:
            y_t = {h: None for h in FORWARD_HORIZONS_MS}
            y_q = {h: None for h in FORWARD_HORIZONS_MS}
            exc = {h: "venue_mismatch" for h in FORWARD_HORIZONS_MS}
            for h in FORWARD_HORIZONS_MS:
                log[f"excluded_{h}"] = log.get(f"excluded_{h}", 0) + 1
            log["excluded_total"] += len(FORWARD_HORIZONS_MS)
            out.append(ForwardObservation(x=vec, y_ticks=y_t, y_quote=y_q,
                                          price_source="microstructure_mid", excluded=exc))
            continue
        base = _at_or_after(vec.ts_ms)
        y_t: dict[int, str | None] = {}
        y_q: dict[int, str | None] = {}
        exc2: dict[int, str] = {}
        for h in FORWARD_HORIZONS_MS:
            target = vec.ts_ms + h
            fwd = _at_or_after(target)
            if base is None or fwd is None:
                y_t[h], y_q[h], exc2[h] = None, None, "end_of_window"
                log[f"excluded_{h}"] = log.get(f"excluded_{h}", 0) + 1
                log["excluded_total"] += 1
                continue
            with localcontext() as ctx:
                ctx.prec = PRECISION
                dq = fwd - base
                y_q[h] = format(dq, "f")
                y_t[h] = format(dq / tick_size, "f")
        out.append(ForwardObservation(x=vec, y_ticks=y_t, y_quote=y_q,
                                      price_source="microstructure_mid", excluded=exc2))
    return out, log


def _forward_input_hash(symbol: str, venue: str, horizon_ms: int,
                        pairs: list[ForwardObservation]) -> str:
    payload = {
        "symbol": symbol.upper(), "venue": venue, "horizon_ms": horizon_ms,
        "model_version": FORWARD_MODEL_VERSION,
        "xs": [p.x.to_dict() for p in pairs],
        "ys": [p.y_ticks.get(horizon_ms) for p in pairs],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fit_forward_ols(
    pairs: list[ForwardObservation],
    *,
    symbol: str,
    venue: str,
    horizon_ms: int,
    feature_keys: list[str] | None = None,
    min_observations: int = MIN_OBSERVATIONS,
) -> tuple[ForwardFit, list[ForwardObservation]]:
    """Per-horizon multivariate OLS Y(h) ~ X (Decimal, time-ordered OOS).

    Usable rows: vector quality ``exact_feed`` with a non-NULL y for this
    horizon. Design columns: ``feature_keys`` (default: sorted union of
    present fields, intercept always added). Time-ordered 70/30 train/test
    split for OOS skill (in-sample R2 on train, OOS R2 on test vs train
    mean). Trichotomy mirrors ``fit_price_impact``: ``insufficient`` when
    under ``min_observations`` or zero-variance design; ``provisional``
    when heteroskedastic or below 2x minimum; else ``validated``.
    """
    if horizon_ms not in FORWARD_HORIZONS_MS:
        raise ValueError(f"unsupported horizon_ms: {horizon_ms!r}")
    usable = [p for p in pairs
              if p.x.quality == "exact_feed" and p.y_ticks.get(horizon_ms) is not None]
    n_excluded = len(pairs) - len(usable)
    digest = _forward_input_hash(symbol, venue, horizon_ms, pairs)

    def _make(betas: dict[str, Decimal], stderr: dict[str, Decimal | None],
              r2: Decimal | None, resid: Decimal | None, hetero: bool,
              oos: Decimal | None, comp: dict[str, str],
              status: str, n: int) -> ForwardFit:
        return ForwardFit(
            fit_id=_fit_id("fwd", digest), symbol=symbol.upper(), venue=venue,
            horizon_ms=horizon_ms,
            betas={k: format(v, "f") for k, v in betas.items()},
            stderr={k: (format(v, "f") if v is not None else None) for k, v in stderr.items()},
            r2=format(r2, "f") if r2 is not None else None,
            resid_std=format(resid, "f") if resid is not None else None,
            hetero_flag=hetero, n_obs=n, n_excluded=n_excluded,
            oos_skill=format(oos, "f") if oos is not None else None,
            comparator=comp, input_hash=digest,
            model_version=FORWARD_MODEL_VERSION, status=status,
        )

    keys = feature_keys or sorted({k for p in usable for k in p.x.fields})
    # Drop zero-variance columns (constant fields are collinear with the
    # intercept and would singularize the normal equations). Dropped keys
    # are simply absent from betas/stderr — documented, never imputed.
    if usable:
        counts = len(usable)
        kept: list[str] = []
        for k in keys:
            vals = [p.x.fields.get(k) for p in usable]
            if all(v == vals[0] for v in vals):
                continue
            kept.append(k)
        keys = kept
    with localcontext() as ctx:
        ctx.prec = PRECISION
        if len(usable) < 2:
            return _make({"intercept": Decimal(0)}, {"intercept": None},
                          None, None, False, None, {}, "insufficient", len(usable)), usable
        cols = ["intercept", *keys]
        X: list[list[Decimal]] = []
        y: list[Decimal] = []
        kept: list[ForwardObservation] = []
        for p in usable:
            try:
                row = [Decimal(1)] + [Decimal(p.x.fields[k]) for k in keys]
                yval = Decimal(str(p.y_ticks[horizon_ms]))
            except Exception:
                continue
            X.append(row)
            y.append(yval)
            kept.append(p)
        n = len(y)
        if n < 2:
            return _make({"intercept": Decimal(0)}, {"intercept": None},
                          None, None, False, None, {}, "insufficient", n), usable
        if not keys:
            # Intercept-only null model: estimable but carries no feature
            # information — must not wear validated/provisional.
            with localcontext() as ctx:
                ctx.prec = PRECISION
                ybar0 = sum(y, Decimal(0)) / Decimal(n)
            return _make({"intercept": ybar0}, {"intercept": None},
                          None, None, False, None, {}, "insufficient", n), kept
        k = len(cols)
        # Normal equations via Gauss-Jordan on Decimal (small k by construction).
        XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
        Xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
        aug = [row[:] + [Xty[a]] for a, row in enumerate(XtX)]
        singular = False
        for col in range(k):
            # Partial pivoting: largest-magnitude pivot in column (stability
            # for ill-conditioned designs, e.g. near-constant AD columns).
            piv = None
            best = Decimal(0)
            for r in range(col, k):
                mag = abs(aug[r][col])
                if mag > best:
                    best, piv = mag, r
            if piv is None or best == 0:
                singular = True
                break
            aug[col], aug[piv] = aug[piv], aug[col]
            pivval = aug[col][col]
            aug[col] = [v / pivval for v in aug[col]]
            for r in range(k):
                if r != col and aug[r][col] != 0:
                    factor = aug[r][col]
                    aug[r] = [rv - factor * cv for rv, cv in zip(aug[r], aug[col])]
        if singular:
            return _make({c: Decimal(0) for c in cols}, {c: None for c in cols},
                          None, None, False, None, {}, "insufficient", n), kept
        beta = [aug[a][k] for a in range(k)]
        betas = dict(zip(cols, beta))
        yhat = [sum(b * v for b, v in zip(beta, row)) for row in X]
        resid = [yi - yh for yi, yh in zip(y, yhat)]
        ybar = sum(y, Decimal(0)) / Decimal(n)
        sst = sum(((v - ybar) ** 2 for v in y), Decimal(0))
        ssr = sum(((e) ** 2 for e in resid), Decimal(0))
        r2 = (Decimal(1) - ssr / sst) if sst != 0 else None
        resid_std = (ssr / Decimal(max(n - k, 1))).sqrt() if n > k else None
        # White/HC0 sandwich: Var(beta) = (X'X)^{-1} S (X'X)^{-1} with
        # S_ab = sum_i e_i^2 X_ia X_ib. Exact (up to Decimal precision),
        # matching the single-regressor HC0 in fit_price_impact. The inverse
        # is readable from the reduced aug block.
        inv = [row[:k] for row in aug]
        meat = [[sum((resid[i] ** 2) * X[i][a] * X[i][b] for i in range(n))
                 for b in range(k)] for a in range(k)]
        se: dict[str, Decimal | None] = {}
        for j, c in enumerate(cols):
            try:
                var = sum(inv[j][a] * meat[a][b] * inv[j][b]
                          for a in range(k) for b in range(k))
                se[c] = var.sqrt() if var >= 0 else None
            except Exception:
                se[c] = None
        # Heteroskedasticity proxy: median split on |yhat - mean|.
        order = sorted(range(n), key=lambda i: abs(yhat[i] - ybar))
        half = n // 2
        lo = sum((resid[i] ** 2 for i in order[:half]), Decimal(0)) / Decimal(max(half, 1))
        hi = sum((resid[i] ** 2 for i in order[half:]), Decimal(0)) / Decimal(max(n - half, 1))
        hetero = bool((hi > lo * 2) or (lo > hi * 2)) if n >= 4 else False
        # Time-ordered OOS: first 70% train, last 30% test, skill vs train mean.
        cut = max(1, int(n * 0.7))
        oos: Decimal | None = None
        if n - cut >= 2:
            ytr, yte = y[:cut], y[cut:]
            mtr = sum(ytr, Decimal(0)) / Decimal(len(ytr))
            sst_te = sum(((v - mtr) ** 2 for v in yte), Decimal(0))
            ssr_te = sum(((v - sum(b * X[cut + i][a] for a, b in enumerate(beta))) ** 2
                           for i, v in enumerate(yte)), Decimal(0))
            oos = (Decimal(1) - ssr_te / sst_te) if sst_te != 0 else None
        # Univariate Cont comparator on the same kept rows (ofi_10s only).
        comp: dict[str, str] = {}
        ofi_rows = [(Decimal(p.x.fields["ofi_10s"]), Decimal(str(p.y_ticks[horizon_ms])))
                     for p in kept if "ofi_10s" in p.x.fields]
        if len(ofi_rows) >= 2:
            xs = [r[0] for r in ofi_rows]
            ys = [r[1] for r in ofi_rows]
            xb = sum(xs, Decimal(0)) / Decimal(len(xs))
            yb = sum(ys, Decimal(0)) / Decimal(len(ys))
            sxx = sum(((v - xb) ** 2 for v in xs), Decimal(0))
            if sxx != 0:
                b1 = sum(((x - xb) * (yy - yb) for x, yy in zip(xs, ys)), Decimal(0)) / sxx
                comp = {"beta_ofi": format(b1, "f"), "n": str(len(ofi_rows))}
        if n < min_observations:
            status = "insufficient"
        elif n < 2 * min_observations or hetero or r2 is None:
            status = "provisional"
        else:
            status = "validated"
        return _make(betas, se, r2, resid_std, hetero, oos, comp, status, n), kept


def predict_distribution(fit: ForwardFit, x: FeatureVector,
                         *, theta_ticks: Decimal | None = None) -> dict[str, Any]:
    """Expected Y(h) + 95% PI + P(>0)/P(>theta) from a FITTED ForwardFit (pure).

    Raises ValueError on gate-failed (``insufficient``) fits — deriving from
    one would be fabrication (mirrors ``derive_price_delta``). Normal-approx
    probabilities are a stated assumption, recorded on the output.
    """
    if fit.status not in ("validated", "provisional"):
        raise ValueError(f"cannot predict from {fit.status} forward fit {fit.fit_id}")
    if (x.symbol.upper(), x.venue) != (fit.symbol.upper(), fit.venue):
        raise ValueError("feature vector belongs to a different instrument")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        betas = {kk: Decimal(vv) for kk, vv in fit.betas.items()}
        exp = betas.get("intercept", Decimal(0)) + sum(
            betas.get(kk, Decimal(0)) * Decimal(x.fields[kk])
            for kk in x.fields if kk in betas
        )
        resid = Decimal(fit.resid_std) if fit.resid_std is not None else None
        out: dict[str, Any] = {
            "expected_ticks": format(exp, "f"),
            "variance_ticks": format(resid * resid, "f") if resid is not None else None,
            "interval_lo_95": format(exp - Decimal("1.96") * resid, "f") if resid is not None else None,
            "interval_hi_95": format(exp + Decimal("1.96") * resid, "f") if resid is not None else None,
            "p_positive": None,
            "p_above_theta": None,
            "resid_std": format(resid, "f") if resid is not None else None,
            "fit_id": fit.fit_id,
            "horizon_ms": fit.horizon_ms,
            "assumption": "normal-approx probabilities from residual std; stated, not proven",
        }
        if resid is not None and resid > 0:
            from math import erf, sqrt as _sqrt
            z = float(exp / resid)
            out["p_positive"] = str(0.5 * (1.0 + erf(z / _sqrt(2.0))))
            if theta_ticks is not None:
                zt = float((exp - theta_ticks) / resid)
                out["p_above_theta"] = str(0.5 * (1.0 + erf(zt / _sqrt(2.0))))
        return out


def calibration_report(fit: ForwardFit,
                       pairs: list[ForwardObservation],
                       *, n_bins: int = 5) -> dict[str, Any]:
    """Reliability bins: predicted vs realized per quantile of expected Y(h)."""
    if n_bins < 2:
        raise ValueError("n_bins must be >= 2")
    scored: list[tuple[Decimal, Decimal]] = []
    for p in pairs:
        raw = p.y_ticks.get(fit.horizon_ms)
        if raw is None or p.x.quality != "exact_feed":
            continue
        try:
            pred = Decimal(predict_distribution(fit, p.x)["expected_ticks"])
            scored.append((pred, Decimal(str(raw))))
        except Exception:
            continue
    bins: list[dict[str, Any]] = []
    calibrated = True
    if scored:
        scored.sort(key=lambda row: row[0])
        size = max(1, len(scored) // n_bins)
        for b in range(n_bins):
            chunk = scored[b * size:(b + 1) * size if b < n_bins - 1 else len(scored)]
            if not chunk:
                bins.append({"bin": b, "n": 0, "predicted": None, "realized": None})
                continue
            mp = sum((r[0] for r in chunk), Decimal(0)) / Decimal(len(chunk))
            mr = sum((r[1] for r in chunk), Decimal(0)) / Decimal(len(chunk))
            bins.append({"bin": b, "n": len(chunk),
                         "predicted": format(mp, "f"), "realized": format(mr, "f")})
            if (mp > 0) != (mr > 0) and abs(mp - mr) > (abs(mp) + abs(mr)) / 2:
                calibrated = False
    return {"fit_id": fit.fit_id, "horizon_ms": fit.horizon_ms,
            "n": len(scored), "n_bins": n_bins, "bins": bins,
            "calibrated": calibrated,
            "refusal": None if calibrated else "miscalibrated: do not quote probabilities"}


# ---------------------------------------------------------------------------
# Track D (D6/D9/D10) — additive only. Existing fits above are frozen.
# ---------------------------------------------------------------------------

from .contracts import EVIDENCE_V2_VERSION, MicrostructureEvidenceV2  # noqa: E402
from .ofi import DEPTH_ESTIMATOR as _DEPTH_ESTIMATOR  # noqa: E402

FORWARD_SCENARIO_VERSION = "fwd-scenario-v1"


def _norm_cdf(z: float) -> float:
    from math import erf, sqrt as _sqrt
    return 0.5 * (1.0 + erf(z / _sqrt(2.0)))


def evaluate_forward_scenario(
    fit: ForwardFit,
    x: FeatureVector,
    *,
    spot_price: Decimal,
    targets: list[Decimal],
    invalidations: list[Decimal],
    tick_size: Decimal,
    calibrated: bool = True,
    calibration_refusal: str | None = None,
) -> dict[str, Any]:
    """D6 horizon-native P(P_{t+h}>=T|X) / P(P_{t+h}<=S|X) curves (pure).

    Read-only over D4/D5. Normal-approx is a stated assumption, recorded on
    every output. Miscalibrated horizon -> probs NULL (never 0).
    """
    if isinstance(tick_size, float):
        raise TypeError("tick_size must be Decimal, not float")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if isinstance(spot_price, float):
        raise TypeError("spot_price must be Decimal, not float")
    if fit.status not in ("validated", "provisional"):
        raise ValueError(f"cannot scenario from {fit.status} fit {fit.fit_id}")
    if (x.symbol.upper(), x.venue) != (fit.symbol.upper(), fit.venue):
        raise ValueError("feature vector belongs to a different instrument")
    if not targets and not invalidations:
        raise ValueError("at least one target or invalidation is required")
    dist = predict_distribution(fit, x)
    mu = Decimal(str(dist["expected_ticks"]))
    sig_raw = dist.get("resid_std")
    sig = Decimal(str(sig_raw)) if sig_raw is not None else None
    null_probs = (sig is None or sig <= 0 or not calibrated)
    # SE projection for bands.
    band_method = "null-no-se"
    se_mu: Decimal | None = None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        try:
            terms: list[Decimal] = []
            if fit.stderr.get("intercept") is not None:
                terms.append(Decimal(str(fit.stderr["intercept"])) ** 2)
            for kk, vv in x.fields.items():
                s = fit.betas.get(kk) and fit.stderr.get(kk)
                if s is not None:
                    terms.append((Decimal(str(s)) * abs(Decimal(vv))) ** 2)
            if terms:
                se_mu = sum(terms, Decimal(0)).sqrt()
                band_method = "se-projection-v1"
            elif sig is not None and fit.n_obs > 0:
                se_mu = sig / Decimal(fit.n_obs).sqrt()
                band_method = "resid-over-sqrt-n-v1"
        except Exception:
            se_mu = None
            band_method = "null-no-se"
    out: dict[str, Any] = {
        "scenario_version": FORWARD_SCENARIO_VERSION,
        "fit_id": fit.fit_id, "horizon_ms": fit.horizon_ms,
        "vector_version": x.vector_version,
        "expected_ticks": format(mu, "f"),
        "expected_quote": format(spot_price + mu * tick_size, "f"),
        "resid_std_ticks": format(sig, "f") if sig is not None else None,
        "spot_price": format(spot_price, "f"),
        "tick_size": format(tick_size, "f"),
        "band_method": band_method,
        "band_note": None if se_mu is not None else "stderr null: no band range",
        "assumption": "normal-approx from D5 residual std; stated, not proven",
        "calibration": {"calibrated": calibrated, "refusal": calibration_refusal},
        "kind": "forward-conditional (horizon-native)",
        "targets": [], "invalidations": [],
    }
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for T in targets:
            if isinstance(T, float):
                raise TypeError("targets must be Decimal, not float")
            d_req = (T - spot_price) / tick_size
            row: dict[str, Any] = {"T": format(T, "f"), "d_req_ticks": format(d_req, "f"),
                                   "p_ge": None, "p_lo": None, "p_hi": None}
            if not null_probs and sig is not None and sig > 0:
                assert sig > 0
                row["p_ge"] = str(1.0 - _norm_cdf(float((d_req - mu) / sig)))
                if se_mu is not None:
                    row["p_lo"] = str(1.0 - _norm_cdf(float((d_req - (mu + Decimal("1.96") * se_mu)) / sig)))
                    row["p_hi"] = str(1.0 - _norm_cdf(float((d_req - (mu - Decimal("1.96") * se_mu)) / sig)))
                    lo, hi = row["p_lo"], row["p_hi"]
                    if lo is not None and hi is not None and float(lo) > float(hi):
                        row["p_lo"], row["p_hi"] = hi, lo
            out["targets"].append(row)
        for S in invalidations:
            if isinstance(S, float):
                raise TypeError("invalidations must be Decimal, not float")
            d_req = (S - spot_price) / tick_size
            row2: dict[str, Any] = {"S": format(S, "f"), "d_req_ticks": format(d_req, "f"),
                                    "p_le": None, "p_lo": None, "p_hi": None}
            if not null_probs and sig is not None and sig > 0:
                assert sig > 0
                row2["p_le"] = str(_norm_cdf(float((d_req - mu) / sig)))
                if se_mu is not None:
                    row2["p_lo"] = str(_norm_cdf(float((d_req - (mu + Decimal("1.96") * se_mu)) / sig)))
                    row2["p_hi"] = str(_norm_cdf(float((d_req - (mu - Decimal("1.96") * se_mu)) / sig)))
                    lo2, hi2 = row2["p_lo"], row2["p_hi"]
                    if lo2 is not None and hi2 is not None and float(lo2) > float(hi2):
                        row2["p_lo"], row2["p_hi"] = hi2, lo2
            out["invalidations"].append(row2)
    return out


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


def population_key(x: FeatureVector, horizon_ms: int, regime: str | None) -> str:
    """D9 population-membership key (statistical, not architectural)."""
    import hashlib as _hl
    import json as _js
    payload = {"vector_version": x.vector_version, "horizon_ms": horizon_ms,
               "regime": regime if regime is not None else "all",
               "quality": x.quality}
    return _hl.sha256(_js.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def skill_decay_report(
    pairs: list[ForwardObservation],
    fits: dict[int, ForwardFit],
    *,
    regime_of: dict[int, str] | None = None,
) -> dict[str, Any]:
    """D9 per-horizon skill-decay curves with bands (read-only over D4 OOS)."""
    per_h: dict[str, Any] = {}
    nulls: list[str] = []
    for h in sorted(fits):
        f = fits[h]
        n = sum(1 for p in pairs if p.y_ticks.get(h) is not None
                and p.x.quality == "exact_feed")
        per_h[str(h)] = {"oos_skill": f.oos_skill, "n": n, "status": f.status}
        if f.status == "insufficient" or f.oos_skill is None:
            nulls.append(f"{h}: no skill (status={f.status}) — null is a result")
    finalized = [h for h in sorted(fits)
                 if fits[h].status != "insufficient" and fits[h].oos_skill is not None]
    per_regime = None
    if regime_of:
        per_regime = {}
        regs = sorted(set(regime_of.values()))
        for r in regs:
            per_regime[r] = {str(h): fits[h].oos_skill for h in sorted(fits)}
    return {
        "per_horizon": per_h,
        "per_regime": per_regime,
        "decay_curve": [{"h": h, "skill": fits[h].oos_skill} for h in sorted(fits)],
        "finalized_horizons": finalized,
        "feed_resolution_note": "1s/5s require event grain (D3); 10s intervals cannot resolve them",
        "null_results": nulls,
    }


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
