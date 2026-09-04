"""Seller-wall migration, buyer-fuel capacity, and keystone-holds scoring.

Lifted from legacy ``seller_wall_check.py`` + ``wall_analysis.py``. The unique
algorithms — snapshot-diff wall migration, zero-bid vacuum, fuel ratio,
level-absorption capacity, trap detection, and the keystone-holds probability
scorecard — are extracted as pure functions. All price levels are parameters
(no hardcoded SOL session prices).

Boards are lists of ``(price, qty)`` pairs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


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
# Tier counts and round-number anchors
# ---------------------------------------------------------------------------

# Tier thresholds lifted from legacy institutional_buyers.py:100-108.
# Mega: institutional-sized; Large: notable size; Medium: standard; Small: thin.
#
# Phase 1.2: these are kept as a DEPRECATED raw-qty fallback for the
# legacy function ``compute_bid_tiers``. New code MUST use
# ``TierConfig`` (USD-notional buckets) via ``compute_bid_tiers_usd``
# so thresholds are price-aware: 200 SOL ≈ $30k vs 200 BTC ≈ $13M are
# not equivalent.
_BID_TIER_THRESHOLDS: tuple[tuple[str, float, float | None], ...] = (
    ("mega",   5000.0, None),      # qty >= 5000
    ("large",  1000.0, 5000.0),    # 1000 <= qty < 5000
    ("medium", 200.0, 1000.0),     # 200 <= qty < 1000
    ("small",  None,  200.0),      # qty < 200
)


@dataclass(frozen=True)
class TierConfig:
    """USD-notional bucket thresholds for bid / ask tier classification.

    Defaults are tuned for the deep-mid-cap crypto order book (SOL, ETH,
    majors at retail venues): a $250k notional mega bid is roughly an
    institutional print; $50k large; $10k medium; anything below is
    thin. Callers can override per-instrument.

    The thresholds are USD-denominated so they generalize across price
    regimes — 200 BTC at $65k is $13M (mega) while 200 SOL at $150 is
    $30k (small). The legacy raw-qty tiers (``_BID_TIER_THRESHOLDS``)
    conflated these into a single ladder and were hardcoded for one SOL
    session; they remain only for backwards compatibility in the
    ``compute_bid_tiers`` wrapper.
    """

    mega_usd: float = 250_000.0
    large_usd: float = 50_000.0
    medium_usd: float = 10_000.0
    # Tier ordering is fixed: mega > large > medium > small. Anything
    # below medium_usd falls into ``small`` by exclusion.

    def thresholds(self) -> tuple[tuple[str, float, float | None], ...]:
        """Materialize the (name, lo_usd, hi_usd) ladder used by the engine.

        ``lo`` is inclusive; ``hi`` is exclusive (None for the top tier).
        Returns the same shape as ``_BID_TIER_THRESHOLDS`` so the engine
        is identical between the legacy and USD-aware paths.
        """
        return (
            ("mega",   self.mega_usd, None),
            ("large",  self.large_usd, self.mega_usd),
            ("medium", self.medium_usd, self.large_usd),
            ("small",  None, self.medium_usd),
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "mega_usd": self.mega_usd,
            "large_usd": self.large_usd,
            "medium_usd": self.medium_usd,
        }


def _bid_tiers_impl(
    bids: list[list[float]] | list[tuple[float, float]],
    thresholds: tuple[tuple[str, float, float | None], ...],
) -> dict[str, Any]:
    """Shared engine for the legacy (raw qty) and USD-aware paths."""
    total_qty = 0.0
    total_notional = 0.0
    tier_qty: dict[str, float] = {name: 0.0 for name, _, _ in thresholds}
    tier_count: dict[str, int] = {name: 0 for name, _, _ in thresholds}
    tier_notional: dict[str, float] = {name: 0.0 for name, _, _ in thresholds}

    for row in bids:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            p, q = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if q <= 0 or p <= 0:
            continue
        notional = p * q
        total_qty += q
        total_notional += notional
        for name, lo, hi in thresholds:
            in_lo = (lo is None) or (notional >= lo)
            in_hi = (hi is None) or (notional < hi)
            if in_lo and in_hi:
                tier_qty[name] += q
                tier_count[name] += 1
                tier_notional[name] += notional
                break

    out: dict[str, Any] = {
        "total_qty": total_qty,
        "total_notional": total_notional,
    }
    for name, _, _ in thresholds:
        out[name] = {
            "count": tier_count[name],
            "qty": tier_qty[name],
            "notional": tier_notional[name],
            "pct": (tier_qty[name] / total_qty * 100.0) if total_qty > 0 else 0.0,
        }
    return out


def compute_bid_tiers_usd(
    bids: list[list[float]] | list[tuple[float, float]],
    tier_config: TierConfig | None = None,
) -> dict[str, Any]:
    """Bucket bids into mega/large/medium/small tiers by USD notional.

    Returns per-tier ``{count, qty, notional, pct}`` plus ``total_qty``
    and ``total_notional``. ``pct`` is the tier's qty share of total
    bid qty, in %.

    This is the price-aware replacement for ``compute_bid_tiers``: the
    legacy function used raw qty thresholds that were correct for one
    SOL session and wrong for everything else. ``tier_config`` carries
    the USD thresholds; default ``TierConfig()`` matches the
    ``institutional_buyers`` legacy for SOL-scale venues.
    """
    cfg = tier_config or TierConfig()
    out = _bid_tiers_impl(bids, cfg.thresholds())
    out["tier_config"] = cfg.to_dict()
    return out


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


# Phase 1.3: ATR-aware wall-band defaults.
#
# The legacy SOL-session defaults (``price * 0.97``, ``price * 1.03``,
# ``price * 1.01``) were hardcoded 3%/1% bands. For instruments with
# higher or lower volatility those bands are wrong: a 3% band on a
# $0.01 micro-cap is enormous, and a 3% band on a $100k BTC 4h range
# is tiny. ``default_wall_band`` scales the band widths with the
# instrument's recent volatility (``atr_pct``) so the wall / path
# absorption adapters use bands that match the actual trading range.

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


# Round-number anchors (legacy institutional_buyers.py:35). Institutional
# buyers tend to anchor bids at psychologically round prices; scanning those
# levels surfaces whether smart-money is concentrated around the marker.
#
# Phase 1.1: ``_ROUND_ANCHORS`` is DEPRECATED. The hardcoded SOL-specific
# levels (74.00..75.50) were never correct for any other instrument.
# New code MUST use ``derive_round_anchors`` in
# ``calculations/orderbook.py`` to generate the anchor list from the
# current price + tick size. This tuple is kept as a fallback when the
# caller does not pass anchors and no price is available.


def compute_round_anchors(
    bids: list[list[float]] | list[tuple[float, float]],
    anchors: Sequence[dict] | None = None,
    tol: float = _ROUND_ANCHOR_TOL,
) -> dict[str, Any]:
    """Aggregate bid qty at each round-number anchor.

    Returns ``{count, total_qty, anchors: [{name, level, qty, notional}]}``.
    A bid within ±``tol`` of a level counts for that level. Bids can match
    multiple levels (overlapping round numbers both increment) — same logic
    as legacy institutional_buyers.py:39-46.

    ``anchors`` is the dynamic anchor list produced by
    ``calculations.orderbook.derive_round_anchors``. When omitted, the
    legacy hardcoded SOL list is used as a fallback (DEPRECATED — new
    callers must always pass anchors derived from the current price).
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

    if anchors:
        anchor_specs = [
            (a.get("name") or f"anchor_{a.get('level'):.4f}",
             float(a["level"]),
             float(a.get("lo", a["level"] - tol)),
             float(a.get("hi", a["level"] + tol)))
            for a in anchors if isinstance(a, dict) and "level" in a
        ]
    else:
        anchor_specs = [
            (name, level, level - tol, level + tol)
            for name, level in _ROUND_ANCHORS
        ]

    out_anchors: list[dict[str, Any]] = []
    total_qty = 0.0
    for name, level, lo, hi in anchor_specs:
        qty = sum(q for p, q, _ in parsed if lo <= p <= hi)
        notional = sum(n for p, _, n in parsed if lo <= p <= hi)
        out_anchors.append({
            "name": name,
            "level": level,
            "lo": lo,
            "hi": hi,
            "qty": qty,
            "notional": notional,
        })
        total_qty += qty

    return {
        "count": len(out_anchors),
        "total_qty": total_qty,
        "anchors": out_anchors,
        "anchor_source": "dynamic" if anchors else "legacy_fallback",
        "tol": tol,
    }


def bid_tier_balance(
    bids: list[list[float]] | list[tuple[float, float]],
    asks: list[list[float]] | list[tuple[float, float]],
    mega_threshold: float | None = None,
    bias_threshold: float = 1.2,
    tier_config: TierConfig | None = None,
) -> dict[str, Any]:
    """Compare mega-tier bid qty vs mega-tier ask qty.

    Returns ``{mega_bids, mega_asks, delta, ratio, verdict, total_bids, total_asks}``.
    ``verdict`` is INSTITUTIONAL-BID-HEAVY / -ASK-HEAVY / BALANCED based on a
    20% bias threshold (configurable via ``bias_threshold``). When one side is
    empty the ratio is None and the verdict is BALANCED unless the other side
    dominates absolutely (5x).

    Mega-tier classification is by USD notional when ``tier_config`` is
    provided (price-aware: a 5 BTC bid at $65k is mega, a 5 SOL bid at
    $150 is small). When omitted, ``mega_threshold`` defaults to the
    legacy raw-qty value of 5000 for backwards compatibility; new
    callers should pass a ``TierConfig``.

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

    if tier_config is not None:
        mega_usd = tier_config.mega_usd
        threshold_used_usd = mega_usd
        legacy_qty = None
        mega_bids = sum(q for p, q in bids_p if p * q >= mega_usd)
        mega_asks = sum(q for p, q in asks_p if p * q >= mega_usd)
    else:
        legacy_qty = 5000.0 if mega_threshold is None else float(mega_threshold)
        mega_bids = sum(q for _, q in bids_p if q >= legacy_qty)
        mega_asks = sum(q for _, q in asks_p if q >= legacy_qty)
        threshold_used_usd = None

    delta = mega_bids - mega_asks
    if legacy_qty is not None:
        one_sided_threshold = legacy_qty
    else:
        one_sided_threshold = float("inf")  # USD path is always bidirectional

    ratio = (mega_bids / mega_asks) if mega_asks > 0 else (None if mega_bids == 0 else float("inf"))
    if mega_asks == 0 and mega_bids > 0:
        verdict = "INSTITUTIONAL-BID-HEAVY"
    elif mega_bids == 0 and mega_asks > 0:
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
        "mega_threshold": legacy_qty,
        "mega_threshold_usd": threshold_used_usd,
        "mega_bids": mega_bids,
        "mega_asks": mega_asks,
        "delta": delta,
        "ratio": ratio,
        "verdict": verdict,
        "total_bids": sum(q for _, q in bids_p),
        "total_asks": sum(q for _, q in asks_p),
        "one_sided_threshold": one_sided_threshold,
    }


def mega_at_keystone(
    bids: list[list[float]] | list[tuple[float, float]],
    keystone_price: float,
    threshold: float | None = None,
    tol: float = 0.10,
    tier_config: TierConfig | None = None,
) -> dict[str, Any]:
    """Mega-tier bid qty within ``±tol`` of the keystone price.

    Returns ``{keystone_price, threshold, threshold_usd, tol, count, qty, notional, levels}``.
    A bid is included when both (a) it sits within ``keystone_price ± tol`` and
    (b) its classification meets the mega threshold. With ``tier_config``
    the classification is USD-notional-based (price-aware); without it
    the legacy raw-qty threshold of 5000 is used for backwards
    compatibility. The level list preserves price + qty so briefings
    can show the institutional bid stack around the keystone.

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
        if not (lo <= p <= hi):
            continue
        if tier_config is not None:
            if p * q < tier_config.mega_usd:
                continue
        else:
            legacy_qty = 5000.0 if threshold is None else float(threshold)
            if q < legacy_qty:
                continue
        parsed.append((p, q))
    parsed.sort(key=lambda x: x[0])
    return {
        "keystone_price": keystone_price,
        "threshold": threshold,
        "threshold_usd": tier_config.mega_usd if tier_config is not None else None,
        "tol": tol,
        "count": len(parsed),
        "qty": sum(q for _, q in parsed),
        "notional": sum(p * q for p, q in parsed),
        "levels": [{"price": p, "qty": q} for p, q in parsed],
    }
