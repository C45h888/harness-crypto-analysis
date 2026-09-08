"""Density substrate worker tests — probe determinism + compute shape.

Pins the density worker's L2 probe against its OWN substrate semantics:

  keystone_delta — fires when the buyer keystone center moved more than the
                   substrate keystone width (0.20) from the worker's last
                   recorded state (the migration verdict's UP/DOWN scale).
  wall_shift     — fires when a top density bid window's qty changed by more
                   than the wall direction threshold (1.15) — the BUILT UP /
                   ERODED semantics of wall_delta.

And the compute contract: density-substrate outputs ONLY (fut+spot
keystones, top density windows, significant levels, keystone trade
intensity) — no ladders, no anchors, no tape-substrate functions (purity
rule enforced by test_substrate_graph.py).
"""

from __future__ import annotations

import unittest
from typing import Any

from market_service.substrate_worker.contracts import SubstrateStatePayload  # noqa: F401
from market_service.substrate_worker.density_worker import (
    KEYSTONE_WIDTH,
    WALL_DIRECTION_THRESHOLD,
    DensityWorker,
)

BOOK_DEEP_BID = [148.10, 400.0]
BOOK = {
    "futures": {
        "order_book": {
            "bids": [[148.20, 900.0], [148.15, 500.0], BOOK_DEEP_BID, [148.00, 250.0]],
            "asks": [[148.32, 600.0], [148.40, 800.0], [148.55, 300.0]],
        },
        "trades_normalized": [
            {"ts": 1_700_000_000_000, "price": 148.25, "qty": 6.0, "is_buyer_maker": False},
            {"ts": 1_700_000_000_050, "price": 148.31, "qty": 1.0, "is_buyer_maker": True},
        ],
    },
    "spot": {
        "order_book": {
            "bids": [[148.22, 300.0], [148.10, 120.0]],
            "asks": [[148.30, 250.0], [148.44, 90.0]],
        },
        "trades_normalized": [],
    },
}


def _last_state(output: dict[str, Any] | None) -> dict[str, Any]:
    """Shape of a persisted SubstrateStatePayload (what probe() reads back)."""
    return {
        "substrate": "density", "symbol": "SOLUSDT",
        "computed_at_ms": 1_700_000_000_000,
        "output": output or {}, "trigger": {"source": "probe", "predicates": {}},
    }


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.w = DensityWorker(_NoStore(), symbol="SOLUSDT")

    def _probe(self, book=BOOK, last_output=None, now_ms=1_700_000_000_500):
        return self.w.probe(book, _last_state(last_output), now_ms)

    def test_no_prior_state_cold_probe_does_not_fire_on_quiet_book(self):
        decision = self._probe(last_output=None)
        self.assertFalse(decision.fired)

    def test_quiet_book_with_matching_state_does_not_fire(self):
        # The "last state" is exactly what this book produces → no probe trips.
        state_output = self.w.compute(BOOK, 20)
        decision = self._probe(last_output=state_output)
        self.assertFalse(decision.fired,
                         f"quiet book fired predicates: {decision.predicates}")

    def test_keystone_displacement_beyond_width_fires(self):
        # Shift every bid down by 0.30 → the keystone moves beyond the width.
        shifted = {
            "futures": {
                "order_book": {
                    "bids": [[p - 0.30, q] for p, q in BOOK["futures"]["order_book"]["bids"]],
                    "asks": BOOK["futures"]["order_book"]["asks"],
                },
                "trades_normalized": BOOK["futures"]["trades_normalized"],
            },
            "spot": BOOK["spot"],
        }
        state_output = self.w.compute(BOOK, 20)
        decision = self._probe(book=shifted, last_output=state_output)
        self.assertTrue(decision.fired, "keystone displacement must fire")
        self.assertIn("keystone_delta", decision.predicates)
        pred = decision.predicates["keystone_delta"]
        self.assertGreater(abs(pred["delta"]), KEYSTONE_WIDTH)

    def test_small_keystone_move_within_width_does_not_fire(self):
        # Nudge the WHOLE book down by 0.05: keystone tracks the mid (within
        # the 0.20 width) and the relative wall structure is unchanged —
        # a genuinely quiet market drift must not fire.
        nudge = 0.05
        nudged = {
            "futures": {
                "order_book": {
                    "bids": [[p - nudge, q] for p, q in BOOK["futures"]["order_book"]["bids"]],
                    "asks": [[p - nudge, q] for p, q in BOOK["futures"]["order_book"]["asks"]],
                },
                "trades_normalized": BOOK["futures"]["trades_normalized"],
            },
            "spot": BOOK["spot"],
        }
        state_output = self.w.compute(BOOK, 20)
        decision = self._probe(book=nudged, last_output=state_output)
        self.assertFalse(decision.fired,
                         f"sub-width keystone move fired: {decision.predicates}")

    def test_wall_qty_shift_beyond_direction_threshold_fires(self):
        # 4x the qty on the deepest bid window → ratio crosses 1.15.
        thickened = {
            "futures": {
                "order_book": {
                    "bids": [[p, q * 4.0] for p, q in BOOK["futures"]["order_book"]["bids"]],
                    "asks": BOOK["futures"]["order_book"]["asks"],
                },
                "trades_normalized": BOOK["futures"]["trades_normalized"],
            },
            "spot": BOOK["spot"],
        }
        state_output = self.w.compute(BOOK, 20)
        decision = self._probe(book=thickened, last_output=state_output)
        self.assertTrue(decision.fired, "major wall build must fire")
        self.assertTrue(any(k.startswith("wall_shift@") for k in decision.predicates))
        for key, pred in decision.predicates.items():
            if key.startswith("wall_shift@"):
                self.assertGreater(pred["ratio"], WALL_DIRECTION_THRESHOLD)
                self.assertEqual(pred["reference"], "mid")

    def test_probe_is_deterministic(self):
        state_output = self.w.compute(BOOK, 20)
        a = self._probe(last_output=state_output)
        b = self._probe(last_output=state_output)
        self.assertEqual(a.fired, b.fired)
        self.assertEqual(a.predicates, b.predicates)


class ComputeShapeTests(unittest.TestCase):
    def setUp(self):
        self.w = DensityWorker(_NoStore(), symbol="SOLUSDT")

    def test_compute_emits_density_outputs_only(self):
        out = self.w.compute(BOOK, 20)
        self.assertEqual(
            set(out),
            {"fut_keystone", "spot_keystone", "fut_top_density_bids",
             "fut_significant_levels", "keystone_trade_intensity"},
        )

    def test_keystone_shape_with_ask_alias(self):
        out = self.w.compute(BOOK, 20)
        kz = out["fut_keystone"]
        self.assertIn("keystone", kz)
        self.assertIn("bid", kz)
        self.assertIn("ask", kz)
        self.assertIn("tight", kz)
        self.assertIn("wide", kz)
        self.assertIsNotNone(kz["ask"])

    def test_empty_book_returns_empty_output(self):
        self.assertEqual(self.w.compute({"futures": {"order_book": {}},
                                         "spot": {"order_book": {}}}, 20), {})


class _NoRedis:
    """A Redis stub that raises if any worker hook tries to use it."""

    def __getattr__(self, name):
        raise AssertionError(f"worker hook touched redis directly during {name}")


class _NoStore:
    """Probe/compute must never touch the store.

    The constructor's Redis namespace string-builders are allowed (pure
    string math, no I/O); any other store access raises.
    """

    def __init__(self):
        self.redis = _NoRedis()

    def raw_stream(self, symbol):
        return f"mkt:stream:raw:{symbol}"

    def substrate_supervisor_key(self, substrate, symbol):
        return f"mkt:substrate:{substrate}:{symbol}:supervisor"

    def substrate_latest_key(self, substrate, symbol):
        return f"mkt:latest:substrate:{substrate}:{symbol}"

    def substrate_stream(self, substrate, symbol):
        return f"mkt:stream:substrate:{substrate}:{symbol}"

    def __getattr__(self, name):
        raise AssertionError(f"worker touched the store during {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)