import json
import unittest
from unittest.mock import AsyncMock, patch

from market_service.commands.collate import _domain_outputs, build_envelope, build_envelope_from_domain_states
from market_service.runtime.contracts import MarketRunEnvelope, MarketStateEnvelope
from market_service.config import Settings


CORE = {
    "symbol": "SOLUSDT",
    "status": "degraded",
    "data_source": "binance_public_rest_live",
    "errors": [{"endpoint": "macro", "error": "timeout"}],
    "coverage": {"spot_trade_count": 10, "futures_trade_count": 10},
    "evidence": {"spot": {"trades": []}},
    "spot": {"flow": {"cvd_usd": 1.0}, "bucketed_cvd": []},
    "futures": {"flow": {"cvd_usd": 2.0}, "bucketed_cvd": []},
    "correlation": {"aligned_buckets": 0},
    "signal_inputs": {"spot_buy_share": 0.5},
    "signals": [],
    "open_interest_analysis": None,
    "liquidation_pressure": None,
    "macro_analysis": None,
    "cryptoquant": {"available": False},
}


class CollationTests(unittest.IsolatedAsyncioTestCase):
    def test_domain_outputs_are_canonical_and_json_safe(self):
        output = _domain_outputs(CORE)
        self.assertIn("data_access", output)
        self.assertEqual(output["calculations"]["spot_flow"]["cvd_usd"], 1.0)
        self.assertEqual(json.loads(json.dumps(output))["analysis"]["cryptoquant"]["available"], False)

    def test_envelope_round_trip_preserves_degraded_state_and_nulls(self):
        envelope = MarketRunEnvelope.create(
            symbol="solusdt", generated_at="2026-08-12T00:00:00+00:00",
            completed_at="2026-08-12T00:00:01+00:00", status="degraded",
            data_source="test", coverage=CORE["coverage"], canonical_state=CORE,
            domain_outputs=_domain_outputs(CORE), errors=CORE["errors"],
        )
        restored = MarketRunEnvelope.from_mapping(json.loads(envelope.to_json()))
        self.assertEqual(restored.run_id, envelope.run_id)
        self.assertEqual(restored.status, "degraded")
        self.assertIsNone(restored.domain_outputs["analysis"]["macro"])

    @patch("market_service.commands.collate.analyze", new_callable=AsyncMock, return_value=CORE)
    async def test_build_envelope_uses_one_run_id_and_canonical_status(self, _analyze):
        envelope = await build_envelope("SOLUSDT", 10, 20, 60)
        self.assertEqual(envelope.symbol, "SOLUSDT")
        self.assertEqual(envelope.status, "degraded")
        self.assertTrue(envelope.run_id)
        self.assertEqual(envelope.source_metadata["requested"]["depth"], 20)

    @patch("market_service.commands.collate.RedisRuntimeStore")
    async def test_domain_collation_uses_exact_run_envelopes(self, store_cls):
        run_id = "00000000-0000-4000-8000-000000000001"
        def state(source, status="healthy"):
            return MarketStateEnvelope(
                symbol="SOLUSDT", source=source, run_id=run_id,
                observed_at="2026-08-12T00:00:00+00:00",
                produced_at="2026-08-12T00:00:00+00:00",
                status=status, data={"source_marker": source},
            )
        store = store_cls.return_value
        store.read_run_domain_state = AsyncMock(side_effect=[
            state("data-access"), state("calculations"), state("analysis"),
        ])
        store.close = AsyncMock()
        settings = Settings(
            database_url="postgresql://x/y", redis_url="redis://x:6379/0",
            redis_key_prefix="marketflow", redis_stream_maxlen=1000,
            symbols=("SOLUSDT",), poll_seconds=30, flow_window_seconds=300,
            depth_levels=20, max_domain_state_age_seconds=999999999,
        )
        envelope = await build_envelope_from_domain_states("SOLUSDT", run_id, settings)
        self.assertEqual(envelope.run_id, run_id)
        self.assertEqual(envelope.status, "healthy")
        self.assertEqual(envelope.domain_outputs["calculations"]["run_id"], run_id)
        self.assertEqual(envelope.canonical_state["analysis"]["source_marker"], "analysis")

    @patch("market_service.commands.collate.RedisRuntimeStore")
    async def test_missing_domain_makes_run_invalid(self, store_cls):
        run_id = "00000000-0000-4000-8000-000000000002"
        store = store_cls.return_value
        store.read_run_domain_state = AsyncMock(return_value=None)
        store.close = AsyncMock()
        settings = Settings(
            database_url="postgresql://x/y", redis_url="redis://x:6379/0",
            redis_key_prefix="marketflow", redis_stream_maxlen=1000,
            symbols=("SOLUSDT",), poll_seconds=30, flow_window_seconds=300,
            depth_levels=20, max_domain_state_age_seconds=999999999,
        )
        envelope = await build_envelope_from_domain_states("SOLUSDT", run_id, settings)
        self.assertEqual(envelope.status, "invalid")
        self.assertEqual(envelope.canonical_state["domain_status"]["analysis"], "missing")


if __name__ == "__main__":
    unittest.main()
