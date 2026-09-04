"""Tests for the new DELTA variable in market_service.calculations.delta."""

from __future__ import annotations

import unittest

from market_service.calculations.delta import (
    delta_state,
    delta_variable,
    flow_alignment,
    range_imbalance,
    tbr_avg_pct,
    tbr_last_pct,
    wall_imbalance,
)


def _bids_asks(bids, asks):
    return {"bids": bids, "asks": asks}


class DeltaWallImbalanceTests(unittest.TestCase):
    def test_empty_book_returns_zero(self):
        self.assertEqual(wall_imbalance([], 0.0, 100.0), 0.0)

    def test_single_bid_in_range(self):
        self.assertEqual(wall_imbalance([(10.0, 5.0)], 0.0, 100.0), 5.0)

    def test_single_bid_out_of_range(self):
        self.assertEqual(wall_imbalance([(200.0, 5.0)], 0.0, 100.0), 0.0)


class DeltaRangeImbalanceTests(unittest.TestCase):
    def test_returns_band_count_for_075_half_range_010_step(self):
        grid = range_imbalance(_bids_asks([(75.0, 100), (75.1, 50)], [(75.3, 80)]), 75.15)
        # 0.75 / 0.10 = 7.5 bands below + 7.5 above + 1 (current step) = 15-17 depending on rounding
        self.assertGreaterEqual(len(grid["bands"]), 14)
        self.assertLessEqual(len(grid["bands"]), 17)

    def test_balanced_book_has_zero_imbalance(self):
        grid = range_imbalance(_bids_asks([(75.0, 100)], [(75.0, 100)]), 75.05)
        for band in grid["bands"]:
            if band["bid_qty"] + band["ask_qty"] > 0:
                self.assertAlmostEqual(band["imbalance"], 0.0, places=6)

    def test_bid_heavy_band(self):
        # 75.0 bid only, 75.0 ask only — band [75.0, 75.1) is bid-heavy
        grid = range_imbalance(_bids_asks([(75.05, 100)], [(75.15, 100)]), 75.15)
        # First band starting at floor((75.15-0.75)/0.10)*0.10 = 74.4
        first = grid["bands"][0]
        if first["bid_qty"] + first["ask_qty"] > 0:
            self.assertGreater(first["imbalance"], 0.0)

    def test_rejects_zero_half_range(self):
        with self.assertRaises(ValueError):
            range_imbalance(_bids_asks([], []), 75.0, half_range=0)
        with self.assertRaises(ValueError):
            range_imbalance(_bids_asks([], []), 75.0, band_step=0)


class DeltaFlowAlignmentTests(unittest.TestCase):
    def test_empty_series_returns_zero(self):
        self.assertEqual(flow_alignment(None), 0.0)
        self.assertEqual(flow_alignment([]), 0.0)

    def test_buysellratio_neutral(self):
        self.assertAlmostEqual(
            flow_alignment([{"buySellRatio": 1.0}]), 0.0, places=6,
        )

    def test_buysellratio_buy_heavy_positive(self):
        # ratio = 2 → (2-1)/(2+1) = 0.333
        self.assertAlmostEqual(
            flow_alignment([{"buySellRatio": 2.0}]), 1 / 3, places=6,
        )

    def test_buysellratio_sell_heavy_negative(self):
        # ratio = 0.5 → (0.5-1)/(0.5+1) = -0.333
        self.assertAlmostEqual(
            flow_alignment([{"buySellRatio": 0.5}]), -1 / 3, places=6,
        )

    def test_buysellratio_zero_handles_safely(self):
        # ratio = 0 must not divide by zero
        self.assertEqual(flow_alignment([{"buySellRatio": 0}]), -1.0)

    def test_falls_back_to_buy_vol_sell_vol(self):
        self.assertAlmostEqual(
            flow_alignment([{"buyVol": 70, "sellVol": 30}]),
            (0.7 - 0.5) * 2, places=6,  # = 0.4
        )


class DeltaTBRHelpersTests(unittest.TestCase):
    def test_tbr_last_pct_basic(self):
        self.assertAlmostEqual(
            tbr_last_pct([{"buyVol": 70, "sellVol": 30}]), 70.0, places=4,
        )

    def test_tbr_last_pct_missing(self):
        self.assertIsNone(tbr_last_pct(None))
        self.assertIsNone(tbr_last_pct([]))
        self.assertIsNone(tbr_last_pct([{"buyVol": 0, "sellVol": 0}]))

    def test_tbr_avg_pct_uses_last_n(self):
        bars = [
            {"buyVol": 0, "sellVol": 100},
            {"buyVol": 50, "sellVol": 50},
            {"buyVol": 100, "sellVol": 0},
        ]
        # 3-avg: (0+50+100) / (100+50+100+100) wait - total = 300, buys = 150 → 50%
        self.assertAlmostEqual(tbr_avg_pct(bars, 3), 50.0, places=4)
        # Last 1 = 100%
        self.assertAlmostEqual(tbr_avg_pct(bars, 1), 100.0, places=4)
        # Last 2 = 75%
        self.assertAlmostEqual(tbr_avg_pct(bars, 2), 75.0, places=4)


class DeltaVariableTests(unittest.TestCase):
    def test_buy_heavy_books_have_positive_delta(self):
        book = _bids_asks(
            bids=[(75.0, 500), (75.05, 200), (75.1, 100)],
            asks=[(75.2, 50), (75.3, 50)],
        )
        tbr = [{"buySellRatio": 1.5, "buyVol": 60, "sellVol": 40}]
        d = delta_variable(book, tbr, 75.15)
        self.assertGreater(d["delta"], 0.0)
        self.assertEqual(d["tbr_last_pct"], 60.0)
        self.assertIn("bands", d)
        self.assertGreater(len(d["bands"]), 0)

    def test_sell_heavy_books_have_negative_delta(self):
        book = _bids_asks(
            bids=[(75.0, 50), (75.05, 50)],
            asks=[(75.2, 500), (75.3, 200), (75.4, 100)],
        )
        tbr = [{"buySellRatio": 0.5, "buyVol": 40, "sellVol": 60}]
        d = delta_variable(book, tbr, 75.15)
        self.assertLess(d["delta"], 0.0)

    def test_no_taker_buy_sell_still_produces_wall_signal(self):
        # Without flow alignment, DELTA collapses to wall imbalance alone.
        book = _bids_asks(
            bids=[(74.5, 500), (74.8, 200)], asks=[(75.5, 50), (75.8, 50)],
        )
        d = delta_variable(book, None, 75.15)
        # flow_alignment = 0 (no taker data) but wall_imbalance is still > 0
        self.assertEqual(d["flow_alignment"], 0.0)
        self.assertGreater(d["wall_imbalance"], 0.0)
        self.assertGreater(d["delta"], 0.0)
        self.assertIsNone(d["tbr_last_pct"])
        self.assertIsNone(d["tbr_3avg_pct"])

    def test_delta_in_legacy_range(self):
        # Both legs are bounded -1..+1, so the sum is in -2..+2.
        book = _bids_asks([], [])
        tbr = [{"buySellRatio": 1.0}]  # neutral
        d = delta_variable(book, tbr, 75.0)
        self.assertGreaterEqual(d["delta"], -2.0)
        self.assertLessEqual(d["delta"], 2.0)


class DeltaStateLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(delta_state(1.5), "BUYERS_IN_CONTROL")
        self.assertEqual(delta_state(0.5), "BUYERS_FAVORED")
        self.assertEqual(delta_state(0.1), "BUYERS_SLIGHTLY_FAVORED")
        self.assertEqual(delta_state(-0.1), "SELLERS_SLIGHTLY_FAVORED")
        self.assertEqual(delta_state(-0.5), "SELLERS_FAVORED")
        self.assertEqual(delta_state(-1.5), "SELLERS_IN_CONTROL")


if __name__ == "__main__":
    unittest.main()

class DerivativeFreshnessTests(unittest.TestCase):
    def test_none_is_stale(self):
        from market_service.nooa_harness.pipeline import _is_deriv_fresh
        self.assertFalse(_is_deriv_fresh(None, 1000, 500))

    def test_missing_observed_at_is_stale(self):
        from market_service.nooa_harness.pipeline import _is_deriv_fresh
        self.assertFalse(_is_deriv_fresh({}, 1000, 500))

    def test_future_ts_is_stale(self):
        from market_service.nooa_harness.pipeline import _is_deriv_fresh
        self.assertFalse(_is_deriv_fresh({"observed_at_ms": 2000}, 1000, 500))

    def test_within_window_is_fresh(self):
        from market_service.nooa_harness.pipeline import _is_deriv_fresh
        self.assertTrue(_is_deriv_fresh({"observed_at_ms": 1000}, 1200, 500))

    def test_beyond_window_is_stale(self):
        from market_service.nooa_harness.pipeline import _is_deriv_fresh
        self.assertFalse(_is_deriv_fresh({"observed_at_ms": 1000}, 2000, 500))
