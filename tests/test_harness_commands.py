"""Tests for the read routes on the outer harness CLI.

These tests cover the parser surface, route resolution, and the read handlers
(``--substrate-read``, ``--read``). They do NOT touch Binance or Redis — all
I/O is mocked so they can run in CI without external dependencies.

The harness's computation and egress flags (``--invoke``,
``--refresh-derivatives``, ``--live``) were removed when the calculation
container became the only process that constructs workers; their removal is
pinned in ``tests/test_harness_read_only.py``.
"""

from __future__ import annotations

import argparse
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from market_service.commands.harness import (
    _read_market,
    build_parser,
)
from market_service.runtime import read_paths


class HarnessParserTests(unittest.TestCase):
    """The parser exposes the read flags and accepts valid combos."""

    def test_default_parser_no_flags(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT"])
        self.assertEqual(args.symbol, "SOLUSDT")
        self.assertFalse(args.substrate_read)
        self.assertFalse(args.read)
        self.assertEqual(args.mode, "snapshot")

    def test_substrate_read_flag(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT", "--substrate-read", "--substrate", "tape"])
        self.assertTrue(args.substrate_read)
        self.assertEqual(args.substrate, "tape")

    def test_cycle_flags_are_removed(self):
        p = build_parser()
        for flag in ("--analyze", "--wall", "--flow", "--structure",
                     "--positioning", "--window", "--no-derivatives",
                     "--no-persist", "--envelope-summary"):
            with self.assertRaises(SystemExit, msg=flag):
                p.parse_args(["SOLUSDT", flag])

    def test_nooa_routing_is_removed(self):
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["--nooa", "market", "envelope", "SOLUSDT", "--latest"])

    def test_latest_flag_is_removed(self):
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["--latest"])

    def test_run_id_flag_stays_for_read_tool(self):
        p = build_parser()
        args = p.parse_args(["--run-id", "abc-123"])
        self.assertEqual(args.run_id, "abc-123")


class HarnessRouteDispatchTests(unittest.TestCase):
    """``main()`` routes flags to the right handler without running anything."""

    @patch("market_service.commands.harness._read_substrates",
           new_callable=AsyncMock)
    def test_default_routes_to_substrate_snapshot(self, mock_read):
        """Default (no flags) reads the warm-plane snapshot.

        The legacy `--live` waveform (build()) is EXCLUDED from the default,
        and the run_cycle pipeline is gone: bare invocation reads worker
        state, it never computes.
        """
        from market_service.commands.harness import main
        mock_read.return_value = {"symbol": "SOLUSDT", "substrates": {}}
        rc = main(["SOLUSDT", "--json"])
        self.assertEqual(rc, 0)
        mock_read.assert_awaited_once()

class HarnessInventoryProjectionTests(unittest.TestCase):
    """Compact inventory projection (shared read_paths surface)."""

    def test_projection_strips_raw_evidence(self):
        env = {
            "schema_version": 1, "symbol": "SOLUSDT", "status": "healthy",
            "generated_at": "2024-01-01T00:00:00Z", "completed_at": "2024-01-01T00:00:00Z",
            "coverage": {}, "run_id": "uuid",
            "canonical_state": {
                "analysis": {
                    "analysis": {
                        "auction": {"verdict": "X"},
                        "delta": {"delta": 0.5},
                        "demand": {"verdict": "Y"},
                    }
                }
            },
        }
        proj = read_paths.market_inventory(env)
        self.assertEqual(proj["symbol"], "SOLUSDT")
        self.assertEqual(proj["run_id"], "uuid")
        self.assertNotIn("canonical_state", proj)
        self.assertIn("analysis_keys", proj)
        self.assertEqual(sorted(proj["analysis_keys"]),
                         ["auction", "delta", "demand"])

class HarnessReadToolTests(unittest.TestCase):
    """The designated outer-CLI read tool (``--read``): parser + schema guard."""

    def test_parser_exposes_read_and_mode(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT", "--read", "--mode", "full"])
        self.assertTrue(args.read)
        self.assertEqual(args.mode, "full")
        self.assertFalse(args.read_errors)

    def test_read_defaults_to_snapshot(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT", "--read"])
        self.assertEqual(args.mode, "snapshot")
        self.assertFalse(args.read_errors)

    def test_read_rejects_bad_mode(self):
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["SOLUSDT", "--read", "--mode", "bogus"])


class ReadCollatedFallbackTests(unittest.IsolatedAsyncioTestCase):
    """``read_paths.read_collated_with_fallback``: redis-first, pg-fallback, guard."""

    _PAYLOAD = {
        "schema_version": 1,
        "symbol": "SOLUSDT",
        "run_id": "r-unique",
        "status": "healthy",
        "generated_at": "2026-09-02T00:00:00Z",
        "completed_at": "2026-09-02T00:00:00Z",
        "data_source": "binance",
        "canonical_state": {
            "data-access": {"evidence": {"futures": {}}},
            "calculations": {"calculations": {}},
            "analysis": {"analysis": {}},
        },
    }

    class FakeRedis:
        def __init__(self, latest: object = None, prefix: str = "marketflow"):
            self.prefix = prefix
            self._latest = latest
            self.redis = self._Client(self)

        class _Client:
            def __init__(self, outer: ReadCollatedFallbackTests.FakeRedis):
                self._outer = outer

            async def get(self, _key: str) -> object:
                return self._outer._latest

        def collated_latest_key(self, symbol: str) -> str:
            return f"{self.prefix}:latest:{symbol.upper()}:collated"

    class FakePg:
        def __init__(self, latest: object = None, by_run: object = None):
            self.latest = latest
            self.by_run = by_run

        async def latest_run(self, _symbol: str) -> object:
            return self.latest

        async def read_run(self, _run_id: str) -> object:
            return self.by_run

    async def test_redis_hit_tagged_redis(self):
        redis = self.FakeRedis('{"schema_version": 1, "run_id": "r-unique"}')
        payload, source = await read_paths.read_collated_with_fallback(
            redis, self.FakePg(), symbol="SOLUSDT")
        self.assertEqual(source, "redis")
        self.assertEqual(payload["run_id"], "r-unique")

    async def test_redis_miss_falls_back_to_postgres(self):
        redis = self.FakeRedis(latest=None)
        pg = self.FakePg(latest=dict(self._PAYLOAD))
        payload, source = await read_paths.read_collated_with_fallback(
            redis, pg, symbol="SOLUSDT")
        self.assertEqual(source, "postgres")
        self.assertEqual(payload["run_id"], "r-unique")

    async def test_both_miss_return_none(self):
        payload, source = await read_paths.read_collated_with_fallback(
            self.FakeRedis(latest=None), self.FakePg(), symbol="SOLUSDT")
        self.assertIsNone(payload)
        self.assertIsNone(source)

    async def test_postgres_absent_degrades_to_redis(self):
        payload, source = await read_paths.read_collated_with_fallback(
            self.FakeRedis(latest=None), None, symbol="SOLUSDT")
        self.assertIsNone(payload)
        self.assertIsNone(source)

    async def test_postgres_schema_mismatch_raises(self):
        redis = self.FakeRedis(latest=None)
        pg = self.FakePg(latest={"schema_version": 99})
        with self.assertRaises(ValueError):
            await read_paths.read_collated_with_fallback(redis, pg, symbol="SOLUSDT")

    async def test_run_id_addr_redis_first_then_postgres(self):
        redis = self.FakeRedis(latest=None)  # simulated run-id redis miss
        pg = self.FakePg(by_run=dict(self._PAYLOAD))
        payload, source = await read_paths.read_collated_with_fallback(
            redis, pg, run_id="r-unique")
        self.assertEqual(source, "postgres")
        self.assertEqual(payload["run_id"], "r-unique")


class HarnessReadMarketHandlerTests(unittest.IsolatedAsyncioTestCase):
    """``_read_market`` mode routing + source tagging + empty-read discipline."""

    def _args(self, **overrides):
        base = dict(symbol="SOLUSDT", run_id=None, read_errors=False)
        base.update(overrides)
        return argparse.Namespace(**base)

    async def test_snapshot_returns_projection_with_source(self):
        from tests._nooa_fixtures import _SAMPLE_ENVELOPE

        env = dict(_SAMPLE_ENVELOPE)
        with patch("market_service.runtime.read_paths"
                   ".read_collated_with_fallback") as m_read, \
             patch("market_service.commands.harness.RedisRuntimeStore") as m_redis, \
             patch("market_service.commands.harness.Settings") as m_settings:
            m_settings.from_redis_env.return_value.database_url = None
            m_redis.return_value.close = AsyncMock()
            async def _r(redis, postgres, *, symbol=None, run_id=None):
                self.assertIsNone(postgres)  # no DATABASE_URL -> pg not opened
                return env, "redis"
            m_read.side_effect = _r
            result = await _read_market(self._args(), "snapshot")
        self.assertEqual(result["source"], "redis")
        self.assertEqual(result["mode"], "snapshot")
        self.assertIn("read", result)
        self.assertNotIn("errors", result)

    async def test_empty_read_returns_null_source_and_errors(self):
        with patch("market_service.runtime.read_paths"
                   ".read_collated_with_fallback") as m_read, \
             patch("market_service.commands.harness.RedisRuntimeStore") as m_redis, \
             patch("market_service.commands.harness.Settings") as m_settings:
            m_settings.from_redis_env.return_value.database_url = None
            m_redis.return_value.close = AsyncMock()
            async def _r(redis, postgres, *, symbol=None, run_id=None):
                return None, None
            m_read.side_effect = _r
            result = await _read_market(self._args(), "snapshot")
        self.assertIsNone(result["source"])
        self.assertIsNone(result["read"])
        self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()