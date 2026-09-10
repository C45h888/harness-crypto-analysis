"""Calculation container healthcheck — worker supervisor-key probes.

Every substrate worker refreshes its supervisor key once per completed read
cycle (blocking-read return — the activity stamp, no timer). A missing key
means that (substrate, symbol) reader task is dead. Exit 0/1; the container
control plane's GET /health exposes the same over HTTP.
"""

from __future__ import annotations

import asyncio
import os
import sys

from market_service.runtime.redis_store import RedisRuntimeStore


async def _check() -> int:
    store = RedisRuntimeStore(
        os.getenv("REDIS_URL") or "redis://localhost:6379/0",
        os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    )
    try:
        from market_service.substrate_worker.healthcheck import check_workers
        from market_service.substrate_worker.runner import _symbols, build_workers

        workers = build_workers(store, symbols=_symbols())
        missing = await check_workers(store, workers)
        return 0 if not missing else 1
    finally:
        await store.close()


def main() -> int:
    return asyncio.run(_check())


if __name__ == "__main__":
    sys.exit(main())