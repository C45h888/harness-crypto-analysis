"""Seller-wall migration, buyer-fuel capacity, and keystone-holds scoring.

Lifted from legacy ``seller_wall_check.py`` + ``wall_analysis.py``. The unique
algorithms — snapshot-diff wall migration, zero-bid vacuum, fuel ratio,
level-absorption capacity, trap detection, and the keystone-holds probability
scorecard — are extracted as pure functions. All price levels are parameters
(no hardcoded SOL session prices).

Boards are lists of ``(price, qty)`` pairs.

Analysis-only: the tier / round-anchor primitives that used to live here were
decomposed DOWN into the calculation substrate layer
(``calculations.substrates.tiers`` / ``.anchors``) where they always belonged;
this module re-exports them at the bottom as a DEPRECATED compat seam for
historical import paths (same pattern as ``market_service/signals.py``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


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
                             net_buy_ratio: float,
                             weights: dict[str, float] | None = None) -> dict:
    """Multi-factor 'will the keystone hold' score (0-10) with probability.

    Legacy source: wall_analysis probability-scorecard block. Deterministic.

    Phase 2.4: weights are configurable. Each factor contributes
    ``{0, 1, weight}`` to the score based on its threshold ladder;
    the default ``weight`` is 2 (the legacy behavior, total ceiling
    10). When ``weights`` is provided, the ceiling scales with the
    weights so operators can re-prioritize drivers without changing
    the threshold logic.

    The legacy signature (positional floats only) is preserved: when
    ``weights`` is omitted the function behaves exactly as before and
    every existing test passes unchanged.
    """
    w = {
        "bid_ask_qty_ratio": 1.0,
        "taker_buy_trend": 1.0,
        "oi_change_trend": 1.0,
        "top_long_drift": 1.0,
        "net_buy_trend": 1.0,
    }
    if weights:
        for k, v in weights.items():
            if k in w:
                try:
                    w[k] = float(v)
                except (TypeError, ValueError):
                    continue
    score = 0.0
    factor_breakdown: dict[str, dict[str, float]] = {}

    # Ladders mirror the legacy threshold ladder exactly. Each entry is
    # ``(threshold, raw_contribution)``; the ladder is ordered tightest
    # first. ``op`` distinguishes the comparison direction:
    #   "gt" — value strictly greater than threshold hits
    #   "lt" — value strictly less than threshold hits
    # This matches the legacy ``> 0.5`` / ``< 0.75`` semantics: 0.5 hits
    # neither, 0.5000001 hits the first ladder entry.
    def _add(name: str, ladder: tuple[tuple[float, float], ...],
             value: float, op: str) -> float:
        weight = w.get(name, 0.0)
        contributed = 0.0
        hit_threshold: float | None = None
        for threshold, contrib in ladder:
            if op == "gt" and value > threshold:
                contributed = contrib
                hit_threshold = threshold
                break
            if op == "lt" and value < threshold:
                contributed = contrib
                hit_threshold = threshold
                break
        weighted = contributed * weight
        factor_breakdown[name] = {
            "value": value,
            "threshold_hit": hit_threshold,
            "raw_contribution": contributed,
            "weight": weight,
            "weighted_contribution": weighted,
        }
        return weighted

    # bid_ask_qty_ratio: higher is better (more bid than ask)
    score += _add("bid_ask_qty_ratio",
                  ((0.5, 2.0), (0.3, 1.0)), bid_ask_qty_ratio, "gt")
    # taker_buy_trend (latest): higher is better (taker buy pressure)
    score += _add("taker_buy_trend",
                  ((0.55, 2.0), (0.50, 1.0)), latest_tbr, "gt")
    # oi_change_trend (latest pct): higher is better
    score += _add("oi_change_trend",
                  ((0.0, 2.0), (-0.1, 1.0)), oi_chg_5m, "gt")
    # top_long_drift: lower is better (less crowding = less risk of long unwind)
    score += _add("top_long_drift",
                  ((0.75, 2.0), (0.80, 1.0)), top_long_pct, "lt")
    # net_buy_trend (latest): higher is better
    score += _add("net_buy_trend",
                  ((0.55, 2.0), (0.50, 1.0)), net_buy_ratio, "gt")

    # Normalize to 0-10 so the score remains a probability ceiling.
    # ``max_weighted`` is what the score would be if every factor was
    # maxed at its ladder top (contrib=2 for each), under the current
    # weights. Dividing by that keeps the ceiling at 10 while letting
    # weights change the relative importance of each driver.
    max_weighted = sum(2.0 * w[k] for k in w) or 10.0
    normalized_score = score / max_weighted * 10.0 if max_weighted else 0.0
    rounded_score = round(normalized_score, 2)
    prob = round(normalized_score / 10.0 * 100, 1)
    return {
        "score": rounded_score,
        "raw_score": round(score, 4),
        "max_score": 10.0,
        "max_weighted": round(max_weighted, 4),
        "weights_used": w,
        "keystone_holds_probability": prob,
        "keystone_breaks_probability": round(100 - prob, 1),
        "factors": {"bid_ask_qty_ratio": bid_ask_qty_ratio, "taker_buy_ratio": latest_tbr,
                    "oi_chg_5m_pct": oi_chg_5m, "top_long_pct": top_long_pct,
                    "net_buy_ratio": net_buy_ratio},
        "factor_breakdown": factor_breakdown,
    }


# ---------------------------------------------------------------------------
# Phase 1.3: ATR-aware wall-band defaults.
#
# The legacy SOL-session defaults (``price * 0.97``, ``price * 1.03``,
# ``price * 1.01``) were hardcoded 3%/1% bands. For instruments with
# higher or lower volatility those bands are wrong. ``default_wall_band``
# scales the band widths with the instrument's recent volatility
# (``atr_pct``) so the wall / path absorption adapters use bands that match
# the actual trading range.
# ---------------------------------------------------------------------------

_DEFAULT_BAND_FLOOR_BPS = 30     # 0.30% minimum floor band
_DEFAULT_BAND_CEILING_BPS = 100  # 1.00% baseline ceiling band
_DEFAULT_BAND_MAX_CEILING_BPS = 300  # 3.00% ceiling band cap

# Volatility scaling: at ``atr_pct >= 2%`` we hit the ceiling cap; below
# 0.5% we sit at the floor; in between we scale linearly. The chosen
# thresholds reflect "calm majors" (BTC/ETH at <0.5% daily) up to
# "high-vol alts" (>2% daily).
_BAND_ATR_FLOOR_PCT = 0.5
_BAND_ATR_CEILING_PCT = 2.0


def default_wall_band(
    price: float,
    atr_pct: float | None = None,
    floor_bps: int = _DEFAULT_BAND_FLOOR_BPS,
    ceiling_bps: int = _DEFAULT_BAND_CEILING_BPS,
    max_ceiling_bps: int = _DEFAULT_BAND_MAX_CEILING_BPS,
) -> dict[str, float]:
    """ATR-aware wall / path absorption band around ``price``.

    Returns ``{bid_floor, entry, ask_target}`` (the three legacy
    multipliers replaced). All three are absolute price levels — the
    adapter passes them straight into the wall / path absorption pure
    functions instead of computing ``price * 0.97`` etc.

    Volatility scaling (when ``atr_pct`` is provided):
      atr_pct <= 0.5%   -> use floor_bps (default 30 bps / 0.30%)
      atr_pct >= 2.0%   -> use min(max_ceiling_bps, ceiling_bps * 3) (default 300 bps / 3.00%)
      in between        -> linear interpolation between floor and ceiling
    When ``atr_pct`` is None the floor_bps is used (legacy behavior
    for adapters that do not have a 5m ATR series yet — the band is
    the safest minimum rather than the historical SOL 3% guess).
    """
    if price <= 0:
        raise ValueError("price must be positive")
    if floor_bps <= 0 or ceiling_bps <= 0 or max_ceiling_bps <= 0:
        raise ValueError("band bps thresholds must be positive")
    if floor_bps > ceiling_bps:
        raise ValueError("floor_bps must be <= ceiling_bps")
    if ceiling_bps > max_ceiling_bps:
        raise ValueError("ceiling_bps must be <= max_ceiling_bps")

    chosen_bps = float(floor_bps)
    scaling_source = "default_floor"
    if atr_pct is not None:
        try:
            atr = float(atr_pct)
        except (TypeError, ValueError):
            atr = None
        if atr is not None and atr > 0:
            ceiling_target = min(max_ceiling_bps, ceiling_bps * 3.0)
            if atr >= _BAND_ATR_CEILING_PCT:
                chosen_bps = ceiling_target
                scaling_source = "atr_ceiling"
            elif atr <= _BAND_ATR_FLOOR_PCT:
                chosen_bps = float(floor_bps)
                scaling_source = "atr_floor"
            else:
                # Linear interpolation between the two anchors.
                span = _BAND_ATR_CEILING_PCT - _BAND_ATR_FLOOR_PCT
                t = (atr - _BAND_ATR_FLOOR_PCT) / span
                chosen_bps = floor_bps + t * (ceiling_target - floor_bps)
                scaling_source = "atr_linear"

    band = chosen_bps / 10000.0  # bps -> fraction
    # ``bid_floor`` is below price by ``band``; ``ask_target`` is above
    # by ``band``; ``entry`` is the simple midpoint (price itself) so
    # the path-absorption fuel ratio uses a target halfway between the
    # two anchors — independent of band width.
    bid_floor = price * (1.0 - band)
    ask_target = price * (1.0 + band)
    entry = price
    return {
        "bid_floor": bid_floor,
        "entry": entry,
        "ask_target": ask_target,
        "band_bps": chosen_bps,
        "scaling_source": scaling_source,
        "atr_pct": atr_pct,
    }


# ---------------------------------------------------------------------------
# DEPRECATED re-export seam — tier / anchor primitives were decomposed DOWN
# into the calculation substrate layer (calculations.substrates.tiers and
# .anchors). This block exists ONLY so historical import paths
# (``analysis.wall_migration.TierConfig`` etc.) keep working; new code must
# import from the substrate package. Same pattern as market_service/signals.py.
# ---------------------------------------------------------------------------

from market_service.calculations.substrates.anchors import (  # noqa: F401
    _ROUND_ANCHOR_TOL,
    _ROUND_ANCHORS,
    compute_round_anchors,
)
from market_service.calculations.substrates.tiers import (  # noqa: F401
    _BID_TIER_THRESHOLDS,
    TierConfig,
    _bid_tiers_impl,
    bid_tier_balance,
    compute_bid_tiers,
    compute_bid_tiers_usd,
    mega_at_keystone,
)

# __all__ includes the DEPRECATED tier/anchor re-exports (owned by
# calculations.substrates.tiers / .anchors) for historical import paths.
__all__ = [
    "TierConfig",
    "bid_tier_balance",
    "compute_bid_tiers",
    "compute_bid_tiers_usd",
    "compute_round_anchors",
    "default_wall_band",
    "densest_clusters",
    "depth_qty",
    "depth_qty_at",
    "fuel_ratio",
    "keystone_holds_scorecard",
    "keystone_wall_balance",
    "level_absorption",
    "mega_at_keystone",
    "wall_delta",
    "wall_trap_assessment",
]