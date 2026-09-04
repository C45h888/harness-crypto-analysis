"""Large-print substrate — taker-tape classifiers for big prints.

tiered_large_flow (large/huge/whale notional aggregation over trailing
windows) and seller_aggression_classify (seller-aggression from big prints).
These classify the TAPE by print size — the tape classifiers split out of the
legacy ``technical`` module so time-series math (EMA/ATR/trend) and tape
classification live in separate substrates.

Pure math over normalized trades (``ts``, ``qty``, ``price``,
``is_buyer_maker``); deterministic and bias-free.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

_TIERS_DEFAULT = (("large", 50.0), ("huge", 200.0), ("whale", 500.0))


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