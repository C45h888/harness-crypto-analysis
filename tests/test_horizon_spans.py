"""Horizon-span contract tests — keys, payload shape, fallback, refusal.

No live Redis/Postgres: the store seams are mocked at the
``interaction_plane.reads`` / ``redis`` / ``postgres`` boundaries.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from market_service.runtime import horizon_spans as hs


class SpanVocabularyTests(unittest.TestCase):
    def test_native_and_long_regimes(self):
        self.assertEqual(hs.regime("60s"), "native")
        self.assertEqual(hs.regime("1h"), "long_horizon")
        self.assertIsNone(hs.regime("5m"))

    def test_unsupported_horizon_is_refused_not_guessed(self):
        payload, error = hs.build_span("SOLUSDT", "5m", status="validated")
        self.assertIsNone(payload)
        self.assertIn("5m", str(error))

    def test_long_span_requires_bridge(self):
        payload, error = hs.build_span("SOLUSDT", "1h", status="validated")
        self.assertIsNone(payload)
        self.assertIn("bridge", str(error))

    def test_native_span_builds(self):
        payload, error = hs.build_span(
            "SOLUSDT", "60s", run_id="r1", status="validated",
            fit={"beta": "0.5"}, coverage={"n_observations": 120})
        self.assertIsNone(error)
        self.assertFalse(payload["extrapolated"])
        self.assertEqual(payload["regime"], "native")
        self.assertEqual(payload["schema_version"], hs.HORIZON_SPAN_SCHEMA_VERSION)

    def test_long_span_builds_with_bridge(self):
        payload, error = hs.build_span(
            "SOLUSDT", "1h", run_id="r1", status="provisional",
            bridge={"sigma_scaled": "1.2", "skill_decay": "0.7",
                    "assumptions": "sqrt-scaled"},
            coverage={"n_observations": 48})
        self.assertIsNone(error)
        self.assertTrue(payload["extrapolated"])

    def test_guard_rejects_stale_version(self):
        with self.assertRaises(ValueError):
            hs.guard_span({"schema_version": 999})
        self.assertIsNone(hs.guard_span(None))


class SpanReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_horizon_prefers_redis(self):
        from market_service.interaction_plane import reads as _reads

        span = {"horizon": "60s", "status": "validated"}
        store = AsyncMock()
        store.read_horizon_span_latest.return_value = span
        store.close = AsyncMock()
        with patch("market_service.interaction_plane.stores.open_redis",
                   return_value=store), \
             patch("market_service.interaction_plane.stores.settings_redis"):
            payload, source = await _reads.read_horizon("SOLUSDT", "60s")
        self.assertEqual(source, "redis")
        self.assertEqual(payload["horizon"], "60s")

    async def test_read_horizon_falls_back_to_postgres(self):
        from market_service.interaction_plane import reads as _reads

        span = {"horizon": "1h", "status": "provisional"}
        store = AsyncMock()
        store.read_horizon_span_latest.return_value = None
        store.close = AsyncMock()
        pg = AsyncMock()
        pg.connect = AsyncMock()
        pg.close = AsyncMock()
        pg.latest_horizon_span.return_value = span
        with patch("market_service.interaction_plane.stores.open_redis",
                   return_value=store), \
             patch("market_service.interaction_plane.stores.settings_redis"), \
             patch("market_service.interaction_plane.stores.open_postgres",
                   return_value=pg):
            payload, source = await _reads.read_horizon("SOLUSDT", "1h")
        self.assertEqual(source, "postgres")
        self.assertEqual(payload["horizon"], "1h")

    async def test_unsupported_horizon_returns_absent(self):
        from market_service.interaction_plane import reads as _reads

        payload, source = await _reads.read_horizon("SOLUSDT", "5m")
        self.assertIsNone(payload)
        self.assertIsNone(source)


class SpanCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_span_slice_wraps_envelope(self):
        from market_service.interaction_plane.cli import _read_horizon

        span = {"horizon": "60s", "status": "validated",
                "schema_version": hs.HORIZON_SPAN_SCHEMA_VERSION}
        with patch("market_service.interaction_plane.reads.read_horizon",
                   return_value=(span, "redis")):
            import argparse
            args = argparse.Namespace(symbol="SOLUSDT", span="60s", mode="snapshot")
            result, source = await _read_horizon(args)
        self.assertEqual(source, "redis")
        self.assertEqual(result["tool"], "market.read")
        self.assertEqual(result["data"]["horizon"], "60s")

    async def test_span_empty_names_present_horizons(self):
        from market_service.interaction_plane.cli import _read_horizon

        with patch("market_service.interaction_plane.reads.read_horizon",
                   return_value=(None, None)), \
             patch("market_service.interaction_plane.reads.horizons_presence",
                   return_value={"present": ["60s"], "absent": ["1h"]}), \
             patch("market_service.interaction_plane.reads.read_surfaces",
                   return_value={"surfaces": [], "available_surfaces": [],
                                 "read_tools": {}}):
            import argparse
            args = argparse.Namespace(symbol="SOLUSDT", span="1h", mode="snapshot")
            result, source = await _read_horizon(args)
        self.assertIsNone(source)
        self.assertEqual(result["status"], "empty")
        self.assertIn("60s", result["data"]["present_horizons"])

    def test_span_flag_exposed(self):
        from market_service.interaction_plane.cli import build_parser

        args = build_parser().parse_args(["SOLUSDT", "--read", "--span", "1h"])
        self.assertEqual(args.span, "1h")


if __name__ == "__main__":
    unittest.main()
