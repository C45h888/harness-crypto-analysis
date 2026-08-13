"""``data-access`` domain node.

Subscribes to ``<prefix>:stream:commands`` filtered on ``domain=data-access``.
On each command it fetches raw Binance evidence for the requested symbol,
normalizes at the client boundary, and publishes a ``MarketStateEnvelope``
with ``source="data-access"`` to:

  * ``<prefix>:latest:<SYM>:data-access``
  * ``<prefix>:stream:domain:data-access:<SYM>``

Per the containerization contract this node is the ONLY place that touches
external network APIs. Calculations and analysis must receive only Redis
payloads from this node.

Run::

    python -m market_service.nodes.data_access
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from market_service.clients.binance import Binance, normalize_fut_trade, normalize_spot_trade
from market_service.config import Settings
from market_service.runtime.contracts import RefreshCommand
from market_service.runtime.redis_store import RedisRuntimeStore

from ._base import (
    build_envelope,
    install_signal_handlers,
    run_command_loop,
    setup_logging,
)

log = logging.getLogger(__name__)

DOMAIN = "data-access"


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
    scope: str = "all",
) -> dict[str, Any]:
    """Pull the eight canonical Binance endpoints in parallel.

    Failures on individual endpoints are tracked (never silently dropped);
    they will become structured ``errors`` on the published envelope.
    """
    now_ms = _now_ms()
    start = now_ms - flow_window_seconds * 1000
    scopes = {
        "order_book": {"spot_book", "fut_book"},
        "trades": {"spot_trades", "fut_trades"},
        "funding": {"fut_funding"},
        "open_interest": {"fut_open_interest"},
        "tickers": {"spot_24h", "fut_24h"},
    }
    requested = set().union(*scopes.values()) if scope == "all" else scopes.get(scope, set())

    async def _safe(coro_factory, name: str):
        try:
            return await coro_factory(), None
        except Exception as exc:
            log.warning("data-access endpoint %s failed: %s", name, exc)
            return None, f"{type(exc).__name__}: {exc}"

    async def _requested(coro_factory, name: str):
        if name not in requested:
            return None, None
        return await _safe(coro_factory, name)

    results = await asyncio.gather(
        _requested(lambda: client.spot_book(symbol, limit=depth_levels), "spot_book"),
        _requested(lambda: client.fut_book(symbol, limit=depth_levels), "fut_book"),
        _requested(lambda: client.spot_24h(symbol), "spot_24h"),
        _requested(lambda: client.fut_24h(symbol), "fut_24h"),
        _requested(lambda: client.fut_funding(symbol), "fut_funding"),
        _requested(lambda: client.fut_open_interest(symbol), "fut_open_interest"),
        _requested(lambda: client.spot_agg_trades(symbol, limit=1000, start_time=start), "spot_trades"),
        _requested(lambda: client.fut_agg_trades(symbol, limit=1000, start_time=start), "fut_trades"),
    )
    (spot_book, e_book), (fut_book, e_fbook), (spot_24h, e24), (fut_24h, ef24), \
    (fut_fund, efund), (fut_oi, eoi), (spot_raw, est), (fut_raw, eft) = results
    endpoint_names = ("spot_book", "fut_book", "spot_24h", "fut_24h",
                      "fut_funding", "fut_open_interest", "spot_trades", "fut_trades")
    endpoint_errs = (e_book, e_fbook, e24, ef24, efund, eoi, est, eft)
    errors = [
        {"endpoint": name, "error": err}
        for name, err in zip(endpoint_names, endpoint_errs) if err
    ]
    # Preserve raw evidence; do NOT derive metrics here. Calculations live in
    # the ``calculations`` node per the contract.
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
        "fetch_window_ms": flow_window_seconds * 1000,
        "depth_levels": depth_levels,
        "requested_scope": scope,
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


async def make_handler(settings: Settings):
    """Build the per-command coroutine that the command loop will call."""
    client = Binance()

    async def _open() -> None:
        await client.__aenter__()

    async def _close() -> None:
        await client.close()

    await _open()

    async def handle(command: RefreshCommand) -> dict[str, Any]:
        symbol = command.symbol.upper()
        params = command.parameters or {}
        depth = int(params.get("depth_levels", settings.depth_levels))
        window = int(params.get("flow_window_seconds", settings.flow_window_seconds))
        scope = str(params.get("scope", "all"))
        evidence = await fetch_binance_evidence(
            client,
            symbol=symbol,
            depth_levels=depth,
            flow_window_seconds=window,
            scope=scope,
        )
        # Per contract: null means unavailable, never zero. Status reflects
        # whether at least the spot/futures books came back.
        required = {
            "all": (evidence["spot"]["order_book"], evidence["futures"]["order_book"]),
            "order_book": (evidence["spot"]["order_book"], evidence["futures"]["order_book"]),
            "trades": (evidence["spot"]["trades_raw"], evidence["futures"]["trades_raw"]),
            "funding": (evidence["futures"]["funding"],),
            "open_interest": (evidence["futures"]["open_interest"],),
            "tickers": (evidence["spot"]["ticker_24h"], evidence["futures"]["ticker_24h"]),
        }[scope]
        ok = all(value is not None for value in required)
        status = "healthy" if ok and not evidence["errors"] else "degraded" if ok else "invalid"
        return {
            "status": status,
            "errors": evidence.pop("errors", []),
            "evidence": evidence,
            "coverage_seconds": window,
            "requested_scope": scope,
        }

    handle.close = _close  # type: ignore[attr-defined]
    return handle


async def main() -> int:
    setup_logging()
    settings = Settings.from_env()
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    try:
        if not await redis.ping_with_retry():
            log.error("redis ping failed; aborting data-access")
            return 1
        handler = await make_handler(settings)
        await run_command_loop(
            redis=redis,
            domain=DOMAIN,
            handler=handler,
            settings=settings,
            stop=stop,
            source=DOMAIN,
        )
    finally:
        try:
            await handler.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        await redis.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
