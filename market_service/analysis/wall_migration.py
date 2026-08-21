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


# ---------------------------------------------------------------------------
# Tier counts and round-number anchors
# ---------------------------------------------------------------------------

# Tier thresholds lifted from legacy institutional_buyers.py:100-108.
# Mega: institutional-sized; Large: notable size; Medium: standard; Small: thin.
_BID_TIER_THRESHOLDS: tuple[tuple[str, float, float | None], ...] = (
    ("mega",   5000.0, None),      # qty >= 5000
    ("large",  1000.0, 5000.0),    # 1000 <= qty < 5000
    ("medium", 200.0, 1000.0),     # 200 <= qty < 1000
    ("small",  None,  200.0),      # qty < 200
)


def compute_bid_tiers(bids: list[list[float]] | list[tuple[float, float]]) -> dict[str, Any]:
    """Bucket bids into mega/large/medium/small tiers.

    Returns per-tier ``{count, qty, notional, pct}`` plus ``total_qty``.
    ``pct`` is the tier's qty share of total bid qty, in %.

    Legacy source: institutional_buyers.py:100-108 (the size-tier discriminator).
    A bid row that does not parse falls into the small tier (defensive default).
    """
    total_qty = 0.0
    tier_qty: dict[str, float] = {name: 0.0 for name, _, _ in _BID_TIER_THRESHOLDS}
    tier_count: dict[str, int] = {name: 0 for name, _, _ in _BID_TIER_THRESHOLDS}
    tier_notional: dict[str, float] = {name: 0.0 for name, _, _ in _BID_TIER_THRESHOLDS}

    for row in bids:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            p, q = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        total_qty += q
        for name, lo, hi in _BID_TIER_THRESHOLDS:
            in_lo = (lo is None) or (q >= lo)
            in_hi = (hi is None) or (q < hi)
            if in_lo and in_hi:
                tier_qty[name] += q
                tier_count[name] += 1
                tier_notional[name] += p * q
                break

    out: dict[str, Any] = {"total_qty": total_qty}
    for name, _, _ in _BID_TIER_THRESHOLDS:
        out[name] = {
            "count": tier_count[name],
            "qty": tier_qty[name],
            "notional": tier_notional[name],
            "pct": (tier_qty[name] / total_qty * 100.0) if total_qty > 0 else 0.0,
        }
    return out


# Round-number anchors (legacy institutional_buyers.py:35). Institutional
# buyers tend to anchor bids at psychologically round prices; scanning those
# levels surfaces whether smart-money is concentrated around the marker.
_ROUND_ANCHORS: tuple[tuple[str, float], ...] = (
    ("anchor_75_00", 75.00),
    ("anchor_74_50", 74.50),
    ("anchor_75_10", 75.10),
    ("anchor_74_80", 74.80),
    ("anchor_75_50", 75.50),
    ("anchor_74_00", 74.00),
    ("anchor_75_25", 75.25),
    ("anchor_75_20", 75.20),
    ("anchor_74_20", 74.20),
    ("anchor_74_30", 74.30),
    ("anchor_73_50", 73.50),
)
_ROUND_ANCHOR_TOL = 0.02


def compute_round_anchors(
    bids: list[list[float]] | list[tuple[float, float]],
) -> dict[str, Any]:
    """Aggregate bid qty at each round-number anchor.

    Returns ``{count, total_qty, anchors: [{name, level, qty, notional}]}``.
    A bid within ±0.02 of a level counts for that level. Bids can match
    multiple levels (overlapping round numbers both increment) — same logic
    as legacy institutional_buyers.py:39-46.
    """
    parsed: list[tuple[float, float, float]] = []  # (price, qty, notional)
    for row in bids:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            p, q = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        parsed.append((p, q, p * q))

    anchors: list[dict[str, Any]] = []
    total_qty = 0.0
    for name, level in _ROUND_ANCHORS:
        lo = level - _ROUND_ANCHOR_TOL
        hi = level + _ROUND_ANCHOR_TOL
        qty = sum(q for p, q, _ in parsed if lo <= p <= hi)
        notional = sum(n for p, _, n in parsed if lo <= p <= hi)
        anchors.append({"name": name, "level": level, "qty": qty, "notional": notional})
        total_qty += qty

    return {"count": len(anchors), "total_qty": total_qty, "anchors": anchors}


def bid_tier_balance(
    bids: list[list[float]] | list[tuple[float, float]],
    asks: list[list[float]] | list[tuple[float, float]],
    mega_threshold: float = 5000.0,
    bias_threshold: float = 1.2,
) -> dict[str, Any]:
    """Compare mega-tier bid qty vs mega-tier ask qty.

    Returns ``{mega_bids, mega_asks, delta, ratio, verdict, total_bids, total_asks}``.
    ``verdict`` is INSTITUTIONAL-BID-HEAVY / -ASK-HEAVY / BALANCED based on a
    20% bias threshold (configurable via ``bias_threshold``). When one side is
    empty the ratio is None and the verdict is BALANCED unless the other side
    dominates absolutely (5x).

    Legacy source: institutional_buyers.py:167-171 (institutional bid vs ask balance).
    """
    def _pairs(rows):
        out = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                out.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError):
                continue
        return out

    bids_p = _pairs(bids)
    asks_p = _pairs(asks)
    mega_bids = sum(q for _, q in bids_p if q >= mega_threshold)
    mega_asks = sum(q for _, q in asks_p if q >= mega_threshold)
    delta = mega_bids - mega_asks
    ratio = (mega_bids / mega_asks) if mega_asks > 0 else (None if mega_bids == 0 else float("inf"))
    if mega_asks == 0 and mega_bids >= mega_threshold:
        verdict = "INSTITUTIONAL-BID-HEAVY"
    elif mega_bids == 0 and mega_asks >= mega_threshold:
        verdict = "INSTITUTIONAL-ASK-HEAVY"
    elif ratio is None:
        verdict = "BALANCED"
    elif ratio > bias_threshold:
        verdict = "INSTITUTIONAL-BID-HEAVY"
    elif ratio < 1.0 / bias_threshold:
        verdict = "INSTITUTIONAL-ASK-HEAVY"
    else:
        verdict = "BALANCED"
    return {
        "mega_threshold": mega_threshold,
        "mega_bids": mega_bids,
        "mega_asks": mega_asks,
        "delta": delta,
        "ratio": ratio,
        "verdict": verdict,
        "total_bids": sum(q for _, q in bids_p),
        "total_asks": sum(q for _, q in asks_p),
    }


def mega_at_keystone(
    bids: list[list[float]] | list[tuple[float, float]],
    keystone_price: float,
    threshold: float = 5000.0,
    tol: float = 0.10,
) -> dict[str, Any]:
    """Mega-tier bid qty within ``±tol`` of the keystone price.

    Returns ``{keystone_price, threshold, tol, count, qty, notional, levels}``.
    A bid is included when both (a) it sits within ``keystone_price ± tol`` and
    (b) its qty meets ``threshold``. The level list preserves price + qty so
    briefings can show the institutional bid stack around the keystone.

    Legacy source: institutional_buyers.py:129 (mega bids at the 75.00 zone).
    """
    if tol < 0:
        raise ValueError("tol must be non-negative")
    lo = keystone_price - tol
    hi = keystone_price + tol
    parsed: list[tuple[float, float]] = []
    for row in bids or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            p, q = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if lo <= p <= hi and q >= threshold:
            parsed.append((p, q))
    parsed.sort(key=lambda x: x[0])
    return {
        "keystone_price": keystone_price,
        "threshold": threshold,
        "tol": tol,
        "count": len(parsed),
        "qty": sum(q for _, q in parsed),
        "notional": sum(p * q for p, q in parsed),
        "levels": [{"price": p, "qty": q} for p, q in parsed],
    }
