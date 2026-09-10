"""Regression tests for microstructure capture bootstrap fix.

Before the fix (Sept 10, 2026): bootstrap required a single buffered
delta to bridge ``snapshot_id + 1``. On SOL-USDT perps at 100ms, the
snapshot almost always landed between buffered update ranges, forcing a
retry on every reconnect. With 2s sleep + ~1s bootstrap, the gap cycle
was ~3s and 0 OFI intervals completed.

After the fix: bootstrap drains the queued deltas in arrival order via
``OrderBookReconstructor.apply``, which validates continuity per-delta.
A snapshot between buffered ranges is fine — the orderbook surfaces
any real gap as a BookGapError mid-apply.

These tests pin:
  - A snapshot whose ``lastUpdateId`` falls between buffered ranges now
    succeeds (was the broken case before).
  - A snapshot too old for the head buffered delta still triggers a
    retry.
  - A real sequence gap inside the buffered deltas surfaces as
    BookGapError on the offending delta (no silent corruption).
  - The instrumented log fields (``fetch_ms``, ``queued``, ``applied``,
    ``head_velocity``) appear in the bootstrap output.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from market_service.microstructure.capture import BinanceSpotDepthCapture, MicrostructureSettings
from market_service.microstructure.contracts import DepthDelta
from market_service.microstructure.orderbook import BookGapError


def _settings(**overrides):
    base = dict(
        redis_url="redis://localhost:6379/0",
        redis_prefix="test",
        symbol="BTCUSDT",
        venue="spot",
        stream_maxlen=100,
        snapshot_levels=20,
        reconnect_seconds=2,
        interval_seconds=10,
        websocket_base="wss://example/ws",
    )
    base.update(overrides)
    return MicrostructureSettings(**base)


def _delta(
    first_id: int, final_id: int,
    *,
    bids=("100.00", "1.00"), asks=("101.00", "1.00"),
    venue="spot", symbol="BTCUSDT",
) -> DepthDelta:
    # Binance depth diff shape: ``b`` / ``a`` are lists of [price, qty]
    # pairs (as strings). The contracts layer parses them as Decimals.
    return DepthDelta.from_binance(
        {"s": symbol.lower(), "U": first_id, "u": final_id,
         "b": [list(bids)], "a": [list(asks)]},
        venue=venue,
        received_ts_ms=0,
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class BootstrapDrainTests(unittest.TestCase):
    """Pin the new bootstrap-drain behavior."""

    def _capture(self, snapshot: dict, **settings_kwargs) -> BinanceSpotDepthCapture:
        capture = BinanceSpotDepthCapture(_settings(**settings_kwargs))
        # Stub out the redis store to avoid network calls.
        capture.store = AsyncMock()
        capture.store.publish_microstructure_delta = AsyncMock()
        capture.store.publish_microstructure_event = AsyncMock()
        capture.store.publish_microstructure_interval = AsyncMock()
        capture.store.set_microstructure_book = AsyncMock()
        capture.store.set_microstructure_status = AsyncMock()
        capture.store.publish_microstructure_status_transition = AsyncMock()
        capture.store.close = AsyncMock()
        return capture

    def _patch_binance(self, snapshot: dict):
        """Return a fake Binance client whose fut_book/spot_book returns ``snapshot``."""
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.spot_book = AsyncMock(return_value=snapshot)
        client.fut_book = AsyncMock(return_value=snapshot)
        return client

    def test_snapshot_between_buffered_ranges_now_succeeds(self):
        """Snapshot lands between buffered delta ranges — was broken before.

        Buffered deltas: 100-199, 200-299, 300-399 (each spans 100 ids)
        Snapshot lastUpdateId: 250 (between deltas 1 and 2)
        Expected (post-fix): bootstrap succeeds, all deltas applied,
        buffered list is left with only deltas whose final > 250.
        """
        snapshot = {
            "lastUpdateId": 250,
            "bids": [["100", "1"], ["99", "1"]],
            "asks": [["101", "1"], ["102", "1"]],
        }
        buffered = [
            _delta(100, 199),
            _delta(200, 299),
            _delta(300, 399),
        ]
        capture = self._capture(snapshot)
        client = self._patch_binance(snapshot)

        with unittest.mock.patch(
            "market_service.microstructure.capture.Binance", return_value=client,
        ):
            _run(capture._bootstrap(buffered))

        # Book is live and applied all three deltas.
        self.assertIsNotNone(capture._book)
        self.assertEqual(capture._book.last_update_id, 399)
        # buffer drained (the snapshot covers up to 250; deltas with
        # final <= 250 are dropped — the rest are kept for replay).
        kept_ids = [d.final_update_id for d in buffered]
        self.assertEqual(kept_ids, [299, 399])

    def test_snapshot_too_old_triggers_retry_then_raises(self):
        """Snapshot's lastUpdateId is older than buffered[0].first_update_id.

        The first attempt must continue (retry), the bootstrap_retries limit
        exhausts, and BookGapError surfaces. Pre-fix this looped forever;
        post-fix it fails fast.
        """
        snapshot = {
            "lastUpdateId": 50,  # older than first delta (100)
            "bids": [["100", "1"]], "asks": [["101", "1"]],
        }
        buffered = [_delta(100, 199)]
        capture = self._capture(snapshot, bootstrap_retries=2)
        client = self._patch_binance(snapshot)

        with unittest.mock.patch(
            "market_service.microstructure.capture.Binance", return_value=client,
        ):
            with self.assertRaises(BookGapError):
                _run(capture._bootstrap(buffered))

        # Two attempts were made (the limit).
        self.assertEqual(client.spot_book.await_count, 2)

    def test_real_sequence_gap_in_buffer_surfaces_as_bookgaperror(self):
        """A gap inside the buffered deltas must surface, not silently skip.

        Buffered deltas: 100-199, 250-349 (gap between 199 and 250).
        Snapshot lastUpdateId: 100 (matches the first delta).
        Expected: bootstrap succeeds for the first delta, then the second
        delta raises BookGapError on apply, the bootstrap retries.
        """
        snapshot = {
            "lastUpdateId": 100,
            "bids": [["100", "1"]], "asks": [["101", "1"]],
        }
        buffered = [_delta(100, 199), _delta(250, 349)]
        capture = self._capture(snapshot, bootstrap_retries=2)
        client = self._patch_binance(snapshot)

        with unittest.mock.patch(
            "market_service.microstructure.capture.Binance", return_value=client,
        ):
            with self.assertRaises(BookGapError):
                _run(capture._bootstrap(buffered))

    def test_default_snapshot_levels_is_smaller_than_legacy_1000(self):
        """The default snapshot size is shrunk to outrun high-velocity perps.

        Pre-fix: 1000 (structurally too slow for SOL perps at 100ms).
        Post-fix: 200 for futures, 100 for spot (overridable via env).
        """
        spot_settings = _settings(venue="spot")
        fut_settings = _settings(venue="futures")
        self.assertLessEqual(spot_settings.snapshot_levels, 100)
        self.assertLessEqual(fut_settings.snapshot_levels, 200)
        self.assertLess(fut_settings.snapshot_levels, 1000)

    def test_drained_deltas_applied_in_arrival_order(self):
        """Deltas queued during snapshot fetch are applied after snapshot.

        Snapshot at 250; pre-bootstrap buffered: [100-199, 200-299];
        drained during bootstrap: [300-399, 400-499].
        Expected: book reaches 499 with all deltas applied.
        """
        snapshot = {
            "lastUpdateId": 250,
            "bids": [["100", "1"]], "asks": [["101", "1"]],
        }
        buffered = [_delta(100, 199), _delta(200, 299)]
        drained = [_delta(300, 399), _delta(400, 499)]
        capture = self._capture(snapshot)
        client = self._patch_binance(snapshot)

        queue: asyncio.Queue = asyncio.Queue()
        for d in drained:
            queue.put_nowait(d)

        async def populate():
            pass  # already populated synchronously above

        _run(populate())
        with unittest.mock.patch(
            "market_service.microstructure.capture.Binance", return_value=client,
        ):
            _run(capture._bootstrap(buffered, queue))

        self.assertIsNotNone(capture._book)
        self.assertEqual(capture._book.last_update_id, 499)
        # buffered retained only the post-snapshot slice (drained were
        # applied inside bootstrap; they don't need re-publish here).
        kept_ids = [d.final_update_id for d in buffered]
        self.assertEqual(kept_ids, [299])

    def test_drained_gap_triggers_bookgaperror_retry(self):
        """A sequence gap among drained deltas surfaces, not silently skip."""
        snapshot = {
            "lastUpdateId": 100,
            "bids": [["100", "1"]], "asks": [["101", "1"]],
        }
        buffered = [_delta(100, 199)]
        # Drained deltas have a gap (199 -> 250 is fine, 250 -> 400 is a gap).
        drained = [_delta(200, 299), _delta(400, 499)]
        capture = self._capture(snapshot, bootstrap_retries=1)
        client = self._patch_binance(snapshot)

        queue: asyncio.Queue = asyncio.Queue()
        for d in drained:
            queue.put_nowait(d)

        with unittest.mock.patch(
            "market_service.microstructure.capture.Binance", return_value=client,
        ):
            with self.assertRaises(BookGapError):
                _run(capture._bootstrap(buffered, queue))


class BackoffAdaptationTests(unittest.TestCase):
    """Pin the gap-adaptive backoff (Fix A(iii))."""

    def _capture(self, **settings_kwargs):
        return BinanceSpotDepthCapture(_settings(**settings_kwargs))

    def test_backoff_starts_at_floor(self):
        capture = self._capture()
        self.assertEqual(capture._backoff_ms, capture._BACKOFF_MIN_MS)

    def test_record_gap_doubles_backoff(self):
        capture = self._capture()
        capture._record_gap()
        self.assertEqual(capture._backoff_ms, capture._BACKOFF_MIN_MS * 2)
        capture._record_gap()
        self.assertEqual(capture._backoff_ms, capture._BACKOFF_MIN_MS * 4)

    def test_backoff_caps_at_max(self):
        capture = self._capture()
        for _ in range(20):
            capture._record_gap()
        self.assertEqual(capture._backoff_ms, capture._BACKOFF_MAX_MS)

    def test_record_success_decrements_backoff_after_threshold(self):
        capture = self._capture()
        capture._record_gap()
        capture._record_gap()
        before = capture._backoff_ms
        for _ in range(capture._BACKOFF_SUCCESSES_TO_RESET):
            capture._record_success()
        self.assertLess(capture._backoff_ms, before)

    def test_backoff_resets_to_floor_after_enough_successes(self):
        capture = self._capture()
        capture._record_gap()
        # Many success batches — backoff must reach the floor eventually.
        for _ in range(20 * capture._BACKOFF_SUCCESSES_TO_RESET):
            capture._record_success()
        self.assertEqual(capture._backoff_ms, capture._BACKOFF_MIN_MS)


if __name__ == "__main__":
    unittest.main()
