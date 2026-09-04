"""Ladders substrate — cumulative bid/ask stacks above/below a price.

absorption ladder (cumulative nearest bids beneath price), keystone bid stack
(below / at / above keystone decomposition), and the ask-wall ladder (per-
bucket ask depth above price with cumulative notional).

Boards are lists of ``(price, qty)`` pairs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def absorption_ladder(bids: Iterable[Sequence[float]], price: float, count: int = 15) -> list[dict]:
    """Cumulative nearest bids below price — how much passive bid is beneath."""
    near = sorted((float(p), float(q)) for p, q in bids if float(p) < price)
    cum = 0.0
    out: list[dict] = []
    for p, q in near[:count]:
        cum += q
        out.append({"price": p, "qty": q, "cum_qty": cum})
    return out


def keystone_bid_stack(bids: Iterable[Sequence[float]], keystone: float,
                       tol: float = 0.05) -> dict[str, Any]:
    """Cumulative bid stack around the keystone price.

    Returns per-level cumulative qty + notional inside the tight zone
    ``[keystone - tol, keystone + tol]``, plus the below / at / above
    keystone decomposition (floor area ``keystone - 6*tol`` up to the
    keystone, exact match at the keystone, and the next defence band above
    up to ``keystone + 3*tol``). Legacy source: deep_keystone.py:104-200.

    Boards are lists of ``(price, qty)`` pairs.
    """
    bids = [(float(p), float(q)) for p, q in bids]
    tight_lo, tight_hi = keystone - tol, keystone + tol
    floor_lo = keystone - 6 * tol
    above_hi = keystone + 3 * tol

    tight_levels = sorted((p, q) for p, q in bids if tight_lo <= p <= tight_hi)
    cum_qty = 0.0
    cum_notional = 0.0
    tight_rows: list[dict] = []
    for p, q in tight_levels:
        cum_qty += q
        cum_notional += p * q
        tight_rows.append({"price": p, "qty": q, "cum_qty": cum_qty,
                           "cum_notional": cum_notional})

    below = [{"price": p, "qty": q, "notional": p * q}
             for p, q in sorted(bids) if floor_lo <= p < keystone]
    k_round = round(keystone, 4)
    exact = [{"price": p, "qty": q, "notional": p * q}
             for p, q in sorted(bids) if round(p, 4) == k_round]
    above = [{"price": p, "qty": q, "notional": p * q}
             for p, q in sorted(bids) if keystone < p <= above_hi]

    return {
        "keystone": keystone,
        "tol": tol,
        "tight": {
            "lo": tight_lo, "hi": tight_hi,
            "levels": tight_rows,
            "total_qty": cum_qty,
            "total_notional": cum_notional,
        },
        "below": below,
        "at": exact,
        "above": above,
    }


def ask_wall_ladder(
    asks: Iterable[Sequence[float]],
    price: float,
    hi_limit: float | None = None,
    step: float = 0.05,
    max_buckets: int = 60,
) -> dict[str, Any]:
    """Ask-wall ladder: per-bucket ask depth above price with cumulative notional.

    Buckets of width ``step`` starting at ``price`` upward to ``hi_limit``
    (default: ``price + max_buckets * step``). Each bucket reports level
    count, qty, notional, and the running cumulative notional. Legacy
    source: wall_analysis.py:83-112.

    Boards are lists of ``(price, qty)`` pairs.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    asks = [(float(p), float(q)) for p, q in asks]
    if hi_limit is None:
        hi_limit = price + max_buckets * step

    zones: list[dict] = []
    cum_notional = 0.0
    total_qty = 0.0
    z = price
    while z < hi_limit + 1e-9:
        z_hi = z + step
        levels = [(p, q) for p, q in asks if z <= p < z_hi]
        qty = sum(q for _, q in levels)
        notional = sum(p * q for p, q in levels)
        cum_notional += notional
        total_qty += qty
        zones.append({"lo": z, "hi": z_hi, "n_levels": len(levels),
                      "qty": qty, "notional": notional,
                      "cum_notional": cum_notional})
        z += step

    return {
        "price": price,
        "hi_limit": hi_limit,
        "step": step,
        "zones": zones,
        "total_qty": total_qty,
        "total_notional": cum_notional,
    }