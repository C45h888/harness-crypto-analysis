import unittest
from unittest.mock import AsyncMock, patch

from market_service.analysis.market import analyze


class FakeBinance:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def spot_book(self, symbol, limit):
        return {"lastUpdateId": 1, "bids": [["99", "10"]], "asks": [["101", "5"]]}

    async def fut_book(self, symbol, limit):
        return {"lastUpdateId": 2, "bids": [["100", "8"]], "asks": [["102", "4"]]}

    async def spot_24h(self, symbol):
        return {"symbol": symbol, "last_price": "100"}

    async def fut_24h(self, symbol):
        return {"symbol": symbol, "last_price": "101"}

    async def fut_funding(self, symbol):
        return {"last_funding_rate": "0.001", "mark_price": "101", "index_price": "100.5"}

    async def fut_open_interest(self, symbol):
        return {"open_interest": "1000"}

    async def spot_trades(self, symbol, limit):
        return [{"T": 1000, "a": 1, "p": "100", "q": "2", "m": False}]

    async def fut_trades(self, symbol, limit):
        return [{"T": 1000, "a": 2, "p": "101", "q": "1", "m": True}]

    async def fut_open_interest_history(self, symbol, period="5m", limit=30, start_time=None, end_time=None):
        return [{"timestamp": 300000, "sum_open_interest": "1000", "sum_open_interest_value": "101000"}]

    async def fut_taker_buy_sell(self, symbol, period="5m", limit=30, start_time=None, end_time=None):
        return [{"timestamp": 300000, "buy_vol": "60", "sell_vol": "40"}]

    async def fut_klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
        return [[300000, "100", "102", "99", "101", "1000"]]

    async def fut_funding_history(self, symbol, limit=30):
        return []


class ContractTests(unittest.IsolatedAsyncioTestCase):
    @patch("market_service.analysis.market.Binance", FakeBinance)
    @patch("market_service.analysis.market.cryptoquant_context", return_value={"available": False})
    async def test_snapshot_contains_raw_and_derived_sections(self, _cq):
        result = await analyze("BTCUSDT", trade_limit=1, depth_limit=1)
        self.assertEqual(result["contract"]["version"], 1)
        self.assertTrue(result["contract"]["raw_evidence_included"])
        self.assertEqual(result["evidence"]["spot"]["trades"][0]["p"], "100")
        self.assertEqual(result["spot"]["flow"]["cvd_usd"], 200.0)
        self.assertIn("signals", result)
        self.assertEqual(result["status"], "healthy")
        self.assertIn("coverage", result)


if __name__ == "__main__":
    unittest.main()
