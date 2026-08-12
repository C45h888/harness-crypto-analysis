"""Test that the Redis domain keys/streams match the contract literally.

Per CONTAINERIZATION_CONTRACT.md, the new keys MUST follow these names:
  <prefix>:latest:<SYMBOL>:data-access
  <prefix>:latest:<SYMBOL>:calculations
  <prefix>:latest:<SYMBOL>:analysis
  <prefix>:stream:domain:data-access:<SYMBOL>
  <prefix>:stream:domain:calculations:<SYMBOL>
  <prefix>:stream:domain:analysis:<SYMBOL>

The collated stream must remain untrimmed (default MAXLEN == None).
"""

import re
import unittest

from market_service.runtime.contracts import (
    MARKET_STATE_SCHEMA_VERSION,
    MarketStateEnvelope,
)
from market_service.runtime.redis_store import RedisRuntimeStore


class DomainKeyContractTests(unittest.TestCase):
    def setUp(self):
        # Construct without touching redis.asyncio (which requires a running
        # loop on Python <3.10). Tests only need key formatting.
        self.store = RedisRuntimeStore.__new__(RedisRuntimeStore)
        self.store.prefix = "marketflow"
        self.store.stream_maxlen = 10000

    def test_domain_latest_key_matches_contract(self):
        cases = [
            ("SOLUSDT", "data-access", "marketflow:latest:SOLUSDT:data-access"),
            ("SOLUSDT", "calculations", "marketflow:latest:SOLUSDT:calculations"),
            ("SOLUSDT", "analysis", "marketflow:latest:SOLUSDT:analysis"),
            ("btcusdt", "Data-Access", "marketflow:latest:BTCUSDT:data-access"),
        ]
        for symbol, source, expected in cases:
            with self.subTest(symbol=symbol, source=source):
                self.assertEqual(self.store.domain_latest_key(symbol, source), expected)

    def test_domain_stream_matches_contract(self):
        cases = [
            ("SOLUSDT", "data-access", "marketflow:stream:domain:data-access:SOLUSDT"),
            ("SOLUSDT", "calculations", "marketflow:stream:domain:calculations:SOLUSDT"),
            ("SOLUSDT", "analysis", "marketflow:stream:domain:analysis:SOLUSDT"),
        ]
        for symbol, source, expected in cases:
            with self.subTest(symbol=symbol, source=source):
                self.assertEqual(self.store.domain_stream(symbol, source), expected)

    def test_preexisting_keys_still_exist(self):
        # Per the contract, the existing keys must be preserved.
        self.assertEqual(
            self.store.latest_key("SOLUSDT", "collector"),
            "marketflow:latest:SOLUSDT:collector",
        )
        self.assertEqual(
            self.store.telemetry_stream("SOLUSDT"),
            "marketflow:stream:market:SOLUSDT",
        )
        self.assertEqual(
            self.store.collated_stream("SOLUSDT"),
            "marketflow:stream:collated:SOLUSDT",
        )
        self.assertEqual(
            self.store.command_stream, "marketflow:stream:commands",
        )
        self.assertEqual(
            self.store.result_stream, "marketflow:stream:results",
        )

    def test_publish_domain_state_rejects_unknown_source(self):
        import asyncio
        async def _go():
            envelope = MarketStateEnvelope(
                symbol="SOLUSDT",
                source="bogus",
                observed_at="2026-08-12T00:00:00+00:00",
                produced_at="2026-08-12T00:00:00+00:00",
                status="healthy",
                data={},
            )
            with self.assertRaises(ValueError):
                await self.store.publish_domain_state(envelope)
        asyncio.run(_go())

    def test_envelope_validation_rejects_unsupported_schema_version(self):
        bad = MarketStateEnvelope(
            symbol="SOLUSDT",
            source="data-access",
            observed_at="2026-08-12T00:00:00+00:00",
            produced_at="2026-08-12T00:00:00+00:00",
            status="healthy",
            data={},
            schema_version=2,
        )
        with self.assertRaises(ValueError):
            MarketStateEnvelope.from_mapping(bad.to_dict())

    def test_envelope_schema_version_is_one(self):
        self.assertEqual(MARKET_STATE_SCHEMA_VERSION, 1)


if __name__ == "__main__":
    unittest.main()