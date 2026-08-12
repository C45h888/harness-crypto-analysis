import unittest

from market_service.analysis.macro import idiosyncratic
from market_service.analysis.oi import (
    find_walls,
    oi_implied_value,
    oi_inflow_outflow,
    oi_weighted_contracts,
    wall_break_assessment,
)


class OiFoldsTests(unittest.TestCase):
    def test_find_walls_ranks_ask_cluster(self):
        asks = [[100.2, 500.0], [100.21, 200.0], [100.5, 100.0], [101.0, 900.0]]
        walls = find_walls(asks, price=100.0, lo_dist=0.05, hi_dist=0.50, window=0.05)
        self.assertGreaterEqual(len(walls), 2)
        self.assertGreaterEqual(walls[0]["qty"], walls[1]["qty"])

    def test_wall_break_verdict(self):
        ample = wall_break_assessment(total_wall_sol=1000, buy_per_min=200, peak_buy_per_min=300)
        self.assertEqual(ample["verdict"], "BUYERS_CAN_BREAK")
        weak = wall_break_assessment(total_wall_sol=10000, buy_per_min=10, peak_buy_per_min=30)
        self.assertEqual(weak["verdict"], "CANNOT_BREAK_VIA_BUY_FLOW")

    def test_oi_implied_value_change(self):
        rows = [{"bucket": b, "oi": 1000.0, "oi_value": 100000.0} for b in range(2)]
        out = oi_implied_value(rows)
        # $100/contract both bars -> 0% change
        self.assertAlmostEqual(out["implied_per_contract_change_pct"], 0.0)
        self.assertEqual(out["bars"][0]["implied_per_contract"], 100.0)

    def test_oi_weighted_contracts(self):
        out = oi_weighted_contracts(1000.0, top_long_pct=0.6, global_long_pct=0.5)
        self.assertEqual(out["top_long_contracts"], 600.0)
        self.assertEqual(out["global_short_contracts"], 500.0)

    def test_oi_inflow_outflow(self):
        series = [float(i) for i in range(1, 200)]
        out = oi_inflow_outflow(series, windows_bars=(12,))
        self.assertIn("1h", out)
        self.assertGreater(out["1h"]["change"], 0)


class MacroFoldsTests(unittest.TestCase):
    def test_idiosyncratic_label(self):
        # SOL up 5%, BTC up 2%, beta 1.5 -> expected 3%, residual +2% -> OUTPERFORMING
        sol = [0.01] * 5
        btc = [0.004] * 5
        out = idiosyncratic(sol, btc, beta=1.5)
        self.assertEqual(out["label"], "OUTPERFORMING")
        # compounded: SOL 5.10%, BTC 2.02%, expected 3.02%, residual ~ +2.08%
        self.assertGreater(out["residual_percent"], 1.0)


if __name__ == "__main__":
    unittest.main()
