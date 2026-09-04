"""Technical analysis primitives: EMA structure, wick-rejection, tiered flow.

Lifted from legacy exploratory tools (sol_deep_monitor) into the canonical
calculations layer so the EMA / visual-rejection thesis and tiered large-print
flow are reusable across assets instead of being hard-coded to one SOL session.

Pure math: takes normalized closes/trades, returns deterministic structures.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Sequence

EMA_PERIODS_DEFAULT = (9, 21, 50, 200)
_TIERS_DEFAULT = (("large", 50.0), ("huge", 200.0), ("whale", 500.0))


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


def tiered_large_flow(
    trades: Iterable[dict],
    now_ms: int | None = None,
    windows_min: Sequence[int] = (5, 15),
    tiers: Sequence[tuple[str, float]] = _TIERS_DEFAULT,
) -> dict[str, dict]:
    """Aggregate taker notional by tier (large/huge/whale) over trailing windows.

    ``trades`` are normalized dicts with ``ts``, ``qty``, ``price``,
    ``is_buyer_maker``. Returns {f"flow_{w}min": {...}} plus the per-minute
    buckets. Each window item carries ``large_sell_to_buy_ratio``.
    """
    if now_ms is None:
        now_ms = max((int(t["ts"]) for t in trades), default=0)

    per_min: dict[int, dict] = defaultdict(lambda: {"b": 0.0, "s": 0.0})
    for t in trades:
        ts = int(t["ts"])
        bucket = ts // 60000 * 60000
        notional = float(t["qty"]) * float(t["price"])
        side = "s" if t.get("is_buyer_maker") else "b"
        per_min[bucket][side] += notional
        for name, threshold in tiers:
            if float(t["qty"]) >= threshold:
                per_min[bucket][name + (side if side != "s" else "s")] = (
                    per_min[bucket].get(name + side, 0.0) + notional
                )
                per_min[bucket][name + side + "_n"] = per_min[bucket].get(name + side + "_n", 0) + 1

    def _aggregate(window_min: int) -> dict:
        cutoff = now_ms - window_min * 60 * 1000
        acc: dict = {"window_min": window_min}
        total_buy = total_sell = 0.0
        for m, d in per_min.items():
            if m < cutoff:
                continue
            total_buy += d.get("b", 0.0)
            total_sell += d.get("s", 0.0)
            for name, _ in tiers:
                for side in ("b", "s"):
                    key_usd = f"{name}_{'buy' if side=='b' else 'sell'}_usd"
                    key_n = f"{name}_{'buy' if side=='b' else 'sell'}_n"
                    acc[key_usd] = round(acc.get(key_usd, 0.0) + d.get(f"{name}{side}", 0.0), 0)
                    acc[key_n] = acc.get(key_n, 0) + d.get(f"{name}{side}_n", 0)
        acc["sell_to_buy_ratio"] = round(total_sell / max(total_buy, 1), 3)
        return acc

    return {f"flow_{w}min": _aggregate(w) for w in windows_min}


def seller_aggression_classify(
    trades: Iterable[dict],
    now_ms: int | None = None,
    window_min: int = 5,
    min_print_qty: float = 100.0,
) -> dict[str, Any]:
    """Seller aggression classification from large taker prints.

    Counts prints with ``qty >= min_print_qty`` inside the trailing
    ``window_min`` window, split by taker side (``is_buyer_maker`` True =
    taker sell). ``sell_ratio`` = taker-sell qty / taker-buy qty; the
    classification thresholds are the legacy seller_wall_check.py:233-240
    discriminator:

      sell_ratio > 1.5  → HIGH
      sell_ratio > 0.8  → MEDIUM
      otherwise         → LOW

    Null discipline: with no trades inside the window, ``sell_ratio`` and
    ``classification`` are None (no evidence — not a fabricated LOW). When
    big sells exist but big buys are zero the ratio is None (unbounded) and
    the classification is HIGH.

    ``trades`` are normalized dicts with ``ts``, ``qty``,
    ``is_buyer_maker``. Legacy source: seller_wall_check.py:162-240.
    """
    if now_ms is None:
        now_ms = max((int(t["ts"]) for t in trades), default=0)
    cutoff_ms = now_ms - window_min * 60 * 1000

    big_buys = 0
    big_sells = 0
    qty_buy = 0.0
    qty_sell = 0.0
    trades_in_window = 0
    for t in trades:
        try:
            ts = int(t["ts"])
            qty = float(t["qty"])
        except (TypeError, ValueError, KeyError):
            continue
        if ts < cutoff_ms:
            continue
        trades_in_window += 1
        if qty >= min_print_qty:
            if t.get("is_buyer_maker"):
                big_sells += 1
                qty_sell += qty
            else:
                big_buys += 1
                qty_buy += qty

    if trades_in_window == 0:
        return {
            "window_min": window_min,
            "min_print_qty": min_print_qty,
            "trades_in_window": 0,
            "big_buys": 0,
            "big_sells": 0,
            "taker_buy_qty": 0.0,
            "taker_sell_qty": 0.0,
            "sell_ratio": None,
            "classification": None,
        }

    if qty_buy > 0:
        sell_ratio: float | None = qty_sell / qty_buy
    else:
        sell_ratio = None

    if sell_ratio is None:
        classification = "HIGH" if qty_sell > 0 else None
    elif sell_ratio > 1.5:
        classification = "HIGH"
    elif sell_ratio > 0.8:
        classification = "MEDIUM"
    else:
        classification = "LOW"

    return {
        "window_min": window_min,
        "min_print_qty": min_print_qty,
        "trades_in_window": trades_in_window,
        "big_buys": big_buys,
        "big_sells": big_sells,
        "taker_buy_qty": qty_buy,
        "taker_sell_qty": qty_sell,
        "sell_ratio": sell_ratio,
        "classification": classification,
    }
