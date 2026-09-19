"""Route A — Direct OFI model (single-factor OLS baseline).

Statistical inference: Y_t(h) = alpha + beta * OFI_t + epsilon_t

This substrate answers: "Given the current OFI, what price displacement does
the direct OFI relationship imply?"

Output: \(\hat\mu_A=\hat\alpha+\hat\beta OFI_t\) along with its uncertainty,
sample size, estimation window, and validation status.

This is your direct microstructure baseline.  It lives independently of the
depth-scaling (Route B) and the full multivariate model (Route C).
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from .contracts import (
    FIT_MODEL_VERSION,
    OFIInterval,
    OrderBookEvent,
    PriceImpactFit,
    PriceImpactObservation,
)
from .ofi import DEPTH_ESTIMATOR
from .fitting_common import PRECISION, MIN_OBSERVATIONS, input_hash, _fit_id


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