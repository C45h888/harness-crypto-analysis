import unittest

from market_service.calculations.flow import (
    microprice_skew_bps,
    price_bucketed_flow,
    spot_turnover_share,
    summarize,
)
from market_service.calculations.orderbook import (
    absorption_ladder,
    find_keystone,
    hourly_keystone_migration,
    top_density_windows,
    zone_ratio_grid,
)
from market_service.calculations.technical import (
    ema_series,
    ema_position,
    tiered_large_flow,
    wick_rejections,
)
from market_service.calculations.volume_profile import (
    build_volume_profile,
    side_split,
    volume_profile_summary,
)


class FlowFoldsTests(unittest.TestCase):
    def test_summarize_exposes_micro_price_skew(self):
        book = {"bids": [["100", "10"], ["99", "10"]], "asks": [["102", "10"], ["103", "5"]]}
        out = summarize([], book=book, depth_levels=5)
        self.assertIn("micro_price_skew_bps", out)
        self.assertIsInstance(out["micro_price_skew_bps"], float)

    def test_microprice_skew_none_on_empty(self):
        self.assertIsNone(microprice_skew_bps([], []))

    def test_price_bucketed_flow_splits_buy_sell(self):
        trades = [
            {"ts": 5000, "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
            {"ts": 6000, "price": 100.05, "qty": 2.0, "is_buyer_maker": True},
        ]
        out = price_bucketed_flow(trades, bucket_size=0.05, now_ts=100000)
        self.assertEqual(len(out), 2)
        net = {r["price"]: r["net"] for r in out}
        self.assertEqual(net[100.0], 1.0)
        self.assertEqual(net[100.05], -2.0)

    def test_spot_turnover_share(self):
        self.assertAlmostEqual(spot_turnover_share(30.0, 70.0), 0.3)
        self.assertIsNone(spot_turnover_share(None, 70.0))


class TechnicalTests(unittest.TestCase):
    def test_ema_series_and_position(self):
        closes = [float(i) for i in range(1, 60)]
        s = ema_series(closes)
        self.assertIn("ema9", s)
        self.assertNotIn("ema200", s)  # not enough data for 200
        pos = ema_position(closes, last_price=50.5)
        self.assertIn(pos["ema9"], ("ABOVE", "BELOW"))

    def test_wick_rejections(self):
        r = wick_rejections(high=10.2, low=9.8, close=9.9, emas=[10.0], prefix="1m")
        self.assertIn("1m_10_upper_wick_reject", r)

    def test_tiered_flow_marks_large_and_huge(self):
        now = 60_000 * 100  # arbitrary
        trades = [{"ts": now - 1000, "qty": 300.0, "price": 100.0, "is_buyer_maker": False}]
        out = tiered_large_flow(trades, now_ms=now)
        agg = out["flow_5min"]
        self.assertGreater(agg["large_buy_usd"], 0)
        self.assertGreater(agg["huge_buy_usd"], 0)


class OrderbookTests(unittest.TestCase):
    def setUp(self):
        self.book = {
            "bids": [[100.0, 500.0], [99.9, 100.0], [99.8, 200.0], [99.7, 300.0]],
            "asks": [[100.1, 400.0], [100.2, 100.0], [100.3, 200.0]],
        }

    def test_top_density_windows_ranks_bid_density(self):
        tops = top_density_windows(self.book, width=0.20, side="bid", top_n=3)
        self.assertEqual(len(tops), 3)
        # densest window should be the largest summed window
        self.assertGreaterEqual(tops[0]["window_qty"], tops[1]["window_qty"])

    def test_find_keystone_within_band(self):
        ks = find_keystone(self.book["bids"], price=100.1, lo_offset=-0.30, hi_offset=-0.05)
        self.assertIn("keystone", ks)
        self.assertIn("tight", ks)

    def test_absorption_ladder_cumulates(self):
        ladder = absorption_ladder(self.book["bids"], price=100.1)
        self.assertGreater(len(ladder), 0)
        self.assertAlmostEqual(ladder[-1]["cum_qty"], sum(q for _, q in self.book["bids"]))

    def test_zone_ratio_grid(self):
        grid = zone_ratio_grid(self.book, lo=99.7, hi=100.3, step=0.1)
        self.assertGreater(len(grid), 0)
        self.assertTrue(all("ratio" in z for z in grid))

    def test_hourly_keystone_migration(self):
        base = 1_600_000_000_000
        trades = [
            {"ts": base, "price": 100.0, "qty": 5.0, "is_buyer_maker": False},
            {"ts": base + 1, "price": 101.0, "qty": 8.0, "is_buyer_maker": False},
            {"ts": base + 3_600_000, "price": 104.0, "qty": 9.0, "is_buyer_maker": False},
        ]
        mig = hourly_keystone_migration(trades, bucket_size=0.05)
        self.assertIn("verdict", mig)
        self.assertEqual(len(mig["hourly"]), 2)


class VolumeProfileTests(unittest.TestCase):
    def test_profile_summary_and_side_split(self):
        trades = [
            {"ts": 1, "price": 100.0, "qty": 10.0, "is_buyer_maker": False},
            {"ts": 2, "price": 100.05, "qty": 5.0, "is_buyer_maker": True},
            {"ts": 3, "price": 100.1, "qty": 2.0, "is_buyer_maker": False},
        ]
        vp = build_volume_profile(trades, bucket_size=0.05)
        summary = volume_profile_summary(vp)
        self.assertIsNotNone(summary)
        self.assertIn("poc", summary)
        self.assertIn("vah", summary)
        split = side_split(vp, top_n=2)
        self.assertGreater(len(split["top_buy"]), 0)


if __name__ == "__main__":
    unittest.main()
