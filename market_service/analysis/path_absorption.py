"""Path absorption: can buyers reach the entry, and can price descend.

Lifted from legacy ``path_absorption.py``. Models whether buyers have enough
passive bid fuel to lift the asks in front of an entry level (simulated
ascent), whether a short can push price down (simulated descent), a fuel-ratio
verdict with confidence, and the TBR x OI demand-source combo.

Pure functions over normalized books. All price levels are parameters (the
legacy hardcoded 74.44 / 73.50 targets are removed by design).
"""

from __future__ import annotations

from typing import Iterable, Sequence


def cum_ask_to(asks: Iterable[Sequence[float]], target: float) -> tuple[float, float]:
    """Cumulative ask qty + notional from the best ask up to ``target``."""
    qty = notional = 0.0
    for p, q in asks:
        if float(p) <= target:
            qty += float(q)
            notional += float(q) * float(p)
    return qty, notional


def cum_bid_to(bids: Iterable[Sequence[float]], target: float) -> tuple[float, float]:
    """Cumulative bid qty + notional from the best bid down to ``target``."""
    qty = notional = 0.0
    for p, q in bids:
        if float(p) >= target:
            qty += float(q)
            notional += float(q) * float(p)
    return qty, notional


def fuel_ratio(bids: Iterable[Sequence[float]], asks: Iterable[Sequence[float]],
               entry: float, bid_floor: float) -> dict:
    """Buyer fuel: required buy volume to reach ``entry`` vs bid pool to floor."""
    req_buy_vol, req_buy_notional = cum_ask_to(asks, entry)
    avail_fuel, avail_notional = cum_bid_to(bids, bid_floor)
    ratio = avail_fuel / req_buy_vol if req_buy_vol > 0 else 999.0
    if ratio > 1.5:
        verdict, confidence = "BUYERS HAVE AMPLE FUEL", "HIGH"
    elif ratio >= 1.0:
        verdict, confidence = "BUYERS CAN REACH", "MEDIUM"
    elif ratio >= 0.5:
        verdict, confidence = "BUYERS STRUGGLE", "MEDIUM-LOW"
    else:
        verdict, confidence = "BUYERS EXHAUSTED BEFORE TARGET", "HIGH"
    return {
        "required_buy_vol_sol": req_buy_vol, "required_buy_notional_usd": req_buy_notional,
        "available_bid_fuel_sol": avail_fuel, "available_bid_notional_usd": avail_notional,
        "fuel_ratio": ratio, "verdict": verdict, "confidence": confidence,
    }


def simulated_ascent(asks: Iterable[Sequence[float]], total_bid_fuel: float,
                     levels: Sequence[float]) -> list[dict]:
    """Simulate buyers pushing up: at each level, asks to absorb vs bid fuel left."""
    out = []
    for lvl in levels:
        asks_needed, _ = cum_ask_to(asks, lvl)
        fuel_remaining = total_bid_fuel - asks_needed
        out.append({"level": lvl, "asks_to_absorb": asks_needed,
                    "bid_fuel_remaining": fuel_remaining,
                    "verdict": "BUYERS_REACH" if fuel_remaining > 0 else "BUYERS_EXHAUSTED"})
    return out


def simulated_descent(bids: Iterable[Sequence[float]], total_ask_fuel: float,
                      total_bid_fuel: float, levels: Sequence[float]) -> list[dict]:
    """Simulate sellers pushing down: bids to absorb vs ask fuel left (short-squeeze check)."""
    out = []
    for lvl in levels:
        bids_to_absorb, _ = cum_bid_to(bids, lvl)
        ask_fuel_remaining = total_ask_fuel - bids_to_absorb
        if ask_fuel_remaining > 0 and bids_to_absorb < total_bid_fuel * 0.6:
            verdict = "SHORT VALID"
        elif ask_fuel_remaining > 0:
            verdict = "SHORT OK (tight)"
        else:
            verdict = "SQUEEZE RISK"
        out.append({"level": lvl, "bids_to_absorb": bids_to_absorb,
                    "ask_fuel_remaining": ask_fuel_remaining, "verdict": verdict})
    return out


def fall_short_level(ascent_verdicts: list[dict]) -> float | None:
    """First level where simulated ascent exhausts bid fuel (buyers fall short at)."""
    for row in ascent_verdicts:
        if row["bid_fuel_remaining"] < 0:
            return row["level"]
    return None


def wall_concentration(asks: Iterable[Sequence[float]], lo: float, hi: float,
                       required_buy_vol: float) -> dict:
    """Seller-wall concentration in a price zone; HEAVY WALL when >50% of required vol."""
    rows = [(float(p), float(q)) for p, q in asks if lo <= float(p) <= hi]
    qty = sum(q for _, q in rows)
    return {
        "zone": {"lo": lo, "hi": hi}, "qty": qty, "levels": len(rows),
        "notional": sum(p * q for p, q in rows),
        "pct_of_required": (qty / required_buy_vol * 100) if required_buy_vol else None,
        "heavy_wall": bool(required_buy_vol and qty > required_buy_vol * 0.5),
    }


def tbr_oi_combo(tbr_trend: str, oi_trend: str) -> str:
    """Demand-source combo from taker-buy-pressure and OI direction.

    Legacy source: path_absorption TBR+OI combined verdict block.
    """
    buy_pressure = "BUY PRESSURE BUILDING" in tbr_trend
    sell_pressure = "SELL PRESSURE BUILDING" in tbr_trend
    if buy_pressure and "OI RISING" in oi_trend:
        return "NEW LONGS OPENING (real buyer demand)"
    if buy_pressure and "OI FLAT" in oi_trend:
        return "SHORT COVERING (paper support, not real demand)"
    if buy_pressure and "OI FALLING" in oi_trend:
        return "SHORT COVERS + LIQUIDATIONS"
    if sell_pressure and "OI RISING" in oi_trend:
        return "NEW SHORTS OPENING (real paper selling)"
    if sell_pressure and "OI FLAT" in oi_trend:
        return "LONG UNWINDING (longs closing)"
    if sell_pressure and "OI FALLING" in oi_trend:
        return "LIQUIDATION CASCADE"
    return "MIXED / STABLE"