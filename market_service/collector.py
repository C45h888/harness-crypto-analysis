from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import asyncpg

from binance import Binance, normalize_fut_trade, normalize_spot_trade
from market_service.config import Settings
from market_service.signals import deterministic_signals

log = logging.getLogger(__name__)


def _flow(trades: list[dict[str, Any]], now_ms: int, window_seconds: int) -> dict[str, float | int]:
    cutoff = now_ms - window_seconds * 1000
    in_window = [t for t in trades if int(t["ts"]) >= cutoff]
    buy_usd = sum(t["price"] * t["qty"] for t in in_window if not t["is_buyer_maker"])
    sell_usd = sum(t["price"] * t["qty"] for t in in_window if t["is_buyer_maker"])
    total = buy_usd + sell_usd
    timestamps = [int(t["ts"]) for t in in_window]
    coverage = (max(timestamps) - min(timestamps)) // 1000 if len(timestamps) > 1 else 0
    return {"buy_share": buy_usd / total if total else 0.5, "cvd_usd": buy_usd - sell_usd, "coverage_seconds": coverage, "trade_count": len(in_window)}


def _obi(book: dict[str, Any], levels: int) -> float:
    bids = book.get("bids", [])[:levels]
    asks = book.get("asks", [])[:levels]
    bid_notional = sum(float(p) * float(q) for p, q in bids)
    ask_notional = sum(float(p) * float(q) for p, q in asks)
    total = bid_notional + ask_notional
    return (bid_notional - ask_notional) / total if total else 0.0


async def _previous(pool: asyncpg.Pool, symbol: str) -> dict[str, float] | None:
    row = await pool.fetchrow(
        "SELECT open_interest, spot_buy_share, futures_buy_share, spot_obi_top_n FROM market_snapshot WHERE symbol = $1 ORDER BY observed_at DESC LIMIT 1",
        symbol,
    )
    return dict(row) if row else None


async def collect_symbol(client: Binance, pool: asyncpg.Pool, settings: Settings, symbol: str) -> None:
    now_ms = int(time.time() * 1000)
    spot_book, futures_book, spot_raw, futures_raw, funding, oi = await asyncio.gather(
        client.spot_book(symbol, settings.depth_levels),
        client.fut_book(symbol, settings.depth_levels),
        client.spot_agg_trades(symbol, limit=1000, start_time=now_ms - settings.flow_window_seconds * 1000),
        client.fut_agg_trades(symbol, limit=1000, start_time=now_ms - settings.flow_window_seconds * 1000),
        client.fut_funding(symbol),
        client.fut_open_interest(symbol),
    )
    spot_trades = [normalize_spot_trade({"time": t["T"], "id": t["a"], "price": t["p"], "qty": t["q"], "isBuyerMaker": t["m"]}) for t in spot_raw]
    futures_trades = [normalize_fut_trade({"time": t["T"], "id": t["a"], "price": t["p"], "qty": t["q"], "isBuyerMaker": t["m"]}) for t in futures_raw]
    spot_flow, futures_flow = _flow(spot_trades, now_ms, settings.flow_window_seconds), _flow(futures_trades, now_ms, settings.flow_window_seconds)
    snapshot = {
        "observed_at": datetime.now(timezone.utc), "symbol": symbol,
        "price": float(funding["mark_price"]), "spot_buy_share": spot_flow["buy_share"], "futures_buy_share": futures_flow["buy_share"],
        "spot_cvd_usd": spot_flow["cvd_usd"], "futures_cvd_usd": futures_flow["cvd_usd"],
        "spot_obi_top_n": _obi(spot_book, settings.depth_levels), "futures_obi_top_n": _obi(futures_book, settings.depth_levels),
        "funding_rate": float(funding["last_funding_rate"]), "mark_price": float(funding["mark_price"]), "index_price": float(funding["index_price"]),
        "open_interest": float(oi["open_interest"]), "flow_window_seconds": settings.flow_window_seconds,
        "spot_flow_coverage_seconds": spot_flow["coverage_seconds"], "futures_flow_coverage_seconds": futures_flow["coverage_seconds"],
        "source_payload": {"spot_trade_count": spot_flow["trade_count"], "futures_trade_count": futures_flow["trade_count"]},
    }
    previous = await _previous(pool, symbol)
    async with pool.acquire() as conn, conn.transaction():
        snapshot_id = await conn.fetchval(
            """INSERT INTO market_snapshot (observed_at, symbol, price, spot_buy_share, futures_buy_share, spot_cvd_usd, futures_cvd_usd, spot_obi_top_n, futures_obi_top_n, funding_rate, mark_price, index_price, open_interest, flow_window_seconds, spot_flow_coverage_seconds, futures_flow_coverage_seconds, source_payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) RETURNING id""",
            *[snapshot[k] if k != "source_payload" else json.dumps(snapshot[k]) for k in ("observed_at", "symbol", "price", "spot_buy_share", "futures_buy_share", "spot_cvd_usd", "futures_cvd_usd", "spot_obi_top_n", "futures_obi_top_n", "funding_rate", "mark_price", "index_price", "open_interest", "flow_window_seconds", "spot_flow_coverage_seconds", "futures_flow_coverage_seconds", "source_payload")],
        )
        for signal in deterministic_signals(snapshot, previous):
            await conn.execute("INSERT INTO signal_event (observed_at, symbol, signal_type, severity, summary, evidence, snapshot_id) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING", snapshot["observed_at"], symbol, signal["signal_type"], signal["severity"], signal["summary"], json.dumps(signal["evidence"]), snapshot_id)
    log.info("stored %s price=%.4f spot_buy=%.1f%% fut_buy=%.1f%%", symbol, snapshot["price"], 100 * snapshot["spot_buy_share"], 100 * snapshot["futures_buy_share"])


async def main() -> None:
    settings = Settings.from_env()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    try:
        async with Binance() as client:
            while True:
                started = time.monotonic()
                results = await asyncio.gather(*(collect_symbol(client, pool, settings, symbol) for symbol in settings.symbols), return_exceptions=True)
                for symbol, result in zip(settings.symbols, results):
                    if isinstance(result, Exception):
                        log.exception("collection failed for %s", symbol, exc_info=result)
                await asyncio.sleep(max(0, settings.poll_seconds - (time.monotonic() - started)))
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
