"""Pass-A inference-engine mechanics tests.

Covers the three mechanical foundations of the agent role shift
(interpretation layer -> inference engine), all nooa-free:

1. ``InferenceArtifact`` contract — round-trip, validation, and the hard
   rule that an insufficient artifact can never carry an interpretation.
2. (retired) the status trichotomy moved out of the agentic path
   inputs always yield identical status + reasons.
3. Capability registry — bounded scope denial and pure dispatch paths
   (fitting.replay / fitting.assemble_evidence over synthetic fixtures,
   no Redis required).
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from market_service.microstructure.contracts import OFIInterval
from market_service.microstructure.ofi import DEPTH_ESTIMATOR
from market_service.nooa_harness.inference import (
    CAPABILITIES,
    CapabilityDenied,
    dispatch_assemble_evidence,
    dispatch_replay,
)
from market_service.runtime.contracts import (
    INFERENCE_ARTIFACT_SCHEMA_VERSION,
    InferenceArtifact,
)


def _artifact(**overrides) -> InferenceArtifact:
    base: dict = {
        "symbol": "BTCUSDT", "venue": "spot",
        "generated_at": "2026-08-28T00:00:00+00:00",
        "completed_at": "2026-08-28T00:00:30+00:00",
        "status": "validated", "window_minutes": 30, "interval_seconds": 10,
        "deterministic_state": {"price_impact_fit": {"beta": "0.001"}},
        "capability_log": [{"capability": "fitting.replay", "result": "ok"}],
        "input_hash": "abc123", "model_version": "ofi-depth-v1",
        "interpretation": {"summary": "positive beta"},
    }
    base.update(overrides)
    return InferenceArtifact.create(**base)


class InferenceArtifactContractTests(unittest.TestCase):
    def test_create_assigns_id_and_round_trips(self):
        artifact = _artifact()
        self.assertTrue(artifact.artifact_id)
        restored = InferenceArtifact.from_mapping(artifact.to_dict())
        self.assertEqual(restored, artifact)

    def test_insufficient_artifact_rejects_interpretation(self):
        # create() with NULL interpretation round-trips.
        artifact = _artifact(status="insufficient", interpretation=None)
        self.assertIsNone(artifact.interpretation)
        artifact.validate()
        # create() itself must refuse to produce insufficient + interpretation:
        # the hard gate is structural, not advisory.
        with self.assertRaises(ValueError):
            _artifact(status="insufficient", interpretation={"summary": "must not exist"})

    def test_insufficient_artifact_allows_null_interpretation(self):
        artifact = _artifact(status="insufficient", interpretation=None)
        self.assertIsNone(artifact.interpretation)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            InferenceArtifact(
                artifact_id="x", symbol="BTCUSDT", venue="spot",
                generated_at="2026-08-28T00:00:00+00:00",
                completed_at="2026-08-28T00:00:30+00:00",
                status="degraded", window_minutes=30, interval_seconds=10,
                deterministic_state={}, capability_log=(),
                input_hash="abc", model_version="ofi-depth-v1",
            ).validate()

    def test_nonpositive_window_rejected(self):
        with self.assertRaises(ValueError):
            InferenceArtifact(
                artifact_id="x", symbol="BTCUSDT", venue="spot",
                generated_at="2026-08-28T00:00:00+00:00",
                completed_at="2026-08-28T00:00:30+00:00",
                status="validated", window_minutes=0, interval_seconds=10,
                deterministic_state={}, capability_log=(),
                input_hash="abc", model_version="ofi-depth-v1",
            ).validate()

    def test_schema_version_is_one(self):
        self.assertEqual(INFERENCE_ARTIFACT_SCHEMA_VERSION, 1)
        with self.assertRaises(ValueError):
            InferenceArtifact(
                artifact_id="x", symbol="BTCUSDT", venue="spot",
                generated_at="2026-08-28T00:00:00+00:00",
                completed_at="2026-08-28T00:00:30+00:00",
                status="validated", window_minutes=30, interval_seconds=10,
                deterministic_state={}, capability_log=(),
                input_hash="abc", model_version="ofi-depth-v1",
                schema_version=2,
            ).validate()





def _synthetic_event_payload(k: int, contribution: str, ts: int) -> dict:
    quote = {
        "schema_version": 1, "symbol": "BTCUSDT", "venue": "spot",
        "update_id": k, "exchange_ts_ms": ts, "received_ts_ms": ts,
        "bid_price": "100", "bid_qty": "10", "ask_price": "101", "ask_qty": "20",
    }
    return {
        "schema_version": 1, "event_type": "best_quote_transition",
        "source_quality": "exact_feed", "contribution": contribution,
        "previous": quote, "current": quote,
    }


def _synthetic_interval(k: int) -> OFIInterval:
    return OFIInterval(
        symbol="BTCUSDT", venue="spot", start_ts_ms=k * 10_000,
        end_ts_ms=(k + 1) * 10_000, event_count=1, ofi=Decimal(5),
        average_depth=Decimal(15), first_update_id=k, last_update_id=k,
        quality="exact_feed", mid_start=Decimal("100.5"), mid_end=Decimal("100.6"),
        depth_estimator=DEPTH_ESTIMATOR,
    )


class CapabilityRegistryTests(unittest.TestCase):
    def test_registry_scope_is_frozen(self):
        for name, cap in CAPABILITIES.items():
            self.assertEqual(cap.name, name)
            # Format gate: unknown venue vocabulary is denied.
            with self.assertRaises(CapabilityDenied):
                cap.validate_scope("BTCUSDT", "binance-options")
            # Format gate accepts any well-formed symbol; liveness is a
            # separate gate against the live active set.
            cap.validate_scope("XRPUSDT", "spot")
            with self.assertRaises(CapabilityDenied):
                cap.validate_live_scope("XRPUSDT", ("SOLUSDT",))
            cap.validate_live_scope("SOLUSDT", ("SOLUSDT",))
            cap.validate_live_scope("XRPUSDT", None)

    def test_replay_dispatch_ok_and_denied_paths(self):
        payloads = [_synthetic_event_payload(1, "5", 100)]
        events, intervals, dropped, log = dispatch_replay(
            payloads, symbol="BTCUSDT", venue="spot", interval_ms=10_000,
        )
        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(len(intervals), 1)
        self.assertEqual(dropped, 0)
        self.assertEqual(log["result"], "ok")

        _e, _i, _d, denied = dispatch_replay(
            payloads, symbol="XRPUSDT", venue="binance-options", interval_ms=10_000,
        )
        self.assertEqual(denied["result"], "denied")

    def test_assemble_evidence_dispatch_ok_and_denied_paths(self):
        intervals = [_synthetic_interval(k) for k in range(40)]
        evidence, log = dispatch_assemble_evidence(
            intervals, symbol="BTCUSDT", venue="spot", tick_size=Decimal("0.01"),
            interval_seconds=10, evidence_id="ev-test", generated_at_ms=400_000,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(log["result"], "ok")
        self.assertEqual(evidence.symbol, "BTCUSDT")

        _none, denied = dispatch_assemble_evidence(
            intervals, symbol="XRPUSDT", venue="binance-options", tick_size=Decimal("0.01"),
            interval_seconds=10, evidence_id="ev-x", generated_at_ms=400_000,
        )
        self.assertEqual(denied["result"], "denied")


if __name__ == "__main__":
    unittest.main()
