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
