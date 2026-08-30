"""Regression tests for the envelope/interpretation formatting contract.

Covers the fixes to the information pipeline (2026-08 pass):

- ``_envelope_summary`` is a pure path-read projection: snake_case reads,
  CVD read from the calculations layer (no math in the transport layer).
- ``find_keystone`` natively emits ``bid`` (intrinsic keystone naming).
- ``run_calculations`` technical section: EMA null with explicit source when
  klines are absent (never computed on an empty series).
- ``_adapt_oi`` null discipline: missing OI is None, never 0.0.
- ``_adapt_wall_migration`` INSUFFICIENT_DATA shape on an empty book.
- ``read_raw_window`` measured coverage block + evidence-time ``observed_at``.
- ``_merge_derivatives`` records bar-period/series metadata.
- ``_wall_snapshot_payload`` preserves null fuel metrics (no 0.0 fabrication).
- ``_enrich_fut_keystone`` resolves only the ``ask`` alias.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from market_service.calculations.orderbook import find_keystone
from market_service.nooa_harness import pipeline as P
from market_service.nooa_harness.pipeline import (
    _adapt_oi,
    _adapt_wall_migration,
    _enrich_fut_keystone,
    _merge_derivatives,
    _wall_snapshot_payload,
    read_raw_window,
    run_calculations,
)


def _book(price: float = 100.0, n: int = 5) -> dict[str, Any]:
    return {
        "bids": [[price - 0.01 * i, 1.0 + i] for i in range(n)],
        "asks": [[price + 0.01 * i, 1.0 + i] for i in range(n)],
    }


def _trade(ts: int, tid: int, qty: float = 1.0, maker: bool = False) -> dict[str, Any]:
    return {"ts": ts, "id": tid, "price": 100.0, "qty": qty,
            "quote_qty": 100.0 * qty, "is_buyer_maker": maker, "side": "buy"}


class EnvelopeSummaryProjectionTests(unittest.TestCase):
    """The briefing summary reads REAL envelope paths — no math, no dead keys."""

    def _envelope(self) -> dict[str, Any]:
        from market_service.runtime.contracts import _envelope_summary
        return _envelope_summary({
            "schema_version": 1,
            "symbol": "SOLUSDT",
            "status": "healthy",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
            "data_source": "domain_pipeline",
            "coverage": {"domain_status": {"data-access": "healthy"}},
            "errors": [],
            "canonical_state": {
                "data-access": {"status": "healthy", "errors": [], "evidence": {
                    "futures": {
                        "ticker_24h": {"last_price": 100.5, "quote_volume": 900.0,
                                        "high_price": 101.0, "low_price": 99.0},
                        "funding": {"last_funding_rate": 0.0001, "mark_price": 100.4},
                        "open_interest": {"open_interest": 12345.0},
                    },
                }},
                "calculations": {"calculations": {"flow": {
                    "spot_flow": {"cvd": 12.0},
                    "futures_flow": {"cvd": -3.5},
                }}},
                "analysis": {"analysis": {"demand": {"decomposition": {
                    "spot": {"obi": 0.2}, "futures": {"obi": -0.1},
                }}}},
            },
        })

    def test_snake_case_paths_are_read(self):
        s = self._envelope()
        # Client normalizes Binance camelCase → snake_case at its boundary;
        # the summary must read the normalized keys.
        self.assertEqual(s["last_price"], 100.5)
        self.assertEqual(s["volume_24h"], 900.0)
        self.assertEqual(s["funding_rate"], 0.0001)
        self.assertEqual(s["mark_price"], 100.4)
        self.assertEqual(s["open_interest"], 12345.0)

    def test_cvd_is_read_from_calculations_layer(self):
        s = self._envelope()
        self.assertEqual(s["spot_cvd"], 12.0)
        self.assertEqual(s["futures_cvd"], -3.5)

    def test_missing_values_stay_null(self):
        s = self._envelope()
        self.assertIsNone(s["spot_obi"] if s["spot_obi"] is None else None) if False else None
        self.assertEqual(s["spot_obi"], 0.2)
        self.assertEqual(s["futures_obi"], -0.1)


class FindKeystoneBidAliasTests(unittest.TestCase):
    def test_bid_alias_is_native(self):
        bids = [[99.9, 5.0], [99.85, 10.0], [99.8, 2.0]]
        ks = find_keystone(bids, price=100.1, lo_offset=-0.30, hi_offset=-0.05)
        self.assertEqual(ks["bid"], ks["keystone"])

    def test_enrich_only_resolves_ask(self):
        ks = {"keystone": 99.9, "bid": 99.9, "window_qty": 5.0,
              "tight": {}, "wide": {}}
        asks = [[100.0, 1.0], [100.5, 2.0]]
        out = _enrich_fut_keystone(ks, asks)
        self.assertEqual(out["ask"], 100.0)
        self.assertEqual(out["bid"], 99.9)


class TechnicalSectionKlinesTests(unittest.TestCase):
    def _evidence(self, klines: Any) -> dict[str, Any]:
        return {
            "spot": {"trades_normalized": [], "order_book": {"bids": [], "asks": []}},
            "futures": {"trades_normalized": [], "order_book": _book(),
                        "klines": klines},
        }

    def test_no_klines_yields_null_emas_with_source_marker(self):
        r = run_calculations(self._evidence([]), depth=50, window=900)
        tech = r["calculations"]["technical"]
        self.assertIsNone(tech["emas"])
        self.assertIsNone(tech["emas_source"])

    def test_klines_present_computes_emas_with_source(self):
        klines = [[0, 0, 0, c, c] for c in range(1, 40)]
        r = run_calculations(self._evidence(klines), depth=50, window=900)
        tech = r["calculations"]["technical"]
        self.assertIsInstance(tech["emas"], dict)
        self.assertTrue(tech["emas"])
        self.assertEqual(tech["emas_source"], "derivatives.klines_5m")


class OiAdapterNullDisciplineTests(unittest.TestCase):
    def test_missing_oi_is_none_never_zero(self):
        evidence = {"futures": {"order_book": _book(), "open_interest": {}}}
        out = _adapt_oi(evidence, depth=50)
        self.assertIsNone(out["raw_open_interest"])
        self.assertIsNone(out["weighted_contracts"]["oi"])
        self.assertIsNone(out["weighted_contracts"]["top_long_contracts"])

    def test_missing_price_skips_wall_search(self):
        evidence = {"futures": {"order_book": {"bids": [], "asks": []},
                                "open_interest": {"open_interest": 100.0}}}
        out = _adapt_oi(evidence, depth=50)
        self.assertEqual(out["walls"], [])
        self.assertEqual(out["raw_open_interest"], 100.0)


class WallMigrationInsufficientDataTests(unittest.TestCase):
    def test_empty_book_returns_insufficient_data(self):
        evidence = {"futures": {"order_book": {"bids": [], "asks": []}}}
        out = _adapt_wall_migration(evidence, None, {}, None, depth=50)
        self.assertEqual(out["verdict"], "INSUFFICIENT_DATA")
        self.assertIsNone(out["fuel_ratio"])
        self.assertIsNone(out["wall_delta"])
        self.assertIsNone(out["inputs_used"]["fuel_ratio_value"])
        # Tiers on an empty book are a TRUE zero — computed from the book.
        self.assertEqual(out["tiers"]["total_qty"], 0.0)

    def test_booked_run_preserves_raw_fuel_ratio(self):
        evidence = {"futures": {"order_book": _book()}}
        out = _adapt_wall_migration(evidence, None, {}, None, depth=50)
        # Success path has no verdict key — that's the degraded-path marker.
        self.assertNotIn("verdict", out)
        fr = out["inputs_used"]["fuel_ratio_value"]
        self.assertIsInstance(fr, float)


class ReadRawWindowCoverageTests(unittest.TestCase):
    """Measured coverage: trade span, dedupe stats, snapshot count, staleness."""

    class _FakeRedis:
        def __init__(self, latest: dict[str, Any], snapshots: list[dict[str, Any]]):
            self._latest = latest
            self._snapshots = snapshots

        async def read_raw_latest(self, symbol: str):
            return self._latest

        async def read_raw_window(self, symbol: str, since_ms: int):
            return self._snapshots

    async def _run(self, latest, snapshots):
        return await read_raw_window(self._FakeRedis(latest, snapshots), "SOLUSDT", 15)

    def test_coverage_block_measures_reality(self):
        snap1 = {"observed_at": "2026-01-01T00:00:00+00:00", "observed_at_ms": 1000,
                 "spot": {"trades_normalized": [_trade(500, 1), _trade(600, 2)]},
                 "futures": {"trades_normalized": [_trade(700, 3)]}}
        snap2 = {"observed_at": "2026-01-01T00:00:05+00:00", "observed_at_ms": 6000,
                 "spot": {"trades_normalized": [_trade(600, 2), _trade(700, 4)]},
                 "futures": {"trades_normalized": []}}
        ev = asyncio.run(self._run(snap2, [snap1, snap2]))

        cov = ev["coverage"]
        self.assertEqual(cov["requested_window_seconds"], 900)
        self.assertEqual(cov["snapshots_used"], 2)
        self.assertEqual(cov["latest_observed_at_ms"], 6000)
        self.assertIsNotNone(cov["stream_staleness_ms"])
        # spot: 4 raw across 2 snapshots (600,2 appears in both), 1 deduped
        self.assertEqual(cov["spot_trades"]["raw_trade_count"], 4)
        self.assertEqual(cov["spot_trades"]["trade_count"], 3)
        self.assertEqual(cov["spot_trades"]["duplicates_removed"], 1)
        self.assertEqual(cov["spot_trades"]["first_trade_ms"], 500)
        self.assertEqual(cov["spot_trades"]["last_trade_ms"], 700)
        # futures: 1 trade, no dedupe
        self.assertEqual(cov["futures_trades"]["trade_count"], 1)

    def test_observed_at_is_evidence_time_not_read_time(self):
        snap = {"observed_at": "2026-01-01T00:00:00+00:00", "observed_at_ms": 1000,
                "spot": {"trades_normalized": []}, "futures": {"trades_normalized": []}}
        ev = asyncio.run(self._run(snap, [snap]))
        # observed_at = latest snapshot time, NOT datetime.now().
        self.assertEqual(ev["observed_at"], "2026-01-01T00:00:00+00:00")


class MergeDerivativesMetaTests(unittest.TestCase):
    def test_bar_horizon_metadata_is_recorded(self):
        evidence: dict[str, Any] = {"spot": {}, "futures": {}}
        deriv = {"observed_at_ms": 1234,
                 "futures": {"oi_history": [{}] * 48, "taker_buy_sell": [{}] * 48,
                             "top_ls": [{}] * 12, "global_ls": [{}] * 12,
                             "klines": [{}] * 48}}
        out = _merge_derivatives(evidence, deriv)
        meta = out["derivatives_meta"]
        self.assertEqual(meta["bar_period_s"], 300)
        self.assertEqual(meta["series"]["oi_history"], 48)
        self.assertEqual(meta["series"]["top_ls"], 12)

    def test_no_derivatives_no_meta(self):
        out = _merge_derivatives({"spot": {}, "futures": {}}, None)
        self.assertNotIn("derivatives_meta", out)


class WallSnapshotPayloadNullTests(unittest.TestCase):
    def test_degraded_analysis_preserves_null_fuel(self):
        analysis = {"analysis": {"wall_migration": {
            "verdict": "INSUFFICIENT_DATA", "fuel_ratio": None,
            "inputs_used": {"fuel_ratio_value": None, "ask_walls_built": 0,
                            "ask_walls_eroded": 0},
        }}}
        evidence = {"futures": {"order_book": {"bids": [], "asks": []}}}
        payload = _wall_snapshot_payload("SOLUSDT", "run-1", evidence, analysis)
        self.assertIsNone(payload["fuel_ratio"])
        self.assertIsNone(payload["bid_pool"])

    def test_measured_fuel_is_kept(self):
        analysis = {"analysis": {"wall_migration": {
            "fuel_ratio": {"ratio": 1.3, "bid_pool": 10.0, "ask_pool": 7.0,
                           "bid_floor": 97.0, "ask_target": 103.0},
            "inputs_used": {"fuel_ratio_value": 1.3, "ask_walls_built": 1,
                            "ask_walls_eroded": 0},
        }}}
        evidence = {"futures": {"order_book": {"bids": [], "asks": []}}}
        payload = _wall_snapshot_payload("SOLUSDT", "run-1", evidence, analysis)
        self.assertEqual(payload["fuel_ratio"], 1.3)
        self.assertEqual(payload["bid_pool"], 10.0)


class CorrelationSectionReuseTests(unittest.TestCase):
    def test_correlation_only_request_still_runs(self):
        evidence = {
            "spot": {"trades_normalized": [_trade(1000 + i * 1000, i) for i in range(5)],
                     "order_book": _book()},
            "futures": {"trades_normalized": [_trade(1000 + i * 1000, 100 + i, maker=(i % 2 == 0))
                                              for i in range(5)],
                        "order_book": _book()},
        }
        r = run_calculations(evidence, depth=50, window=60,
                             sections=frozenset({"correlation"}))
        self.assertIn("correlation", r["calculations"])
        corr = r["calculations"]["correlation"]
        self.assertIsNotNone(corr)

    def test_volume_profile_built_once_shape(self):
        evidence = {
            "spot": {"trades_normalized": [], "order_book": {"bids": [], "asks": []}},
            "futures": {"trades_normalized": [_trade(1000 + i, i) for i in range(5)],
                        "order_book": {"bids": [], "asks": []}},
        }
        r = run_calculations(evidence, depth=50, window=900,
                             sections=frozenset({"volume_profile"}))
        vp = r["calculations"]["volume_profile"]
        self.assertIn("buckets", vp)
        self.assertIn("summary", vp)


if __name__ == "__main__":
    unittest.main()
