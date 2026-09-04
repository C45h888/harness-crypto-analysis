import unittest
from unittest.mock import AsyncMock, patch

from market_service.commands.harness import build
from market_service.config import Settings

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


if __name__ == "__main__":
    unittest.main()
