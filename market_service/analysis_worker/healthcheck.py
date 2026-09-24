"""Analysis container healthcheck — analysis supervisor-key probes.

Mirror of the calculation container's healthcheck: every analysis worker
refreshes its supervisor key once per completed read cycle. A missing key
means that (analysis, symbol) reader task is dead. Exit 0/1.
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
        from market_service.analysis_worker.runner import _symbols, build_workers

        workers = build_workers(store, symbols=_symbols())
        missing: list[str] = []
        for w in workers:
            try:
                alive = await store.redis.get(w._supervisor_key)
            except Exception:
                alive = None
            if alive is None:
                missing.append(f"{w.SUBSTRATE_NAME}:{w.symbol}")
        return 0 if not missing else 1
    finally:
        await store.close()


def main() -> int:
    return asyncio.run(_check())


if __name__ == "__main__":
    sys.exit(main())