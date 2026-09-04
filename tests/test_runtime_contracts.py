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
