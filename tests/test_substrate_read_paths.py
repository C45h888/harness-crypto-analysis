"""Substrate read paths — Task 4 (latest + snapshot)."""

from __future__ import annotations

import time
import unittest
from typing import Any

from market_service.runtime import read_paths
from market_service.substrate_worker import WORKER_REGISTRY


class _DictStore:
    """Minimal store seam: per-substrate latest payloads."""

    def __init__(self, latests: dict[str, dict[str, Any]] | None = None):
        self.latests = latests or {}

    async def read_substrate_latest(self, substrate: str, symbol: str):
        return self.latests.get(substrate.lower())

    async def read(self, namespace, *, ids=None, count=None, newest_first=True, start="-", end="+"):
        if namespace.startswith("substrate."):
            sub = namespace.split(".", 1)[1].removesuffix(".events")
            if namespace.endswith(".events"):
                return []
            return self.latests.get(sub.lower())
        return None

    def ns(self, namespace):
        from tests._singular_compat import fake_ns
        return fake_ns(namespace)

    async def read(self, namespace, *, ids=None, count=None, newest_first=True, start="-", end="+"):
        from tests._singular_compat import fake_read
        # _DictStore speaks substrate latest only; bridge it.
        if namespace.startswith("substrate."):
            sub = namespace.split(".", 1)[1].removesuffix(".events")
            if namespace.endswith(".events"):
                return []
            return self.latests.get(sub.lower())
        return await fake_read(self, namespace, ids=ids, count=count, newest_first=newest_first, start=start, end=end)


class ReadPathsTests(unittest.TestCase):
    def test_latest_returns_payload_plus_age(self):
        payload = {"substrate": "tape", "symbol": "SOLUSDT",
                   "computed_at_ms": int(time.time() * 1000) - 500,
                   "output": {}}
        store = _DictStore({"tape": payload})
        import asyncio
        entry = asyncio.new_event_loop().run_until_complete(
            read_paths.read_substrate_latest(store, "tape", "solusdt"))
        self.assertEqual(entry["payload"], payload)
        self.assertGreaterEqual(entry["age_ms"], 0)
        self.assertLess(entry["age_ms"], 60_000)

    def test_latest_none_when_absent(self):
        import asyncio
        entry = asyncio.new_event_loop().run_until_complete(
            read_paths.read_substrate_latest(_DictStore(), "tape", "SOLUSDT"))
        self.assertIsNone(entry)

    def test_snapshot_covers_every_registry_name(self):
        import asyncio
        payload = {"substrate": "tape", "symbol": "SOLUSDT",
                   "computed_at_ms": int(time.time() * 1000), "output": {}}
        snap = asyncio.new_event_loop().run_until_complete(
            read_paths.read_substrate_snapshot(_DictStore({"tape": payload}), "SOLUSDT"))
        self.assertEqual(set(snap), set(WORKER_REGISTRY))
        self.assertEqual(snap["tape"]["payload"], payload)
        for name, entry in snap.items():
            if name != "tape":
                self.assertEqual(entry, {"available": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
