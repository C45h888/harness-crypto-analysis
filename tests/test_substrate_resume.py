"""Kill-9 resume — worker restart convergence (Phase 3 proof gate a).

Integration: seeds the raw stream, runs a worker task, cancels it mid-loop
(the kill-9 analogue — no graceful stop, no state flush), restarts a fresh
worker instance on the same consumer group, and asserts the latest payload
converges to the same output. Restart convergence comes from two facts:
the consumer-group position survives (crash resume) and the probe reads
its own latest (probe state).

Requires a live Redis (REDIS_URL); skipped otherwise.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest

REDIS_URL = os.getenv("REDIS_URL")
requires_redis = unittest.skipUnless(
    REDIS_URL, "kill-9 resume needs a live Redis (REDIS_URL unset — skipping)")


def _payload(ts_ms: int) -> dict[str, Any]:
    book = {
        "bids": [[148.20, 900.0], [148.15, 500.0], [148.10, 400.0]],
        "asks": [[148.32, 600.0], [148.40, 800.0]],
    }
    return {
        "observed_at": "2026-09-08T00:00:00+00:00",
        "observed_at_ms": ts_ms,
        "depth_levels": 20,
        "errors": [],
        "spot": {"ticker_24h": None, "order_book": book,
                 "trades_raw": [], "trades_normalized": []},
        "futures": {"ticker_24h": None, "order_book": book,
                    "trades_raw": [],
                    "trades_normalized": [
                        {"id": i, "ts": ts_ms - 1000 + i * 10,
                         "price": 148.25, "qty": 2.0,
                         "is_buyer_maker": bool(i % 2)}
                        for i in range(10)
                    ],
                    "funding": {}, "open_interest": {}},
    }


from typing import Any


@requires_redis
class Kill9ResumeTests(unittest.TestCase):
    def test_restart_converges_to_same_output(self):
        from market_service.runtime.redis_store import RedisRuntimeStore
        from market_service.substrate_worker.density_worker import DensityWorker

        async def _scenario():
            store = RedisRuntimeStore(REDIS_URL, "resumetest", 10_000)
            symbol = "SOLUSDT"
            now_ms = int(time.time() * 1000)
            for i in range(3):
                await store.publish_raw_evidence(symbol, _payload(now_ms + i))
            first = DensityWorker(store, symbol=symbol, cooldown_s=0,
                                  staleness_s=3600)
            await first.start()
            task = asyncio.create_task(first.run_forever())
            await asyncio.sleep(2.0)
            task.cancel()  # kill-9: no stop(), no flush, mid-loop
            try:
                await task
            except asyncio.CancelledError:
                pass
            before = await store.read_substrate_latest("density", symbol)
            self.assertIsNotNone(before, "first run should have fired cold-start")

            second = DensityWorker(store, symbol=symbol, cooldown_s=0,
                                   staleness_s=3600)
            await second.start()
            task2 = asyncio.create_task(second.run_forever())
            await asyncio.sleep(2.0)
            task2.cancel()
            try:
                await task2
            except asyncio.CancelledError:
                pass
            after = await store.read_substrate_latest("density", symbol)
            self.assertIsNotNone(after)
            self.assertEqual(
                (after["output"]["fut_keystone"] or {}).get("keystone"),
                (before["output"]["fut_keystone"] or {}).get("keystone"),
                "restart must converge to the same keystone output",
            )
            await store.close()

        asyncio.new_event_loop().run_until_complete(_scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
