import json
import unittest

from market_service.runtime.contracts import MarketStateEnvelope


class RuntimeContractTests(unittest.TestCase):
    def test_state_round_trip_preserves_nulls_and_version(self):
        state = MarketStateEnvelope.from_mapping({
            "symbol": "solusdt",
            "source": "order_book",
            "observed_at": "2026-08-12T00:00:00+00:00",
            "status": "degraded",
            "data": {"obi": None},
            "errors": [{"endpoint": "depth", "error": "timeout"}],
        })
        decoded = json.loads(state.to_json())
        self.assertEqual(decoded["symbol"], "SOLUSDT")
        self.assertIsNone(decoded["data"]["obi"])
        self.assertEqual(decoded["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()


class ValidatorHardeningContractTests(unittest.TestCase):
    def _artifact_kwargs(self, **overrides):
        base = {
            "symbol": "SOLUSDT",
            "venue": "futures",
            "generated_at": "2026-09-10T00:00:00+00:00",
            "completed_at": "2026-09-10T00:00:01+00:00",
            "status": "validated",
            "window_minutes": 30,
            "interval_seconds": 10,
            "deterministic_state": {},
            "capability_log": [],
            "input_hash": "abc",
            "model_version": "inference-engine-v1",
            "interpretation": {
                "summary": "ok",
                "evidence": [
                    {"path": "calc.price.delta → x", "interpretation": "shows delta"},
                ],
                "confidence": "medium",
            },
        }
        base.update(overrides)
        return base

    def test_normalize_confidence_aliases(self):
        from market_service.runtime.contracts import normalize_confidence
        self.assertEqual(normalize_confidence("moderate"), "medium")
        self.assertEqual(normalize_confidence("low-medium"), "low")
        self.assertEqual(normalize_confidence("MEDIUM_HIGH"), "medium")
        self.assertEqual(normalize_confidence("high"), "high")
        self.assertIsNone(normalize_confidence("ultra"))
        self.assertIsNone(normalize_confidence(None))

    def test_artifact_rejects_bad_confidence(self):
        from market_service.runtime.contracts import InferenceArtifact
        with self.assertRaises(ValueError):
            InferenceArtifact.create(**self._artifact_kwargs(
                interpretation={"summary": "x",
                                "evidence": [{"path": "a", "interpretation": "b"}],
                                "confidence": "ultra"}))

    def test_artifact_accepts_alias_confidence(self):
        from market_service.runtime.contracts import InferenceArtifact
        art = InferenceArtifact.create(**self._artifact_kwargs(
            interpretation={"summary": "x",
                            "evidence": [{"path": "a", "interpretation": "b"}],
                            "confidence": "moderate"}))
        self.assertEqual(art.interpretation["confidence"], "moderate")

    def test_artifact_rejects_empty_interpretation(self):
        from market_service.runtime.contracts import InferenceArtifact
        with self.assertRaises(ValueError):
            InferenceArtifact.create(**self._artifact_kwargs(
                interpretation={"summary": "x",
                                "evidence": [{"path": "calc.price.delta → x",
                                              "interpretation": ""}],
                                "confidence": "medium"}))
