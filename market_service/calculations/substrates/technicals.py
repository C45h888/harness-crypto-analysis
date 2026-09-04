"""Technicals substrate — time-series technical primitives.

EMA structure, wick-rejection against EMA levels, ATR-as-percent-of-close,
and OLS slope / drift on trailing windows. Pure math: takes normalized closes
and kline series, returns deterministic structures. The large-print tape
classifiers (tiered_large_flow, seller_aggression_classify) live in the
``large_print`` substrate, not here — this substrate owns time-series math
only.

Lifted from legacy exploratory tools (sol_deep_monitor) into the canonical
calculations layer so the EMA / visual-rejection thesis and ATR scaling are
reusable across assets instead of being hard-coded to one SOL session.
"""

from __future__ import annotations

from collections.abc import Sequence

EMA_PERIODS_DEFAULT = (9, 21, 50, 200)


def ema(values: Sequence[float], period: int) -> float | None:
    """Exponential moving average. Seeded with the SMA of the first ``period`` values.

    Returns None if fewer than ``period`` values are available (so callers can
    distinguish "no signal yet" from a real value).
    """
    if not values or len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for v in values[period:]:
        value = v * k + value * (1 - k)
    return value


def atr_pct_from_klines(
    klines: Sequence[Sequence[float]],
    period: int = 14,
) -> float | None:
    """ATR as a percent of the last close from a Binance kline series.

    ``klines`` is a sequence of Binance candles in the documented shape:
    ``[open_time, open, high, low, close, volume, ...]``. Returns the
    ``period``-bar SMA of true-range expressed as a percent of the most
    recent close (so 1.5 = 1.5% daily ATR). Returns None when fewer
    than ``period + 1`` candles are available.

    True range per bar is ``max(high - low, |high - prev_close|, |low - prev_close|)``.
    The percent expression makes the result comparable across instruments
    and price regimes (used by ``default_wall_band`` in the wall adapter
    to scale band widths with volatility).
    """
    if not klines or period <= 0:
        return None
    candles = [k for k in klines if isinstance(k, (list, tuple)) and len(k) >= 5]
    if len(candles) < period + 1:
        return None
    prev_close = None
    trs: list[float] = []
    for k in candles:
        try:
            _o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
        except (TypeError, ValueError):
            prev_close = None
            trs.clear()
            continue
        if prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    last_close = float(candles[-1][4])
    if last_close <= 0:
        return None
    return atr / last_close * 100.0


def trend_slope(values: Sequence[float], n: int = 3) -> float | None:
    """OLS slope of the last ``n`` values vs integer index (1..n).

    Returns the per-step slope in raw units. Useful for ``n``-bar
    trends on TBR / OI / funding / top-trader series. Positive slope
    = rising, negative = falling, zero = flat. Returns None if fewer
    than ``n`` finite values are available.

    The slope is scaled to per-step (per-bar) units, not per-window, so
    a slope of ``+0.02`` over ``n=3`` means ``+0.02`` per bar (not
    ``+0.06`` over the 3-bar window). Callers wanting window-total
    change should multiply by ``n - 1``.
    """
    if n < 2:
        raise ValueError("n must be >= 2 for OLS slope")
    if not values:
        return None
    finite: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            finite.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(finite) < n:
        return None
    y = finite[-n:]
    # x = 0..n-1 (n points). Mean center for OLS slope = sum((x - x̄)(y - ȳ)) / sum((x - x̄)^2)
    xs = list(range(n))
    mean_x = (n - 1) / 2.0
    mean_y = sum(y) / n
    num = sum((x - mean_x) * (yv - mean_y) for x, yv in zip(xs, y))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


def trend_drift(values: Sequence[float], n: int = 3) -> float | None:
    """Last minus first of the trailing ``n`` values (simple drift).

    Cheaper and more interpretable than ``trend_slope`` for binary
    "is this rising or falling" classification. Returns None if
    fewer than ``n`` finite values are available.
    """
    if n <= 1 or not values:
        return None
    finite: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            finite.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(finite) < n:
        return None
    window = finite[-n:]
    return window[-1] - window[0]


def ema_series(values: Sequence[float], periods: Sequence[int] = EMA_PERIODS_DEFAULT) -> dict[str, float]:
    """Return {emaN: value} for each period that has enough data."""
    out: dict[str, float] = {}
    for p in periods:
        v = ema(values, p)
        if v is not None:
            out[f"ema{p}"] = round(v, 6)
    return out


def ema_position(values: Sequence[float], periods: Sequence[int] = EMA_PERIODS_DEFAULT,
                 last_price: float | None = None) -> dict[str, str]:
    """Classify the latest close as ABOVE / BELOW each EMA (None if no signal).

    ``last_price`` defaults to the final close in ``values``.
    """
    if not values:
        return {}
    ref = values[-1] if last_price is None else last_price
    return {f"ema{p}": ("ABOVE" if ref > v else "BELOW")
            for p in periods if (v := ema(values, p)) is not None}


def wick_rejections(high: float, low: float, close: float, emas: Sequence[float],
                    prefix: str = "") -> list[str]:
    """Detect wick rejections of EMA levels on one candle.

    upper rejection: price wicked above the level but closed below it.
    lower rejection: price wicked below the level but closed above it.
    Each returned label is ``<prefix>_<rounded>_upper_wick_reject`` (or lower).
    """
    rejects: list[str] = []
    for v in emas:
        tag = f"{prefix}_{v:.0f}" if prefix else f"{v:.0f}"
        if high > v and close < v:
            rejects.append(f"{tag}_upper_wick_reject")
        if low < v and close > v:
            rejects.append(f"{tag}_lower_wick_reject")
    return rejects