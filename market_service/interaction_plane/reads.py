"""Segment reads — async implementations moved verbatim out of harness.py.

Each function returns a plain dict; the CLI wraps it in the ToolResult
envelope (parsing.wrap_result) + stdout header (prompts.attach_header).
Read-only: no writes, no agents, no compute.
"""

from __future__ import annotations

from typing import Any

from market_service.interaction_plane import stores as _stores


async def read_warm(
    symbol: str, *, substrates: list[str] | None = None, mode: str = "compact",
) -> tuple[dict[str, Any], str | None]:
    """Warm-plane worker projections. Returns (payload, source=redis)."""
    from market_service.substrate_worker import tools as substrate_tools

    settings = _stores.settings_redis()
    store = _stores.open_redis(settings)
    try:
        payload = await substrate_tools.read_state(
            store, symbol.upper(), substrates=substrates, mode=mode)
        return payload, "redis"
    finally:
        await store.close()


async def read_ledger(
    symbol: str, *, run_id: str | None = None, mode: str = "snapshot",
    include_errors: bool = False,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Live market read. Returns (payload, source, surfaces_or_None).

    The collated envelope ledger had no writer, so this now shares the one
    implementation with the agent's ``market.read`` tool: live
    venue-scoped Redis projections (microstructure + derivatives +
    substrate), each with ``age_ms``. ``run_id`` is not addressable here —
    run records live under the canonical output plane.
    """
    from market_service.nooa_harness.inference.tooling.tools_market import (
        dispatch_market_read,
    )

    settings = _stores.settings_redis()
    symbol = symbol.upper()
    venue = _stores.venue()
    redis = _stores.open_redis(settings)
    try:
        payload, _log = await dispatch_market_read(
            redis, symbol, mode=mode, venue=venue, run_id=run_id)
    finally:
        await redis.close()
    if payload is None:
        return None, None, None
    return payload, payload.get("source"), None


async def read_surfaces(symbol: str) -> dict[str, Any]:
    """Surface inventory: which LIVE planes hold data (empty-read redirect)."""
    from market_service.runtime import read_paths

    settings = _stores.settings_redis()
    redis = _stores.open_redis(settings)
    try:
        return await read_paths.read_surface_inventory(redis, symbol.upper())
    finally:
        await redis.close()


async def read_history(
    symbol: str, *, limit: int = 100,
) -> tuple[dict[str, Any], str | None]:
    """Keystone ledger + migration verdict (pure read-side derivation)."""
    from market_service.calculations.orderbook import keystone_cycle_migration

    settings = _stores.settings_redis()
    symbol = symbol.upper()
    limit = max(1, int(limit or 100))
    store = _stores.open_redis(settings)
    history: list[dict[str, Any]] = []
    source: str | None = "redis"
    try:
        history = await store.read_keystone_history(symbol, count=limit)
    finally:
        await store.close()

    if not history and settings.database_url:
        pg = _stores.open_postgres(settings)
        try:
            await pg.connect()
            history = await pg.read_keystone_history(symbol, limit=limit)
            source = "postgres"
        finally:
            await pg.close()

    migration = keystone_cycle_migration(history) if history else {
        "cycles": [], "verdict": None, "net_buckets": 0,
    }
    return {
        "symbol": symbol,
        "source": source if history else None,
        "history_count": len(history),
        "history": history,
        "cycles": migration.get("cycles"),
        "verdict": migration.get("verdict"),
        "net_buckets": migration.get("net_buckets"),
    }, (source if history else None)


async def read_micro_status(symbol: str) -> tuple[dict[str, Any], str | None]:
    """Isolated capture health projection, Redis-only, runtime venue."""
    settings = _stores.settings_redis()
    symbol = symbol.upper()
    venue = _stores.venue()
    store = _stores.open_redis(settings)
    try:
        status = await store.read_microstructure_status(venue, symbol)
        return {
            "symbol": symbol,
            "venue": venue,
            "status_key": store.microstructure_status_key(venue, symbol),
            "raw_stream": store.microstructure_raw_stream(venue, symbol),
            "event_stream": store.microstructure_event_stream(venue, symbol),
            "ofi_stream": store.microstructure_ofi_stream(venue, symbol),
            "status": status,
        }, "redis"
    finally:
        await store.close()


__all__ = [
    "read_history",
    "read_ledger",
    "read_micro_status",
    "read_surfaces",
    "read_warm",
]
