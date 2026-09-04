"""Reusable open-interest/proactiveness analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from market_service.clients.binance import Binance

TBR_BULL = 0.55
TBR_BEAR = 0.45
OI_FLAT_PCT = 0.05
PX_FLAT_PCT = 0.05


def _f(value: Any) -> float:
    return float(value)


def classify_bar(row: dict[str, float]) -> str:
    oi_up, oi_down = row["oi_delta_pct"] > OI_FLAT_PCT, row["oi_delta_pct"] < -OI_FLAT_PCT
    px_up, px_down = row["px_delta_pct"] > PX_FLAT_PCT, row["px_delta_pct"] < -PX_FLAT_PCT
    if oi_up and px_up and row["tbr"] >= TBR_BULL: return "AGGRESSIVE_LONG"
    if oi_up and px_up and row["tbr"] <= TBR_BEAR: return "DISTRIBUTION"
    if oi_up and px_down and row["tbr"] >= TBR_BULL: return "ABSORPTION"
    if oi_up and px_down and row["tbr"] <= TBR_BEAR: return "SHORT_BUILD"
    if oi_down and px_up: return "SHORT_COVER"
    if oi_down and px_down: return "LONG_UNWIND"
    if not oi_up and not oi_down and not px_up and not px_down: return "TWO_SIDED_FLAT"
    if oi_up and px_up: return "NEUTRAL_LONG_BUILD"
    if oi_up and px_down: return "NEUTRAL_SHORT_BUILD"
    return "NEUTRAL_MIXED"


PROACT_SCORE = {
    "AGGRESSIVE_LONG": 2, "ABSORPTION": 1, "DISTRIBUTION": -1,
    "SHORT_BUILD": -2, "SHORT_COVER": 0, "LONG_UNWIND": -1,
    "NEUTRAL_LONG_BUILD": 1, "NEUTRAL_SHORT_BUILD": -1,
    "TWO_SIDED_FLAT": 0, "NEUTRAL_MIXED": 0,
}


def _aligned_rows(oi_hist: list[dict], tbr_hist: list[dict], klines: list[list]) -> list[dict]:
    oi_map = {int(r["timestamp"]) // 300000 * 300000: r for r in oi_hist}
    tbr_map = {int(r["timestamp"]) // 300000 * 300000: r for r in tbr_hist}
    px_map = {int(k[0]) // 300000 * 300000: k for k in klines}
    rows, prev_oi, prev_close = [], None, None
    for bucket in sorted(set(oi_map) & set(tbr_map) & set(px_map)):
        oi, tbr, candle = oi_map[bucket], tbr_map[bucket], px_map[bucket]
        current_oi, close = _f(oi["sum_open_interest"]), _f(candle[4])
        buy, sell = _f(tbr["buy_vol"]), _f(tbr["sell_vol"])
        row = {
            "bucket": bucket, "oi": current_oi,
            "oi_value": _f(oi["sum_open_interest_value"]),
            "oi_delta_pct": ((current_oi - prev_oi) / prev_oi * 100) if prev_oi else 0.0,
            "px_open": _f(candle[1]), "px_close": close,
            "px_high": _f(candle[2]), "px_low": _f(candle[3]),
            "px_delta_pct": ((close - prev_close) / prev_close * 100) if prev_close else 0.0,
            "tbr": buy / (buy + sell) if buy + sell else 0.5,
            "buy_vol": buy, "sell_vol": sell,
        }
        row["net_vol"] = buy - sell
        row["class"] = classify_bar(row)
        row["proactiveness"] = PROACT_SCORE[row["class"]]
        rows.append(row)
        prev_oi, prev_close = current_oi, close
    return rows


def _verdict(rows: list[dict]) -> dict:
    score = sum(row["proactiveness"] for row in rows[-3:]) if rows else 0
    if score >= 7: label, confidence = "PROACTIVE", "HIGH"
    elif score >= 4: label, confidence = "WARMING", "MEDIUM"
    elif score >= 1: label, confidence = "PASSIVE", "MEDIUM"
    else: label, confidence = "ABSENT", "HIGH"
    return {"score": score, "label": label, "confidence": confidence, "window_bars": min(3, len(rows))}


async def analyze_open_interest(client: Binance, symbol: str, lookback_bars: int = 48) -> dict:
    """Fetch live OI inputs and return raw evidence plus deterministic output."""
    oi, tbr, klines = await __import__("asyncio").gather(
        client.fut_open_interest_history(symbol, period="5m", limit=lookback_bars),
        client.fut_taker_buy_sell(symbol, period="5m", limit=lookback_bars),
        client.fut_klines(symbol, interval="5m", limit=lookback_bars),
    )
    rows = _aligned_rows(oi, tbr, klines)
    return {
        "parameters": {"lookback_bars": lookback_bars, "period": "5m"},
        "evidence": {"open_interest_history": oi, "taker_buy_sell": tbr, "klines_5m": klines},
        "bars": rows,
        "verdict": _verdict(rows),
        "coverage": {"requested_bars": lookback_bars, "aligned_bars": len(rows)},
    }


# ----- Derived OI metrics (migrated from legacy oi_analysis.py / oi_analysis_run.py) -----


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


def wall_break_assessment(total_wall_sol: float, buy_per_min: float,
                          peak_buy_per_min: float, target_minutes: int = 15) -> dict:
    """Can buyers clear the seller walls via buy-flow alone?

    Compares current and peak buy rate (SOL/min) against the rate required to
    clear ``total_wall_sol`` in ``target_minutes``. Mirrors oi_analysis's
    wall-break math. Returns adequacy ratios, time-to-clear, and a verdict.
    """
    required = total_wall_sol / max(target_minutes, 1)
    safe_req = max(required, 1e-9)
    if buy_per_min >= required:
        verdict = "BUYERS_CAN_BREAK"
    elif peak_buy_per_min >= required:
        verdict = "FIREPOWER_AVAILABLE"
    else:
        verdict = "CANNOT_BREAK_VIA_BUY_FLOW"
    return {
        "total_wall_sol": total_wall_sol,
        "required_rate_sol_min": required,
        "current_buy_rate_sol_min": buy_per_min,
        "peak_buy_rate_sol_min": peak_buy_per_min,
        "current_adequacy_ratio": buy_per_min / safe_req,
        "peak_adequacy_ratio": peak_buy_per_min / safe_req,
        "time_to_clear_current_min": total_wall_sol / max(buy_per_min, 1e-9),
        "time_to_clear_peak_min": total_wall_sol / max(peak_buy_per_min, 1e-9),
        "verdict": verdict,
    }


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
