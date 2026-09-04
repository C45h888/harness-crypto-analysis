"""Density substrate — order-book density windows, keystone detection, zones.

Rolling bid/ask density windows, top density, buyer-keystone auto-derivation,
price-zone depth aggregation, significant-level filtering, keystone trade
intensity, and zone ratio / buy-sell grids.

Boards are lists of ``(price, qty)`` sorted descending for bids, ascending for
asks (Binance depth shape). All functions are pure and deterministic.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any


def rolling_density(levels: Iterable[Sequence[float]], width: float, side: str) -> list[dict]:
    """Rolling window density for each level.

    bid side: window = [p - width, p] (levels at or below the base).
    ask side: window = [p, p + width] (levels at or above the base).
    Returns [{price, qty, window_qty}] sorted by window_qty descending.
    """
    if width <= 0:
        raise ValueError("width must be positive")
    levels = [(float(p), float(q)) for p, q in levels]
    rows: list[dict] = []
    for price, qty in levels:
        if side == "bid":
            window_qty = sum(q for p, q in levels if price - width <= p <= price)
        else:
            window_qty = sum(q for p, q in levels if price <= p <= price + width)
        rows.append({"price": price, "qty": qty, "window_qty": window_qty})
    rows.sort(key=lambda r: r["window_qty"], reverse=True)
    return rows


def top_density_windows(book: dict, width: float, side: str, top_n: int = 10) -> list[dict]:
    """Return the top-N densest rolling windows for one side of the book."""
    return rolling_density(book.get(side + "s", []), width, side)[:top_n]


def find_keystone(
    bids: Iterable[Sequence[float]],
    price: float,
    width: float = 0.20,
    lo_offset: float = -0.30,
    hi_offset: float = -0.05,
    fallback: float | None = None,
) -> dict:
    """Auto-derive the buyer keystone = densest rolling bid window center in band.

    The search band is ``[price + lo_offset, price + hi_offset]`` (default just
    below price, where a buyer keystone is defended). Returns the keystone price
    and window qty, plus the conventional tight/wide defence bands around it.

    Legacy sources: deep_keystone (band ``[px-0.50, px-0.05]``), keystone_scan.
    """
    lo, hi = price + lo_offset, price + hi_offset
    seen: set[float] = set()
    candidates: list[dict] = []
    for p, q in bids:
        center = float(p)
        if not (lo <= center <= hi):
            continue
        c_round = round(center, 4)
        if c_round in seen:
            continue
        seen.add(c_round)
        window_qty = sum(bq for bp, bq in bids if center - width <= float(bp) <= center)
        candidates.append({"price": center, "window_qty": window_qty})
    if not candidates:
        center = fallback if fallback is not None else (price + lo_offset)
    else:
        candidates.sort(key=lambda c: c["window_qty"], reverse=True)
        center = candidates[0]["price"]
    return {
        # ``keystone`` is the densest bid-window center; ``bid`` is the same
        # value under its intrinsic name (a buyer keystone IS a bid-side
        # defense). Downstream readers (briefing summary) read ``bid``.
        "keystone": center,
        "bid": center,
        "window_qty": max((c["window_qty"] for c in candidates), default=0.0),
        "tight": {"lo": center - 0.05, "hi": center + 0.05},
        "wide": {"lo": center - 0.10, "hi": center + 0.10},
    }


def zone_depth(book: dict, lo: float, hi: float) -> dict:
    """Aggregate bid/ask qty + notional within a price band."""
    bids = [(float(p), float(q)) for p, q in book.get("bids", []) if lo <= float(p) <= hi]
    asks = [(float(p), float(q)) for p, q in book.get("asks", []) if lo <= float(p) <= hi]
    bid_qty = sum(q for _, q in bids)
    ask_qty = sum(q for _, q in asks)
    bid_notional = sum(p * q for p, q in bids)
    ask_notional = sum(p * q for p, q in asks)
    return {
        "lo": lo, "hi": hi,
        "bid_qty": bid_qty, "ask_qty": ask_qty,
        "bid_notional": bid_notional, "ask_notional": ask_notional,
        "net_qty": bid_qty - ask_qty,
        "net_notional": bid_notional - ask_notional,
    }


def significant_levels(levels: Iterable[Sequence[float]], min_qty: float) -> list[dict]:
    """Filter book levels to significant size (e.g. >= 1500 SOL), sorted by price."""
    out = [{"price": float(p), "qty": float(q), "notional": float(p) * float(q)}
           for p, q in levels if float(q) >= min_qty]
    out.sort(key=lambda r: r["price"])
    return out


def zone_ratio_grid(book: dict, lo: float, hi: float, step: float = 0.05) -> list[dict]:
    """Bid/ask ratio per fixed price sub-zone inside a band (keystone_scan grid)."""
    if step <= 0:
        raise ValueError("step must be positive")
    bids = [(float(p), float(q)) for p, q in book.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in book.get("asks", [])]
    zones: list[dict] = []
    z = round(lo / step) * step
    while z <= hi + 1e-9:
        z_hi = z + step
        bq = sum(q for p, q in bids if z <= p < z_hi)
        aq = sum(q for p, q in asks if z <= p < z_hi)
        ratio = bq / aq if aq else (None if bq == 0 else float("inf"))
        flag = "BID" if bq > aq * 3 else ("ASK" if aq > bq * 3 else "")
        zones.append({"lo": z, "hi": z_hi, "bid_qty": bq, "ask_qty": aq,
                      "net": bq - aq, "ratio": ratio, "flag": flag})
        z += step
    return zones


def zone_buy_sell(trades: Iterable[dict], lo: float, hi: float, step: float = 0.05) -> list[dict]:
    """Per-sub-zone buyer/seller taker qty + notional + counts inside a band."""
    if step <= 0:
        raise ValueError("step must be positive")
    zones: dict[float, dict] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "buy_n": 0, "sell_n": 0})
    for t in trades:
        price = float(t["price"])
        if not (lo <= price <= hi):
            continue
        bucket = round(round(price / step) * step, 10)
        qty = float(t["qty"])
        if t.get("is_buyer_maker"):
            zones[bucket]["sell"] += qty
            zones[bucket]["sell_n"] += 1
        else:
            zones[bucket]["buy"] += qty
            zones[bucket]["buy_n"] += 1
    return [{
        "price": p, "buy": d["buy"], "sell": d["sell"], "net": d["buy"] - d["sell"],
        "buy_n": d["buy_n"], "sell_n": d["sell_n"],
    } for p, d in sorted(zones.items())]


def keystone_trade_intensity(
    trades: Iterable[dict],
    tight_lo: float,
    tight_hi: float,
    wide_lo: float,
    wide_hi: float,
    min_wide_qty: float = 5.0,
) -> dict[str, Any]:
    """Trade-flow intensity at the keystone defence zone.

    Splits trades inside the tight zone ``[tight_lo, tight_hi]`` into
    aggressive buys (``is_buyer_maker`` False) and aggressive sells, and
    counts aggressive buys with ``qty >= min_wide_qty`` in the wide zone
    ``[wide_lo, wide_hi]``. Legacy source: deep_keystone.py:74-101 (large
    trades at keystone + aggressive buys in wide zone), keystone_scan.py:130-165.

    ``trades`` are normalized dicts with ``ts``, ``price``, ``qty``,
    ``is_buyer_maker``. All functions are pure and deterministic.
    """
    tight_buys: list[dict] = []
    tight_sells: list[dict] = []
    wide_aggressive_buys: list[dict] = []
    for t in trades:
        try:
            price = float(t["price"])
            qty = float(t["qty"])
        except (TypeError, ValueError, KeyError):
            continue
        is_buy = not t.get("is_buyer_maker")
        if tight_lo <= price <= tight_hi:
            row = {"ts": int(t.get("ts") or 0), "price": price, "qty": qty,
                   "notional": price * qty}
            if is_buy:
                tight_buys.append(row)
            else:
                tight_sells.append(row)
        if wide_lo <= price <= wide_hi and is_buy and qty >= min_wide_qty:
            wide_aggressive_buys.append({"ts": int(t.get("ts") or 0),
                                         "price": price, "qty": qty,
                                         "notional": price * qty})

    return {
        "tight": {
            "lo": tight_lo, "hi": tight_hi,
            "buy_count": len(tight_buys),
            "buy_qty": sum(r["qty"] for r in tight_buys),
            "buy_notional": sum(r["notional"] for r in tight_buys),
            "sell_count": len(tight_sells),
            "sell_qty": sum(r["qty"] for r in tight_sells),
            "sell_notional": sum(r["notional"] for r in tight_sells),
        },
        "wide_aggressive_buys": {
            "lo": wide_lo, "hi": wide_hi,
            "min_qty": min_wide_qty,
            "count": len(wide_aggressive_buys),
            "notional": sum(r["notional"] for r in wide_aggressive_buys),
        },
    }