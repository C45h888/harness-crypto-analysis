"""Positioning substrate — open-interest wall + contract composition math.

Owns the pure OI-derived primitives: seller-wall cluster search
(``find_walls``), OI-weighted long/short contract splits, windowed OI
inflow/outflow, and implied dollar-per-contract. Lifted verbatim from
``market_service.analysis.oi`` (move-don't-rewrite); the analysis module
keeps a re-export shim so historical import paths resolve to these SAME
objects.

Pure math; never touches Binance, Redis, or another substrate.
"""

from __future__ import annotations


def find_walls(asks: list[list[float]], price: float, lo_dist: float, hi_dist: float,
               window: float = 0.05, top_n: int = 5) -> list[dict]:
    """Densest rolling ask-cluster centres within ``[price+lo_dist, price+hi_dist]``.

    A seller wall is a concentrated ask cluster above price. Returns the top-N
    clusters as ``{"price", "qty"}`` sorted by cluster quantity descending.

    Legacy source: oi_analysis.find_walls.
    """
    lo, hi = price + lo_dist, price + hi_dist
    seen: set[float] = set()
    clusters: list[dict] = []
    for p, _ in asks:
        p = float(p)
        if not (lo <= p <= hi):
            continue
        c = round(p, 4)
        if c in seen:
            continue
        seen.add(c)
        qty = sum(q for ap, q in asks if p - window / 2 <= float(ap) <= p + window / 2)
        clusters.append({"price": p, "qty": qty})
    clusters.sort(key=lambda c: c["qty"], reverse=True)
    return clusters[:top_n]


def oi_implied_value(rows: list[dict], last_n: int = 8) -> dict:
    """Implied dollar-per-contract from OI notional / OI contracts.

    Rising $/contract while OI is flat = longs adding at higher prices;
    falling while OI rises = shorts/leverage adding. Returns the series plus
    the percentage change over the window.
    """
    bars = rows[-last_n:] if rows else []
    implied = []
    for r in bars:
        oi = r.get("oi")
        oi_val = r.get("oi_value")
        per = (oi_val / oi) if oi and oi_val is not None else None
        implied.append({"bucket": r.get("bucket"), "oi": oi,
                        "oi_value": oi_val, "implied_per_contract": per})
    change = None
    if len(implied) >= 2 and implied[0]["implied_per_contract"]:
        first = implied[0]["implied_per_contract"]
        last = implied[-1]["implied_per_contract"]
        if first:
            change = (last - first) / first * 100
    return {"bars": implied, "implied_per_contract_change_pct": change}


def oi_weighted_contracts(oi: float, top_long_pct: float | None, global_long_pct: float | None) -> dict:
    """Approximate long/short CONTRACT counts = OI * long% per cohort.

    Composition signal: how the open interest splits between top traders and
    the global crowd. Legacy source: oi_analysis_run's OI-weighted positioning.
    """
    out: dict = {"oi": oi}
    if top_long_pct is not None:
        out["top_long_contracts"] = oi * top_long_pct
        out["top_short_contracts"] = oi * (1 - top_long_pct)
    if global_long_pct is not None:
        out["global_long_contracts"] = oi * global_long_pct
        out["global_short_contracts"] = oi * (1 - global_long_pct)
    return out


def oi_inflow_outflow(oi_series: list[float], windows_bars: tuple[int, ...] = (12, 24, 48, 72, 144)) -> dict:
    """Windowed OI change (bars of 5min) — inflow (+) / outflow (-) per horizon."""
    out: dict = {}
    for n in windows_bars:
        if len(oi_series) >= n + 1:
            first, last = oi_series[-n - 1], oi_series[-1]
            change = last - first
            out[f"{n * 5 // 60}h"] = {
                "change": change,
                "change_pct": (change / first * 100) if first else None,
            }
    return out
