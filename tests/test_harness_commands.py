"""Tests for the new calculation routes on the outer harness CLI.

These tests cover the parser surface, route resolution, and the
calculation-pipeline handlers (``--analyze``, ``--refresh-derivatives``).
They do NOT touch Binance or Redis \u2014 all I/O is mocked so they can run
in CI without external dependencies.
"""

from __future__ import annotations

import argparse
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from market_service.commands.harness import (
    _projection,
    _refresh_derivatives,
    _run_analyze,
    _summarize_derivatives_cache,
    build_parser,
)


class HarnessParserTests(unittest.TestCase):
    """The parser exposes the calculation flags and accepts valid combos."""

    def test_default_parser_no_flags(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT"])
        self.assertEqual(args.symbol, "SOLUSDT")
        self.assertFalse(args.analyze)
        self.assertFalse(args.refresh_derivatives)
        self.assertFalse(args.with_cross_asset)
        self.assertFalse(args.no_derivatives)
        self.assertFalse(args.force_refresh_derivatives)
        self.assertFalse(args.envelope_summary)
        self.assertEqual(args.window, "15m")
        self.assertEqual(args.deriv_ttl, 300)

    def test_analyze_flag(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT", "--analyze", "--window", "1h", "--json"])
        self.assertTrue(args.analyze)
        self.assertEqual(args.window, "1h")
        self.assertTrue(args.json)

    def test_refresh_derivatives_flag(self):
        p = build_parser()
        args = p.parse_args(["BTCUSDT", "--refresh-derivatives",
                              "--with-cross-asset", "--deriv-ttl", "60"])
        self.assertTrue(args.refresh_derivatives)
        self.assertTrue(args.with_cross_asset)
        self.assertEqual(args.deriv_ttl, 60)
        self.assertEqual(args.symbol, "BTCUSDT")

    def test_analyze_and_refresh_are_mutually_exclusive(self):
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["SOLUSDT", "--analyze", "--refresh-derivatives"])

    def test_nooa_routing_passes_through_remainder(self):
        p = build_parser()
        args = p.parse_args(["--nooa", "market", "envelope", "SOLUSDT", "--latest"])
        self.assertEqual(args.nooa, ["market", "envelope", "SOLUSDT", "--latest"])

    def test_latest_and_run_id_flags(self):
        p = build_parser()
        args_a = p.parse_args(["--latest"])
        self.assertTrue(args_a.latest)
        args_b = p.parse_args(["--run-id", "abc-123"])
        self.assertEqual(args_b.run_id, "abc-123")


class HarnessRouteDispatchTests(unittest.TestCase):
    """``main()`` routes flags to the right handler without running anything."""

    @patch("market_service.commands.harness._refresh_derivatives",
           new_callable=AsyncMock)
    def test_refresh_derivatives_routes_to_handler(self, mock_refresh):
        from market_service.commands.harness import main
        mock_refresh.return_value = {
            "status": "ok", "symbol": "SOLUSDT",
            "stream_id": "1-0", "cache_ttl_seconds": 300,
            "with_cross_asset": False,
        }
        rc = main(["SOLUSDT", "--refresh-derivatives"])
        self.assertEqual(rc, 0)
        mock_refresh.assert_awaited_once()
        # The handler was called with the parsed args (not the raw argv)
        call_args = mock_refresh.await_args.args
        self.assertEqual(call_args[0].symbol, "SOLUSDT")

    @patch("market_service.commands.harness._run_analyze",
           new_callable=AsyncMock)
    def test_analyze_routes_to_handler(self, mock_analyze):
        from market_service.commands.harness import main
        mock_analyze.return_value = {
            "status": "healthy", "symbol": "SOLUSDT",
            "run_id": "uuid-1", "window_minutes": 15,
            "elapsed_ms": 100.0, "persistence": {"mode": "persisted"},
            "derivatives_cache": {"include_derivatives": True},
        }
        rc = main(["SOLUSDT", "--analyze"])
        self.assertEqual(rc, 0)
        mock_analyze.assert_awaited_once()

    @patch("market_service.commands.harness._run_analyze",
           new_callable=AsyncMock)
    def test_default_routes_to_canonical_analyze(self, mock_analyze):
        """Default (no flags) runs the canonical calculation pipeline.

        The legacy `--live` waveform (build()) is EXCLUDED from the default:
        the coherent E2E is poller → Redis stream → deterministic calcs →
        MarketRunEnvelope v2, so the default must run the pipeline, not a
        live Binance fetch.
        """
        from market_service.commands.harness import main
        mock_analyze.return_value = {
            "status": "healthy", "symbol": "SOLUSDT",
            "run_id": "uuid-1", "window_minutes": 15,
            "elapsed_ms": 100.0, "persistence": {"mode": "persisted"},
            "derivatives_cache": {"include_derivatives": True},
        }
        rc = main(["SOLUSDT", "--json"])
        self.assertEqual(rc, 0)
        mock_analyze.assert_awaited_once()

    @patch("market_service.commands.harness._run_analyze",
           new_callable=AsyncMock)
    def test_analyze_flag_routes_to_handler(self, mock_analyze):
        """Explicit --analyze maps to the same canonical handler."""
        from market_service.commands.harness import main
        mock_analyze.return_value = {
            "status": "healthy", "symbol": "SOLUSDT",
            "run_id": "uuid-1", "window_minutes": 15,
            "elapsed_ms": 100.0, "persistence": {"mode": "persisted"},
            "derivatives_cache": {"include_derivatives": True},
        }
        rc = main(["SOLUSDT", "--analyze"])
        self.assertEqual(rc, 0)
        mock_analyze.assert_awaited_once()

    @patch("market_service.commands.harness.build",
           new_callable=AsyncMock)
    def test_live_flag_routes_to_legacy_build(self, mock_build):
        """Explicit --live is the ONLY path to the legacy live waveform."""
        from market_service.commands.harness import main
        mock_build.return_value = {
            "contract": {"name": "x"}, "symbol": "SOLUSDT",
            "status": "healthy", "errors": [],
            "latency_ms": 50.0,
            "core": {"status": "healthy"},
        }
        rc = main(["SOLUSDT", "--live", "--json"])
        self.assertEqual(rc, 0)
        mock_build.assert_awaited_once()

    def test_default_does_not_touch_legacy_build(self):
        """Without --live, build() is never reached (canonical only)."""
        from market_service.commands.harness import build_parser
        args = build_parser().parse_args(["SOLUSDT"])
        self.assertFalse(args.live)
        self.assertTrue(args.analyze is False)
        # The absence of any read/pipeline flag is the default-pipeline signal
        self.assertFalse(args.latest)
        self.assertIsNone(args.run_id)


class HarnessRefreshDerivativesTests(unittest.IsolatedAsyncioTestCase):
    """The refresh handler opens Binance + Redis once, with the right TTL."""

    def setUp(self):
        # Settings.from_env requires DATABASE_URL/REDIS_URL — inject them so
        # the handler can build a RedisRuntimeStore without a live connection.
        import os
        os.environ.setdefault("DATABASE_URL",
                              "postgresql://x:y@localhost/z")
        os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

    @patch("market_service.commands.harness.RedisRuntimeStore")
    @patch("market_service.nooa_harness.pipeline.fetch_derivative_evidence",
           new_callable=AsyncMock)
    @patch("market_service.clients.binance.Binance")
    async def test_refresh_writes_with_correct_ttl(
        self, mock_binance_cls, mock_fetch, mock_store_cls,
    ):
        mock_fetch.return_value = {
            "observed_at_ms": 1234567890000,
            "futures": {"oi_history": [{"sumOpenInterest": 100}],
                         "taker_buy_sell": [], "top_ls": [],
                         "global_ls": [], "klines": [], "funding": None},
            "cross_asset": {"tickers_24h": [], "funding": []},
        }
        mock_store = MagicMock()
        mock_store.publish_derivative_evidence = AsyncMock(return_value="1-0")
        mock_store.derivative_cache_ttl = AsyncMock(return_value=300)
        mock_store.derivatives_key = MagicMock(return_value="marketflow:latest:SOLUSDT:derivatives")
        mock_store.close = AsyncMock(return_value=None)
        mock_store_cls.return_value = mock_store

        args = argparse.Namespace(
            symbol="SOLUSDT", deriv_ttl=300, with_cross_asset=False,
        )
        out = await _refresh_derivatives(args)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["symbol"], "SOLUSDT")
        self.assertEqual(out["cache_ttl_seconds"], 300)
        self.assertIn("oi_history", out["futures_keys_present"])
        # With_cross_asset=False — no cross asset calls
        mock_fetch.assert_awaited_once()
        kwargs = mock_fetch.await_args.kwargs
        self.assertEqual(kwargs["include_cross_asset"], False)
        # symbol is positional
        positional = mock_fetch.await_args.args
        self.assertEqual(positional[1], "SOLUSDT")
        # TTL passed through to the publisher
        call_kwargs = mock_store.publish_derivative_evidence.await_args.kwargs
        self.assertEqual(call_kwargs["ttl_s"], 300)

    @patch("market_service.commands.harness.RedisRuntimeStore")
    @patch("market_service.nooa_harness.pipeline.fetch_derivative_evidence",
           new_callable=AsyncMock)
    @patch("market_service.clients.binance.Binance")
    async def test_refresh_with_cross_asset(
        self, mock_binance_cls, mock_fetch, mock_store_cls,
    ):
        mock_fetch.return_value = {
            "observed_at_ms": 0,
            "futures": {}, "cross_asset": {"tickers_24h": [{"symbol": "BTC"}],
                                            "funding": [{"symbol": "BTC"}]},
        }
        mock_store = MagicMock()
        mock_store.publish_derivative_evidence = AsyncMock(return_value="1-0")
        mock_store.derivative_cache_ttl = AsyncMock(return_value=300)
        mock_store.derivatives_key = MagicMock(return_value="k")
        mock_store.close = AsyncMock(return_value=None)
        mock_store_cls.return_value = mock_store

        args = argparse.Namespace(
            symbol="BTCUSDT", deriv_ttl=60, with_cross_asset=True,
        )
        out = await _refresh_derivatives(args)
        kwargs = mock_fetch.await_args.kwargs
        self.assertTrue(kwargs["include_cross_asset"])
        self.assertEqual(sorted(out["cross_asset_keys_present"]),
                         ["funding", "tickers_24h"])


class HarnessAnalyzeProjectionTests(unittest.TestCase):
    """Compact projection + cache metadata extractors."""

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
        proj = _projection(env)
        self.assertEqual(proj["symbol"], "SOLUSDT")
        self.assertEqual(proj["run_id"], "uuid")
        self.assertNotIn("canonical_state", proj)
        self.assertIn("analysis_keys", proj)
        self.assertEqual(sorted(proj["analysis_keys"]),
                         ["auction", "delta", "demand"])

    def test_derivatives_cache_summary(self):
        env = {
            "canonical_state": {
                "data-access": {
                    "evidence": {
                        "derivative_observed_at_ms": 1000,
                        "futures": {
                            "oi_history": [{"x": 1}],
                            "taker_buy_sell": None,
                            "top_ls": [{"longAccount": 0.6}],
                        },
                        "cross_asset": {
                            "tickers_24h": [{"symbol": "BTC"}],
                            "funding": [{"symbol": "BTC"}],
                        },
                    }
                }
            }
        }
        summary = _summarize_derivatives_cache(env)
        self.assertEqual(summary["observed_at_ms"], 1000)
        self.assertIn("oi_history", summary["fields_present"])
        self.assertIn("top_ls", summary["fields_present"])
        self.assertNotIn("taker_buy_sell", summary["fields_present"])
        self.assertEqual(summary["cross_asset"], {"tickers": 1, "funding": 1})

    def test_derivatives_cache_summary_with_no_evidence(self):
        summary = _summarize_derivatives_cache({})
        # An empty envelope has no derivative metadata; the function still
        # returns a stable shape with falsy defaults.
        self.assertIsNone(summary["observed_at_ms"])
        self.assertFalse(summary["include_derivatives"])
        self.assertEqual(summary.get("fields_present", []), [])


class HarnessAnalyzeNoPersistTests(unittest.IsolatedAsyncioTestCase):
    """--no-persist is a real dry-run: Redis-only resolver + persist=False."""

    def setUp(self):
        import os
        os.environ.setdefault("DATABASE_URL",
                              "postgresql://x:y@localhost/z")
        os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

    def _env_dict(self):
        return {
            "schema_version": 2, "symbol": "SOLUSDT", "status": "healthy",
            "generated_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:00Z",
            "coverage": {}, "run_id": "uuid", "data_source": "x",
            "canonical_state": {}, "domain_outputs": {}, "errors": [],
            "source_metadata": {},
        }

    @patch("market_service.nooa_harness.pipeline.run_cycle", new_callable=AsyncMock)
    async def test_no_persist_uses_redis_only_settings_and_skips_persist(
        self, mock_run_cycle,
    ):
        """--no-persist must thread persist=False into run_cycle (real dry-run).

        It must ALSO resolve Settings via from_redis_env (no DATABASE_URL
        requirement) since nothing is written.
        """
        from market_service.commands.harness import _run_analyze
        import argparse

        mock_run_cycle.return_value = MarketRunEnvelopeStub(**self._env_dict())
        args = argparse.Namespace(
            symbol="SOLUSDT", window="15m", deriv_ttl=300,
            no_persist=True, no_derivatives=False,
            with_cross_asset=False, force_refresh_derivatives=False,
            envelope_summary=False,
        )

        with patch.dict(
            os.environ, {"DATABASE_URL": "", "REDIS_URL": "redis://x:6379/0"},
            clear=False,
        ):
            result = await _run_analyze(args)

        # run_cycle must have been called with persist=False
        call_kwargs = mock_run_cycle.call_args.kwargs
        self.assertIs(call_kwargs["persist"], False)
        self.assertEqual(result["persistence"], {"mode": "dry_run"})

    @patch("market_service.nooa_harness.pipeline.run_cycle", new_callable=AsyncMock)
    async def test_persist_uses_full_settings(self, mock_run_cycle):
        """Without --no-persist we persist, so full Settings (DB required)."""
        from market_service.commands.harness import _run_analyze

        mock_run_cycle.side_effect = lambda settings, env, window, **kw: (
            MarketRunEnvelopeStub(**self._env_dict())
        )
        args = argparse.Namespace(
            symbol="SOLUSDT", window="15m", deriv_ttl=300,
            no_persist=False, no_derivatives=False,
            with_cross_asset=False, force_refresh_derivatives=False,
            envelope_summary=False,
        )
        result = await _run_analyze(args)
        call_kwargs = mock_run_cycle.call_args.kwargs
        self.assertIs(call_kwargs["persist"], True)
        self.assertEqual(result["persistence"], {"mode": "persisted"})

    @patch("market_service.nooa_harness.pipeline.run_cycle", new_callable=AsyncMock)
    async def test_envelope_summary_uses_projection(self, mock_run_cycle):
        """--envelope-summary returns the compact projection, never dead lookups."""
        from market_service.commands.harness import _run_analyze
        from tests._nooa_fixtures import _SAMPLE_ENVELOPE

        import market_service.runtime.contracts as RC
        envelope_obj = RC.MarketRunEnvelope.from_mapping(dict(_SAMPLE_ENVELOPE))
        mock_run_cycle.side_effect = lambda settings, env, ws, **kw: envelope_obj
        args = argparse.Namespace(
            symbol="SOLUSDT", window="15m", deriv_ttl=300,
            no_persist=False, no_derivatives=False,
            with_cross_asset=False, force_refresh_derivatives=False,
            envelope_summary=True,
        )
        result = await _run_analyze(args)
        self.assertIn("envelope_summary", result)
        self.assertNotIn("envelope", result)


class MarketRunEnvelopeStub:
    """Minimal duck-typed stub exposing only what _run_analyze needs."""

    def __init__(self, **fields):
        self._fields = fields

    def to_dict(self):
        return dict(self._fields)


if __name__ == "__main__":
    unittest.main()