"""Regression tests for the OI string-vs-number coercion in signals_worker.

Binance's /fapi/v1/openInterest endpoint returns ``"openInterest": "7923049.05"``
— a JSON STRING, not a number. The raw stream preserves that shape, so any
worker that does arithmetic on the value (``current_oi - previous_oi``)
would raise TypeError once both sides are strings.

This regression test pins the coercion in ``signals_worker._snapshot_from``
so a future refactor can't reintroduce the TypeError.

Live observation (Sept 10, 2026):
  - signals worker fired 633 times with ``last_error: "fire: unsupported
    operand type(s) for -: 'str' and 'str'"``
  - The :latest projection stored ``open_interest: "7923049.05"`` (string)
  - The fix coerces to float at the snapshot boundary; the :latest then
    stores ``open_interest: 7923049.05`` (number)
"""

import unittest

from market_service.substrate_worker.signals_worker import _snapshot_from


def _window(oi_value):
    """Build a minimal window dict the snapshot_from helper accepts."""
    return {
        "futures": {
            "open_interest": {
                "symbol": "SOLUSDT",
                "time": 1_789_030_297_898,
                "open_interest": oi_value,
            },
        },
        "substrate_dependencies": {
            "tape": {
                "available": True,
                "output": {
                    "spot_flow": {"buy_share": 0.4, "obi": 0.1},
                    "futures_flow": {"buy_share": 0.5},
                },
            },
        },
    }


class OIStringCoercionTests(unittest.TestCase):
    """Pin the snapshot_from coercion contract."""

    def test_string_oi_is_coerced_to_float(self):
        snapshot = _snapshot_from(_window("7923049.05"))
        self.assertIsInstance(snapshot["open_interest"], float)
        self.assertAlmostEqual(snapshot["open_interest"], 7923049.05, places=2)

    def test_numeric_oi_passes_through(self):
        snapshot = _snapshot_from(_window(7923049.05))
        self.assertIsInstance(snapshot["open_interest"], float)
        self.assertAlmostEqual(snapshot["open_interest"], 7923049.05, places=2)

    def test_unparseable_string_oi_becomes_none(self):
        # Null discipline: never fabricate. Bad shape → drop the field.
        snapshot = _snapshot_from(_window("not-a-number"))
        self.assertIsNone(snapshot["open_interest"])

    def test_none_oi_stays_none(self):
        snapshot = _snapshot_from(_window(None))
        self.assertIsNone(snapshot["open_interest"])

    def test_arithmetic_after_coercion_does_not_raise(self):
        """The original bug: two consecutive string OIs → TypeError on subtract."""
        from market_service.calculations.substrates.signals import deterministic_signals

        snapshot_a = _snapshot_from(_window("7919158.94"))
        snapshot_b = _snapshot_from(_window("7923049.05"))
        # Pre-fix: TypeError. Post-fix: emits an open_interest_shift signal
        # because the change (0.49%) exceeds the 1% threshold only when the
        # delta is large enough; here it's small, so we expect no signal,
        # but the IMPORTANT property is that NO exception is raised.
        signals = deterministic_signals(snapshot_b, snapshot_a)
        self.assertIsInstance(signals, list)

    def test_large_oi_change_emits_signal(self):
        """Sanity: a >1% OI move after coercion does trip the signal."""
        from market_service.calculations.substrates.signals import deterministic_signals

        snapshot_a = _snapshot_from(_window("1000000.00"))
        snapshot_b = _snapshot_from(_window("1100000.00"))  # +10%
        signals = deterministic_signals(snapshot_b, snapshot_a)
        types = [s["signal_type"] for s in signals]
        self.assertIn("open_interest_shift", types)

    def test_snapshot_returns_none_when_tape_unavailable(self):
        window = _window("7923049.05")
        window["substrate_dependencies"]["tape"] = {"available": False}
        self.assertIsNone(_snapshot_from(window))


class StaleStringOIToleranceTests(unittest.TestCase):
    """Defensive coercion in signals.py for stale string ``previous_oi``.

    The signals-worker boundary coerces on write, but a projection written
    BEFORE the fix still carries ``open_interest: "7923049.05"``. Once
    the fix is deployed the worker must tolerate that stale data on the
    very next fire — otherwise it stays in the runaway loop forever.
    """

    def test_arithmetic_with_string_previous_oi_does_not_raise(self):
        """The original bug pattern: current=float, previous=str → TypeError."""
        from market_service.calculations.substrates.signals import deterministic_signals

        snapshot = {"open_interest": 7923049.05}      # fresh, float (post-fix)
        previous = {"open_interest": "7919158.94"}   # stale, string (pre-fix)
        # The coercion in signals.py must accept both shapes.
        signals = deterministic_signals(snapshot, previous)
        self.assertIsInstance(signals, list)

    def test_arithmetic_with_string_current_and_previous_does_not_raise(self):
        """Both sides strings (entirely pre-fix data) must also work."""
        from market_service.calculations.substrates.signals import deterministic_signals

        snapshot = {"open_interest": "7923049.05"}
        previous = {"open_interest": "7919158.94"}
        signals = deterministic_signals(snapshot, previous)
        self.assertIsInstance(signals, list)

    def test_oi_shift_signal_still_fires_after_coercion(self):
        """A real >1% OI move must trip the open_interest_shift signal even
        when previous is a stale string and current is a fresh float."""
        from market_service.calculations.substrates.signals import deterministic_signals

        snapshot = {"open_interest": 1_100_000.0}     # +10% as float
        previous = {"open_interest": "1000000"}      # stale string from old cycle
        signals = deterministic_signals(snapshot, previous)
        types = [s["signal_type"] for s in signals]
        self.assertIn("open_interest_shift", types)


if __name__ == "__main__":
    unittest.main()
