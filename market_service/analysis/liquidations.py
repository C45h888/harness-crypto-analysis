"""Liquidation-pressure proxy analysis using public Binance evidence."""

from __future__ import annotations

import asyncio
from typing import Any

from market_service.clients.binance import Binance, normalize_fut_trade


def _signal(oi_change: float, price_change: float) -> str:
    if oi_change < -0.3 and price_change < -0.3: return "LONG_LIQUIDATION_LIKELY"
    if oi_change < -0.3 and price_change > 0.3: return "SHORT_LIQUIDATION_LIKELY"
    if abs(oi_change) < 0.2 and price_change < -0.3: return "ORGANIC_SELLING"
    if abs(oi_change) < 0.2 and price_change > 0.3: return "ORGANIC_BUYING"
    if oi_change > 0.3 and price_change < -0.3: return "SHORT_ADDING_INTO_WEAKNESS"
    if oi_change > 0.3 and price_change > 0.3: return "LONG_ADDING_INTO_STRENGTH"
    return "MIXED_RANGE_BOUND"


def _oi_price(oi_hist: list[dict], klines: list[list]) -> dict:
    oi = sorted((int(x["timestamp"]), float(x["sum_open_interest"])) for x in oi_hist)
    prices = {int(k[0]) // 300000 * 300000: (float(k[1]), float(k[4])) for k in klines}
    oi_changes, price_changes, rows = [], [], []
    for previous, current in zip(oi, oi[1:]):
        oi_change = (current[1] - previous[1]) / previous[1] * 100 if previous[1] else 0.0
        open_price, close_price = prices.get(current[0], (None, None))
        price_change = ((close_price - open_price) / open_price * 100) if open_price else 0.0
        oi_changes.append(oi_change); price_changes.append(price_change)
        rows.append({"timestamp": current[0], "oi_change_pct": oi_change, "price_open": open_price, "price_close": close_price, "price_change_pct": price_change})
    oi_cum, price_cum = sum(oi_changes), sum(price_changes)
    return {"rows": rows, "cumulative_oi_change_pct": oi_cum, "cumulative_price_change_pct": price_cum, "signal": _signal(oi_cum, price_cum)}


def _clusters(trades: list[dict], cutoff_ms: int) -> list[dict]:
    recent = sorted((t for t in trades if t["ts"] >= cutoff_ms), key=lambda x: x["ts"])
    clusters, i = [], 0
    while i < len(recent):
        group, j = [recent[i]], i + 1
        while j < len(recent) and recent[j]["ts"] - recent[i]["ts"] <= 1500 and recent[j]["qty"] >= 30:
            group.append(recent[j]); j += 1
        if len(group) >= 3:
            qty = sum(t["qty"] for t in group)
            clusters.append({"ts": group[0]["ts"], "trade_count": len(group), "total_qty": qty,
                             "buy_qty": sum(t["qty"] for t in group if not t["is_buyer_maker"]),
                             "sell_qty": sum(t["qty"] for t in group if t["is_buyer_maker"]),
                             "average_price": sum(t["price"] * t["qty"] for t in group) / qty})
        i = max(j, i + 1)
    return sorted(clusters, key=lambda x: x["total_qty"], reverse=True)[:10]


async def analyze_liquidation_pressure(client: Binance, symbol: str, lookback_bars: int = 24, trade_limit: int = 1000) -> dict:
    oi, tbr, trades_raw, klines, funding = await asyncio.gather(
        client.fut_open_interest_history(symbol, period="5m", limit=lookback_bars),
        client.fut_taker_buy_sell(symbol, period="5m", limit=lookback_bars),
        client.fut_trades(symbol, limit=trade_limit),
        client.fut_klines(symbol, interval="1m", limit=lookback_bars * 10),
        client.fut_funding(symbol),
    )
    trades = [normalize_fut_trade(t) for t in trades_raw]
    oi_price = _oi_price(oi, klines)
    tbr_rows = [{"timestamp": int(x["timestamp"]), "buy_ratio": float(x["buy_vol"]) / max(float(x["buy_vol"]) + float(x["sell_vol"]), 0.001)} for x in tbr[-12:]]
    avg_tbr = sum(x["buy_ratio"] for x in tbr_rows) / len(tbr_rows) if tbr_rows else 0.5
    mark, index = float(funding["mark_price"]), float(funding["index_price"])
    funding_bps = float(funding["last_funding_rate"]) * 10000
    return {
        "limitations": ["This is a liquidation-pressure proxy; Binance public REST does not provide a complete liquidation tape."],
        "evidence": {"open_interest_history": oi, "taker_buy_sell": tbr, "trades": trades_raw, "klines_1m": klines, "funding": funding},
        "oi_price": oi_price, "taker_ratio": {"rows": tbr_rows, "average": avg_tbr},
        "funding": {"rate": float(funding["last_funding_rate"]), "bps": funding_bps, "mark_index_spread_bps": (mark - index) / index * 10000 if index else None},
        "large_trade_clusters": _clusters(trades, max((t["ts"] for t in trades), default=0) - 300000),
        "status": "proxy_only",
    }
