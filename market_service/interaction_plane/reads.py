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
    """Collated envelope ledger. Returns (payload, source, surfaces_or_None).

    surfaces is non-None only when the read is empty (redirect contract).
    """
    from market_service.runtime import read_paths

    settings = _stores.settings_redis()
    symbol = symbol.upper()
    redis = _stores.open_redis(settings)
    postgres = _stores.open_postgres(settings)
    try:
        if postgres is not None:
            await postgres.connect()
        payload, source = await read_paths.read_collated_with_fallback(
            redis, postgres, symbol=symbol, run_id=run_id)
    finally:
        await redis.close()
        if postgres is not None:
            await postgres.close()

    if payload is None:
        return None, None, None

    if mode == "full":
        read: Any = payload
    elif mode == "inventory":
        read = read_paths.market_inventory(payload)
    else:
        read = read_paths.market_snapshot(payload)

    out: dict[str, Any] = {
        "symbol": symbol,
        "run_id": payload.get("run_id"),
        "source": source,
        "mode": mode,
        "read": read,
    }
    if include_errors:
        out["errors"] = list(payload.get("errors") or [])
    return out, source, None


async def read_surfaces(symbol: str) -> dict[str, Any]:
    """Surface inventory: which planes hold data (empty-read redirect)."""
    from market_service.runtime import read_paths

    settings = _stores.settings_redis()
    redis = _stores.open_redis(settings)
    postgres = _stores.open_postgres(settings)
    try:
        if postgres is not None:
            await postgres.connect()
        return await read_paths.read_surface_inventory(
            redis, symbol.upper(), postgres=postgres)
    finally:
        await redis.close()
        if postgres is not None:
            await postgres.close()


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


async def horizons_presence(symbol: str) -> dict[str, Any]:
    """Which span horizons hold data for this symbol (bounded, cheap)."""
    from market_service.runtime.horizon_spans import SPAN_HORIZONS

    settings = _stores.settings_redis()
    store = _stores.open_redis(settings)
    try:
        present: list[str] = []
        for horizon in SPAN_HORIZONS:
            raw = await store.redis.get(
                store.horizon_span_latest_key(horizon, symbol.upper()))
            if raw:
                present.append(horizon)
        return {
            "symbol": symbol.upper(),
            "horizons": list(SPAN_HORIZONS),
            "present": present,
            "absent": [h for h in SPAN_HORIZONS if h not in present],
        }
    finally:
        await store.close()


async def read_horizon(
    symbol: str, horizon: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """One horizon span: Redis latest → Postgres fallback. (None, None) = absent."""
    from market_service.runtime.horizon_spans import is_supported

    symbol = symbol.upper()
    horizon = horizon.lower()
    if not is_supported(horizon):
        return None, None
    settings = _stores.settings_redis()
    store = _stores.open_redis(settings)
    try:
        span = await store.read_horizon_span_latest(horizon, symbol)
    finally:
        await store.close()
    if span is not None:
        return span, "redis"
    postgres = _stores.open_postgres(settings)
    if postgres is None:
        return None, None
    try:
        await postgres.connect()
        span = await postgres.latest_horizon_span(symbol, horizon)
        return (span, "postgres") if span is not None else (None, None)
    finally:
        await postgres.close()


__all__ = [
    "horizons_presence",
    "read_history",
    "read_horizon",
    "read_ledger",
    "read_micro_status",
    "read_surfaces",
    "read_warm",
]
