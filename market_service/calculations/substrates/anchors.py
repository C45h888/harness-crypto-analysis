"""Anchors substrate — dynamic round-number anchors + anchor aggregation.

derive_round_anchors builds a round-number anchor grid from the current price
+ step; compute_round_anchors aggregates bid qty/notional at each anchor. The
legacy hardcoded SOL anchor list is retained ONLY as an explicit DEPRECATED
fallback for callers that do not pass anchors.

Legacy sources: institutional_buyers.py (round anchors),
calculations/orderbook.py (derive_round_anchors).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

_ROUND_ANCHOR_TOL = 0.02


# Round-number anchors (legacy institutional_buyers.py:35). Institutional
# buyers tend to anchor bids at psychologically round prices; scanning those
# levels surfaces whether smart-money is concentrated around the marker.
#
# Phase 1.1: ``_ROUND_ANCHORS`` is DEPRECATED. The hardcoded SOL-specific
# levels (74.00..75.50) were never correct for any other instrument.
# New code MUST use ``derive_round_anchors`` to generate the anchor list
# from the current price + tick size. This tuple is kept as a fallback when
# the caller does not pass anchors and no price is available.
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


def derive_round_anchors(
    price: float,
    tick_size: float = 0.01,
    step: float | None = None,
    depth_below: int = 4,
    depth_above: int = 4,
    tol: float = 0.02,
) -> list[dict]:
    """Derive round-number anchor levels dynamically around ``price``.

    Replaces the legacy hardcoded ``_ROUND_ANCHORS`` list
    (``analysis/wall_migration.py``) with levels computed from the
    current price + step. Anchors are returned at ``k * step`` for
    ``k`` in ``[center - depth_below, center + depth_above]``, where
    ``center = round(price / step)``.

    The ``step`` is the spacing between adjacent round anchors. Common
    choices:
      SOL $150 with step=0.05  → 149.80, 149.85, ..., 150.20 (every 5¢)
      SOL $150 with step=0.50  → 149.00, 149.50, 150.00, 150.50, 151.00
      BTC $65000 with step=50  → 64850, 64900, 64950, 65000, 65050, 65100, 65150
      BTC $65000 with step=100 → 64700, 64800, ..., 65300 (every $100)

    When ``step`` is omitted it defaults to ``tick_size`` (every tick
    is a round anchor; usually too dense but always valid).

    Each entry carries ``{name, level, lo, hi, tolerance}``. ``lo``/``hi``
    are the half-open scan band used to attribute bids to the anchor;
    callers pass the list straight into ``compute_round_anchors``.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if step is None:
        step = tick_size
    if step <= 0:
        raise ValueError("step must be positive")
    if tol < 0:
        raise ValueError("tol must be non-negative")
    if depth_below < 0 or depth_above < 0:
        raise ValueError("depth_below / depth_below must be non-negative")

    # Round the price to the nearest step first so anchors align with
    # the visible round-number lattice; otherwise micro-priced instruments
    # drift away from the grid.
    center = round(price / step) * step

    anchors: list[dict] = []
    for offset in range(-depth_below, depth_above + 1):
        level = round(center + offset * step, 10)
        name = f"anchor_{level:.4f}".rstrip("0").rstrip(".")
        anchors.append({
            "name": name,
            "level": level,
            "lo": level - tol,
            "hi": level + tol,
            "tolerance": tol,
        })
    return anchors


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

    ``anchors`` is the dynamic anchor list produced by ``derive_round_anchors``.
    When omitted, the legacy hardcoded SOL list is used as a fallback
    (DEPRECATED — new callers must always pass anchors derived from the
    current price).
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