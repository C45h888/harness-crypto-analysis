"""Ladders / anchors / tiers / signals worker tests — Wave 2 truth tables."""

from __future__ import annotations

import unittest
from typing import Any

from market_service.substrate_worker.anchors_worker import AnchorsWorker
from market_service.substrate_worker.ladders_worker import LaddersWorker
from market_service.substrate_worker.signals_worker import SignalsWorker
from market_service.substrate_worker.tiers_worker import TiersWorker
from tests.test_density_worker import _NoStore

BOOK = {
    "futures": {
        "order_book": {
            "bids": [[148.20, 900.0], [148.15, 500.0], [148.10, 400.0],
                     [148.00, 250.0]],
            "asks": [[148.32, 600.0], [148.40, 800.0], [148.55, 300.0]],
        },
        "trades_normalized": [],
    },
    "spot": {"order_book": {}, "trades_normalized": []},
}


def _last(output: dict[str, Any] | None) -> dict[str, Any]:
    return {"substrate": "x", "symbol": "SOLUSDT", "computed_at_ms": 1,
            "output": output or {}, "trigger": {"source": "probe", "predicates": {}}}


def _tape_latest(spot_share=0.5, fut_share=0.5, obi=0.0,
                 computed_at_ms=1_700_000_000_000) -> dict[str, Any]:
    return {
        "substrate": "tape", "symbol": "SOLUSDT",
        "computed_at_ms": computed_at_ms,
        "output": {
            "spot_flow": {"buy_share": spot_share, "obi": obi},
            "futures_flow": {"buy_share": fut_share},
        },
    }


def _sig_window(tape=None, oi=1_000_000.0) -> dict[str, Any]:
    return {
        "futures": {"open_interest": {"open_interest": oi}},
        "substrate_dependencies": {"tape": tape or _tape_latest()},
    }


class LaddersProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = LaddersWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_ladder_does_not_fire(self):
        out = self.w.compute(BOOK, 20)
        d = self.w.probe(BOOK, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"quiet ladder fired: {d.predicates}")

    def test_rung_build_fires(self):
        import copy
        out = self.w.compute(BOOK, 20)
        thick = copy.deepcopy(BOOK)
        thick["futures"]["order_book"]["bids"] = [
            [p, q * 4.0] for p, q in BOOK["futures"]["order_book"]["bids"]]
        d = self.w.probe(thick, _last(out), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertTrue(any(k.startswith("ladder_shift@") for k in d.predicates))

    def test_whole_book_drift_does_not_fire(self):
        import copy
        out = self.w.compute(BOOK, 20)
        drifted = copy.deepcopy(BOOK)
        drifted["futures"]["order_book"]["bids"] = [
            [p - 0.05, q] for p, q in BOOK["futures"]["order_book"]["bids"]]
        drifted["futures"]["order_book"]["asks"] = [
            [p - 0.05, q] for p, q in BOOK["futures"]["order_book"]["asks"]]
        d = self.w.probe(drifted, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"structural drift fired: {d.predicates}")

    def test_compute_shape_and_empty(self):
        out = self.w.compute(BOOK, 20)
        self.assertEqual(set(out),
                         {"fut_absorption_ladder", "ask_wall_ladder", "reference"})
        self.assertIn("mid", out["reference"])
        self.assertEqual(self.w.compute({"futures": {"order_book": {}}}, 20), {})


class AnchorsProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = AnchorsWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_anchors_do_not_fire(self):
        out = self.w.compute(BOOK, 20)
        d = self.w.probe(BOOK, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"quiet anchors fired: {d.predicates}")

    def test_anchor_build_fires(self):
        import copy
        out = self.w.compute(BOOK, 20)
        thick = copy.deepcopy(BOOK)
        thick["futures"]["order_book"]["bids"] = [
            [p, q * 5.0] for p, q in BOOK["futures"]["order_book"]["bids"]]
        d = self.w.probe(thick, _last(out), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertTrue(
            any(k.startswith("anchor_shift@") for k in d.predicates)
            or "grid_shift" in d.predicates)

    def test_grid_shift_fires(self):
        import copy
        out = self.w.compute(BOOK, 20)
        moved = copy.deepcopy(BOOK)
        moved["futures"]["order_book"]["bids"] = [
            [p + 2.0, q] for p, q in BOOK["futures"]["order_book"]["bids"]]
        moved["futures"]["order_book"]["asks"] = [
            [p + 2.0, q] for p, q in BOOK["futures"]["order_book"]["asks"]]
        d = self.w.probe(moved, _last(out), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("grid_shift", d.predicates)

    def test_compute_shape_and_empty(self):
        out = self.w.compute(BOOK, 20)
        self.assertEqual(set(out), {"anchors", "aggregation", "anchor_source",
                                    "tick_size", "step"})
        self.assertEqual(out["anchor_source"], "dynamic")
        self.assertEqual(self.w.compute({"futures": {"order_book": {}}}, 20), {})


class TiersProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = TiersWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_tiers_do_not_fire(self):
        out = self.w.compute(BOOK, 20)
        d = self.w.probe(BOOK, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"quiet tiers fired: {d.predicates}")

    def test_verdict_flip_fires(self):
        import copy
        heavy = copy.deepcopy(BOOK)
        heavy["futures"]["order_book"]["bids"] = [[148.20, 5000.0]]
        heavy["futures"]["order_book"]["asks"] = [[148.32, 1.0]]
        out = self.w.compute(heavy, 20)
        self.assertEqual(out["tier_balance"]["verdict"], "INSTITUTIONAL-BID-HEAVY")
        prior = dict(out)
        prior["tier_balance"] = {"verdict": "BALANCED", "ratio": 1.0}
        d = self.w.probe(heavy, _last(prior), 1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("tier_verdict_flip", d.predicates)

    def test_compute_shape_and_empty(self):
        out = self.w.compute(BOOK, 20)
        self.assertEqual(set(out), {"tiers", "tier_balance"})
        self.assertEqual(self.w.compute({"futures": {"order_book": {}}}, 20), {})


class SignalsProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = SignalsWorker(_NoStore(), symbol="SOLUSDT")

    def test_quiet_snapshot_does_not_fire(self):
        window = _sig_window()
        out = self.w.compute({**window, "own_last_output": {"snapshot": {
            "spot_buy_share": 0.5, "futures_buy_share": 0.5,
            "spot_obi_top_n": 0.0, "open_interest": 1_000_000.0}}}, 20)
        d = self.w.probe(window, _last(out), 1_700_000_001_000)
        self.assertFalse(d.fired, f"quiet signals fired: {d.predicates}")

    def test_divergence_fires(self):
        window = _sig_window(tape=_tape_latest(spot_share=0.30, fut_share=0.70))
        d = self.w.probe(window, _last({"snapshot": {
            "spot_buy_share": 0.5, "futures_buy_share": 0.5,
            "spot_obi_top_n": 0.0, "open_interest": 1_000_000.0}}),
            1_700_000_001_000)
        self.assertTrue(d.fired)
        self.assertIn("spot_futures_divergence", d.predicates)

    def test_missing_dependency_stays_quiet(self):
        d = self.w.probe({"futures": {}}, _last(None), 1_700_000_001_000)
        self.assertFalse(d.fired)

    def test_compute_shape_and_missing_dep(self):
        window = _sig_window()
        out = self.w.compute({**window, "own_last_output": {}}, 20)
        self.assertEqual(set(out), {"signals", "snapshot"})
        self.assertEqual(self.w.compute({"futures": {}}, 20), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
