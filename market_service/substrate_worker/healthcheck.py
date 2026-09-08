"""Substrate worker healthcheck — deployment liveness gate.

Checks every enabled worker's supervisor heartbeat key exists in Redis
(the key the core's ``_tick`` refreshes every few seconds). A missing key
means that (substrate, symbol) pair has no live worker. Exit 0 when all
present, 1 otherwise. Wired as the compose ``substrate-workers``
healthcheck.

Usage:
    python -m market_service.substrate_worker.healthcheck
"""

from __future__ import annotations

import asyncio
import logging
import os

from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.substrate_worker.runner import _symbols, build_workers

log = logging.getLogger(__name__)


async def check_workers(store: RedisRuntimeStore, workers: list) -> list[str]:
    """Return ``name:symbol`` pairs whose supervisor key is absent."""
    missing: list[str] = []
    for w in workers:
        try:
            alive = await store.redis.get(w._supervisor_key)
        except Exception as exc:  # noqa: BLE001 — healthcheck must never crash
            log.warning("healthcheck read failed for %s: %s", w._supervisor_key, exc)
            alive = None
        if alive is None:
            missing.append(f"{w.SUBSTRATE_NAME}:{w.symbol}")
    return missing


async def run_check() -> int:
    store = RedisRuntimeStore(
        os.getenv("REDIS_URL") or "redis://localhost:6379/0",
        os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    )
    try:
        workers = build_workers(store, symbols=_symbols())
        missing = await check_workers(store, workers)
    finally:
        await store.close()
    if missing:
        print(f"SUBSTRATE UNHEALTHY — missing heartbeats: {sorted(missing)}")
        return 1
    print(f"SUBSTRATE HEALTHY — {len(workers)} worker heartbeats present")
    return 0


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))
    return asyncio.run(run_check())


if __name__ == "__main__":
    raise SystemExit(main())
