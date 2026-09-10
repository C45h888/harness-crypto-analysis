"""Regression tests for the OI string-vs-number coercion in oi_worker.

Companion to test_signals_oi_coercion.py. The same Binance string-number
shape (``"openInterest": "7923049.05"``) lands in the futures window's
``open_interest`` dict; the oi worker's probe and compute both consume
it. Without coercion, ``isinstance(oi_value, (int, float))`` returns
False for a string and the worker treats the value as missing — a silent
degradation. With coercion the value is normalised to float.
"""

import unittest

from market_service.substrate_worker.oi_worker import OiWorker


def _window(oi_value):
    """Minimal futures window for the OiWorker probe path."""
    return {
        "futures": {
            "open_interest": {
                "symbol": "SOLUSDT",
                "time": 1_789_030_297_898,
                "open_interest": oi_value,
            },
            "order_book": {
                "bids": [["100.0", "1.0"], ["99.0", "1.0"]],
                "asks": [["101.0", "1.0"], ["102.0", "1.0"]],
            },
        },
    }


class _NoStore:
    """Stub store — OiWorker init needs an attribute or two we don't use."""


class OIWorkerStringCoercionTests(unittest.TestCase):
    """Pin the OiWorker.probe string-coercion contract."""

    def _worker(self):
        # OiWorker is instantiated by the runner with (store, symbol=...).
        # We only exercise probe/compute so the store isn't touched.
        return OiWorker.__new__(OiWorker)

    def test_string_oi_is_coerced_to_float_in_probe(self):
        worker = self._worker()
        window = _window("7923049.05")
        # No prior state → probe should fire false (no previous OI to
        # compare against). The important property is that the OI is
        # recognised, not rejected as missing.
        decision = worker.probe(window, None, now_ms=1)
        self.assertFalse(decision.fired)
        # If the probe had rejected the string OI as missing, it would
        # have set the ``no_open_interest`` reason. Verify it didn't.
        self.assertNotEqual(decision.predicates.get("reason"), "no_open_interest")

    def test_numeric_oi_passes_through_in_probe(self):
        worker = self._worker()
        window = _window(7923049.05)
        decision = worker.probe(window, None, now_ms=1)
        self.assertFalse(decision.fired)
        self.assertNotEqual(decision.predicates.get("reason"), "no_open_interest")

    def test_string_oi_triggers_oi_move_probe(self):
        """A >1% change between a stale string ``previous`` and a fresh
        string ``current`` must trip the oi_move predicate."""
        worker = self._worker()
        window = _window("1100000.00")  # +10% over previous
        last_state = {"output": {"raw_open_interest": "1000000"}}  # stale string
        decision = worker.probe(window, last_state, now_ms=1)
        self.assertTrue(decision.fired)
        self.assertIn("oi_move", decision.predicates)
        move = decision.predicates["oi_move"]
        self.assertAlmostEqual(move["move_pct"], 10.0, places=2)

    def test_none_oi_stays_none(self):
        worker = self._worker()
        window = _window(None)
        decision = worker.probe(window, None, now_ms=1)
        # No OI at all → the no_open_interest predicate is correct here.
        self.assertEqual(decision.predicates.get("reason"), "no_open_interest")

    def test_unparseable_string_oi_is_rejected(self):
        worker = self._worker()
        window = _window("not-a-number")
        decision = worker.probe(window, None, now_ms=1)
        # A string that fails float() coercion → treated as missing.
        self.assertEqual(decision.predicates.get("reason"), "no_open_interest")


if __name__ == "__main__":
    unittest.main()
