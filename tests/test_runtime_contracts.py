import json
import unittest

from market_service.runtime.contracts import MarketEvent, MarketStateEnvelope, RefreshCommand


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

    def test_event_and_command_are_transport_safe(self):
        event = MarketEvent("signal", "SOLUSDT", {"severity": 3})
        command = RefreshCommand("order_book", "solusdt", parameters={"depth": 20})
        self.assertEqual(event.to_fields()["symbol"], "SOLUSDT")
        self.assertEqual(json.loads(command.to_fields()["parameters"])["depth"], 20)


if __name__ == "__main__":
    unittest.main()
