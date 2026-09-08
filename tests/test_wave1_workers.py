"""Tape / large-print / delta worker tests — Wave 1 probe truth tables.

Pattern: tests/test_density_worker.py — fire / quiet / determinism /
insufficient-input cases, plus compute-shape contracts. Probes run against
plain window dicts through the _NoStore seam (no Redis touched).
"""

from __future__ import annotations

import unittest
from typing import Any

from market_service.substrate_worker.delta_worker import DeltaWorker
from market_service.substrate_worker.large_print_worker import (
    LARGE_TIER_QTY,
    LargePrintWorker,
)
from market_service.substrate_worker.tape_worker import TapeWorker
from tests.test_density_worker import _NoStore

BOOK = {
    "futures": {
        "order_book": {
            "bids": [[148.20, 900.0], [148.15, 500.0], [148.10, 400.0]],
            "asks": [[148.32, 600.0], [148.40, 800.0], [148.55, 300.0]],
        },
        "trades_normalized": [
            {"id": 1, "ts": 1_700_000_000_000, "price": 148.25, "qty": 6.0,
             "is_buyer_maker": False},
            {"id": 2, "ts": 1_700_000_000_050, "price": 148.31, "qty": 1.0,
             "is_buyer_maker": True},
        ],
    },
    "spot": {
        "order_book": {
            "bids": [[148.22, 300.0], [148.10, 120.0]],
            "asks": [[148.30, 250.0], [148.44, 90.0]],
        },
        "trades_normalized": [
            {"id": 11, "ts": 1_700_000_000_000, "price": 148.26, "qty": 2.0,
             "is_buyer_maker": False},
            {"id": 12, "ts": 1_700_000_000_060, "price": 148.28, "qty": 2.0,
             "is_buyer_maker": True},
        ],
    },
}

TBR_NEUTRAL = [{"buy_vol": 100.0, "sell_vol": 100.0}]
TBR_BUY_HEAVY = [{"buy_vol": 400.0, "sell_vol": 100.0}]


def _last(output: dict[str, Any] | None) -> dict[str, Any]:
    return {"substrate": "x", "symbol": "SOLUSDT", "computed_at_ms": 1,
            "output": output or {}, "trigger": {"source": "probe", "predicates": {}}}


def _with_tbr(window: dict[str, Any], tbr: list[dict]) -> dict[str, Any]:
    import copy
    window = copy.deepcopy(window)
    window["futures"]["taker_buy_sell"] = tbr
    return window


class TapeProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = TapeWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_tape_does_not_fire(self):
        out = self.w.compute(BOOK, 20)
        d = self.w.probe(BOOK, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"quiet tape fired: {d.predicates}")

    def test_buy_share_band_cross_fires(self):
        import copy
        out = self.w.compute(BOOK, 20)
        heavy = copy.deepcopy(BOOK)
        heavy["futures"]["trades_normalized"] = [
            {"id": 20 + i, "ts": 1_700_000_000_100 + i, "price": 148.25,
             "qty": 5.0, "is_buyer_maker": True} for i in range(6)
        ]
        d = self.w.probe(heavy, _last(out), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("buy_share_cross:futures", d.predicates)

    def test_no_trades_stays_quiet(self):
        empty = {"futures": {"order_book": {}, "trades_normalized": []},
                 "spot": {"order_book": {}, "trades_normalized": []}}
        d = self.w.probe(empty, _last(None), 1_700_000_001_000)
        self.assertFalse(d.fired)

    def test_probe_deterministic(self):
        out = self.w.compute(BOOK, 20)
        a = self.w.probe(BOOK, _last(out), 1_700_000_001_000)
        b = self.w.probe(BOOK, _last(out), 1_700_000_001_000)
        self.assertEqual((a.fired, a.predicates), (b.fired, b.predicates))


class TapeComputeTests(unittest.TestCase):
    def setUp(self):
        self.w = TapeWorker(_NoStore(), symbol="SOLUSDT")

    def test_compute_emits_tape_outputs(self):
        out = self.w.compute(BOOK, 20)
        self.assertEqual(
            set(out),
            {"spot_flow", "futures_flow", "spot_bucketed_cvd",
             "futures_bucketed_cvd", "cvd_series_corr", "spot_turnover_share",
             "fut_microprice_skew_bps"},
        )
        self.assertGreater(out["futures_flow"]["trade_count"], 0)


class LargePrintProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = LargePrintWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_prints_do_not_fire(self):
        out = self.w.compute(BOOK, 20)
        d = self.w.probe(BOOK, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"quiet prints fired: {d.predicates}")

    def test_new_large_print_fires(self):
        import copy
        out = self.w.compute(BOOK, 20)
        novel = copy.deepcopy(BOOK)
        novel["futures"]["trades_normalized"] = list(
            BOOK["futures"]["trades_normalized"]) + [
            {"id": 99, "ts": 1_700_000_000_900, "price": 148.25,
             "qty": LARGE_TIER_QTY + 10.0, "is_buyer_maker": False},
        ]
        d = self.w.probe(novel, _last(out), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("large_print:99", d.predicates)

    def test_aggression_flip_fires(self):
        import copy
        sell_heavy = copy.deepcopy(BOOK)
        sell_heavy["futures"]["trades_normalized"] = [
            {"id": 30 + i, "ts": 1_700_000_000_100 + i * 1000, "price": 148.25,
             "qty": 150.0, "is_buyer_maker": True} for i in range(3)
        ] + [
            {"id": 40, "ts": 1_700_000_000_200, "price": 148.25,
             "qty": 150.0, "is_buyer_maker": False},
        ]
        out = self.w.compute(sell_heavy, 20)
        self.assertEqual(out["seller_aggression"]["classification"], "HIGH")
        prior = dict(out)
        prior["seller_aggression"] = {"classification": "LOW"}
        d = self.w.probe(sell_heavy, _last(prior), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("aggression_flip", d.predicates)

    def test_empty_tape_returns_empty_output(self):
        self.assertEqual(
            self.w.compute({"futures": {"trades_normalized": []}}, 20), {})


class DeltaProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = DeltaWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_delta_does_not_fire(self):
        window = _with_tbr(BOOK, TBR_NEUTRAL)
        out = self.w.compute(window, 20)
        d = self.w.probe(window, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"quiet delta fired: {d.predicates}")

    def test_state_flip_fires(self):
        import copy
        window = _with_tbr(BOOK, TBR_NEUTRAL)
        out = self.w.compute(window, 20)
        bid_heavy = copy.deepcopy(window)
        bid_heavy["futures"]["order_book"]["bids"] = [
            [148.20, 9000.0], [148.15, 5000.0], [148.10, 4000.0]]
        bid_heavy["futures"]["order_book"]["asks"] = [
            [148.32, 6.0], [148.40, 8.0]]
        d = self.w.probe(bid_heavy, _last(out), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("delta_state_flip", d.predicates)

    def test_tbr_band_cross_fires(self):
        calm = _with_tbr(BOOK, TBR_NEUTRAL)
        out = self.w.compute(calm, 20)
        excited = _with_tbr(BOOK, TBR_BUY_HEAVY)
        d = self.w.probe(excited, _last(out), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("tbr_band_cross", d.predicates)

    def test_no_book_stays_quiet(self):
        d = self.w.probe({"futures": {"order_book": {}}}, _last(None),
                         1_700_000_001_000)
        self.assertFalse(d.fired)

    def test_compute_shape_and_empty(self):
        out = self.w.compute(_with_tbr(BOOK, TBR_NEUTRAL), 20)
        for key in ("delta", "wall_imbalance", "flow_alignment", "tbr_last_pct",
                    "bands", "state"):
            self.assertIn(key, out)
        self.assertEqual(self.w.compute({"futures": {"order_book": {}}}, 20), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
