import unittest

from market_service.analysis.auction import (
    auction_verdict,
    flow_persistence,
    initiated_flow,
    microprice,
)
from market_service.analysis.demand import demand_verdict, macro_climate
from market_service.analysis.regime import regime_verdict


def _trades(n_buy=0, n_sell=0, price=100.0, qty=1.0):
    out = []
    for _ in range(n_buy):
        out.append({"ts": 1, "price": price, "qty": qty, "is_buyer_maker": False})
    for _ in range(n_sell):
        out.append({"ts": 1, "price": price, "qty": qty, "is_buyer_maker": True})
    return out


class AuctionTests(unittest.TestCase):
    def test_microprice_and_initiated_flow(self):
        book = {"bids": [[100.0, 10.0]], "asks": [[102.0, 10.0]]}
        mp = microprice(book)
        self.assertIn("skew_bps", mp)
        self.assertIn("depth", mp)
        fl = initiated_flow(_trades(n_buy=3, n_sell=1))
        self.assertAlmostEqual(fl["buy_share"], 0.75)
        self.assertEqual(fl["cvd_qty"], 2.0)

    def test_flow_persistence_buy_acceleration(self):
        # 30 trades, first 15 (older) mostly sell, next 15 (recent) mostly buy
        older = _trades(n_buy=2, n_sell=13)
        recent = _trades(n_buy=13, n_sell=2)
        per = flow_persistence(recent + older)  # trades newest-first
        self.assertEqual(per["trend"], "accelerating-buy")

    def test_auction_verdict_deterministic(self):
        buy_book = {"bids": [[100.0, 10.0], [99.9, 1.0]], "asks": [[102.0, 1.0], [103.0, 1.0]]}
        sell_heavy = _trades(n_buy=2, n_sell=18)
        buy_heavy = _trades(n_buy=18, n_sell=2)
        state = {
            "spot": {"microprice": microprice(buy_book),
                     "flow": initiated_flow(buy_heavy),
                     "persistence": flow_persistence(list(reversed(buy_heavy + buy_heavy)))},
            "futures": {"microprice": microprice({"bids": [], "asks": []}),
                        "flow": initiated_flow([]),
                        "persistence": flow_persistence([])},
            "derivatives": {"funding": 0.0005, "taker_buy_ratio": 1.2, "oi_change_pct": 2.0},
        }
        verdict, reasons = auction_verdict(state)
        self.assertIsInstance(verdict, str)
        self.assertIsInstance(reasons, list)


class DemandTests(unittest.TestCase):
    def test_demand_verdict_spot_bid(self):
        dx = {
            "spot": {"cvd": 100.0, "buy_share": 0.7},
            "futures": {"cvd": 50.0, "buy_share": 0.6},
            "derivs": {"funding": -0.0002, "long_pct": 0.5, "top_long_pct": 0.6, "oi": 1000},
        }
        verdict, reasons = demand_verdict(dx)
        self.assertIn("SPOT-DEMAND", verdict)
        self.assertIsInstance(reasons, list)

    def test_macro_climate_risk_off(self):
        macro = {"btc": {"change_pct": -2.0, "funding": 0}, "eth": {"change_pct": -2.5, "funding": 0}}
        self.assertIn("RISK-OFF", macro_climate(macro, target_change_pct=-1.0))


class RegimeTests(unittest.TestCase):
    def test_regime_verdict_trend_up(self):
        d = {
            "futures": {
                "flow": {"last_price": 100.5, "vwap": 100.0, "cvd": 50.0, "obi": 0.1,
                         "buy_sell_ratio": 1.5, "large_trades": [{"side": "buy", "notional_usd": 1000}]},
                "open_interest": {"open_interest": "1000"},
                "funding": {"last_funding_rate": "0.0003"},
                "mark_price": {},
                "long_short_ratio": {},
                "top_long_short_accounts": {},
            }
        }
        regime, reasons = regime_verdict(d)
        self.assertIn("TREND-UP", regime)
        self.assertIsInstance(reasons, list)


if __name__ == "__main__":
    unittest.main()
