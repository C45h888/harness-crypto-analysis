"""Tests for the new wall_migration.py functions:

- bid_tier_balance (institutional_buyers.py:167-171)
- mega_at_keystone (institutional_buyers.py:129)

Plus regression coverage for the promoted compute_bid_tiers /
compute_round_anchors which used to live inline in pipeline.py.
"""

from __future__ import annotations

import unittest

from market_service.analysis.wall_migration import (
    bid_tier_balance,
    compute_bid_tiers,
    compute_round_anchors,
    mega_at_keystone,
)


class ComputeBidTiersTests(unittest.TestCase):
    def test_basic_thresholds(self):
        bids = [
            (75.00, 6000),  # mega
            (74.50, 4500),  # large
            (75.10, 1500),  # large
            (75.20, 800),   # medium
            (75.30, 100),   # small
        ]
        tiers = compute_bid_tiers(bids)
        self.assertEqual(tiers["mega"]["count"], 1)
        self.assertEqual(tiers["large"]["count"], 2)
        self.assertEqual(tiers["medium"]["count"], 1)
        self.assertEqual(tiers["small"]["count"], 1)
        self.assertAlmostEqual(tiers["mega"]["qty"], 6000)
        self.assertAlmostEqual(tiers["mega"]["notional"], 75.00 * 6000)
        # pct should be 6000 / total
        total = 6000 + 4500 + 1500 + 800 + 100
        self.assertAlmostEqual(tiers["mega"]["pct"], 6000 / total * 100, places=4)

    def test_empty_bids(self):
        tiers = compute_bid_tiers([])
        self.assertEqual(tiers["total_qty"], 0)
        for tier in ("mega", "large", "medium", "small"):
            self.assertEqual(tiers[tier]["pct"], 0.0)

    def test_unparseable_rows_fall_into_small(self):
        bids = [(75.0, 50), "garbage", (75.1, 6000), (None, 100)]
        tiers = compute_bid_tiers(bids)
        self.assertEqual(tiers["mega"]["count"], 1)


class ComputeRoundAnchorsTests(unittest.TestCase):
    def test_basic_round_anchor_match(self):
        bids = [
            (75.00, 1000),  # matches anchor_75_00
            (74.98, 500),   # matches anchor_75_00 (within 0.02)
            (74.50, 2000),  # matches anchor_74_50
        ]
        anchors = compute_round_anchors(bids)
        self.assertEqual(anchors["count"], len(anchors["anchors"]))
        # Find anchor_75_00 result
        a_75 = next(a for a in anchors["anchors"] if a["name"] == "anchor_75_00")
        self.assertEqual(a_75["qty"], 1500)  # 1000 + 500
        a_74_50 = next(a for a in anchors["anchors"] if a["name"] == "anchor_74_50")
        self.assertEqual(a_74_50["qty"], 2000)


class BidTierBalanceTests(unittest.TestCase):
    def test_institutional_bid_heavy(self):
        bids = [(75.0, 6000), (75.1, 5500), (75.2, 100)]
        asks = [(75.3, 1000)]
        result = bid_tier_balance(bids, asks, mega_threshold=5000)
        self.assertEqual(result["mega_bids"], 11500)
        self.assertEqual(result["mega_asks"], 0)
        self.assertEqual(result["verdict"], "INSTITUTIONAL-BID-HEAVY")

    def test_institutional_ask_heavy(self):
        bids = [(75.0, 500)]
        asks = [(75.3, 8000), (75.4, 6000)]
        result = bid_tier_balance(bids, asks, mega_threshold=5000)
        self.assertEqual(result["mega_asks"], 14000)
        self.assertEqual(result["mega_bids"], 0)
        self.assertEqual(result["verdict"], "INSTITUTIONAL-ASK-HEAVY")

    def test_balanced(self):
        bids = [(75.0, 6000), (75.1, 5500)]
        asks = [(75.3, 6000), (75.4, 5500)]
        result = bid_tier_balance(bids, asks, mega_threshold=5000)
        self.assertEqual(result["verdict"], "BALANCED")
        self.assertEqual(result["delta"], 0)
        self.assertAlmostEqual(result["ratio"], 1.0, places=4)

    def test_mega_threshold_filters_out_small_levels(self):
        bids = [(75.0, 100), (75.1, 200)]  # only small/large, no mega
        asks = [(75.3, 6000)]              # one mega
        result = bid_tier_balance(bids, asks, mega_threshold=5000)
        self.assertEqual(result["mega_bids"], 0)
        self.assertEqual(result["mega_asks"], 6000)
        self.assertEqual(result["verdict"], "INSTITUTIONAL-ASK-HEAVY")

    def test_zero_mega_on_both_sides_is_balanced(self):
        bids = [(75.0, 100), (75.1, 200)]
        asks = [(75.3, 100)]
        result = bid_tier_balance(bids, asks, mega_threshold=5000)
        self.assertEqual(result["verdict"], "BALANCED")


class MegaAtKeystoneTests(unittest.TestCase):
    def test_finds_mega_bids_in_window(self):
        bids = [
            (75.05, 6000),  # mega, within [74.95, 75.15]
            (75.10, 5500),  # mega, within window
            (75.20, 6000),  # mega, OUTSIDE window
            (75.05, 100),   # small, in window — should be excluded
        ]
        result = mega_at_keystone(bids, 75.05, threshold=5000, tol=0.10)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["qty"], 11500)
        self.assertEqual(result["keystone_price"], 75.05)
        self.assertEqual(result["tol"], 0.10)

    def test_no_mega_in_window(self):
        bids = [(75.05, 100), (75.10, 200)]
        result = mega_at_keystone(bids, 75.05, threshold=5000, tol=0.10)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["qty"], 0)

    def test_empty_bids(self):
        result = mega_at_keystone([], 75.0, threshold=5000, tol=0.10)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["qty"], 0)

    def test_levels_sorted_by_price(self):
        bids = [(75.10, 5500), (75.05, 6000), (75.08, 7000)]
        result = mega_at_keystone(bids, 75.05, threshold=5000, tol=0.10)
        prices = [l["price"] for l in result["levels"]]
        self.assertEqual(prices, sorted(prices))

    def test_negative_tol_rejected(self):
        with self.assertRaises(ValueError):
            mega_at_keystone([], 75.0, threshold=5000, tol=-0.1)


if __name__ == "__main__":
    unittest.main()

class TestPipelineWiring:
    """Verify the wired legacy-parity signals are emitted by _adapt_wall_migration."""

    def _evidence(self):
        from market_service.nooa_harness.pipeline import _adapt_wall_migration
        bids = [[float(p), f] for p, f in [(100.00, 50), (99.80, 40), (99.70, 30),
                                           (99.85, 60), (99.60, 20)]]
        asks = [[float(p), f] for p, f in [(100.20, 30), (100.30, 80), (100.40, 50),
                                           (100.50, 70), (100.10, 25)]]
        trades = [
            {"ts": 1700000000000, "qty": 10.0, "is_buyer_maker": False, "price": 100.0},
            {"ts": 1700000060000, "qty": 8.0, "is_buyer_maker": True, "price": 100.2},
        ]
        return {
            "futures": {
                "order_book": {"bids": bids, "asks": asks},
                "trades_normalized": trades,
                "taker_buy_sell": [{"buyVol": 100, "sellVol": 80}],
                "oi_history": [{"sumOpenInterest": 1000}, {"sumOpenInterest": 1100}],
                "top_ls": [{"longAccount": 0.70}],
            }
        }

    def test_new_wall_keys_present(self):
        from market_service.nooa_harness.pipeline import _adapt_wall_migration
        evidence = self._evidence()
        out = _adapt_wall_migration(evidence, [], {}, None,
                                    orderbook={"fut_keystone": {"keystone": 99.80}},
                                    depth=10)
        for key in ("keystone_wall_balance", "keystone_holds_scorecard",
                    "level_absorption", "wall_break", "zone_ratio_grid",
                    "zone_buy_sell"):
            assert key in out, f"missing {key}"
        assert "keystone_holds_probability" in out["keystone_holds_scorecard"] or "score" in out["keystone_holds_scorecard"]
        assert isinstance(out["zone_ratio_grid"], list)
        assert isinstance(out["wall_break"], dict)
