"""DELTA substrate — the signed -2..+2 DELTA variable.

Combo of range wall imbalance + taker buy/sell flow alignment, plus the
per-band imbalance heatmap. Lifted from legacy ``consolidated.py:107`` and
``delta_calc.py:96``. Pure over normalized inputs (book dict + taker_buy_sell
list); deterministic and bias-free.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def _pairs(levels: Iterable[Sequence[float]] | None) -> list[tuple[float, float]]:
    """Coerce an iterable of (price, qty) sequences into a list of float pairs."""
    out: list[tuple[float, float]] = []
    for row in levels or ():
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            out.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError):
            continue
    return out


def wall_imbalance(levels: Iterable[Sequence[float]], lo: float, hi: float) -> float:
    """Signed (qty_in_lo_hi - 0) on a one-sided book slice.

    Range: -1..+1 (positive = more qty than zero, negative = same). Returns 0
    on an empty slice. Caller decides whether the slice is bids or asks.
    """
    s = 0.0
    for p, q in _pairs(levels):
        if lo <= p <= hi:
            s += q
    return s  # range -1..+1 won't apply — qty is unbounded; see range_imbalance()


def range_imbalance(
    book: dict[str, Any],
    price: float,
    half_range: float = 0.75,
    band_step: float = 0.10,
) -> dict[str, Any]:
    """Per-band wall imbalance across [price-half_range, price+half_range].

    Returns one entry per ``band_step`` slice plus the summed range imbalance
    (sum of per-band signed imbalances, each in -1..+1). Bands with no qty
    contribute 0. The band grid lives here so callers don't have to compute
    the geometric banding themselves.
    """
    if half_range <= 0:
        raise ValueError("half_range must be positive")
    if band_step <= 0:
        raise ValueError("band_step must be positive")
    # Floor the lower band to band_step so the grid aligns across runs.
    lo = (int((price - half_range) / band_step)) * band_step
    hi = (int((price + half_range) / band_step) + 1) * band_step

    bids = _pairs(book.get("bids"))
    asks = _pairs(book.get("asks"))

    bands: list[dict[str, Any]] = []
    sum_imb = 0.0
    z = lo
    while z < hi:
        band_lo, band_hi = z, z + band_step
        bq = sum(q for p, q in bids if band_lo <= p < band_hi)
        aq = sum(q for p, q in asks if band_lo <= p < band_hi)
        s = bq + aq
        if s > 0:
            imb = (bq - aq) / s  # -1..+1
        else:
            imb = 0.0
        bands.append({
            "lo": band_lo, "hi": band_hi,
            "bid_qty": bq, "ask_qty": aq,
            "imbalance": imb,
        })
        sum_imb += imb
        z += band_step

    return {
        "price": price,
        "range": {"lo": lo, "hi": hi, "half": half_range, "step": band_step},
        "bands": bands,
        "sum_imbalance": sum_imb,
    }


def flow_alignment(taker_buy_sell: list[dict[str, Any]] | None) -> float:
    """Latest-bar taker buy alignment, normalized to -1..+1.

    ``taker_buy_sell`` is a list of bars newest-last (Binance default). Uses
    the last bar's buySellRatio if present, else falls back to the buy_vol /
    (buy_vol + sell_vol) ratio. Returns 0 on missing / empty / malformed input.
    """
    if not taker_buy_sell:
        return 0.0
    last = taker_buy_sell[-1] if isinstance(taker_buy_sell[-1], dict) else None
    if not last:
        return 0.0
    # Prefer buySellRatio (buy_vol / sell_vol) — Binance's canonical field.
    for k in ("buySellRatio", "buy_sell_ratio"):
        if k in last and last[k] is not None:
            try:
                r = float(last[k])
                # buySellRatio = 1 means neutral; >1 = buy-heavy, <1 = sell-heavy.
                # Map (0, +inf) onto (-1, +1) via tanh-like transform so a
                # 2:1 buy ratio doesn't pin to +1.
                if r <= 0:
                    return -1.0
                # r -> (r - 1) / (r + 1) gives (-1, +1) for r in (0, +inf)
                return (r - 1.0) / (r + 1.0)
            except (TypeError, ValueError):
                pass
    bv = float(last.get("buyVol") or last.get("buy_vol") or 0)
    sv = float(last.get("sellVol") or last.get("sell_vol") or 0)
    s = bv + sv
    if s <= 0:
        return 0.0
    return (bv / s - 0.5) * 2  # -1..+1


def tbr_last_pct(taker_buy_sell: list[dict[str, Any]] | None) -> float | None:
    """Latest-bar taker buy share as a percentage (50 = neutral). None on missing."""
    if not taker_buy_sell:
        return None
    last = taker_buy_sell[-1] if isinstance(taker_buy_sell[-1], dict) else None
    if not last:
        return None
    bv = float(last.get("buyVol") or last.get("buy_vol") or 0)
    sv = float(last.get("sellVol") or last.get("sell_vol") or 0)
    s = bv + sv
    if s <= 0:
        return None
    return bv / s * 100.0


def tbr_avg_pct(taker_buy_sell: list[dict[str, Any]] | None, last_n: int = 3) -> float | None:
    """Average taker buy share over the last ``last_n`` bars (3 by default)."""
    if not taker_buy_sell:
        return None
    rows = taker_buy_sell[-last_n:] if last_n > 0 else []
    if not rows:
        return None
    bv_total = sv_total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        bv_total += float(r.get("buyVol") or r.get("buy_vol") or 0)
        sv_total += float(r.get("sellVol") or r.get("sell_vol") or 0)
    s = bv_total + sv_total
    if s <= 0:
        return None
    return bv_total / s * 100.0


def delta_variable(
    book: dict[str, Any],
    taker_buy_sell: list[dict[str, Any]] | None,
    price: float,
    *,
    half_range: float = 0.75,
    band_step: float = 0.10,
) -> dict[str, Any]:
    """DELTA = range wall imbalance + flow alignment. Range: -2..+2.

    Legacy source: ``consolidated.py:107``, ``delta_calc.py:96``. Returns the
    band grid alongside the signed combo so callers can read both the
    heatmap (per-band bid/ask imbalance) and the single signed variable.

    ``wall_imbalance`` matches the legacy single-value form:
        (sum_bids - sum_asks) / (sum_bids + sum_asks) over the half-range
    — a number in -1..+1. ``flow_alignment`` is also -1..+1 (from the latest
    taker buy/sell bar). The DELTA sum is the legacy -2..+2 contract.

    ``bands`` and ``sum_imbalance`` are the per-band heatmap (each in -1..+1,
    sum lives in -N..+N where N=number of bands). The per-band sum is exposed
    as ``delta_raw`` for callers who want the unbounded version.
    """
    grid = range_imbalance(book, price, half_range=half_range, band_step=band_step)
    fa = flow_alignment(taker_buy_sell)

    # Legacy single-value wall imbalance over the whole half-range.
    bids = _pairs(book.get("bids"))
    asks = _pairs(book.get("asks"))
    lo = grid["range"]["lo"]
    hi = grid["range"]["hi"]
    sum_b = sum(q for p, q in bids if lo <= p <= hi)
    sum_a = sum(q for p, q in asks if lo <= p <= hi)
    s = sum_b + sum_a
    wall_imb = (sum_b - sum_a) / s if s > 0 else 0.0  # -1..+1

    delta = wall_imb + fa  # -2..+2
    raw = grid["sum_imbalance"] + fa
    n_bands = max(len(grid["bands"]), 1)
    tbr_pct = tbr_last_pct(taker_buy_sell)
    return {
        "price": price,
        "wall_imbalance": wall_imb,             # -1..+1 (legacy single-value)
        "flow_alignment": fa,                   # -1..+1
        "delta": delta,                          # -2..+2 (legacy contract)
        "delta_raw": raw,                        # sum-of-bands + fa, unbounded
        "tbr_last_pct": tbr_pct,                 # 0..100, None on missing
        "tbr_3avg_pct": tbr_avg_pct(taker_buy_sell, 3),
        "bands": grid["bands"],
        "range": grid["range"],
        "n_bands": n_bands,
        "range_totals": {"bid_qty": sum_b, "ask_qty": sum_a},
    }


def delta_state(delta: float) -> str:
    """Map a signed DELTA value to a human-readable state label."""
    if delta > 1.0:
        return "BUYERS_IN_CONTROL"
    if delta > 0.25:
        return "BUYERS_FAVORED"
    if delta > 0.0:
        return "BUYERS_SLIGHTLY_FAVORED"
    if delta > -0.25:
        return "SELLERS_SLIGHTLY_FAVORED"
    if delta > -1.0:
        return "SELLERS_FAVORED"
    return "SELLERS_IN_CONTROL"