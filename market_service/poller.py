"""5-second raw-evidence firehose — Binance → Redis, no math, no analysis.

Replaces the ``data-access`` node. Runs a tight loop: fetch the eight
canonical Binance endpoints, normalise trades at the client boundary, and
append the raw evidence dict to a time-windowed Redis stream. The harness
reads from this stream on its own schedule (15m / 1h / 4h windows).

This is the ONLY place that touches external network APIs. The harness
never calls Binance directly — it reads from the Redis ledger.

Run::

    python -m market_service.poller
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from market_service.clients.binance import Binance, normalize_fut_trade, normalize_spot_trade
from market_service.config import Settings
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def fetch_binance_evidence(
    client: Binance,
    *,
    symbol: str,
    depth_levels: int,
    flow_window_seconds: int,
) -> dict[str, Any]:
    """Pull the eight canonical Binance endpoints in parallel.

    Same shape as the data-access node produced — the harness pipeline
    consumes this dict unchanged.
    """
    now_ms = _now_ms()
    start = now_ms - flow_window_seconds * 1000

    results = await asyncio.gather(
        _safe(client.spot_book(symbol, limit=depth_levels), "spot_book"),
        _safe(client.fut_book(symbol, limit=depth_levels), "fut_book"),
        _safe(client.spot_24h(symbol), "spot_24h"),
        _safe(client.fut_24h(symbol), "fut_24h"),
        _safe(client.fut_funding(symbol), "fut_funding"),
        _safe(client.fut_open_interest(symbol), "fut_open_interest"),
        _safe(client.spot_agg_trades(symbol, limit=1000, start_time=start), "spot_trades"),
        _safe(client.fut_agg_trades(symbol, limit=1000, start_time=start), "fut_trades"),
    )
    (spot_book, e_book), (fut_book, e_fbook), (spot_24h, e24), (fut_24h, ef24), \
    (fut_fund, efund), (fut_oi, eoi), (spot_raw, est), (fut_raw, eft) = results

    endpoint_names = (
        "spot_book", "fut_book", "spot_24h", "fut_24h",
        "fut_funding", "fut_open_interest", "spot_trades", "fut_trades",
    )
    endpoint_errs = (e_book, e_fbook, e24, ef24, efund, eoi, est, eft)
    errors = [
        {"endpoint": name, "error": err}
        for name, err in zip(endpoint_names, endpoint_errs) if err
    ]

    spot_trades = [
        {**normalize_spot_trade({"time": t["T"], "id": t["a"], "price": t["p"],
                                  "qty": t["q"], "isBuyerMaker": t["m"]}), "venue": "spot"}
        for t in (spot_raw or [])
    ]
    fut_trades = [
        normalize_fut_trade({"time": t["T"], "id": t["a"], "price": t["p"],
                              "qty": t["q"], "isBuyerMaker": t["m"]})
        for t in (fut_raw or [])
    ]

    return {
        "observed_at": _iso_now(),
        "observed_at_ms": now_ms,
        "fetch_window_ms": flow_window_seconds * 1000,
        "depth_levels": depth_levels,
        "errors": errors,
        "spot": {
            "ticker_24h": spot_24h,
            "order_book": spot_book,
            "trades_raw": spot_raw,
            "trades_normalized": spot_trades,
        },
        "futures": {
            "ticker_24h": fut_24h,
            "order_book": fut_book,
            "trades_raw": fut_raw,
            "trades_normalized": fut_trades,
            "funding": fut_fund,
            "open_interest": fut_oi,
        },
    }


async def _safe(coro, name: str) -> tuple[Any, str | None]:
    try:
        return await coro, None
    except Exception as exc:
        log.warning("poller endpoint %s failed: %s", name, exc)
        return None, f"{type(exc).__name__}: {exc}"


async def poll_symbol(
    client: Binance,
    redis: RedisRuntimeStore,
    settings: Settings,
    symbol: str,
) -> None:
    """Fetch evidence for one symbol and append to the raw stream."""
    evidence = await fetch_binance_evidence(
        client,
        symbol=symbol,
        depth_levels=settings.depth_levels,
        flow_window_seconds=settings.flow_window_seconds,
    )
    await redis.set_raw_latest(symbol, evidence)
    stream_id = await redis.append_raw_evidence(symbol, evidence)
    log.debug("poller %s: %d spot trades, %d fut trades, stream=%s",
              symbol,
              len(evidence["spot"]["trades_normalized"]),
              len(evidence["futures"]["trades_normalized"]),
              stream_id)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        if not await redis.ping_with_retry():
            log.error("redis ping failed; aborting poller")
            return 1
        async with Binance() as client:
            log.info("poller starting (symbols=%s poll=%ss)",
                     settings.symbols, settings.poll_seconds)
            while True:
                started = time.monotonic()
                for symbol in settings.symbols:
                    try:
                        await poll_symbol(client, redis, settings, symbol)
                    except Exception:
                        log.exception("poller cycle failed for %s", symbol)
                elapsed = time.monotonic() - started
                sleep_s = max(0.0, settings.poll_seconds - elapsed)
                await asyncio.sleep(sleep_s)
    finally:
        await redis.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))