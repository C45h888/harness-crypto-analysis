"""Tests for the poller's periodic derivative cache warm-up task.

Background: delta / oi / technicals substrate workers all declare
``DERIVATIVE_INPUTS`` that the core reads from the Redis derivative
evidence cache. Before this warm-up existed, the cache was populated
only by an out-of-band ``harness --refresh-derivatives`` invocation that
nothing scheduled, leaving three of twelve workers permanently dormant.

The fix: a periodic task in ``market_service.poller.main`` that calls
``fetch_derivative_evidence`` + ``publish_derivative_evidence`` for every
active symbol on a slower cadence than the 5s evidence poll.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from market_service.poller import (
    _derivative_refresh_seconds,
    _derivative_warm_loop,
    _warm_one_symbol,
)
from market_service.config import Settings


def _settings() -> Settings:
    return Settings(
        database_url=None,
        redis_url="redis://localhost:6379/0",
        redis_key_prefix="marketflow",
        redis_stream_maxlen=1200,
        symbols=("SOLUSDT",),
        poll_symbols=("SOLUSDT",),
        poll_seconds=5,
        flow_window_seconds=300,
        depth_levels=500,
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class DerivativeWarmSettingsTests(unittest.TestCase):
    def test_default_refresh_is_60_seconds(self):
        with patch.dict(os.environ if False else __import__("os").environ, {}, clear=False):
            self.assertEqual(_derivative_refresh_seconds(), 60)

    def test_env_override(self):
        os_environ = __import__("os").environ
        old = os_environ.get("POLLER_DERIV_REFRESH_S")
        try:
            os_environ["POLLER_DERIV_REFRESH_S"] = "30"
            self.assertEqual(_derivative_refresh_seconds(), 30)
        finally:
            if old is None:
                os_environ.pop("POLLER_DERIV_REFRESH_S", None)
            else:
                os_environ["POLLER_DERIV_REFRESH_S"] = old

    def test_negative_or_zero_falls_back_to_default(self):
        os_environ = __import__("os").environ
        old = os_environ.get("POLLER_DERIV_REFRESH_S")
        try:
            os_environ["POLLER_DERIV_REFRESH_S"] = "0"
            self.assertEqual(_derivative_refresh_seconds(), 60)
        finally:
            if old is None:
                os_environ.pop("POLLER_DERIV_REFRESH_S", None)
            else:
                os_environ["POLLER_DERIV_REFRESH_S"] = old


class WarmOneSymbolTests(unittest.TestCase):
    """Per-symbol warm: fetch + publish, swallow errors."""

    def test_publishes_derivative_payload(self):
        os_environ = __import__("os").environ
        old_ttl = os_environ.get("POLLER_DERIV_TTL_S")
        os_environ["POLLER_DERIV_TTL_S"] = "300"
        try:
            client = MagicMock()
            store = MagicMock()
            store.publish_derivative_evidence = AsyncMock(return_value="1234-0")
            deriv = {"observed_at_ms": 1, "futures": {"klines": []}}

            async def fake_fetch(_client, _symbol, include_cross_asset=True):
                self.assertFalse(include_cross_asset, "default is no cross-asset")
                return deriv

            with patch(
                "market_service.nooa_harness.pipeline_interpretation.fetch_derivative_evidence",
                side_effect=fake_fetch,
            ):
                _run(_warm_one_symbol(client, store, "SOLUSDT", cross_asset=False))

            store.publish_derivative_evidence.assert_awaited_once()
            args, kwargs = store.publish_derivative_evidence.call_args
            self.assertEqual(args[0], "SOLUSDT")
            self.assertEqual(args[1], deriv)
            self.assertEqual(kwargs.get("ttl_s"), 300)
        finally:
            if old_ttl is None:
                os_environ.pop("POLLER_DERIV_TTL_S", None)
            else:
                os_environ["POLLER_DERIV_TTL_S"] = old_ttl

    def test_swallows_errors_and_does_not_raise(self):
        """A failed refresh is logged, not raised — workers stay dormant correctly."""
        client = MagicMock()
        store = MagicMock()
        store.publish_derivative_evidence = AsyncMock(side_effect=RuntimeError("redis down"))

        async def fake_fetch(_client, _symbol, include_cross_asset=True):
            raise RuntimeError("binance 503")

        with patch(
            "market_service.nooa_harness.pipeline_interpretation.fetch_derivative_evidence",
            side_effect=fake_fetch,
        ):
            # No exception should escape.
            _run(_warm_one_symbol(client, store, "SOLUSDT", cross_asset=False))


class WarmLoopTests(unittest.TestCase):
    """The background loop: refresh all symbols, sleep, repeat."""

    def test_loop_refreshes_each_symbol_then_sleeps(self):
        client = MagicMock()
        store = MagicMock()
        store.read_poller_symbols = AsyncMock(side_effect=lambda: ["SOLUSDT"])
        store.publish_derivative_evidence = AsyncMock(return_value="1234-0")

        async def fake_fetch(_client, _symbol, include_cross_asset=True):
            return {"observed_at_ms": 1, "futures": {}}

        sleep_calls = []
        async def fake_sleep(s):
            sleep_calls.append(s)
            # Exit after one cycle so the test terminates.
            raise asyncio.CancelledError()

        with patch(
            "market_service.nooa_harness.pipeline_interpretation.fetch_derivative_evidence",
            side_effect=fake_fetch,
        ), patch("market_service.poller.asyncio.sleep", side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                _run(_derivative_warm_loop(client, store, _settings(), ("SOLUSDT",)))

        store.publish_derivative_evidence.assert_awaited_once()
        # First sleep is the configured refresh interval.
        self.assertEqual(sleep_calls, [_derivative_refresh_seconds()])


if __name__ == "__main__":
    import os
    unittest.main()
