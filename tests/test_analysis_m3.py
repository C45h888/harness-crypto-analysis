import unittest

from market_service.analysis.path_absorption import (
    cum_ask_to,
    cum_bid_to,
    fall_short_level,
    fuel_ratio,
    simulated_ascent,
    simulated_descent,
    tbr_oi_combo,
)
from market_service.analysis.stage import infer_stage, vol_direction_split
from market_service.analysis.wall_migration import (
    depth_qty,
    fuel_ratio as wall_fuel,
    keystone_holds_scorecard,
    level_absorption,
    wall_delta,
    wall_trap_assessment,
)

BIDS = [[100.0, 500.0], [99.9, 100.0], [99.8, 200.0], [99.7, 300.0]]
ASKS = [[100.1, 400.0], [100.2, 100.0], [100.3, 200.0]]


class WallMigrationTests(unittest.TestCase):
    def test_depth_qty(self):
        self.assertEqual(depth_qty(BIDS, 99.8, 100.0), 800.0)

    def test_wall_delta_directions(self):
        prior = {100.2: 100.0, 100.3: 1000.0}
        result = wall_delta(prior, ASKS, window=0.05)
        by_level = {w["level"]: w["direction"] for w in result["walls"]}
        self.assertEqual(by_level[100.2], "STABLE")  # now 100 == prior 100
        self.assertEqual(by_level[100.3], "ERODED")  # now 200 < prior 1000

    def test_fuel_ratio_verdict(self):
        fr = wall_fuel(BIDS, ASKS, price=100.05, bid_floor=99.7, ask_target=100.3)
        self.assertIn(fr["verdict"], ("BUYERS_HAVE_FUEL", "SELLERS_DOMINATE"))
        self.assertGreater(fr["ratio"], 0)

    def test_level_absorption_no_bid(self):
        out = level_absorption(BIDS, [100.0, 99.6], buffer=0.01)
        self.assertFalse(out[0]["no_bid"])
        self.assertTrue(out[1]["no_bid"])

    def test_trap_and_scoring(self):
        trap = wall_trap_assessment(2.0, ask_walls_built=0)
        self.assertEqual(trap["probability_absorb"], 0.65)
        s = keystone_holds_scorecard(bid_ask_qty_ratio=0.6, latest_tbr=0.6,
                                     oi_chg_5m=0.2, top_long_pct=0.7, net_buy_ratio=0.6)
        self.assertGreater(s["score"], 0)


class PathAbsorptionTests(unittest.TestCase):
    def test_cum_and_ascent(self):
        self.assertEqual(cum_ask_to(ASKS, 100.3)[0], 700.0)  # all asks
        self.assertEqual(cum_bid_to(BIDS, 99.8)[0], 800.0)  # 100, 99.9, 99.8
        fr = fuel_ratio(BIDS, ASKS, entry=100.3, bid_floor=99.7)
        self.assertIn("fuel_ratio", fr)
        ascent = simulated_ascent(ASKS, total_bid_fuel=600.0, levels=[100.2, 100.5])
        self.assertEqual(ascent[0]["verdict"], "BUYERS_REACH")
        self.assertEqual(ascent[1]["verdict"], "BUYERS_EXHAUSTED")
        self.assertEqual(fall_short_level(ascent), 100.5)

    def test_descent_and_combo(self):
        descent = simulated_descent(BIDS, total_ask_fuel=700.0, total_bid_fuel=1100.0, levels=[99.9])
        self.assertIn(descent[0]["verdict"], ("SHORT VALID", "SHORT OK (tight)", "SQUEEZE RISK"))
        self.assertEqual(tbr_oi_combo("BUY PRESSURE BUILDING", "OI RISING"),
                         "NEW LONGS OPENING (real buyer demand)")


class StageTests(unittest.TestCase):
    def test_vol_direction_split(self):
        klines = [[1, 100.0, 102.0, 99.0, 101.0, 10.0],
                  [2, 100.0, 102.0, 99.0, 99.0, 5.0]]
        up, down, pct = vol_direction_split(klines)
        self.assertEqual(up, 10.0)
        self.assertEqual(down, 5.0)
        self.assertAlmostEqual(pct, 66.666, places=2)

    def test_infer_stage_markup(self):
        out = infer_stage(px_chg_4h=2.0, oi_chg_4h=1.0, up_pct_4h=60.0,
                          up_steps=3, down_steps=1)
        self.assertIn(out["stage"], ("MARKUP", "TRANSITION / UNCLEAR"))
        self.assertGreaterEqual(out["score"], 3)


if __name__ == "__main__":
    unittest.main()