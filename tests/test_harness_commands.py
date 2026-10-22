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

    @patch("market_service.interaction_plane.cli._read_substrates",
           new_callable=AsyncMock)
    def test_default_routes_to_substrate_snapshot(self, mock_read):
        """Default (no flags) reads the warm-plane snapshot.

        The legacy `--live` waveform (build()) is EXCLUDED from the default,
        and the run_cycle pipeline is gone: bare invocation reads worker
        state, it never computes.
        """
        from market_service.commands.harness import main
        mock_read.return_value = (
            {"tool": "substrate.read", "status": "ok", "reason": None,
             "data": {"symbol": "SOLUSDT", "substrates": {}},
             "null_fields": [], "budget_receipt": {"truncated": False}},
            "redis",
        )
        rc = main(["SOLUSDT", "--json"])
        self.assertEqual(rc, 0)
        mock_read.assert_awaited_once()

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


class HarnessReadMarketHandlerTests(unittest.IsolatedAsyncioTestCase):
    """``_read_market`` mode routing + source tagging + empty-read discipline."""

    def _args(self, **overrides):
        base = dict(symbol="SOLUSDT", run_id=None, read_errors=False)
        base.update(overrides)
        return argparse.Namespace(**base)

    async def test_snapshot_returns_projection_with_source(self):
        # The live read shape (dispatch_market_read): every section carries its
        # own age_ms; there is no collated-envelope projection to unwrap.
        live = {
            "symbol": "SOLUSDT", "venue": "futures", "status": "ok",
            "source": "redis", "mode": "snapshot",
            "sections": {
                "microstructure": {"available": True, "age_ms": 5,
                                   "last_price": "93.5"},
            },
        }
        with patch("market_service.interaction_plane.reads.read_ledger") as m_ledger:
            async def _ledger(symbol, *, run_id=None, mode="snapshot",
                              include_errors=False):
                return (dict(live, symbol=symbol, mode=mode), "redis", None)
            m_ledger.side_effect = _ledger
            result, source = await _read_market(self._args(), "snapshot")
        self.assertEqual(source, "redis")
        self.assertEqual(result["tool"], "market.read")
        self.assertEqual(result["status"], "ok")
        self.assertIn("data", result)
        self.assertIn("budget_receipt", result)
        # The live payload IS the data (no envelope re-projection): every
        # section carries the age_ms the read measured.
        self.assertIn("sections", result["data"])
        self.assertEqual(
            result["data"]["sections"]["microstructure"]["age_ms"], 5)

    async def test_empty_read_returns_empty_envelope(self):
        with patch("market_service.interaction_plane.reads.read_ledger") as m_ledger, \
             patch("market_service.interaction_plane.reads.read_surfaces") as m_surfaces:
            async def _ledger(symbol, *, run_id=None, mode="snapshot",
                              include_errors=False):
                return None, None, None
            m_ledger.side_effect = _ledger
            m_surfaces.return_value = {
                "symbol": "SOLUSDT", "surfaces": [],
                "available_surfaces": [], "read_tools": {},
            }
            result, source = await _read_market(self._args(), "snapshot")
        self.assertIsNone(source)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["tool"], "market.read")


if __name__ == "__main__":
    unittest.main()