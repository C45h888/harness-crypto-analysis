"""Vector-1: WS surface streams bounded pages, never one bulk read."""

from __future__ import annotations

import unittest

from market_service.runtime.raw_window import build_ws_surface


def _delta(i: int, base_ts: int) -> dict:
    return {
        "exchange_ts_ms": base_ts + i * 100,
        "received_ts_ms": base_ts + i * 100 + 5,
        "bids": [[f"{100.0 - i * 0.01:.2f}", "10.0"]],
        "asks": [[f"{100.1 + i * 0.01:.2f}", "10.0"]],
    }


class _PagedWsStore:
    def __init__(self, deltas: list[dict]):
        self._deltas = deltas
        self.page_requests: list[int] = []

    async def read_stream_pages(self, namespace, *, ids, start="-", end="+", page=500):
        assert namespace == "micro.raw"
        self.page_requests.append(page)
        idx = [d for d in self._deltas]
        if start != "-":
            assert start.startswith("(")
            idx = idx[int(start[1:]) + 1:]
        pos = 0
        while pos < len(idx):
            chunk = idx[pos:pos + page]
            pos += page
            for d in chunk:
                yield d

    async def read(self, namespace, **kw):
        raise AssertionError(f"paged path must not call read({namespace})")


class _LegacyWsStore:
    def __init__(self, deltas: list[dict]):
        self._deltas = deltas

    async def read(self, namespace, *, ids=None, count=None, **kw):
        assert namespace == "micro.raw"
        return self._deltas if count is None else list(self._deltas[:count])


class WsSurfaceStreamingTests(unittest.TestCase):
    def test_paged_matches_legacy(self):
        import asyncio
        deltas = [_delta(i, 1_700_000_000_000) for i in range(1_200)]
        paged = asyncio.run(build_ws_surface(
            _PagedWsStore(deltas), "SOLUSDT", "futures", window_minutes=15))
        legacy = asyncio.run(build_ws_surface(
            _LegacyWsStore(deltas), "SOLUSDT", "futures", window_minutes=15))
        self.assertEqual(paged["delta_count"], legacy["delta_count"])
        self.assertEqual(paged["source"], legacy["source"])
        # age_ms anchors to the call instant — strip it before comparing.
        strip = lambda rows: [
            {k: v for k, v in r.items() if k != "age_ms"} for r in rows]
        self.assertEqual(strip(paged["deltas"]), strip(legacy["deltas"]))

    def test_pages_bounded_and_capped(self):
        import asyncio
        store = _PagedWsStore([_delta(i, 1_700_000_000_000) for i in range(3_000)])
        out = asyncio.run(build_ws_surface(
            store, "SOLUSDT", "futures", window_minutes=15))
        # 15m * 60 * 1000 // 100 = 9000 cap; 3000 deltas fit.
        self.assertEqual(out["delta_count"], 3_000)
        self.assertTrue(store.page_requests)
        self.assertTrue(all(p == 500 for p in store.page_requests))

    def test_cap_truncates_oldest_first(self):
        import asyncio
        # window cap smaller than the available deltas: keep oldest-first prefix.
        store = _PagedWsStore([_delta(i, 1_700_000_000_000) for i in range(100)])
        out = asyncio.run(build_ws_surface(
            store, "SOLUSDT", "futures", window_minutes=1))
        # 1m cap = 600; all 100 fit.
        self.assertEqual(out["delta_count"], 100)
        tiny = _PagedWsStore([_delta(i, 1_700_000_000_000) for i in range(10)])
        out2 = asyncio.run(build_ws_surface(
            tiny, "SOLUSDT", "futures", window_minutes=1))
        self.assertEqual(out2["delta_count"], 10)
