import unittest
from unittest.mock import AsyncMock, patch

from market_service.commands.harness import build

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
        out = await build("SOLUSDT", 500, 50, 60, with_scripts=False)
        self.assertEqual(out["contract"]["name"], "crypto-ai-market-harness")
        self.assertEqual(out["contract"]["version"], 1)
        self.assertEqual(out["symbol"], "SOLUSDT")
        self.assertEqual(out["status"], "healthy")
        self.assertEqual(out["core"]["status"], "healthy")
        self.assertNotIn("legacy_script_runs", out)

    @patch("market_service.commands.harness._run_legacy",
           new_callable=AsyncMock,
           side_effect=lambda spec, symbol: {
               "script": spec["name"], "domain": spec["domain"], "kind": "prose",
               "ok": True, "exit": 0, "timed_out": False, "output": "x", "stderr": ""})
    @patch("market_service.commands.harness.analyze",
           new_callable=AsyncMock, return_value=FAKE_CORE)
    async def test_build_with_scripts_adds_curated_runs(self, _a, _r):
        out = await build("SOLUSDT", 500, 50, 60, with_scripts=True)
        self.assertIn("legacy_script_runs", out)
        scripts = {r["script"] for r in out["legacy_script_runs"]}
        self.assertIn("session_regime", scripts)
        self.assertIn("auction_dynamics", scripts)


if __name__ == "__main__":
    unittest.main()
