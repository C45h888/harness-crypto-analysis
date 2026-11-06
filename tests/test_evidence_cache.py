"""Vector-1/4: shared evidence cache — single-flight, TTL, bounded keys.

Also pins the backfill routing: concurrent horizon backfills for one
(symbol, minutes) coalesce on a single widened materialization.
"""

from __future__ import annotations

import asyncio
import unittest

from market_service.runtime.horizons import new_fold_state
from market_service.substrate_worker import evidence as evidence_module


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _CacheHygiene(unittest.TestCase):
    def setUp(self):
        evidence_module._EVIDENCE_CACHE.clear()
        evidence_module._INFLIGHT.clear()

    def tearDown(self):
        evidence_module._EVIDENCE_CACHE.clear()
        evidence_module._INFLIGHT.clear()


class SingleFlightTests(_CacheHygiene):
    def test_concurrent_ticks_share_one_build(self):
        calls = 0

        async def _build():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return {"n": calls}

        async def _main():
            return await asyncio.gather(*[
                evidence_module._shared(("S", 15, False), _build)
                for _ in range(6)
            ])

        out = _run(_main())
        self.assertEqual(calls, 1)
        self.assertTrue(all(o == {"n": 1} for o in out))

    def test_ttl_hit_skips_rebuild(self):
        calls = 0

        async def _build():
            nonlocal calls
            calls += 1
            return {"n": calls}

        async def _main():
            first = await evidence_module._shared(("S", 15, False), _build)
            second = await evidence_module._shared(("S", 15, False), _build)
            return first, second

        first, second = _run(_main())
        self.assertEqual(calls, 1)
        self.assertEqual(first, second)


class EvictionTests(_CacheHygiene):
    def test_cap_bounds_keys_and_sweeps_expired(self):
        async def _b():
            return {}

        async def _main():
            for i in range(80):
                await evidence_module._shared((f"S{i}", 15, False), _b)
            return len(evidence_module._EVIDENCE_CACHE)

        n = _run(_main())
        self.assertLessEqual(n, 64)

    def test_expired_entries_swept_on_write(self):
        import time

        async def _b():
            return {}

        async def _main():
            await evidence_module._shared(("OLD", 15, False), _b)
            # Age the entry past the TTL, then write a fresh key.
            ts, payload = evidence_module._EVIDENCE_CACHE[("OLD", 15, False)]
            evidence_module._EVIDENCE_CACHE[("OLD", 15, False)] = (
                ts - evidence_module._EVIDENCE_CACHE_TTL_S - 1.0, payload)
            await evidence_module._shared(("NEW", 15, False), _b)
            return dict(evidence_module._EVIDENCE_CACHE)

        cache = _run(_main())
        self.assertNotIn(("OLD", 15, False), cache)
        self.assertIn(("NEW", 15, False), cache)


class BackfillSharingTests(_CacheHygiene):
    def _bare_migration(self):
        from market_service.substrate_worker.migration_worker import MigrationWorker
        worker = MigrationWorker.__new__(MigrationWorker)
        worker.SUBSTRATE_NAME = "migration"
        worker.symbol = "SOLUSDT"
        worker.horizons = ("1h", "4h")
        worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
        worker._horizons_backfilled = False
        worker._horizons_rebuilt_from = None
        worker._plane = "substrate"
        worker.pg_store = None
        worker.store = object()
        worker._last_error = None
        return worker

    def test_concurrent_backfills_share_one_widened_build(self):
        from unittest.mock import patch
        calls = []

        async def _fake_build(redis, symbol, minutes):
            calls.append((symbol, minutes))
            await asyncio.sleep(0.05)
            return {"spot": {"trades_normalized": []},
                    "futures": {"trades_normalized": []}}

        async def _main():
            w1, w2 = self._bare_migration(), self._bare_migration()
            with patch.object(evidence_module, "build_raw_window", _fake_build):
                await asyncio.gather(w1._backfill_horizons(), w2._backfill_horizons())
            return w1, w2

        w1, w2 = _run(_main())
        # Both workers need the 240m window; single-flight serves both once.
        self.assertEqual(calls, [("SOLUSDT", 240)])
        self.assertTrue(w1._horizons_backfilled and w2._horizons_backfilled)
