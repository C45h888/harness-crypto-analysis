"""Vector-1 regression: build_raw_window streams bounded pages, peak ~1x.

The old builder retained the full decoded range (rows + decoded list),
then the snapshots plus two raw trade lists plus two deduped lists plus
two ts re-scans — 3-4x window volume at peak. The rewritten builder folds
one page at a time and releases each snapshot before fetching the next.

Covers: paged-path output identical to the legacy contract (counts,
dedupe, first/last/span, snapshots_used), every page carries an explicit
COUNT, peak allocation under bound on a large synthetic window, and the
legacy read() fallback for seams without read_stream_pages.
"""

from __future__ import annotations

import time
import tracemalloc
import unittest

from market_service.runtime.raw_window import build_raw_window


def _trade(tid: int, ts: int) -> dict:
    return {"id": tid, "ts": ts, "price": 100.0 + tid * 0.01, "qty": 1.0}


def _snap(ts_ms: int, ids: range) -> dict:
    trades = [_trade(i, ts_ms + i) for i in ids]
    return {
        "observed_at_ms": ts_ms,
        "spot": {"trades_normalized": trades},
        "futures": {"trades_normalized": trades},
    }


def _latest() -> dict:
    return {
        "observed_at": "2026-01-01T00:00:00+00:00",
        "observed_at_ms": 9_999_999,
        "depth_levels": 20,
        "errors": [],
        "spot": {"ticker_24h": None, "order_book": {"bids": [], "asks": []},
                 "trades_raw": [], "trades_normalized": []},
        "futures": {"ticker_24h": None, "order_book": {"bids": [], "asks": []},
                    "trades_raw": [], "trades_normalized": [],
                    "funding": {}, "open_interest": {}},
    }


class _PagedStore:
    """Store seam with real COUNT-bounded paging over canned snapshots."""

    PAGE = 500

    def __init__(self, latest: dict, snaps: list[dict]):
        self._latest = latest
        self._snaps = snaps
        self.page_requests: list[int] = []

    async def read(self, namespace: str, **kwargs):
        if namespace == "raw.latest":
            return self._latest
        raise AssertionError(f"paged path must not call read({namespace})")

    async def read_raw_oldest_ms(self):
        return None

    async def read_stream_pages(self, namespace, *, ids, start="-", end="+", page=500):
        assert namespace == "raw.stream"
        self.page_requests.append(page)
        # First call carries the builder's since_ms timestamp (same contract
        # as read_stream's start); continuations carry exclusive "(idx".
        if start == "-":
            ids_all = list(range(len(self._snaps)))
        elif start.startswith("("):
            floor = int(start[1:])
            ids_all = [i for i in range(len(self._snaps)) if i > floor]
        else:
            since = int(start)
            ids_all = [i for i, s in enumerate(self._snaps)
                       if s.get("observed_at_ms", 0) >= since]
        pos = 0
        while pos < len(ids_all):
            chunk = ids_all[pos: pos + page]
            pos += page
            for i in chunk:
                yield self._snaps[i]


class _LegacyStore:
    """Seam without read_stream_pages — exercises the legacy read() path."""

    def __init__(self, latest: dict, snaps: list[dict]):
        self._latest = latest
        self._snaps = snaps

    async def read(self, namespace: str, **kwargs):
        if namespace == "raw.latest":
            return self._latest
        if namespace == "raw.stream":
            return self._snaps
        raise AssertionError(namespace)

    async def read_raw_oldest_ms(self):
        return None


def _window_snaps(n_snaps: int, per_snap: int, overlap: int) -> list[dict]:
    """Rolling snapshots: each repeats the last `overlap` trade ids."""
    snaps = []
    nxt = 0
    base_ts = int(time.time() * 1000) - n_snaps * 5_000
    for s in range(n_snaps):
        start = max(0, nxt - overlap)
        ids = range(start, start + per_snap)
        nxt = start + per_snap
        snaps.append(_snap(base_ts + s * 5_000, ids))
    return snaps


class StreamingWindowTests(unittest.TestCase):
    def test_paged_output_matches_contract(self):
        snaps = _window_snaps(12, 20, overlap=8)
        store = _PagedStore(_latest(), snaps)
        out = __import__("asyncio").run(build_raw_window(store, "SOLUSDT", 15))
        fut_cov = out["coverage"]["futures_trades"]
        # Unique ids across the rolling window.
        expect_unique = len({t["id"] for s in snaps for t in s["futures"]["trades_normalized"]})
        expect_raw = sum(len(s["futures"]["trades_normalized"]) for s in snaps)
        self.assertEqual(out["coverage"]["snapshots_used"], 12)
        self.assertEqual(fut_cov["trade_count"], expect_unique)
        self.assertEqual(fut_cov["raw_trade_count"], expect_raw)
        self.assertEqual(fut_cov["duplicates_removed"], expect_raw - expect_unique)
        self.assertEqual(fut_cov["first_trade_ms"],
                         min(t["ts"] for s in snaps for t in s["futures"]["trades_normalized"]))
        self.assertEqual(fut_cov["last_trade_ms"],
                         max(t["ts"] for s in snaps for t in s["futures"]["trades_normalized"]))
        self.assertEqual(len(out["futures"]["trades_normalized"]), expect_unique)
        # Pages were bounded and covered the whole range.
        self.assertTrue(store.page_requests)
        self.assertTrue(all(p == 500 for p in store.page_requests))

    def test_legacy_fallback_matches_paged(self):
        snaps = _window_snaps(12, 20, overlap=8)
        import asyncio
        paged = asyncio.run(build_raw_window(_PagedStore(_latest(), snaps), "SOLUSDT", 15))
        legacy = asyncio.run(build_raw_window(_LegacyStore(_latest(), snaps), "SOLUSDT", 15))
        # requested_since_ms / staleness anchor to the call instant — drop
        # clock-varying keys before comparing the materialized content.
        def _norm(out):
            cov = dict(out["coverage"])
            cov.pop("requested_since_ms", None)
            cov.pop("stream_staleness_ms", None)
            return cov, out["futures"]["trades_normalized"], out["spot"]["trades_normalized"]
        self.assertEqual(_norm(paged), _norm(legacy))

    def test_peak_stays_near_single_copy(self):
        snaps = _window_snaps(1_500, 40, overlap=16)
        store = _PagedStore(_latest(), snaps)
        import asyncio
        tracemalloc.start()
        try:
            asyncio.run(build_raw_window(store, "SOLUSDT", 240))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # ~120k trade dicts total raw; accumulator holds ~85k unique.
        # Old shape peaked at ~3-4x retained volume (>120MB here); the
        # streaming fold must stay near one retained copy (<64MB).
        self.assertLess(peak, 64 * 1024 * 1024, f"peak bytes={peak}")
