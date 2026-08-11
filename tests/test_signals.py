import unittest

from market_service.calculations.flow import bucketed_cvd, summarize
from market_service.calculations.signals import deterministic_signals


def snapshot(**overrides):
    base = {"spot_buy_share": 0.5, "futures_buy_share": 0.5, "spot_obi_top_n": 0.0, "open_interest": 100.0}
    base.update(overrides)
    return base


class DeterministicSignalTests(unittest.TestCase):
    def test_emits_divergence_when_it_first_appears(self):
        signals = deterministic_signals(snapshot(spot_buy_share=0.3, futures_buy_share=0.7), snapshot())
        self.assertIn("spot_futures_divergence", [signal["signal_type"] for signal in signals])

    def test_does_not_repeat_unchanged_divergence(self):
        previous = snapshot(spot_buy_share=0.3, futures_buy_share=0.7)
        signals = deterministic_signals(snapshot(spot_buy_share=0.2, futures_buy_share=0.8), previous)
        self.assertNotIn("spot_futures_divergence", [signal["signal_type"] for signal in signals])

    def test_emits_depth_event_when_threshold_is_crossed(self):
        signals = deterministic_signals(snapshot(spot_obi_top_n=0.5), snapshot(spot_obi_top_n=0.1))
        self.assertIn("spot_depth_imbalance", [signal["signal_type"] for signal in signals])

    def test_live_flow_includes_quote_cvd_and_book_metrics(self):
        trades = [
            {"ts": 1, "price": 100.0, "qty": 2.0, "is_buyer_maker": False},
            {"ts": 2, "price": 101.0, "qty": 1.0, "is_buyer_maker": True},
        ]
        result = summarize(trades, {"bids": [["99", "10"]], "asks": [["101", "5"]]})
        self.assertEqual(result["cvd"], 1.0)
        self.assertEqual(result["cvd_usd"], 99.0)
        self.assertAlmostEqual(result["obi"], (990 - 505) / (990 + 505))
        self.assertAlmostEqual(result["spread_bps"], (2 / 100) * 10000)
        self.assertAlmostEqual(result["buy_share"], 2 / 3)
        self.assertEqual(result["buy_notional_usd"], 200.0)
        self.assertEqual(result["sell_notional_usd"], 101.0)

    def test_bucketed_cvd_preserves_quote_currency_evidence(self):
        trades = [
            {"ts": 60000, "price": 100.0, "qty": 2.0, "is_buyer_maker": False},
            {"ts": 61000, "price": 101.0, "qty": 1.0, "is_buyer_maker": True},
        ]
        bucket = bucketed_cvd(trades, window_s=60)[0]
        self.assertEqual(bucket["delta"], 1.0)
        self.assertEqual(bucket["delta_usd"], 99.0)
        self.assertEqual(bucket["buy_usd"], 200.0)
        self.assertEqual(bucket["sell_usd"], 101.0)

    def test_signal_evidence_contains_rule_and_inputs(self):
        signals = deterministic_signals(snapshot(spot_buy_share=0.3, futures_buy_share=0.7), snapshot())
        evidence = signals[0]["evidence"]
        self.assertIn("rule", evidence)
        self.assertEqual(evidence["spot_buy_share"], 0.3)
        self.assertEqual(evidence["futures_buy_share"], 0.7)


if __name__ == "__main__":
    unittest.main()
