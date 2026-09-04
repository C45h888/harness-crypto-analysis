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
    return MicrostructureEvidence(
        symbol=symbol.upper(), venue=venue, evidence_id=evidence_id,
        generated_at_ms=generated_at_ms, interval_seconds=interval_seconds,
        window_start_ms=first.start_ts_ms, window_end_ms=last.end_ts_ms,
        tick_size=tick_size, depth_estimator=DEPTH_ESTIMATOR,
        input_hash=price_fit.input_hash, model_version=FIT_MODEL_VERSION,
        price_impact_fit=price_fit, sensitivity_fit=sensitivity_fit,
        depth_scaling_fit=depth_fit, block_average_depth=price_fit.mean_ad,
        coverage=coverage or {}, status=status,
    )
