"""NOOA container healthcheck — supervisor-key existence probe.

The wake supervisor refreshes its supervisor key every completed read cycle
(blocking-read return — the activity stamp, no timer). A missing key means
the wake reader task is dead. Exit 0/1, used as the container healthcheck.
"""

from __future__ import annotations

import asyncio
import os
import sys

from market_service.runtime.redis_store import RedisRuntimeStore


def main() -> int:
    store = RedisRuntimeStore(
        os.getenv("REDIS_URL") or "redis://localhost:6379/0",
        os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    )

    async def _check() -> int:
        try:
            key = store.inference_wake_supervisor_key(
                (os.getenv("MICROSTRUCTURE_SYMBOL") or "SOLUSDT").upper(),
                (os.getenv("MICROSTRUCTURE_VENUE") or "futures").lower(),
            )
            alive = await store.redis.get(key)
            return 0 if alive else 1
        finally:
            await store.close()

    return asyncio.run(_check())


if __name__ == "__main__":
    sys.exit(main())