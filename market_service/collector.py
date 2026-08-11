from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import asyncpg

from market_service.clients.binance import Binance, normalize_fut_trade, normalize_spot_trade
from market_service.config import Settings
from market_service.calculations.flow import summarize
from market_service.calculations.signals import deterministic_signals

log = logging.getLogger(__name__)


def _window_flow(trades: list[dict[str, Any]], now_ms: int, window_seconds: int, book: dict[str, Any], levels: int) -> dict[str, Any]:
    cutoff = now_ms - window_seconds * 1000
    in_window = [t for t in trades if int(t["ts"]) >= cutoff]
    result = summarize(in_window, book, depth_levels=levels)
    timestamps = [int(t["ts"]) for t in in_window if int(t["ts"]) > 0]
    result["coverage_seconds"] = (max(timestamps) - min(timestamps)) // 1000 if len(timestamps) > 1 else 0
    return result


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
    spot_flow = _window_flow(spot_trades, now_ms, settings.flow_window_seconds, spot_book, settings.depth_levels)
    futures_flow = _window_flow(futures_trades, now_ms, settings.flow_window_seconds, futures_book, settings.depth_levels)
    snapshot = {
        "observed_at": datetime.now(timezone.utc), "symbol": symbol,
        "price": float(funding["mark_price"]), "spot_buy_share": spot_flow["buy_share"], "futures_buy_share": futures_flow["buy_share"],
        "spot_cvd_usd": spot_flow["cvd_usd"], "futures_cvd_usd": futures_flow["cvd_usd"],
        "spot_obi_top_n": spot_flow["obi"] or 0.0, "futures_obi_top_n": futures_flow["obi"] or 0.0,
        "funding_rate": float(funding["last_funding_rate"]), "mark_price": float(funding["mark_price"]), "index_price": float(funding["index_price"]),
        "open_interest": float(oi["open_interest"]), "flow_window_seconds": settings.flow_window_seconds,
        "spot_flow_coverage_seconds": spot_flow["coverage_seconds"], "futures_flow_coverage_seconds": futures_flow["coverage_seconds"],
        "source_payload": {
            "spot_trade_count": spot_flow["trade_count"],
            "futures_trade_count": futures_flow["trade_count"],
            "spot_order_book": spot_book,
            "futures_order_book": futures_book,
            "spot_trades": spot_raw,
            "futures_trades": futures_raw,
            "funding": funding,
            "open_interest": oi,
        },
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
