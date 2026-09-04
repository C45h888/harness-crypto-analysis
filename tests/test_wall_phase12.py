"""Phase 1 + Phase 2 wall calculation surface tests.

Covers the four parameterization wins (round anchors, tier config,
ATR-aware wall band, scorecard weights) and the five multi-bar trend
helpers wired into ``_wall_keystone_holds``.

Run::

    python -m pytest tests/test_wall_phase12.py -q
"""

from __future__ import annotations

import unittest

from market_service.analysis.wall_migration import (
    TierConfig,
    bid_tier_balance,
    compute_bid_tiers_usd,
    compute_round_anchors,
    default_wall_band,
    keystone_holds_scorecard,
    mega_at_keystone,
)
from market_service.calculations.orderbook import derive_round_anchors
from market_service.calculations.technical import (
    atr_pct_from_klines,
    trend_drift,
    trend_slope,
)


# ---------------------------------------------------------------------------
# Phase 1.1 — derive_round_anchors (dynamic, price-aware)
# ---------------------------------------------------------------------------


class DeriveRoundAnchorsTests(unittest.TestCase):
    def test_sol_at_150_includes_whole_and_half(self):
        anchors = derive_round_anchors(price=150.0, tick_size=0.01,
                                       step=0.05,
                                       depth_below=2, depth_above=2)
        levels = [a["level"] for a in anchors]
        # step=0.05; center=150.00; offsets -2..+2 cover 149.90..150.10
        self.assertIn(150.00, levels)
        self.assertIn(150.05, levels)
        self.assertIn(149.95, levels)

    def test_sol_at_150_with_step_050(self):
        anchors = derive_round_anchors(price=150.0, tick_size=0.01,
                                       step=0.50,
                                       depth_below=2, depth_above=2)
        levels = sorted({a["level"] for a in anchors})
        self.assertEqual(levels, [149.0, 149.5, 150.0, 150.5, 151.0])

    def test_btc_at_65000_with_10_dollar_step(self):
        anchors = derive_round_anchors(price=65000.0, tick_size=10.0,
                                       step=10.0,
                                       depth_below=3, depth_above=3)
        levels = sorted({a["level"] for a in anchors})
        self.assertEqual(levels, [64970.0, 64980.0, 64990.0,
                                  65000.0, 65010.0, 65020.0, 65030.0])

    def test_btc_at_65000_with_50_dollar_step(self):
        anchors = derive_round_anchors(price=65000.0, tick_size=10.0,
                                       step=50.0,
                                       depth_below=3, depth_above=3)
        levels = sorted({a["level"] for a in anchors})
        self.assertEqual(levels, [64850.0, 64900.0, 64950.0,
                                  65000.0, 65050.0, 65100.0, 65150.0])

    def test_negative_or_zero_price_rejected(self):
        with self.assertRaises(ValueError):
            derive_round_anchors(price=0.0, tick_size=0.01)
        with self.assertRaises(ValueError):
            derive_round_anchors(price=-1.0, tick_size=0.01)

    def test_zero_tick_rejected(self):
        with self.assertRaises(ValueError):
            derive_round_anchors(price=100.0, tick_size=0.0)

    def test_zero_step_rejected(self):
        with self.assertRaises(ValueError):
            derive_round_anchors(price=100.0, tick_size=0.01, step=0.0)

    def test_default_step_uses_tick_size(self):
        anchors = derive_round_anchors(price=100.0, tick_size=0.05,
                                       depth_below=1, depth_above=1)
        levels = sorted({a["level"] for a in anchors})
        self.assertEqual(levels, [99.95, 100.0, 100.05])

    def test_anchor_band_is_correct(self):
        anchors = derive_round_anchors(price=100.0, tick_size=0.01,
                                       step=0.10,
                                       depth_below=1, depth_above=1, tol=0.02)
        for a in anchors:
            self.assertEqual(a["lo"], a["level"] - 0.02)
            self.assertEqual(a["hi"], a["level"] + 0.02)
            self.assertEqual(a["tolerance"], 0.02)


# ---------------------------------------------------------------------------
# Phase 1.2 — TierConfig + compute_bid_tiers_usd
# ---------------------------------------------------------------------------


class TierConfigTests(unittest.TestCase):
    def test_default_thresholds_match_documented(self):
        cfg = TierConfig()
        self.assertEqual(cfg.mega_usd, 250_000.0)
        self.assertEqual(cfg.large_usd, 50_000.0)
        self.assertEqual(cfg.medium_usd, 10_000.0)

    def test_thresholds_materializes_correct_ladder(self):
        cfg = TierConfig(mega_usd=100, large_usd=50, medium_usd=10)
        ladder = cfg.thresholds()
        self.assertEqual(ladder[0], ("mega", 100, None))
        self.assertEqual(ladder[1], ("large", 50, 100))
        self.assertEqual(ladder[2], ("medium", 10, 50))
        self.assertEqual(ladder[3], ("small", None, 10))

    def test_to_dict_round_trip(self):
        cfg = TierConfig(mega_usd=999, large_usd=88, medium_usd=7)
        d = cfg.to_dict()
        self.assertEqual(d, {"mega_usd": 999, "large_usd": 88, "medium_usd": 7})


class ComputeBidTiersUsdTests(unittest.TestCase):
    def test_classifies_5_btc_as_mega(self):
        # 5 BTC at $65k = $325k notional -> mega at default threshold.
        bids = [[65000.0, 5.0]]
        tiers = compute_bid_tiers_usd(bids)
        self.assertEqual(tiers["mega"]["count"], 1)
        self.assertEqual(tiers["mega"]["qty"], 5.0)
        self.assertEqual(tiers["mega"]["notional"], 65000.0 * 5.0)

    def test_classifies_5_sol_as_small(self):
        # 5 SOL at $150 = $750 notional -> small at default threshold.
        bids = [[150.0, 5.0]]
        tiers = compute_bid_tiers_usd(bids)
        self.assertEqual(tiers["small"]["count"], 1)
        self.assertEqual(tiers["mega"]["count"], 0)

    def test_custom_threshold(self):
        bids = [[100.0, 50.0]]  # $5000 notional
        # Default: small ($5000 < $10000 medium threshold).
        tiers_default = compute_bid_tiers_usd(bids)
        self.assertEqual(tiers_default["small"]["count"], 1)
        # Custom: medium threshold raised above $5000 -> $5000 is now large.
        cfg = TierConfig(mega_usd=100_000, large_usd=5_000, medium_usd=1_000)
        tiers_custom = compute_bid_tiers_usd(bids, cfg)
        self.assertEqual(tiers_custom["large"]["count"], 1)
        self.assertAlmostEqual(tiers_custom["total_notional"], 5000.0)

    def test_unparseable_rows_ignored(self):
        tiers = compute_bid_tiers_usd(["garbage", [100.0, 50.0]])
        self.assertGreater(tiers["total_notional"], 0)

    def test_total_qty_and_notional_emitted(self):
        bids = [[100.0, 10.0], [101.0, 5.0]]
        tiers = compute_bid_tiers_usd(bids)
        self.assertEqual(tiers["total_qty"], 15.0)
        self.assertAlmostEqual(tiers["total_notional"], 100 * 10 + 101 * 5)

    def test_tier_config_in_output(self):
        tiers = compute_bid_tiers_usd([[100.0, 1.0]], TierConfig(mega_usd=99))
        self.assertEqual(tiers["tier_config"]["mega_usd"], 99)


class BidTierBalanceUsdTests(unittest.TestCase):
    def test_usd_threshold_classifies_by_notional(self):
        # 1 BTC bid ($65k) is large by default; 1 SOL ask ($150) is small.
        bids = [[65000.0, 1.0]]
        asks = [[150.0, 1.0]]
        cfg = TierConfig()
        result = bid_tier_balance(bids, asks, tier_config=cfg)
        # Neither side is mega at the default $250k threshold, so
        # the verdict is BALANCED.
        self.assertEqual(result["total_bids"], 1.0)
        self.assertEqual(result["total_asks"], 1.0)
        self.assertEqual(result["mega_bids"], 0.0)
        self.assertEqual(result["mega_threshold_usd"], cfg.mega_usd)
        self.assertEqual(result["verdict"], "BALANCED")

    def test_usd_path_with_mega_classification(self):
        # Use a TierConfig where $32.5k is mega so both sides are
        # classified mega and the verdict reflects the qty imbalance.
        cfg = TierConfig(mega_usd=30_000.0, large_usd=10_000.0, medium_usd=1_000.0)
        bids = [[65000.0, 2.0]]  # $130k notional -> mega
        asks = [[65000.0, 0.5]]  # $32.5k notional -> mega
        result = bid_tier_balance(bids, asks, tier_config=cfg)
        self.assertEqual(result["mega_bids"], 2.0)
        self.assertEqual(result["mega_asks"], 0.5)
        self.assertEqual(result["verdict"], "INSTITUTIONAL-BID-HEAVY")

    def test_legacy_raw_qty_path_still_works(self):
        bids = [[100.0, 6000.0]]
        asks = [[101.0, 100.0]]
        result = bid_tier_balance(bids, asks, mega_threshold=5000.0)
        self.assertEqual(result["mega_bids"], 6000.0)
        self.assertEqual(result["verdict"], "INSTITUTIONAL-BID-HEAVY")


class MegaAtKeystoneUsdTests(unittest.TestCase):
    def test_usd_threshold_filters_by_notional(self):
        bids = [
            [75.05, 6000.0],   # 75.05 * 6000 = $450k -> mega by default
            [75.10, 100.0],    # $7.5k -> medium, not mega
        ]
        cfg = TierConfig()
        result = mega_at_keystone(bids, 75.05, tier_config=cfg)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["threshold_usd"], cfg.mega_usd)


# ---------------------------------------------------------------------------
# Phase 1.3 — default_wall_band (ATR-aware)
# ---------------------------------------------------------------------------


class DefaultWallBandTests(unittest.TestCase):
    def test_floor_when_atr_below_threshold(self):
        band = default_wall_band(price=100.0, atr_pct=0.3)
        self.assertAlmostEqual(band["band_bps"], 30.0)
        self.assertAlmostEqual(band["bid_floor"], 100 * 0.997)
        self.assertAlmostEqual(band["ask_target"], 100 * 1.003)
        # entry is the simple midpoint (price itself).
        self.assertAlmostEqual(band["entry"], 100.0)

    def test_ceiling_when_atr_above_threshold(self):
        band = default_wall_band(price=100.0, atr_pct=3.0)
        self.assertAlmostEqual(band["band_bps"], 300.0)
        self.assertAlmostEqual(band["ask_target"], 100 * 1.03)
        self.assertEqual(band["scaling_source"], "atr_ceiling")

    def test_linear_interpolation_in_middle(self):
        band = default_wall_band(price=100.0, atr_pct=1.25)
        # Span = 1.5, t = (1.25-0.5)/1.5 = 0.5; ceiling = min(300, 100*3)=300
        # band_bps = 30 + 0.5 * (300-30) = 165.
        self.assertAlmostEqual(band["band_bps"], 165.0)
        self.assertEqual(band["scaling_source"], "atr_linear")

    def test_no_atr_falls_back_to_floor(self):
        band = default_wall_band(price=100.0, atr_pct=None)
        self.assertAlmostEqual(band["band_bps"], 30.0)
        self.assertEqual(band["scaling_source"], "default_floor")

    def test_invalid_price_rejected(self):
        with self.assertRaises(ValueError):
            default_wall_band(price=0.0, atr_pct=1.0)
        with self.assertRaises(ValueError):
            default_wall_band(price=-1.0, atr_pct=1.0)

    def test_invalid_bps_thresholds_rejected(self):
        with self.assertRaises(ValueError):
            default_wall_band(price=100.0, atr_pct=1.0, floor_bps=0)
        with self.assertRaises(ValueError):
            default_wall_band(price=100.0, atr_pct=1.0, ceiling_bps=-5)
        with self.assertRaises(ValueError):
            default_wall_band(price=100.0, atr_pct=1.0,
                              floor_bps=200, ceiling_bps=100)


class RoundAnchorsDynamicTests(unittest.TestCase):
    def test_compute_round_anchors_with_dynamic_list(self):
        anchors = [
            {"name": "anchor_75_00", "level": 75.00, "lo": 74.98, "hi": 75.02},
            {"name": "anchor_74_50", "level": 74.50, "lo": 74.48, "hi": 74.52},
        ]
        bids = [[75.00, 1000.0], [74.98, 500.0], [74.50, 2000.0]]
        result = compute_round_anchors(bids, anchors=anchors)
        self.assertEqual(result["anchor_source"], "dynamic")
        self.assertEqual(result["count"], 2)
        a_75 = next(a for a in result["anchors"] if a["name"] == "anchor_75_00")
        self.assertEqual(a_75["qty"], 1500.0)
        a_74_50 = next(a for a in result["anchors"] if a["name"] == "anchor_74_50")
        self.assertEqual(a_74_50["qty"], 2000.0)

    def test_legacy_fallback_marked(self):
        result = compute_round_anchors([[75.0, 1000.0]])
        self.assertEqual(result["anchor_source"], "legacy_fallback")
        self.assertEqual(result["count"], 11)


# ---------------------------------------------------------------------------
# Phase 2.1 — trend_slope + trend_drift helpers
# ---------------------------------------------------------------------------


class TrendSlopeTests(unittest.TestCase):
    def test_rising_series_has_positive_slope(self):
        s = trend_slope([1.0, 2.0, 3.0], n=3)
        self.assertAlmostEqual(s, 1.0)

    def test_falling_series_has_negative_slope(self):
        s = trend_slope([3.0, 2.0, 1.0], n=3)
        self.assertAlmostEqual(s, -1.0)

    def test_flat_series_has_zero_slope(self):
        s = trend_slope([5.0, 5.0, 5.0], n=3)
        self.assertAlmostEqual(s, 0.0)

    def test_too_few_returns_none(self):
        self.assertIsNone(trend_slope([1.0, 2.0], n=3))
        self.assertIsNone(trend_slope([], n=3))

    def test_skips_none_entries(self):
        s = trend_slope([None, 1.0, 2.0, 3.0], n=3)
        self.assertAlmostEqual(s, 1.0)

    def test_n_must_be_positive(self):
        with self.assertRaises(ValueError):
            trend_slope([1.0, 2.0], n=1)
        with self.assertRaises(ValueError):
            trend_slope([1.0, 2.0], n=0)


class TrendDriftTests(unittest.TestCase):
    def test_rising_drift(self):
        self.assertAlmostEqual(trend_drift([1.0, 2.0, 4.0], n=3), 3.0)

    def test_falling_drift(self):
        self.assertAlmostEqual(trend_drift([4.0, 2.0, 1.0], n=3), -3.0)

    def test_too_few_returns_none(self):
        self.assertIsNone(trend_drift([1.0, 2.0], n=3))
        self.assertIsNone(trend_drift([], n=3))


# ---------------------------------------------------------------------------
# Phase 2.1+ — ATR from klines
# ---------------------------------------------------------------------------


class AtrPctTests(unittest.TestCase):
    def _k(self, ts, o, h, l, c):
        return [ts, o, h, l, c, 0]

    def test_atr_pct_from_quiet_series_is_small(self):
        klines = [self._k(i, 100.0, 100.1, 99.9, 100.0) for i in range(20)]
        atr = atr_pct_from_klines(klines, period=14)
        self.assertIsNotNone(atr)
        self.assertAlmostEqual(atr, 0.2, places=4)

    def test_atr_pct_from_volatile_series_is_large(self):
        klines = [self._k(i, 100.0, 110.0, 90.0, 100.0) for i in range(20)]
        atr = atr_pct_from_klines(klines, period=14)
        self.assertAlmostEqual(atr, 20.0, places=2)

    def test_returns_none_when_too_few_candles(self):
        atr = atr_pct_from_klines([self._k(0, 100, 101, 99, 100)], period=14)
        self.assertIsNone(atr)

    def test_handles_missing_or_short_rows(self):
        klines = [self._k(0, 100, 101, 99, 100),
                  None,
                  self._k(2, 101, 110, 100, 105)]
        atr = atr_pct_from_klines(klines + [self._k(i, 105, 110, 100, 105)
                                            for i in range(3, 20)], period=14)
        self.assertIsNotNone(atr)


# ---------------------------------------------------------------------------
# Phase 2.4 — keystone_holds_scorecard with weights
# ---------------------------------------------------------------------------


class KeystoneHoldsScorecardWeightsTests(unittest.TestCase):
    def test_default_weights_match_legacy_score(self):
        # All factors maxed: raw=10, score=10 (100% probability).
        s = keystone_holds_scorecard(
            bid_ask_qty_ratio=0.6, latest_tbr=0.6,
            oi_chg_5m=0.5, top_long_pct=0.70, net_buy_ratio=0.6,
        )
        self.assertEqual(s["score"], 10.0)
        self.assertEqual(s["raw_score"], 10.0)
        self.assertEqual(s["max_weighted"], 10.0)
        self.assertEqual(s["keystone_holds_probability"], 100.0)

    def test_zero_score_when_all_factors_zero(self):
        s = keystone_holds_scorecard(
            bid_ask_qty_ratio=0.0, latest_tbr=0.0,
            oi_chg_5m=-0.5, top_long_pct=1.0, net_buy_ratio=0.0,
        )
        self.assertEqual(s["score"], 0.0)
        self.assertEqual(s["keystone_holds_probability"], 0.0)

    def test_oi_zero_is_a_positive_signal(self):
        s = keystone_holds_scorecard(
            bid_ask_qty_ratio=0.0, latest_tbr=0.0,
            oi_chg_5m=0.0, top_long_pct=1.0, net_buy_ratio=0.0,
        )
        self.assertEqual(s["score"], 1.0)
        self.assertEqual(s["raw_score"], 1.0)

    def test_default_weights_match_legacy_partial_score(self):
        # 3 of 5 factors maxed -> raw=6 -> score=6 (60%).
        s = keystone_holds_scorecard(
            bid_ask_qty_ratio=0.6, latest_tbr=0.4,  # not maxed
            oi_chg_5m=0.5, top_long_pct=0.70, net_buy_ratio=0.4,  # not maxed
        )
        self.assertEqual(s["raw_score"], 6.0)
        self.assertEqual(s["max_weighted"], 10.0)
        self.assertEqual(s["score"], 6.0)
        self.assertEqual(s["keystone_holds_probability"], 60.0)

    def test_weighted_zero_weight_disables_factor(self):
        # Setting weight=0 for a maxed factor removes its contribution.
        s = keystone_holds_scorecard(
            bid_ask_qty_ratio=0.6, latest_tbr=0.6,
            oi_chg_5m=0.5, top_long_pct=0.70, net_buy_ratio=0.6,
            weights={"taker_buy_trend": 0.0},
        )
        # raw_score = 2 + 0 + 2 + 2 + 2 = 8
        # max_weighted = 2 + 0 + 2 + 2 + 2 = 8
        self.assertEqual(s["raw_score"], 8.0)
        self.assertEqual(s["max_weighted"], 8.0)
        self.assertEqual(s["score"], 10.0)

    def test_factor_breakdown_records_each_input(self):
        s = keystone_holds_scorecard(
            bid_ask_qty_ratio=0.6, latest_tbr=0.6,
            oi_chg_5m=0.5, top_long_pct=0.70, net_buy_ratio=0.6,
        )
        self.assertIn("factor_breakdown", s)
        for k in ("bid_ask_qty_ratio", "taker_buy_trend", "oi_change_trend",
                  "top_long_drift", "net_buy_trend"):
            self.assertIn(k, s["factor_breakdown"])
            self.assertIn("value", s["factor_breakdown"][k])
            self.assertIn("weight", s["factor_breakdown"][k])
            self.assertIn("weighted_contribution", s["factor_breakdown"][k])

    def test_legacy_signature_unchanged(self):
        # Positional-only call (no weights) should match legacy exactly.
        # Legacy tally:
        #   bid_ask=0.5  -> 0.5 > 0.3 True  -> +1 raw
        #   tbr=0.5      -> neither        -> +0 raw
        #   oi=0.0       -> 0.0 > -0.1 True -> +1 raw
        #   top_long=0.7 -> 0.7 < 0.75 True -> +2 raw
        #   net_buy=0.5  -> neither        -> +0 raw
        # raw_score = 4, prob = 40%.
        s = keystone_holds_scorecard(0.5, 0.5, 0.0, 0.7, 0.5)
        self.assertEqual(s["score"], 4.0)
        self.assertEqual(s["raw_score"], 4.0)
        self.assertEqual(s["keystone_holds_probability"], 40.0)

    def test_invalid_weight_ignored(self):
        weights = {"bid_ask_qty_ratio": "not-a-number"}
        s = keystone_holds_scorecard(
            bid_ask_qty_ratio=0.6, latest_tbr=0.6,
            oi_chg_5m=0.5, top_long_pct=0.70, net_buy_ratio=0.6,
            weights=weights,
        )
        self.assertEqual(s["weights_used"]["bid_ask_qty_ratio"], 1.0)


# ---------------------------------------------------------------------------
# Phase 2.4 — Settings env-var loaders
# ---------------------------------------------------------------------------


class SettingsWallConfigTests(unittest.TestCase):
    def test_default_tier_config(self):
        import os
        os.environ.pop("WALL_TIER_CONFIG", None)
        from market_service.config import _parse_tier_config
        cfg = _parse_tier_config(None)
        self.assertEqual(cfg["mega_usd"], 250_000.0)

    def test_default_tier_config_via_env_default_keyword(self):
        from market_service.config import _parse_tier_config
        cfg = _parse_tier_config("default")
        self.assertEqual(cfg["mega_usd"], 250_000.0)

    def test_json_tier_config_override(self):
        from market_service.config import _parse_tier_config
        cfg = _parse_tier_config('{"mega_usd": 1000000}')
        self.assertEqual(cfg["mega_usd"], 1_000_000.0)
        self.assertEqual(cfg["large_usd"], 50_000.0)

    def test_malformed_tier_config_raises(self):
        from market_service.config import _parse_tier_config
        with self.assertRaises(ValueError):
            _parse_tier_config("not-json")

    def test_default_scorecard_weights(self):
        from market_service.config import _parse_scorecard_weights
        w = _parse_scorecard_weights(None)
        self.assertEqual(len(w), 5)
        self.assertEqual(w["bid_ask_qty_ratio"], 1.0)

    def test_json_scorecard_weights_override(self):
        from market_service.config import _parse_scorecard_weights
        w = _parse_scorecard_weights(
            '{"taker_buy_trend": 2.5, "oi_change_trend": 0.5}',
        )
        self.assertEqual(w["taker_buy_trend"], 2.5)
        self.assertEqual(w["oi_change_trend"], 0.5)
        self.assertEqual(w["bid_ask_qty_ratio"], 1.0)

    def test_unknown_scorecard_factor_ignored(self):
        from market_service.config import _parse_scorecard_weights
        w = _parse_scorecard_weights('{"unknown_factor": 99.0}')
        self.assertNotIn("unknown_factor", w)


if __name__ == "__main__":
    unittest.main()
