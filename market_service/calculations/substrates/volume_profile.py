"""Volume-profile substrate — price-bucketed volume distribution.

POC / value area / HVN / LVN from normalized trades. Lifted from legacy
``long_term_flow.py`` so the volume-profile read is a reusable canonical
primitive. Pure math over normalized trades (``ts``, ``price``, ``qty``,
``is_buyer_maker``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

VALUE_AREA_RATIO = 0.70  # share of total volume that defines the value area


def build_volume_profile(trades: Iterable[dict], bucket_size: float = 0.05) -> dict[str, dict]:
    """Aggregate buy/sell qty and trade counts per price bucket.

    Returns {price: {buy, sell, n, buy_n, sell_n}}. Price is rounded to the
    nearest ``bucket_size`` multiple (rounded, not floored — the same bucketing
    the legacy profile used).
    """
    if bucket_size <= 0:
        raise ValueError("bucket_size must be positive")
    buckets: dict[str, dict] = defaultdict(
        lambda: {"price": 0.0, "buy": 0.0, "sell": 0.0, "n": 0, "buy_n": 0, "sell_n": 0})
    for t in trades:
        b = round(round(float(t["price"]) / bucket_size) * bucket_size, 10)
        d = buckets[repr(b)]
        d["price"] = b
        d["n"] += 1
        if t.get("is_buyer_maker"):
            d["sell"] += float(t["qty"])
            d["sell_n"] += 1
        else:
            d["buy"] += float(t["qty"])
            d["buy_n"] += 1
        buckets[repr(b)] = d
    return buckets


def volume_profile_summary(buckets: dict[str, dict]) -> dict | None:
    """Compute POC, VAH/VAL, HVN/LVN and price range from a volume profile."""
    if not buckets:
        return None
    prices = sorted(float(d["price"]) for d in buckets.values())
    total_vol = sum(d["buy"] + d["sell"] for d in buckets.values())
    if total_vol <= 0:
        return None
    # POC = bucket with max total volume
    poc = max(buckets.values(), key=lambda d: d["buy"] + d["sell"])["price"]
    # Value area = the densest buckets covering VALUE_AREA_RATIO of volume
    sorted_by_vol = sorted(buckets.values(), key=lambda d: d["buy"] + d["sell"], reverse=True)
    cum = 0.0
    va_prices: list[float] = []
    for d in sorted_by_vol:
        cum += d["buy"] + d["sell"]
        va_prices.append(float(d["price"]))
        if cum >= total_vol * VALUE_AREA_RATIO:
            break
    vah, val = max(va_prices), min(va_prices)
    # HVN = top-decile volume buckets; LVN = sparse (gap) buckets.
    # Clamp the threshold index: a profile with <2 buckets (quiet window,
    # single price bucket) must not raise IndexError.
    vols = sorted((d["buy"] + d["sell"]) for d in buckets.values())
    hvn_idx = min(max(1, len(vols) // 10), len(vols) - 1)
    hvn_threshold = vols[hvn_idx]
    hvn = sorted(float(d["price"]) for d in buckets.values() if (d["buy"] + d["sell"]) >= hvn_threshold)
    mean_vol = total_vol / len(buckets)
    lvn = sorted(float(d["price"]) for d in buckets.values() if (d["buy"] + d["sell"]) < mean_vol * 0.3)
    return {
        "total_volume": total_vol,
        "poc": poc,
        "vah": vah,
        "val": val,
        "low": prices[0],
        "high": prices[-1],
        "hvn": hvn,
        "lvn": lvn,
        "bucket_count": len(buckets),
    }


def side_split(buckets: dict[str, dict], top_n: int = 8) -> dict:
    """Top buckets by aggressive BUY volume and by SELL volume."""
    items = list(buckets.values())
    by_buy = sorted(items, key=lambda d: d["buy"], reverse=True)[:top_n]
    by_sell = sorted(items, key=lambda d: d["sell"], reverse=True)[:top_n]
    return {
        "top_buy": [{"price": float(d["price"]), "buy_qty": d["buy"],
                     "notional": float(d["price"]) * d["buy"], "trades": d["buy_n"]} for d in by_buy],
        "top_sell": [{"price": float(d["price"]), "sell_qty": d["sell"],
                      "notional": float(d["price"]) * d["sell"], "trades": d["sell_n"]} for d in by_sell],
    }