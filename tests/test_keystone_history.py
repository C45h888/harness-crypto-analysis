"""Tests for the Pass 2 keystone pivot.

Covers:
- keystone_cycle_migration (pure cross-cycle migration verdict)
- PostgresRuntimeStore._opt_float (null-discipline coercion)
- _keystone_snapshot_payload (pipeline payload assembly, null discipline)
- harness _projection signal inventory (scalar headlines, bounded)
"""

from __future__ import annotations

import unittest

from market_service.calculations.orderbook import keystone_cycle_migration


class KeystoneCycleMigrationTests(unittest.TestCase):
    def test_up_migration(self):
        snaps = [
            {"cycle_ts": "2026-08-23T00:00:00Z", "keystone_price": 93.50},
            {"cycle_ts": "2026-08-23T00:30:00Z", "keystone_price": 93.80},
            {"cycle_ts": "2026-08-23T01:00:00Z", "keystone_price": 94.10},
        ]
        r = keystone_cycle_migration(snaps, width=0.20)
        self.assertEqual(r["verdict"], "MIGRATING_UP")
        self.assertEqual(r["net_buckets"], 2)
        self.assertEqual(r["cycles"][0]["migration"], None)  # first has no prior
        self.assertEqual(r["cycles"][1]["migration"], "UP")
        self.assertEqual(r["cycles"][2]["migration"], "UP")

    def test_down_migration(self):
        # Prices fall over time once sorted oldest-first by cycle_ts.
        snaps = [
            {"cycle_ts": "2026-08-23T00:00:00Z", "keystone_price": 94.10},
            {"cycle_ts": "2026-08-23T00:30:00Z", "keystone_price": 93.80},
            {"cycle_ts": "2026-08-23T01:00:00Z", "keystone_price": 93.50},
        ]
        r = keystone_cycle_migration(snaps, width=0.20)
        self.assertEqual(r["verdict"], "MIGRATING_DOWN")
        self.assertEqual(r["net_buckets"], -2)

    def test_flat_within_width(self):
        snaps = [
            {"cycle_ts": "2026-08-23T00:00:00Z", "keystone_price": 93.50},
            {"cycle_ts": "2026-08-23T00:30:00Z", "keystone_price": 93.55},
            {"cycle_ts": "2026-08-23T01:00:00Z", "keystone_price": 93.60},
        ]
        r = keystone_cycle_migration(snaps, width=0.20)
        self.assertEqual(r["verdict"], "FLAT")
        self.assertEqual(r["cycles"][1]["migration"], "FLAT")
        self.assertEqual(r["cycles"][2]["migration"], "FLAT")

    def test_unsorted_input_is_ordered_by_cycle_ts(self):
        snaps = [
            {"cycle_ts": "2026-08-23T01:00:00Z", "keystone_price": 94.10},
            {"cycle_ts": "2026-08-23T00:00:00Z", "keystone_price": 93.50},
        ]
        r = keystone_cycle_migration(snaps, width=0.20)
        self.assertEqual(r["cycles"][0]["keystone"], 93.50)
        self.assertEqual(r["cycles"][1]["keystone"], 94.10)
        self.assertEqual(r["verdict"], "MIGRATING_UP")

    def test_none_keystone_price_skipped(self):
        snaps = [
            {"cycle_ts": "2026-08-23T00:00:00Z", "keystone_price": None},
            {"cycle_ts": "2026-08-23T00:30:00Z", "keystone_price": 93.50},
        ]
        r = keystone_cycle_migration(snaps)
        self.assertEqual(len(r["cycles"]), 1)
        self.assertEqual(r["verdict"], "FLAT")

    def test_empty_history(self):
        r = keystone_cycle_migration([])
        self.assertEqual(r["cycles"], [])
        self.assertEqual(r["verdict"], "FLAT")
        self.assertEqual(r["net_buckets"], 0)

    def test_non_dict_rows_skipped(self):
        r = keystone_cycle_migration([None, "garbage", {"keystone_price": "bad"}])
        self.assertEqual(r["cycles"], [])


class OptFloatTests(unittest.TestCase):
    def test_null_discipline(self):
        from market_service.runtime.postgres_store import PostgresRuntimeStore
        f = PostgresRuntimeStore._opt_float
        self.assertIsNone(f(None))
        self.assertIsNone(f("garbage"))
        self.assertIsNone(f(float("nan")))
        self.assertIsNone(f(float("inf")))
        self.assertAlmostEqual(f("93.5"), 93.5)
        self.assertAlmostEqual(f(93.5), 93.5)


class KeystoneSnapshotPayloadTests(unittest.TestCase):
    def test_null_discipline_on_missing_keystone(self):
        from market_service.nooa_harness.pipeline import _keystone_snapshot_payload
        payload = _keystone_snapshot_payload("SOLUSDT", "run-1", {"calculations": {}})
        self.assertIsNone(payload["keystone_price"])
        self.assertIsNone(payload["window_qty"])
        self.assertIsNone(payload["keystone_bid_qty"])
        self.assertIsNone(payload["ask_ladder_notional"])
        self.assertEqual(payload["schema_version"], 1)

    def test_extraction_from_calculations(self):
        from market_service.nooa_harness.pipeline import _keystone_snapshot_payload
        calc = {"calculations": {"orderbook": {
            "fut_keystone": {
                "keystone": 93.50, "window_qty": 1200.0,
                "tight": {"lo": 93.45, "hi": 93.55},
                "wide": {"lo": 93.40, "hi": 93.60},
            },
            "keystone_bid_stack": {"tight": {"total_qty": 14870.0}},
            "ask_wall_ladder": {"total_notional": 41431290.0},
        }}}
        payload = _keystone_snapshot_payload("SOLUSDT", "run-1", calc)
        self.assertAlmostEqual(payload["keystone_price"], 93.50)
        self.assertAlmostEqual(payload["window_qty"], 1200.0)
        self.assertAlmostEqual(payload["tight_lo"], 93.45)
        self.assertAlmostEqual(payload["tight_hi"], 93.55)
        self.assertAlmostEqual(payload["keystone_bid_qty"], 14870.0)
        self.assertAlmostEqual(payload["ask_ladder_notional"], 41431290.0)


class ProjectionInventoryTests(unittest.TestCase):
    def test_signal_inventory_and_headlines(self):
        from market_service.runtime import read_paths
        env = {
            "schema_version": 1, "symbol": "SOLUSDT", "status": "healthy",
            "canonical_state": {
                "calculations": {"calculations": {
                    "orderbook": {
                        "fut_keystone": {"bid": 93.5, "ask": 93.69},
                        "keystone_bid_stack": {"tight": {"total_qty": 100.0}},
                        "ask_wall_ladder": {"total_notional": 999.0},
                        "keystone_trade_intensity": {"tight": {"buy_qty": 1.0, "sell_qty": 2.0}},
                        "hourly_keystone_migration": {"verdict": "FLAT"},
                    },
                    "technical": {"seller_aggression": {"classification": "MEDIUM"}},
                }},
                "analysis": {"analysis": {"wall_migration": {}}},
            },
        }
        out = read_paths.market_inventory(env)
        self.assertIn("fut_keystone", out["orderbook_keys"])
        self.assertIn("seller_aggression", out["technical_keys"])
        # Headlines live inside the snapshot sub-projection now
        snap = out["snapshot"]
        self.assertEqual(snap["fut_keystone_bid"], 93.5)
        self.assertEqual(snap["seller_aggression"], "MEDIUM")
        self.assertEqual(snap["hourly_keystone_verdict"], "FLAT")

    def test_headlines_null_when_absent(self):
        from market_service.runtime import read_paths
        out = read_paths.market_inventory({"canonical_state": {}})
        snap = out["snapshot"]
        self.assertIsNone(snap["fut_keystone_bid"])
        self.assertIsNone(snap["seller_aggression"])


if __name__ == "__main__":
    unittest.main()
