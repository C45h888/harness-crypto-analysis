"""Pass-B1 wake-plane tests.

Covers the deterministic trigger plane of the statistical inference engine,
all nooa-free:

- ``evaluate_triggers``: the pure fire/no-fire matrix (event_delta threshold,
  cold start, capture recovery, unestablished capture, determinism).
- ``WakeEnvelope`` contract: round-trip, source/predicate validation.
- ``coalesce_wakes``: merging, cooldown deferral, empty drain.
- ``revalidate_wake``: two-phase wake verification against live counters.
- ``wake_dedupe_id``: identical conditions collapse to one wake.
"""

from __future__ import annotations

import unittest

from market_service.nooa_harness.inference import (
    CounterSnapshot,
    WakeConfig,
    build_wake_envelope,
    coalesce_wakes,
    evaluate_triggers,
    revalidate_wake,
    wake_dedupe_id,
)
from market_service.runtime.contracts import WakeEnvelope

CONFIG = WakeConfig(event_delta_threshold=1_800, cooldown_seconds=60)
NOW_MS = 1_700_000_600_000


def _snap(**overrides) -> CounterSnapshot:
    """Default snapshot: 1,500 events since the last artifact (below threshold)."""
    base: dict = {
        "event_stream_len": 2_500,
        "capture_state": "running",
        "last_artifact_events_total": 1_000,
        "last_artifact_capture_state": "running",
        "last_artifact_completed_at_ms": NOW_MS - 600_000,
    }
    base.update(overrides)
    return CounterSnapshot(**base)


class EvaluateTriggersTests(unittest.TestCase):
    def test_no_fire_when_delta_below_threshold(self):
        result = evaluate_triggers(_snap(), CONFIG, now_ms=NOW_MS)
        self.assertFalse(result["fired"])
        self.assertEqual(result["predicates"], {})

    def test_fires_event_delta_above_threshold(self):
        result = evaluate_triggers(_snap(event_stream_len=3_000), CONFIG, now_ms=NOW_MS)
        self.assertTrue(result["fired"])
        self.assertIn("event_delta", result["predicates"])
        detail = result["predicates"]["event_delta"]
        self.assertEqual(detail["new_events"], 2_000)
        self.assertEqual(detail["high_water"], 1_000)

    def test_cold_start_fires_without_prior_artifact(self):
        result = evaluate_triggers(
            _snap(last_artifact_events_total=None,
                  last_artifact_capture_state=None,
                  last_artifact_completed_at_ms=None),
            CONFIG, now_ms=NOW_MS,
        )
        self.assertTrue(result["fired"])
        self.assertIn("cold_start", result["predicates"])

    def test_capture_recovery_fires_on_gap_to_running(self):
        result = evaluate_triggers(
            _snap(event_stream_len=1_100, last_artifact_capture_state="gap"),
            CONFIG, now_ms=NOW_MS,
        )
        self.assertTrue(result["fired"])
        self.assertEqual(result["predicates"]["capture_recovery"]["from"], "gap")

    def test_capture_recovery_alone_fires_even_below_delta(self):
        # 100 new events (< 1800) but capture recovered from gap: re-infer.
        result = evaluate_triggers(
            _snap(event_stream_len=1_100, last_artifact_capture_state="gap"),
            CONFIG, now_ms=NOW_MS,
        )
        self.assertIn("capture_recovery", result["predicates"])

    def test_never_fires_when_capture_not_established(self):
        for state in ("starting", "stopped", None):
            with self.subTest(state=state):
                result = evaluate_triggers(
                    _snap(capture_state=state, last_artifact_events_total=None,
                          last_artifact_capture_state=None),
                    CONFIG, now_ms=NOW_MS,
                )
                self.assertFalse(result["fired"])

    def test_determinism_identical_inputs_identical_outputs(self):
        first = evaluate_triggers(_snap(), CONFIG, now_ms=NOW_MS)
        second = evaluate_triggers(_snap(), CONFIG, now_ms=NOW_MS)
        self.assertEqual(first, second)


class WakeEnvelopeContractTests(unittest.TestCase):
    def test_round_trip(self):
        envelope = build_wake_envelope(
            symbol="btcusdt", venue="spot", trigger_source="watcher",
            evaluation=evaluate_triggers(_snap(event_stream_len=3_000), CONFIG, now_ms=NOW_MS),
        )
        restored = WakeEnvelope.from_mapping(envelope.to_dict())
        self.assertEqual(restored, envelope)
        self.assertIn("event_delta", envelope.predicates_fired)

    def test_invalid_source_rejected(self):
        with self.assertRaises(ValueError):
            WakeEnvelope.create(
                symbol="BTCUSDT", venue="spot", trigger_source="aliens",
                predicates_fired={"manual": {}}, counter_snapshot={}, high_water={},
            )

    def test_invalid_predicate_rejected(self):
        with self.assertRaises(ValueError):
            WakeEnvelope.create(
                symbol="BTCUSDT", venue="spot", trigger_source="manual",
                predicates_fired={"moon_alignment": {}},
                counter_snapshot={}, high_water={},
            )

    def test_empty_predicates_rejected(self):
        with self.assertRaises(ValueError):
            WakeEnvelope.create(
                symbol="BTCUSDT", venue="spot", trigger_source="manual",
                predicates_fired={}, counter_snapshot={}, high_water={},
            )


def _wake(source: str, predicates: dict) -> WakeEnvelope:
    return WakeEnvelope.create(
        symbol="BTCUSDT", venue="spot", trigger_source=source,
        predicates_fired=predicates, counter_snapshot={}, high_water={},
    )


class CoalesceTests(unittest.TestCase):
    def test_merges_union_of_predicates_and_records_sources(self):
        a = _wake("manual", {"manual": {}})
        b = _wake("watcher", {"event_delta": {"new_events": 5_000, "high_water": 1_000}})
        merged, meta = coalesce_wakes(
            [a, b], cooldown_seconds=60, now_ms=NOW_MS,
            last_cycle_completed_at_ms=None,
        )
        self.assertEqual(meta["decision"], "fire")
        self.assertEqual(meta["merged_sources"], ["manual", "watcher"])
        assert merged is not None
        self.assertEqual(merged.predicates_fired["event_delta"]["new_events"], 5_000)
        self.assertEqual(meta["consumed_wake_ids"], [a.wake_id, b.wake_id])

    def test_keeps_strongest_event_delta_detail(self):
        a = _wake("watcher", {"event_delta": {"new_events": 2_000, "high_water": 1_000}})
        b = _wake("watcher", {"event_delta": {"new_events": 5_000, "high_water": 1_000}})
        merged, _meta = coalesce_wakes(
            [a, b], cooldown_seconds=60, now_ms=NOW_MS, last_cycle_completed_at_ms=None,
        )
        assert merged is not None
        self.assertEqual(merged.predicates_fired["event_delta"]["new_events"], 5_000)

    def test_cooldown_defers_never_drops(self):
        envelope = _wake("manual", {"manual": {}})
        _merged, meta = coalesce_wakes(
            [envelope], cooldown_seconds=60, now_ms=NOW_MS,
            last_cycle_completed_at_ms=NOW_MS - 10_000,
        )
        self.assertEqual(meta["decision"], "cooldown")
        self.assertEqual(meta["consumed_wake_ids"], [envelope.wake_id])

    def test_empty_drain(self):
        _merged, meta = coalesce_wakes(
            [], cooldown_seconds=60, now_ms=NOW_MS, last_cycle_completed_at_ms=None,
        )
        self.assertEqual(meta["decision"], "no_pending_wakes")


class RevalidateTests(unittest.TestCase):
    def test_stale_delta_is_dropped(self):
        envelope = _wake("watcher", {"event_delta": {"new_events": 2_000, "high_water": 1_000}})
        # Live stream barely above the high-water: delta already consumed.
        holds, detail = revalidate_wake(envelope, _snap(event_stream_len=1_400))
        self.assertFalse(holds)
        self.assertEqual(detail["reason"], "event_delta_consumed")

    def test_hold_when_delta_intact(self):
        envelope = _wake("watcher", {"event_delta": {"new_events": 2_000, "high_water": 1_000}})
        holds, detail = revalidate_wake(envelope, _snap(event_stream_len=3_500))
        self.assertTrue(holds)
        self.assertEqual(detail["reason"], "holds")

    def test_drop_when_capture_not_established(self):
        envelope = _wake("manual", {"manual": {}})
        holds, detail = revalidate_wake(envelope, _snap(capture_state="stopped"))
        self.assertFalse(holds)
        self.assertEqual(detail["reason"], "capture_not_established")

    def test_cold_start_holds_when_established(self):
        envelope = _wake("watcher", {"cold_start": {"capture_state": "running"}})
        holds, _detail = revalidate_wake(envelope, _snap())
        self.assertTrue(holds)


class DedupeTests(unittest.TestCase):
    def test_identical_conditions_collapse(self):
        predicates = {"event_delta": {"new_events": 2_000}}
        high_water = {"events_total": 1_000}
        self.assertEqual(
            wake_dedupe_id("BTCUSDT", "spot", predicates, high_water),
            wake_dedupe_id("BTCUSDT", "spot", predicates, high_water),
        )
        self.assertNotEqual(
            wake_dedupe_id("BTCUSDT", "spot", predicates, high_water),
            wake_dedupe_id("BTCUSDT", "spot", {"cold_start": {}}, high_water),
        )


if __name__ == "__main__":
    unittest.main()
