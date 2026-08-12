"""Seller-wall migration, buyer-fuel capacity, and keystone-holds scoring.

Lifted from legacy ``seller_wall_check.py`` + ``wall_analysis.py``. The unique
algorithms — snapshot-diff wall migration, zero-bid vacuum, fuel ratio,
level-absorption capacity, trap detection, and the keystone-holds probability
scorecard — are extracted as pure functions. All price levels are parameters
(no hardcoded SOL session prices).

Boards are lists of ``(price, qty)`` pairs.
"""

from __future__ import annotations

from typing import Iterable, Sequence


def depth_qty(levels: Iterable[Sequence[float]], lo: float, hi: float) -> float:
    """Total quantity of levels within ``[lo, hi]``."""
    return sum(float(q) for p, q in levels if lo <= float(p) <= hi)


def depth_qty_at(levels: Iterable[Sequence[float]], price: float, tol: float = 0.005) -> float:
    """Total quantity within ``price +/- tol``."""
    return sum(float(q) for p, q in levels if abs(float(p) - price) <= tol)


def wall_delta(prior_walls: dict[float, float], ask_levels: Iterable[Sequence[float]],
               window: float = 0.02, direction_threshold: float = 1.15) -> dict:
    """Snapshot-diff wall migration: compare current ask clusters to a prior pull.

    ``prior_walls`` = {level: prior_qty}. Returns per-level
    {level, prior, current_exact, current_window, delta, direction} where
    direction is BUILT UP / ERODED / STABLE (vs ``direction_threshold``).
    Legacy source: seller_wall_check (30-min wall migration block).
    """
    asks = [(float(p), float(q)) for p, q in ask_levels]
    out: dict = {"probes": {}, "walls": []}
    for level, prior in sorted(prior_walls.items()):
        now_exact = depth_qty_at(asks, level)
        now_window = depth_qty(asks, level - window / 2, level + window / 2)
        if now_window > prior * direction_threshold:
            direction = "BUILT UP"
        elif now_window < prior / direction_threshold:
            direction = "ERODED"
        else:
            direction = "STABLE"
        out["walls"].append({
            "level": level, "prior": prior, "current_exact": now_exact,
            "current_window": now_window, "delta_window": now_window - prior,
            "direction": direction,
        })
    return out


def fuel_ratio(bids: Iterable[Sequence[float]], asks: Iterable[Sequence[float]],
               price: float, bid_floor: float, ask_target: float) -> dict:
    """Buyer fuel: bid pool below ``price`` vs ask pool up to ``ask_target``.

    Returned ``ratio`` > 1.0 => buyers have more passive bid than they need to
    lift the asks in front of them. Legacy source: seller_wall_check fuel calc.
    """
    bids = [(float(p), float(q)) for p, q in bids]
    asks = [(float(p), float(q)) for p, q in asks]
    bid_pool = depth_qty(bids, bid_floor, price)
    ask_pool = depth_qty(asks, price, ask_target)
    ratio = bid_pool / ask_pool if ask_pool > 0 else float("inf")
    return {
        "bid_floor": bid_floor, "ask_target": ask_target,
        "bid_pool": bid_pool, "ask_pool": ask_pool,
        "ratio": ratio,
        "verdict": "BUYERS_HAVE_FUEL" if ratio > 1.0 else "SELLERS_DOMINATE",
    }


def densest_clusters(bids: Iterable[Sequence[float]], floors: Iterable[float],
                     window: float = 0.10) -> list[dict]:
    """Rank bid clusters by density over fixed windows anchored at ``floors``.

    Legacy source: seller_wall_check densest-bid-cluster block.
    """
    bids = [(float(p), float(q)) for p, q in bids]
    clusters = []
    for lo in floors:
        q = depth_qty(bids, lo, lo + window)
        clusters.append({"lo": lo, "hi": lo + window, "qty": q})
    clusters.sort(key=lambda c: c["qty"], reverse=True)
    return clusters


def level_absorption(bids: Iterable[Sequence[float]], levels: Iterable[float],
                     buffer: float = 0.01, sizes: Sequence[float] = (1000.0, 5000.0, 10000.0)) -> list[dict]:
    """Absorption capacity per key bid level — % of each notional the book absorbs.

    A zero-quantity level is reported as ``no_bid`` (the zero-bid vacuum).
    Legacy source: seller_wall_check reaction-at-key-bid-levels block.
    """
    bids = [(float(p), float(q)) for p, q in bids]
    out = []
    for lvl in levels:
        qty = depth_qty_at(bids, lvl, buffer)
        if qty == 0:
            out.append({"level": lvl, "qty": 0.0, "no_bid": True,
                        "absorbed_pct": {s: None for s in sizes}})
            continue
        out.append({"level": lvl, "qty": qty, "no_bid": False,
                    "absorbed_pct": {s: s / qty * 100 for s in sizes}})
    return out


def wall_trap_assessment(fuel_ratio_value: float, ask_walls_built: int,
                         ask_walls_eroded: int = 0) -> dict:
    """Probability of buyers absorbing the asks + trap-down flag.

    Legacy source: seller_wall_check final-verdict probability block.
    """
    if fuel_ratio_value > 1.5 and ask_walls_built <= 1:
        prob = 0.65
    elif fuel_ratio_value > 1.2 and ask_walls_built <= 2:
        prob = 0.55
    elif fuel_ratio_value > 1.0:
        prob = 0.45
    else:
        prob = 0.30
    trap = ask_walls_built >= 3 or fuel_ratio_value < 1.0
    return {"probability_absorb": prob, "trap_down": trap,
            "ask_walls_built": ask_walls_built, "ask_walls_eroded": ask_walls_eroded}


def keystone_wall_balance(bids: Iterable[Sequence[float]], asks: Iterable[Sequence[float]],
                          kz_lo: float, kz_hi: float, sw_lo: float, sw_hi: float) -> dict:
    """Bid/ask qty + notional balance between the keystone zone and seller wall."""
    bids = [(float(p), float(q)) for p, q in bids]
    asks = [(float(p), float(q)) for p, q in asks]
    kz_qty = depth_qty(bids, kz_lo, kz_hi)
    kz_notional = sum(p * q for p, q in bids if kz_lo <= p <= kz_hi)
    sw_qty = depth_qty(asks, sw_lo, sw_hi)
    sw_notional = sum(p * q for p, q in asks if sw_lo <= p <= sw_hi)
    return {
        "keystone": {"qty": kz_qty, "notional": kz_notional},
        "seller_wall": {"qty": sw_qty, "notional": sw_notional},
        "bid_ask_qty_ratio": kz_qty / max(sw_qty, 1e-9),
        "bid_ask_notional_ratio": kz_notional / max(sw_notional, 1e-9),
    }


def keystone_holds_scorecard(bid_ask_qty_ratio: float, latest_tbr: float,
                             oi_chg_5m: float, top_long_pct: float,
                             net_buy_ratio: float) -> dict:
    """Multi-factor 'will the keystone hold' score (0-10) with probability.

    Legacy source: wall_analysis probability-scorecard block. Deterministic.
    """
    score = 0
    max_score = 10
    if bid_ask_qty_ratio > 0.5:
        score += 2
    elif bid_ask_qty_ratio > 0.3:
        score += 1
    if latest_tbr > 0.55:
        score += 2
    elif latest_tbr > 0.50:
        score += 1
    if oi_chg_5m > 0:
        score += 2
    elif oi_chg_5m > -0.1:
        score += 1
    if top_long_pct < 0.75:
        score += 2
    elif top_long_pct < 0.80:
        score += 1
    if net_buy_ratio > 0.55:
        score += 2
    elif net_buy_ratio > 0.50:
        score += 1
    prob = score / max_score * 100
    return {
        "score": score, "max_score": max_score,
        "keystone_holds_probability": round(prob, 1),
        "keystone_breaks_probability": round(100 - prob, 1),
        "factors": {"bid_ask_qty_ratio": bid_ask_qty_ratio, "taker_buy_ratio": latest_tbr,
                    "oi_chg_5m_pct": oi_chg_5m, "top_long_pct": top_long_pct,
                    "net_buy_ratio": net_buy_ratio},
    }
