import unittest
from unittest.mock import AsyncMock, patch

from market_service.commands.harness import build, trigger_full_cycle
from market_service.config import Settings
from market_service.runtime.contracts import RuntimeRunState

FAKE_CORE = {
    "contract": {"name": "crypto-ai-market-snapshot", "version": 1},
    "symbol": "SOLUSDT",
    "status": "healthy",
    "errors": [],
    "spot": {"flow": {"cvd_usd": 100.0}},
    "futures": {"flow": {"cvd_usd": 150.0}},
    "coverage": {},
    "signals": [],
}


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    @patch("market_service.commands.harness.analyze",
           new_callable=AsyncMock, return_value=FAKE_CORE)
    async def test_build_returns_clean_contract(self, _a):
        out = await build("SOLUSDT", 500, 50, 60)
        self.assertEqual(out["contract"]["name"], "crypto-ai-market-harness")
        self.assertEqual(out["contract"]["version"], 2)
        self.assertEqual(out["symbol"], "SOLUSDT")
        self.assertEqual(out["status"], "healthy")
        self.assertEqual(out["core"]["status"], "healthy")
        self.assertEqual(out["requested"]["trade_limit"], 500)

    @patch("market_service.commands.harness.RedisRuntimeStore")
    async def test_trigger_full_cycle_uses_exact_request_run_and_scope(self, store_cls):
        store = store_cls.return_value
        store.write_runtime_run = AsyncMock()
        store.request_harness_run = AsyncMock(return_value="1-0")
        store.read_runtime_run = AsyncMock(return_value={
            "phase": "PUBLISHED",
            "run_id": "request-id",
        })
        envelope = type("Envelope", (), {
            "to_dict": lambda self: {"run_id": "request-id", "status": "degraded"}
        })()
        store.read_run = AsyncMock(return_value=envelope)
        store.close = AsyncMock()

        test_settings = Settings(
            database_url="postgresql://x/y",
            redis_url="redis://redis:6379/0",
            redis_key_prefix="marketflow",
            redis_stream_maxlen=1000,
            symbols=("SOLUSDT",),
            poll_seconds=30,
            flow_window_seconds=300,
            depth_levels=20,
        )
        with patch("market_service.commands.harness.Settings.from_env", return_value=test_settings), \
             patch("market_service.commands.harness.uuid.uuid4", return_value="request-id"):
            result = await trigger_full_cycle("solusdt", 1.0, "order_book")

        request = store.request_harness_run.await_args.args[0]
        self.assertEqual(request.symbol, "solusdt")
        self.assertEqual(request.parameters, {"scope": "order_book"})
        self.assertEqual(result["envelope"]["run_id"], "request-id")
        self.assertEqual(result["scope"], "order_book")
        state = store.write_runtime_run.await_args.args[0]
        self.assertIsInstance(state, RuntimeRunState)
        self.assertEqual(state.phase, "REQUESTED")


if __name__ == "__main__":
    unittest.main()
