"""Volume-profile / technicals / migration / OI worker tests — Wave 3."""

from __future__ import annotations

import unittest
from typing import Any

from market_service.substrate_worker.migration_worker import MigrationWorker
from market_service.substrate_worker.oi_worker import OiWorker
from market_service.substrate_worker.technicals_worker import TechnicalsWorker
from market_service.substrate_worker.volume_profile_worker import VolumeProfileWorker
from tests.test_density_worker import _NoStore

BASE_TS = 1_700_000_000_000


def _trades(n=40, price=148.25, start=BASE_TS, step_ms=10_000, qty=2.0,
            buyer_maker=False, start_id=0):
    return [
        {"id": start_id + i, "ts": start + i * step_ms, "price": price,
         "qty": qty, "is_buyer_maker": buyer_maker}
        for i in range(n)
    ]


def _klines(n=30, start_price=140.0, step=0.5, start=BASE_TS):
    rows = []
    for i in range(n):
        close = start_price + i * step
        rows.append([start + i * 300_000, close - 0.2, close + 0.3,
                     close - 0.4, close, 100.0 + i])
    return rows


def _book():
    return {
        "bids": [[148.20, 900.0], [148.15, 500.0], [148.10, 400.0]],
        "asks": [[148.32, 600.0], [148.40, 800.0]],
    }


def _last(output: dict[str, Any] | None) -> dict[str, Any]:
    return {"substrate": "x", "symbol": "SOLUSDT", "computed_at_ms": 1,
            "output": output or {}, "trigger": {"source": "probe", "predicates": {}}}


def _vp_window(trades):
    return {"futures": {"trades_normalized": trades}}


class VolumeProfileProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = VolumeProfileWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_profile_does_not_fire(self):
        window = _vp_window(_trades())
        out = self.w.compute(window, 20)
        d = self.w.probe(window, _last(out), BASE_TS + 10_000_000)
        self.assertFalse(d.fired, f"quiet profile fired: {d.predicates}")

    def test_poc_move_fires(self):
        window = _vp_window(_trades())
        out = self.w.compute(window, 20)
        moved = _vp_window(_trades(price=149.50))
        d = self.w.probe(moved, _last(out), BASE_TS + 10_000_000)
        self.assertTrue(d.fired)
        self.assertIn("poc_moved", d.predicates)

    def test_empty_tape_returns_empty(self):
        self.assertEqual(self.w.compute(_vp_window([]), 20), {})
        d = self.w.probe(_vp_window([]), _last(None), BASE_TS)
        self.assertFalse(d.fired)


class TechnicalsProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = TechnicalsWorker(_NoStore(), symbol="SOLUSDT")

    def _window(self, klines):
        return {"futures": {"klines": klines}}

    def test_quiet_bars_do_not_fire(self):
        window = self._window(_klines())
        out = self.w.compute(window, 20)
        self.assertIn("ema21", out["ema_position"])
        d = self.w.probe(window, _last(out), BASE_TS + 10_000_000)
        self.assertFalse(d.fired, f"quiet technicals fired: {d.predicates}")

    def test_new_bar_fires(self):
        klines = _klines()
        out = self.w.compute(self._window(klines), 20)
        prior = dict(out)
        prior["kline_count"] = len(klines) - 2
        d = self.w.probe(self._window(klines), _last(prior), BASE_TS + 10_000_000)
        self.assertTrue(d.fired)
        self.assertIn("new_bar", d.predicates)

    def test_ema_flip_fires(self):
        klines = _klines()
        out = self.w.compute(self._window(klines), 20)
        cur_pos = out["ema_position"]["ema21"]
        flipped = "BELOW" if cur_pos == "ABOVE" else "ABOVE"
        prior = dict(out)
        prior["ema_position"] = {"ema21": flipped}
        d = self.w.probe(self._window(klines), _last(prior), BASE_TS + 10_000_000)
        self.assertTrue(d.fired)
        self.assertIn("ema_position_flip", d.predicates)

    def test_compute_shape_and_empty(self):
        out = self.w.compute(self._window(_klines()), 20)
        for key in ("emas", "emas_source", "atr_pct", "trend_slope",
                    "trend_drift", "ema_position", "kline_count"):
            self.assertIn(key, out)
        self.assertEqual(out["emas_source"], "derivatives.klines_5m")
        self.assertEqual(self.w.compute(self._window([]), 20), {})


class MigrationProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = MigrationWorker(_NoStore(), symbol="SOLUSDT")

    def _two_hour(self):
        hour = 3_600_000
        base = (BASE_TS // hour) * hour
        return _trades(10, price=148.0, start=base, step_ms=60_000, start_id=1) + \
            _trades(10, price=148.0, start=base + hour, step_ms=60_000, start_id=101)

    def test_quiet_migration_does_not_fire(self):
        window = {"futures": {"trades_normalized": self._two_hour()}}
        out = self.w.compute(window, 20)
        d = self.w.probe(window, _last(out), BASE_TS + 10_000_000)
        self.assertFalse(d.fired, f"quiet migration fired: {d.predicates}")

    def test_hour_rollover_fires(self):
        hour = 3_600_000
        base = (BASE_TS // hour) * hour
        one_hour = {"futures": {"trades_normalized": _trades(
            10, price=148.0, start=base, step_ms=60_000, start_id=1)}}
        out = self.w.compute(one_hour, 20)
        two_hour = {"futures": {"trades_normalized": self._two_hour()}}
        d = self.w.probe(two_hour, _last(out), BASE_TS + 10_000_000)
        self.assertTrue(d.fired)
        self.assertIn("hour_rollover", d.predicates)

    def test_empty_tape_returns_empty(self):
        self.assertEqual(self.w.compute({"futures": {"trades_normalized": []}}, 20), {})


class OiProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = OiWorker(_NoStore(), symbol="SOLUSDT")

    def _window(self, oi=1_000_000.0):
        hist = [
            {"sum_open_interest": 990_000.0 + i * 1_000.0,
             "sum_open_interest_value": 148_000_000.0 + i * 148_000.0,
             "timestamp": BASE_TS + i * 300_000}
            for i in range(14)
        ]
        return {"futures": {
            "order_book": _book(),
            "open_interest": {"open_interest": oi},
            "oi_history": hist,
            "top_ls": [{"longAccount": "0.55"}],
            "global_ls": [{"long_account": 0.48}],
        }}

    def test_quiet_oi_does_not_fire(self):
        window = self._window()
        out = self.w.compute(window, 20)
        self.assertEqual(out["raw_open_interest"], 1_000_000.0)
        d = self.w.probe(window, _last(out), BASE_TS + 10_000_000)
        self.assertFalse(d.fired, f"quiet OI fired: {d.predicates}")

    def test_oi_move_fires(self):
        window = self._window(oi=1_020_000.0)
        prior = {"raw_open_interest": 1_000_000.0}
        d = self.w.probe(window, _last(prior), BASE_TS + 10_000_000)
        self.assertTrue(d.fired)
        self.assertIn("oi_move", d.predicates)

    def test_missing_oi_stays_quiet(self):
        d = self.w.probe({"futures": {"order_book": _book()}},
                         _last(None), BASE_TS)
        self.assertFalse(d.fired)

    def test_compute_shape(self):
        out = self.w.compute(self._window(), 20)
        for key in ("walls", "weighted_contracts", "inflow_outflow",
                    "implied_value", "raw_open_interest", "bars_available"):
            self.assertIn(key, out)
        self.assertEqual(
            self.w.compute({"futures": {"order_book": {}}}, 20), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
