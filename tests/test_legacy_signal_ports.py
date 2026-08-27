"""Tests for the Issue 3 legacy-signal ports (Pass 1: Layer 1 + Layer 2).

Covers the four new pure functions:
- seller_aggression_classify  (legacy seller_wall_check.py:162-240)
- keystone_trade_intensity    (legacy deep_keystone.py:74-101 + keystone_scan.py:130-165)
- keystone_bid_stack          (legacy deep_keystone.py:104-200)
- ask_wall_ladder             (legacy wall_analysis.py:83-112)

Plus regression coverage for the newly-wired orphans:
- tiered_large_flow           (legacy sol_deep_monitor.py:191-247)
"""

from __future__ import annotations

import unittest

from market_service.calculations.orderbook import (
    ask_wall_ladder,
    keystone_bid_stack,
    keystone_trade_intensity,
)
from market_service.calculations.technical import (
    seller_aggression_classify,
    tiered_large_flow,
)


def _trade(ts, price, qty, is_buyer_maker):
    return {"ts": ts, "price": price, "qty": qty, "is_buyer_maker": is_buyer_maker}


class SellerAggressionClassifyTests(unittest.TestCase):
    def test_high_aggression(self):
        now = 1_000_000
        trades = [
            _trade(now - 1000, 100.0, 200, True),   # big sell (taker)
            _trade(now - 2000, 100.0, 100, False),  # big buy (taker)
            _trade(now - 3000, 100.0, 50, True),    # small — excluded
        ]
        r = seller_aggression_classify(trades, now_ms=now)
        self.assertEqual(r["big_sells"], 1)
        self.assertEqual(r["big_buys"], 1)
        self.assertAlmostEqual(r["sell_ratio"], 2.0)
        self.assertEqual(r["classification"], "HIGH")

    def test_medium_aggression(self):
        now = 1_000_000
        trades = [
            _trade(now - 1000, 100.0, 100, True),
            _trade(now - 2000, 100.0, 100, False),
        ]
        r = seller_aggression_classify(trades, now_ms=now)
        self.assertAlmostEqual(r["sell_ratio"], 1.0)
        self.assertEqual(r["classification"], "MEDIUM")

    def test_low_aggression(self):
        now = 1_000_000
        trades = [
            _trade(now - 1000, 100.0, 100, False),  # big buy
            _trade(now - 2000, 100.0, 50, True),    # below threshold
        ]
        r = seller_aggression_classify(trades, now_ms=now)
        self.assertAlmostEqual(r["sell_ratio"], 0.0)
        self.assertEqual(r["classification"], "LOW")

    def test_no_buys_with_sells_is_high(self):
        now = 1_000_000
        trades = [_trade(now - 1000, 100.0, 300, True)]
        r = seller_aggression_classify(trades, now_ms=now)
        self.assertIsNone(r["sell_ratio"])
        self.assertEqual(r["classification"], "HIGH")

    def test_empty_window_returns_null(self):
        r = seller_aggression_classify([], now_ms=1_000_000)
        self.assertIsNone(r["sell_ratio"])
        self.assertIsNone(r["classification"])
        self.assertEqual(r["trades_in_window"], 0)

    def test_outside_window_excluded(self):
        now = 1_000_000
        cutoff = now - 5 * 60 * 1000
        trades = [_trade(cutoff - 1, 100.0, 500, True)]
        r = seller_aggression_classify(trades, now_ms=now)
        self.assertEqual(r["trades_in_window"], 0)
        self.assertIsNone(r["classification"])


class KeystoneTradeIntensityTests(unittest.TestCase):
    def test_tight_zone_split(self):
        trades = [
            _trade(1, 100.02, 10, False),  # tight buy (also inside wide)
            _trade(2, 100.03, 5, True),    # tight sell
            _trade(3, 100.20, 20, False),  # outside wide zone (hi=100.10)
            _trade(4, 100.08, 3, False),   # wide buy below min_qty
        ]
        r = keystone_trade_intensity(trades, 100.0, 100.05, 99.95, 100.10)
        self.assertEqual(r["tight"]["buy_count"], 1)
        self.assertEqual(r["tight"]["buy_qty"], 10)
        self.assertEqual(r["tight"]["sell_count"], 1)
        # wide zone includes the tight-zone buy (qty 10 >= 5); 100.20 is
        # outside wide hi; qty=3 trade is below min_qty.
        self.assertEqual(r["wide_aggressive_buys"]["count"], 1)
        self.assertAlmostEqual(r["wide_aggressive_buys"]["notional"], 100.02 * 10)

    def test_empty_trades(self):
        r = keystone_trade_intensity([], 100.0, 100.05, 99.95, 100.10)
        self.assertEqual(r["tight"]["buy_count"], 0)
        self.assertEqual(r["wide_aggressive_buys"]["count"], 0)
        self.assertEqual(r["tight"]["buy_qty"], 0.0)


class KeystoneBidStackTests(unittest.TestCase):
    def test_cumulative_and_decomposition(self):
        bids = [
            (99.90, 100),  # floor area (below keystone)
            (99.97, 200),  # tight zone + below keystone
            (100.00, 500),  # exact keystone
            (100.03, 300),  # tight zone
            (100.10, 400),  # above (next defence)
        ]
        r = keystone_bid_stack(bids, 100.00, tol=0.05)
        # tight zone: 99.97, 100.00, 100.03
        self.assertEqual(len(r["tight"]["levels"]), 3)
        self.assertAlmostEqual(r["tight"]["total_qty"], 200 + 500 + 300)
        # below: 99.90 and 99.97 (floor_lo = keystone - 6*tol = 99.70)
        self.assertEqual(len(r["below"]), 2)
        self.assertAlmostEqual(r["below"][0]["qty"], 100)
        self.assertAlmostEqual(r["below"][1]["qty"], 200)
        # at: exact match
        self.assertEqual(len(r["at"]), 1)
        self.assertAlmostEqual(r["at"][0]["qty"], 500)
        # above: 100.03 and 100.10 both sit above keystone within
        # keystone + 3*tol = 100.15 (legacy deep_keystone ABOVE band)
        self.assertEqual(len(r["above"]), 2)

    def test_empty_book(self):
        r = keystone_bid_stack([], 100.0)
        self.assertEqual(r["tight"]["total_qty"], 0.0)
        self.assertEqual(r["below"], [])
        self.assertEqual(r["at"], [])
        self.assertEqual(r["above"], [])


class AskWallLadderTests(unittest.TestCase):
    def test_cumulative_notional(self):
        asks = [
            (100.01, 10),
            (100.02, 20),
            (100.06, 30),
        ]
        r = ask_wall_ladder(asks, 100.00, hi_limit=100.10, step=0.05)
        # Inclusive hi_limit (legacy wall_analysis.py:86): 3 buckets
        # [100.00,100.05), [100.05,100.10), [100.10,100.15)
        self.assertEqual(len(r["zones"]), 3)
        # First bucket: 10 + 20 = 30
        self.assertAlmostEqual(r["zones"][0]["qty"], 30)
        # Second bucket: 30
        self.assertAlmostEqual(r["zones"][1]["qty"], 30)
        # Third bucket: empty (inclusive boundary bucket)
        self.assertAlmostEqual(r["zones"][2]["qty"], 0)
        self.assertAlmostEqual(r["zones"][2]["cum_notional"],
                               r["zones"][0]["notional"] + r["zones"][1]["notional"])
        self.assertAlmostEqual(r["total_qty"], 60)

    def test_invalid_step(self):
        with self.assertRaises(ValueError):
            ask_wall_ladder([], 100.0, step=0)


class TieredLargeFlowTests(unittest.TestCase):
    def test_tier_aggregation(self):
        now = 1_000_000
        trades = [
            _trade(now - 1000, 100.0, 60, False),    # large buy
            _trade(now - 2000, 100.0, 250, True),    # huge sell
            _trade(now - 3000, 100.0, 600, False),   # whale buy (also large + huge)
        ]
        r = tiered_large_flow(trades, now_ms=now, windows_min=(5,))
        flow = r["flow_5min"]
        # Cumulative tiers (legacy sol_deep_monitor.py:200-207): a 600-qty
        # trade counts as large AND huge AND whale.
        self.assertEqual(flow["large_buy_n"], 2)   # 60 + 600
        self.assertEqual(flow["huge_buy_n"], 1)    # 600
        self.assertEqual(flow["whale_buy_n"], 1)   # 600
        self.assertEqual(flow["huge_sell_n"], 1)   # 250

    def test_empty_trades(self):
        r = tiered_large_flow([], now_ms=1_000_000, windows_min=(5,))
        flow = r["flow_5min"]
        self.assertEqual(flow.get("large_buy_n", 0), 0)


if __name__ == "__main__":
    unittest.main()
