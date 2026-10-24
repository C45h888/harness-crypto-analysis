"""Tests for the Pass 2 keystone pivot.

Covers:
- keystone_cycle_migration (pure cross-cycle migration verdict)
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


if __name__ == "__main__":
    unittest.main()
