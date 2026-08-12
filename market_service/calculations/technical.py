"""Technical analysis primitives: EMA structure, wick-rejection, tiered flow.

Lifted from legacy exploratory tools (sol_deep_monitor) into the canonical
calculations layer so the EMA / visual-rejection thesis and tiered large-print
flow are reusable across assets instead of being hard-coded to one SOL session.

Pure math: takes normalized closes/trades, returns deterministic structures.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Sequence

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
