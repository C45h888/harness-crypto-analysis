"""Tiers substrate — USD-notional tier buckets + institutional balance.

TierConfig carries the USD-notional bucket thresholds (mega/large/medium/small)
so tier classification is price-aware: 200 SOL at $150 is ``small`` while 200
BTC at $65k is ``mega``. Owns the tier-bucket engine, the institutional
bid-vs-ask balance, and mega-tier concentration at the keystone.

The legacy raw-qty ladder (``_BID_TIER_THRESHOLDS``) and its
``compute_bid_tiers`` wrapper remain as DEPRECATED fallbacks for backward
compatibility; new code MUST use ``TierConfig`` via
``compute_bid_tiers_usd``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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

    DEPRECATED: raw-qty thresholds are price-blind. Use
    ``compute_bid_tiers_usd`` with a ``TierConfig`` for new callers.
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