"""Canonical order-flow calculations for all market analyses."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Iterable


def _bucket(ts_ms: int, window_s: int) -> int:
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    return (ts_ms // (window_s * 1000)) * window_s


def summarize(trades: list[dict], book: dict | None = None, depth_levels: int = 20) -> dict:
    """Return deterministic flow metrics plus the figures used to calculate them."""
    if depth_levels <= 0:
        raise ValueError("depth_levels must be positive")
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
        "spread": asks[0][0] - bids[0][0] if bids and asks else None,
        "spread_bps": ((asks[0][0] - bids[0][0]) / ((asks[0][0] + bids[0][0]) / 2) * 10000)
        if bids and asks and asks[0][0] + bids[0][0] else None,
        "obi": (bid_notional - ask_notional) / depth_total if depth_total else None,
        "book_levels": min(len(bids), len(asks)),
        "book_bid_notional": bid_notional,
        "book_ask_notional": ask_notional,
    }
    if not trades:
        return {
            "trade_count": 0, "buy_vol": 0.0, "sell_vol": 0.0,
            "buy_notional_usd": 0.0, "sell_notional_usd": 0.0,
            "cvd": 0.0, "cvd_usd": 0.0, "buy_share": 0.5,
            "buy_sell_ratio": None, "vwap": 0.0, "vwap_stddev": 0.0,
            "last_price": 0.0, "last_ts": None, "large_trades": [],
            "large_trade_count": 0, "large_trade_kept": 0, **book_metrics,
        }

    buy_vol = sell_vol = buy_usd = sell_usd = pv = pv2 = 0.0
    qtys: list[float] = []
    last_price = 0.0
    last_ts = 0
    for trade in trades:
        qty, price = float(trade["qty"]), float(trade["price"])
        notional = price * qty
        if trade["is_buyer_maker"]:
            sell_vol += qty
            sell_usd += notional
        else:
            buy_vol += qty
            buy_usd += notional
        pv += notional
        pv2 += price * price * qty
        qtys.append(qty)
        if int(trade["ts"]) >= last_ts:
            last_ts, last_price = int(trade["ts"]), price

    total_qty = buy_vol + sell_vol
    median_qty = statistics.median(qtys) if qtys else 0.0
    threshold = 5 * median_qty if median_qty > 0 else float("inf")
    large = [
        {"ts": int(t["ts"]), "price": float(t["price"]), "qty": float(t["qty"]),
         "notional_usd": float(t["price"]) * float(t["qty"]),
         "side": "sell" if t["is_buyer_maker"] else "buy", "venue": t.get("venue", "unknown")}
        for t in trades if float(t["qty"]) >= threshold
    ]
    vwap = pv / total_qty if total_qty else 0.0
    variance = max(0.0, pv2 / total_qty - vwap * vwap) if total_qty else 0.0
    return {
        "trade_count": len(trades), "buy_vol": buy_vol, "sell_vol": sell_vol,
        "buy_notional_usd": buy_usd, "sell_notional_usd": sell_usd,
        "cvd": buy_vol - sell_vol, "cvd_usd": buy_usd - sell_usd,
        "buy_share": buy_vol / total_qty if total_qty else 0.5,
        "buy_sell_ratio": buy_vol / sell_vol if sell_vol else None,
        "vwap": vwap, "vwap_stddev": math.sqrt(variance),
        "last_price": last_price, "last_ts": last_ts,
        "large_trades": large[:10], "large_trade_count": len(large),
        "large_trade_kept": min(len(large), 10), **book_metrics,
    }


def bucketed_cvd(trades: Iterable[dict], window_s: int = 60) -> list[dict]:
    buckets: dict[int, dict] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "buy_usd": 0.0, "sell_usd": 0.0, "trades": 0})
    for trade in trades:
        bucket = buckets[_bucket(int(trade["ts"]), window_s)]
        notional = float(trade["price"]) * float(trade["qty"])
        side = "sell" if trade["is_buyer_maker"] else "buy"
        bucket[side] += float(trade["qty"])
        bucket[f"{side}_usd"] += notional
        bucket["trades"] += 1
    return [{
        "t": ts, "delta": value["buy"] - value["sell"],
        "delta_usd": value["buy_usd"] - value["sell_usd"],
        "buy": value["buy"], "sell": value["sell"],
        "buy_usd": value["buy_usd"], "sell_usd": value["sell_usd"],
        "trades": value["trades"],
    } for ts, value in sorted(buckets.items())]


def correlate(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 2:
        return None
    left, right = [float(x) for x in a[:n]], [float(x) for x in b[:n]]
    mean_a, mean_b = statistics.fmean(left), statistics.fmean(right)
    variance_a = sum((x - mean_a) ** 2 for x in left)
    variance_b = sum((x - mean_b) ** 2 for x in right)
    if variance_a == 0 or variance_b == 0:
        return None
    return sum((x - mean_a) * (y - mean_b) for x, y in zip(left, right)) / math.sqrt(variance_a * variance_b)


def cvd_series_corr(spot_buckets: list[dict], fut_buckets: list[dict], window_s: int = 60) -> dict:
    spot = {b["t"]: b["delta"] for b in spot_buckets}
    futures = {b["t"]: b["delta"] for b in fut_buckets}
    common = sorted(set(spot) & set(futures))
    return {
        "aligned_buckets": len(common),
        "spot_vs_futures_corr": correlate([spot[t] for t in common], [futures[t] for t in common]),
        "aligned": [{"t": t, "spot_delta": spot[t], "futures_delta": futures[t]} for t in common],
        "spot_buckets": spot_buckets, "fut_buckets": fut_buckets,
        "window_seconds": window_s,
    }
