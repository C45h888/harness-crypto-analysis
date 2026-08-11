"""
Pure-Python orderflow math. Same primitives as institutional orderflow tools.

Inputs are plain dicts in the shape produced by `binance.normalize_*_trade`:
  {ts, id, price, qty, is_buyer_maker, ...}

Outputs are plain dicts so callers can JSON-dump them.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Iterable


def _signed_qty(t: dict) -> float:
    """Positive if taker bought, negative if taker sold."""
    return t["qty"] if not t["is_buyer_maker"] else -t["qty"]


def _bucket(ts_ms: int, window_s: int) -> int:
    return (ts_ms // (window_s * 1000)) * window_s


# ---------- single-shot summary ----------


def summarize(trades: list[dict], book: dict | None = None, depth_levels: int = 20) -> dict:
    """
    One-pass summary of live trades and (when supplied) the live order book.

    Trade volumes are base-asset quantities.  ``cvd_usd`` is also returned so
    spot and futures can be compared in the same quote currency.  Book fields
    are deliberately calculated here rather than by renderers, ensuring every
    harness consumer receives the same normalized evidence.

    "Large" = qty >= 5x the median of the trade set (computed once).
    """
    trades = list(trades)
    book = book or {}
    bids = [(float(p), float(q)) for p, q in book.get("bids", [])[:depth_levels]]
    asks = [(float(p), float(q)) for p, q in book.get("asks", [])[:depth_levels]]
    bid_notional = sum(p * q for p, q in bids)
    ask_notional = sum(p * q for p, q in asks)
    depth_total = bid_notional + ask_notional
    book_metrics = {
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "spread": (asks[0][0] - bids[0][0]) if bids and asks else None,
        "spread_bps": ((asks[0][0] - bids[0][0]) / ((asks[0][0] + bids[0][0]) / 2) * 10000)
        if bids and asks and (asks[0][0] + bids[0][0]) else None,
        "obi": (bid_notional - ask_notional) / depth_total if depth_total else None,
        "book_levels": min(len(bids), len(asks)),
    }
    if not trades:
        return {
            "trade_count": 0, "buy_vol": 0.0, "sell_vol": 0.0, "cvd": 0.0,
            "cvd_usd": 0.0, "buy_sell_ratio": None, "vwap": 0.0,
            "vwap_stddev": 0.0, "last_price": 0.0, "large_trades": [],
            "large_trade_count": 0, **book_metrics,
        }

    buy_vol = sell_vol = 0.0
    buy_notional = sell_notional = 0.0
    pv = 0.0
    pv2 = 0.0
    qtys: list[float] = []
    last_price = 0.0
    last_ts = 0

    for t in trades:
        q = t["qty"]
        p = t["price"]
        if not t["is_buyer_maker"]:
            buy_vol += q
            buy_notional += p * q
        else:
            sell_vol += q
            sell_notional += p * q
        pv += p * q
        pv2 += p * p * q
        qtys.append(q)
        last_price = p
        last_ts = max(last_ts, int(t["ts"]))

    # Compute the median ONCE; flag any trade whose qty is >= 5x it.
    # Skipped when median is 0 (uniform zero-qty inputs, never happens with real trades).
    med = statistics.median(qtys) if qtys else 0.0
    large_threshold = 5 * med if med > 0 else float("inf")

    large_trades: list[dict] = []
    for t in trades:
        if t["qty"] >= large_threshold:
            large_trades.append({
                "ts": int(t["ts"]),
                "price": t["price"],
                "qty": t["qty"],
                "notional_usd": t["price"] * t["qty"],
                "side": "buy" if not t["is_buyer_maker"] else "sell",
                "venue": t.get("venue", "spot"),
            })

    total_qty = buy_vol + sell_vol
    vwap = pv / total_qty if total_qty > 0 else 0.0
    var = max(0.0, (pv2 / total_qty) - vwap * vwap) if total_qty > 0 else 0.0
    stddev = math.sqrt(var)
    ratio = (buy_vol / sell_vol) if sell_vol > 0 else None

    # Cap the embedded list but keep the count truthful - caller can see the
    # full number even when only the top N are stored.
    LARGE_TRADES_KEEP = 10
    return {
        "trade_count": len(trades),
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "cvd": buy_vol - sell_vol,
        "cvd_usd": buy_notional - sell_notional,
        "buy_sell_ratio": ratio,
        "vwap": vwap,
        "vwap_stddev": stddev,
        "last_price": last_price,
        "last_ts": last_ts,
        "large_trades": large_trades[:LARGE_TRADES_KEEP],
        "large_trade_count": len(large_trades),
        "large_trade_kept": min(len(large_trades), LARGE_TRADES_KEEP),
        **book_metrics,
    }


# ---------- bucketed CVD (time series) ----------


def bucketed_cvd(trades: Iterable[dict], window_s: int = 60) -> list[dict]:
    """Per-bucket delta for plotting; cumsum gives the CVD curve."""
    buckets: dict[int, dict] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "trades": 0})
    for t in trades:
        b = buckets[_bucket(int(t["ts"]), window_s)]
        if t["is_buyer_maker"]:
            b["sell"] += t["qty"]
        else:
            b["buy"] += t["qty"]
        b["trades"] += 1
    return [
        {
            "t": ts,
            "delta": v["buy"] - v["sell"],
            "buy": v["buy"],
            "sell": v["sell"],
            "trades": v["trades"],
        }
        for ts, v in sorted(buckets.items())
    ]


# ---------- correlation between two flows ----------


def correlate(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation. Returns None if either series has <2 points or no variance.

    `EPS` is a relative tolerance for "no variance" - cheaper than fully
    guarding against float-exact zero comparison while still returning None
    for flat series.
    """
    n = min(len(a), len(b))
    if n < 2:
        return None
    a = [float(x) for x in a[:n]]
    b = [float(x) for x in b[:n]]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    sa = sum((x - ma) ** 2 for x in a)
    sb = sum((x - mb) ** 2 for x in b)
    # Tolerance: ~ machine epsilon * max(values)^2 * n. Treat very-near-zero
    # variance as zero so flat series don't blow up division.
    eps = 1e-12 * max(abs(ma), abs(mb)) ** 2 * n
    if sa < eps or sb < eps:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(sa * sb)


def cvd_series_corr(spot_buckets: list[dict], fut_buckets: list[dict], window_s: int = 60) -> dict:
    """
    Align spot and futures bucket deltas on identical wall-clock buckets and
    return their Pearson correlation along with per-bucket arrays.

    High positive correlation = spot + futures agree (directional).
    Negative correlation = divergence (often a tell: spot bids up while
    futures keep selling -> late-stage exhaustion / short squeeze setup).
    """
    s_map = {b["t"]: b["delta"] for b in spot_buckets}
    f_map = {b["t"]: b["delta"] for b in fut_buckets}
    common = sorted(set(s_map) & set(f_map))
    s = [s_map[t] for t in common]
    f = [f_map[t] for t in common]
    return {
        "aligned_buckets": len(common),
        "spot_vs_futures_corr": correlate(s, f),
        "spot_buckets": spot_buckets,
        "fut_buckets": fut_buckets,
    }