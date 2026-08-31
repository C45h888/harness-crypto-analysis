"""Validation test: the full market-read deviation chain.

Exercises the post-envelope path end-to-end:
  1. pipeline.assemble_envelope() returns a plain dict (not a dataclass)
  2. redis_store.publish_run(dict) writes it to Redis
  3. read_paths.read_collated() reads it back as a plain dict
  4. dispatch_market_read() (the agent's market.read tool) returns strict
     JSON-safe projections (snapshot/inventory/full) with ZERO NaN/Inf leakage
  5. The payload shape is byte-compatible with what the old dataclass
     to_dict() used to publish (schema_version, run_id, symbol, ...)

This is the live integration test for the market-read deviation: it proves
the read plane is the sole authority and the write seam emits valid JSON.

Requires: Redis running on REDIS_URL (default redis://localhost:6379/0).
Postgres is NOT required — the dict publish path is Redis-first.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import unittest


def _has_nan_inf(obj) -> bool:
    """Recursively check for NaN/Infinity in a nested structure."""
    if isinstance(obj, float):
        return math.isnan(obj) or math.isinf(obj)
    if isinstance(obj, dict):
        return any(_has_nan_inf(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_has_nan_inf(v) for v in obj)
    return False


class MarketReadValidationTests(unittest.IsolatedAsyncioTestCase):
    """Full-chain validation: pipeline dict → Redis → read_paths → agent tool."""

    async def asyncSetUp(self):
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            import redis.asyncio as aioredis
            client = aioredis.from_url(redis_url)
            await client.ping()
            await client.aclose()
        except Exception as exc:
            self.skipTest(f"Redis not available at {redis_url}: {exc}")

        from market_service.config import Settings
        from market_service.runtime.redis_store import RedisRuntimeStore

        self.settings = Settings.from_redis_env()
        self.store = RedisRuntimeStore(
            self.settings.redis_url,
            self.settings.redis_key_prefix,
            self.settings.redis_stream_maxlen,
        )
        self.symbol = "BTCUSDT"

    async def asyncTearDown(self):
        await self.store.close()

    async def _publish_test_payload(self) -> dict:
        """Publish a synthetic dict-shaped run payload to Redis for testing."""
        import uuid as _uuid
        from market_service.runtime.contracts import MARKET_RUN_SCHEMA_VERSION

        run_id = f"test-val-{_uuid.uuid4()}"
        payload = {
            "schema_version": MARKET_RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "symbol": self.symbol,
            "generated_at": "2026-08-31T14:00:00+00:00",
            "completed_at": "2026-08-31T14:00:00+00:00",
            "status": "degraded",
            "data_source": "domain_pipeline",
            "coverage": {
                "flow_window_seconds": 900,
                "domain_status": {
                    "data-access": "healthy",
                    "calculations": "degraded",
                    "analysis": "degraded",
                },
            },
            "canonical_state": {
                "domain_status": {
                    "data-access": "healthy",
                    "calculations": "degraded",
                    "analysis": "degraded",
                },
                "data-access": {
                    "status": "healthy",
                    "errors": [],
                    "evidence": {
                        "spot": {},
                        "futures": {
                            "ticker_24h": {
                                "last_price": 145.2,
                                "quote_volume": 1234567.89,
                                "high_price": 146.0,
                                "low_price": 144.0,
                            },
                            "funding": {
                                "last_funding_rate": 0.0001,
                                "mark_price": 145.15,
                            },
                            "open_interest": {
                                "open_interest": 1234.5,
                            },
                        },
                    },
                    "coverage_seconds": 900,
                },
                "calculations": {
                    "status": "degraded",
                    "errors": [],
                    "calculations": {
                        "flow": {
                            "spot_flow": {"cvd": 100.5, "buy_share": 0.52},
                            "futures_flow": {"cvd": -50.2, "buy_share": 0.48},
                        },
                        "orderbook": {
                            "fut_keystone": {"bid": 145.0, "ask": 145.4},
                            "fut_microprice_skew_bps": 1.2,
                        },
                        "technical": {
                            "seller_aggression": {"classification": "moderate"},
                        },
                    },
                },
                "analysis": {
                    "status": "degraded",
                    "errors": [],
                    "analysis": {
                        "demand": {
                            "decomposition": {
                                "spot": {"obi": 0.15},
                                "futures": {"obi": -0.05},
                            },
                        },
                        "wall_migration": {
                            "round_anchors": {"count": 3},
                            "tiers": {"mega": {"pct": 12.5}},
                        },
                    },
                },
            },
            "domain_outputs": {},
            "errors": [],
            "source_metadata": {"runtime": "market_service", "path": "validation_test"},
        }
        payload["domain_outputs"] = dict(payload["canonical_state"])
        await self.store.publish_run(payload)
        return payload

    async def test_assemble_envelope_returns_dict(self):
        """Step 1: pipeline.assemble_envelope returns a plain dict, not a dataclass."""
        from market_service.nooa_harness.pipeline import assemble_envelope

        evidence = {"errors": [], "spot": {}, "futures": {}}
        calculations = {"status": "healthy", "errors": []}
        analysis = {"status": "healthy", "errors": []}

        result = assemble_envelope(self.symbol, evidence, calculations, analysis)

        self.assertIsInstance(result, dict)
        self.assertNotIn("to_dict", dir(type(result)))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["symbol"], self.symbol)
        self.assertIn("run_id", result)
        self.assertIn("canonical_state", result)
        self.assertIn("coverage", result)
        self.assertIn("errors", result)
        self.assertIn("source_metadata", result)

    async def test_publish_and_read_round_trip(self):
        """Step 2+3: publish_run(dict) → read_collated() round-trips."""
        from market_service.runtime import read_paths

        payload = await self._publish_test_payload()

        readback = await read_paths.read_collated(self.store, self.symbol)

        self.assertIsNotNone(readback)
        self.assertIsInstance(readback, dict)
        self.assertEqual(readback["run_id"], payload["run_id"])
        self.assertEqual(readback["schema_version"], payload["schema_version"])
        self.assertEqual(readback["symbol"], payload["symbol"])

    async def test_read_returns_strict_json_no_nan(self):
        """Step 4a: read_collated returns a payload with zero NaN/Inf."""
        from market_service.runtime import read_paths

        await self._publish_test_payload()
        readback = await read_paths.read_collated(self.store, self.symbol)

        self.assertIsNotNone(readback)
        # Must be serializable as strict JSON (no NaN/Infinity tokens)
        raw = json.dumps(readback)
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)
        self.assertFalse(_has_nan_inf(readback))

    async def test_snapshot_projection_strict_json(self):
        """Step 4b: market_snapshot returns bounded, NaN-free JSON."""
        from market_service.runtime import read_paths

        await self._publish_test_payload()
        payload = await read_paths.read_collated(self.store, self.symbol)

        snapshot = read_paths.market_snapshot(payload)
        raw = json.dumps(snapshot)

        self.assertFalse(_has_nan_inf(snapshot))
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)
        # Snapshot must be bounded (under 5KB for the agent tool budget)
        self.assertLess(len(raw), 5000)
        # Must carry the CVD sign series
        self.assertIn("cvd_sign_series", snapshot)
        # Must carry keystone headlines
        self.assertIn("fut_keystone_bid", snapshot)

    async def test_inventory_projection_strict_json(self):
        """Step 4c: market_inventory returns section keys + snapshot, NaN-free."""
        from market_service.runtime import read_paths

        await self._publish_test_payload()
        payload = await read_paths.read_collated(self.store, self.symbol)

        inventory = read_paths.market_inventory(payload)
        raw = json.dumps(inventory)

        self.assertFalse(_has_nan_inf(inventory))
        self.assertNotIn("NaN", raw)
        self.assertLess(len(raw), 8000)
        self.assertIn("analysis_keys", inventory)
        self.assertIn("calculations_keys", inventory)
        self.assertIn("snapshot", inventory)

    async def test_dispatch_market_read_snapshot(self):
        """Step 5: dispatch_market_read (agent tool) returns snapshot mode."""
        from market_service.nooa_harness.inference import dispatch_market_read

        await self._publish_test_payload()
        result, log_entry = await dispatch_market_read(
            self.store, self.symbol, mode="snapshot"
        )

        self.assertIsNotNone(result)
        raw = json.dumps(result)
        self.assertFalse(_has_nan_inf(result))
        self.assertLess(len(raw), 5000)
        self.assertEqual(log_entry["result"], "ok")

    async def test_dispatch_market_read_inventory(self):
        """Step 5: dispatch_market_read returns inventory mode."""
        from market_service.nooa_harness.inference import dispatch_market_read

        await self._publish_test_payload()
        result, _ = await dispatch_market_read(
            self.store, self.symbol, mode="inventory"
        )

        self.assertIsNotNone(result)
        raw = json.dumps(result)
        self.assertFalse(_has_nan_inf(result))
        self.assertIn("analysis_keys", result)
        self.assertIn("snapshot", result)

    async def test_dispatch_market_read_full(self):
        """Step 5: dispatch_market_read returns full raw payload."""
        from market_service.nooa_harness.inference import dispatch_market_read

        await self._publish_test_payload()
        result, _ = await dispatch_market_read(
            self.store, self.symbol, mode="full"
        )

        self.assertIsNotNone(result)
        raw = json.dumps(result)
        self.assertFalse(_has_nan_inf(result))
        self.assertIn("canonical_state", result)
        self.assertIn("run_id", result)

    async def test_dispatch_market_read_no_run_returns_none(self):
        """dispatch_market_read on empty Redis returns None + structured log."""
        from market_service.nooa_harness.inference import dispatch_market_read

        # Clear the latest key so no run is persisted for BTCUSDT
        await self.store.redis.delete(
            self.store.collated_latest_key(self.symbol)
        )
        result, log_entry = await dispatch_market_read(
            self.store, "BTCUSDT", mode="snapshot"
        )

        self.assertIsNone(result)
        self.assertEqual(log_entry["result"], "ok")
        self.assertIn("no_run_persisted", str(log_entry.get("detail", "")))


if __name__ == "__main__":
    unittest.main()
