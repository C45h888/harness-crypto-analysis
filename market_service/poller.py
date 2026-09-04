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
import os
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from market_service.clients.binance import Binance, normalize_fut_trade, normalize_spot_trade
from market_service.config import Settings
from market_service.rate_limit import IpBanError, RateLimitError
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

# Rate errors are control-flow signals, NOT endpoint noise: they must
# propagate past _safe so the cycle loop can back off the whole poller.
_RATE_ERRORS = (RateLimitError, IpBanError)

# Backoff curve multiplier cap on top of POLL_SECONDS after rate errors.
_MAX_BACKOFF_MULTIPLIER = 5


def _ban_cooldown_s() -> float:
    """Safety margin slept AFTER a 418 ban expires (env: BINANCE_BAN_COOLDOWN_S)."""
    try:
        value = float(os.getenv("BINANCE_BAN_COOLDOWN_S", "30"))
    except (TypeError, ValueError):
        value = 30.0
    return max(0.0, value)


def _next_rate_backoff(current_s: float, poll_seconds: int) -> float:
    """Double the backoff on repeated rate errors, capped at 5x poll cadence."""
    floor = float(poll_seconds)
    if current_s <= 0.0:
        return floor
    return min(current_s * 2.0, floor * _MAX_BACKOFF_MULTIPLIER)


def _rate_backoff_from_errors(
    errors: Sequence[BaseException], poll_seconds: int, current_backoff_s: float,
) -> float:
    """Pick the next cycle backoff for a batch of gathered rate errors.

    IpBanError dominates: sleep the full ban remainder + cooldown margin.
    Otherwise escalate the doubling backoff by one step from the current.
    """
    bans = [e for e in errors if isinstance(e, IpBanError)]
    if bans:
        return max(e.ban_remaining_s for e in bans) + _ban_cooldown_s()
    return _next_rate_backoff(current_backoff_s, poll_seconds)


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
    """Pull the canonical Binance endpoints in parallel.

    Same shape as the data-access node produced — the harness pipeline
    consumes this dict unchanged.

    Eight spot/futures endpoints form the canonical minimum. Four
    derivative-stats endpoints (taker_buy_sell, top_long_short,
    global_long_short, open_interest_history) are now part of the
    canonical surface so analysis adapters can compute TBR series,
    top-trader drift, OI 1/3/6-bar deltas without re-fetching the
    live API.
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
        _safe(client.fut_taker_buy_sell(symbol, period="5m", limit=12), "fut_taker_buy_sell"),
        _safe(client.fut_top_long_short_accounts(symbol, period="5m", limit=12), "fut_top_long_short"),
        _safe(client.fut_long_short_ratio(symbol, period="5m", limit=12), "fut_global_long_short"),
        _safe(client.fut_open_interest_history(symbol, period="5m", limit=12), "fut_open_interest_history"),
    )
    (spot_book, e_book), (fut_book, e_fbook), (spot_24h, e24), (fut_24h, ef24), \
    (fut_fund, efund), (fut_oi, eoi), (spot_raw, est), (fut_raw, eft), \
    (fut_tbs, etbs), (fut_top, etop), (fut_glb, eglb), (fut_oih, eoih) = results

    endpoint_names = (
        "spot_book", "fut_book", "spot_24h", "fut_24h",
        "fut_funding", "fut_open_interest", "spot_trades", "fut_trades",
        "fut_taker_buy_sell", "fut_top_long_short", "fut_global_long_short",
        "fut_open_interest_history",
    )
    endpoint_errs = (
        e_book, e_fbook, e24, ef24, efund, eoi, est, eft,
        etbs, etop, eglb, eoih,
    )
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
            # Legacy-analysis surfaces — optional fallback for legacy
            # TBR / top-trader / OI-drift signals. Each is a list of bars
            # (newest last), or None on endpoint failure.
            "taker_buy_sell": fut_tbs,
            "top_ls": fut_top,
            "global_ls": fut_glb,
            "oi_history": fut_oih,
        },
    }


async def _safe(coro, name: str) -> tuple[Any, str | None]:
    """Per-endpoint guard. Swallows ordinary failures into the evidence
    ``errors`` list, but re-raises rate errors — those are control-flow
    signals the cycle loop must see to back off the whole poller."""
    try:
        return await coro, None
    except _RATE_ERRORS:
        raise
    except Exception as exc:
        # Use %r (repr) so the exception TYPE is always visible — for
        # asyncio.TimeoutError, str(exc) is '' and the log line would
        # otherwise be unreadable ("poller endpoint X failed: ").
        log.warning("poller endpoint %s failed: %r", name, exc)
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
    stream_id = await redis.publish_raw_evidence(symbol, evidence)
    if stream_id is None:
        log.debug("poller %s: duplicate snapshot skipped", symbol)
        return
    log.debug("poller %s: %d spot trades, %d fut trades, stream=%s",
              symbol,
              len(evidence["spot"]["trades_normalized"]),
              len(evidence["futures"]["trades_normalized"]),
              stream_id)


async def _safe_poll_symbol(
    client: Binance,
    redis: RedisRuntimeStore,
    settings: Settings,
    symbol: str,
) -> None:
    """Guarded wrapper so one symbol's failure never cancels the others.

    Rate errors are the exception: they re-raise so the cycle loop's gather
    collects them and backs off the whole poller (a single 429 on one
    endpoint means the IP is hot — hammering the other symbols makes it
    worse).
    """
    try:
        await poll_symbol(client, redis, settings, symbol)
    except _RATE_ERRORS:
        raise
    except Exception as exc:
        log.warning("poller cycle failed for %s: %s", symbol, exc)


async def resolve_active_symbols(
    redis: RedisRuntimeStore,
    settings: Settings,
) -> tuple[tuple[str, ...], str]:
    """Resolve the symbols to poll this cycle, with precedence:

    Redis control key  >  POLL_SYMBOLS env  >  SYMBOLS env

    Returns (symbols, source) where source is one of
    "redis_control", "poll_symbols_env", "symbols_env".
    """
    override = await redis.read_poller_symbols()
    if override:
        return tuple(override), "redis_control"
    if settings.poll_symbols != settings.symbols:
        return settings.poll_symbols, "poll_symbols_env"
    return settings.symbols, "symbols_env"


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_redis_env()
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        if not await redis.ping_with_retry():
            log.error("redis ping failed; aborting poller")
            return 1
        async with Binance() as client:
            active_symbols, source = await resolve_active_symbols(redis, settings)
            log.info("poller starting (symbols=%s poll=%ss source=%s)",
                     active_symbols, settings.poll_seconds, source)
            # Backoff ladder state: 0 = healthy; escalates on rate errors,
            # resets to 0 on a clean cycle.
            current_backoff_s = 0.0
            while True:
                started = time.monotonic()
                # Per-cycle symbol resolution: a Redis control key written
                # by the harness (--poller-symbols) takes effect within
                # one poll interval without restarting the container.
                cycle_symbols, cycle_source = await resolve_active_symbols(redis, settings)
                if cycle_symbols != active_symbols:
                    log.info("poller symbols changed: %s -> %s (source=%s)",
                             active_symbols, cycle_symbols, cycle_source)
                    active_symbols = cycle_symbols
                # All active symbols fetched concurrently — each symbol's
                # 12 endpoints already gather in parallel, so sequential
                # iteration multiplied cycle time by len(symbols).
                results = await asyncio.gather(
                    *[
                        _safe_poll_symbol(client, redis, settings, symbol)
                        for symbol in active_symbols
                    ],
                    return_exceptions=True,
                )
                # --- rate-error detection + cycle backoff -----------------
                # Rate errors re-raise through _safe / _safe_poll_symbol so
                # gather collects them here instead of them being swallowed
                # per endpoint. Any rate error means the IP is hot: back off
                # the WHOLE poller instead of firing the next cycle.
                rate_errors = [r for r in results if isinstance(r, _RATE_ERRORS)]
                other_failures = [
                    (symbol, res)
                    for symbol, res in zip(active_symbols, results)
                    if isinstance(res, Exception) and not isinstance(res, _RATE_ERRORS)
                ]
                for symbol, res in other_failures:
                    log.exception("poller cycle failed for %s", symbol, exc_info=res)
                elapsed = time.monotonic() - started
                if rate_errors:
                    backoff_s = _rate_backoff_from_errors(
                        rate_errors, settings.poll_seconds, current_backoff_s,
                    )
                    current_backoff_s = backoff_s
                    kind = "IP ban (418)" if any(
                        isinstance(e, IpBanError) for e in rate_errors) else "rate limit (429)"
                    log.warning(
                        "poller %s on %d/%d symbol task(s); backing off %.1fs "
                        "before the next cycle",
                        kind, len(rate_errors), len(active_symbols), backoff_s,
                    )
                    await redis.publish_poller_status({
                        "symbols": list(active_symbols),
                        "source": cycle_source,
                        "poll_seconds": settings.poll_seconds,
                        "last_cycle_at": _iso_now(),
                        "cycle_ms": int(elapsed * 1000),
                        "rate_limited": True,
                        "rate_backoff_s": backoff_s,
                    })
                    await asyncio.sleep(backoff_s)
                    continue
                # Clean cycle: reset the backoff ladder.
                current_backoff_s = 0.0
                if elapsed > settings.poll_seconds:
                    log.warning("poller overrun: cycle took %.2fs > poll=%ss",
                                elapsed, settings.poll_seconds)
                # Operator-visible status: confirms what the poller is
                # actually polling right now (readable via harness --poller-status).
                await redis.publish_poller_status({
                    "symbols": list(active_symbols),
                    "source": cycle_source,
                    "poll_seconds": settings.poll_seconds,
                    "last_cycle_at": _iso_now(),
                    "cycle_ms": int(elapsed * 1000),
                })
                sleep_s = max(0.0, settings.poll_seconds - elapsed)
                await asyncio.sleep(sleep_s)
    finally:
        await redis.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))