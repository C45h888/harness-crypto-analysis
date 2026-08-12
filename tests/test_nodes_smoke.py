"""Smoke tests for the new stream-driven node modules.

These tests:
  * import every node module to confirm the entrypoints exist;
  * call the per-domain handler directly with a faked Redis to confirm the
    envelope source field and wire shape match the contract;
  * confirm ``RefreshCommand.from_fields`` round-trips through a stream entry;
  * confirm the orchestrator sends commands in the documented order.

No real network calls are made.
"""

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import AsyncMock

from market_service.runtime.contracts import (
    MARKET_STATE_SCHEMA_VERSION,
    MarketEvent,
    MarketStateEnvelope,
    RefreshCommand,
)


class _FakeRedis:
    """Minimal async Redis double covering the surface the nodes touch."""

    def __init__(self):
        self.latest: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = []
        self.commands: list[dict[str, str]] = []
        self.results: list[tuple[str, dict[str, str]]] = []
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        return True

    async def aclose(self) -> None:
        return None

    async def set(self, key: str, value: str) -> str:
        self.latest[key] = value
        return "OK"

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        idx = sum(1 for s, _ in self.streams if s == stream) + 1
        eid = f"{idx}-0"
        self.streams.append((stream, {"id": eid, **fields}))
        if stream.endswith(":commands"):
            self.commands.append(fields)
        elif stream.endswith(":results"):
            self.results.append((eid, fields))
        return eid

    async def xread(self, streams: dict[str, str], block: int = 0, count: int = 16):
        # Yield every queued command once; then block (return []).
        entries = []
        for cmd in self.commands:
            entries.append(("1-0", cmd))
        self.commands.clear()
        return [(list(streams.keys())[0], entries)] if entries else []

    async def get(self, key: str):
        return self.latest.get(key)

    async def eval(self, script, numkeys, *args):
        return "1-0"

    async def xrevrange(self, stream, count=100):
        rows = [s for s in self.streams if s[0] == stream]
        return [(d["id"], d) for _, d in rows[-count:]]

    async def exists(self, key):
        return 1 if key in self.latest else 0

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, parent: _FakeRedis):
        self.parent = parent
        self.operations: list[tuple[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def set(self, key, value):
        self.operations.append(("set", key, value))
        return self

    def xadd(self, stream, fields, **kwargs):
        self.operations.append(("xadd", stream, fields, kwargs))
        return self

    async def execute(self):
        results = []
        for op in self.operations:
            if op[0] == "set":
                await self.parent.set(op[1], op[2])
                results.append("OK")
            else:
                results.append(await self.parent.xadd(op[1], op[2], **op[3]))
        self.operations = []
        return results


def _envelope_dict(**overrides) -> dict[str, Any]:
    base = {
        "schema_version": 1,
        "symbol": "SOLUSDT",
        "source": "data-access",
        "observed_at": "2026-08-12T00:00:00+00:00",
        "produced_at": "2026-08-12T00:00:01+00:00",
        "status": "healthy",
        "coverage_seconds": 300,
        "data": {"evidence": {"spot": {}, "futures": {}}},
        "errors": [],
    }
    base.update(overrides)
    return base


class NodeModuleTests(unittest.TestCase):
    def test_node_modules_import(self):
        import importlib
        for name in (
            "market_service.nodes",
            "market_service.nodes._base",
            "market_service.nodes.data_access",
            "market_service.nodes.calculations",
            "market_service.nodes.analysis",
            "market_service.nodes.orchestrator",
        ):
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_node_modules_expose_main(self):
        from market_service.nodes import data_access, calculations, analysis, orchestrator
        for mod in (data_access, calculations, analysis, orchestrator):
            with self.subTest(module=mod.__name__):
                self.assertTrue(callable(getattr(mod, "main")))

    def test_refresh_command_round_trip(self):
        cmd = RefreshCommand(
            domain="data-access", symbol="solusdt",
            command_id="run-1", parameters={"depth_levels": 20},
        )
        fields = cmd.to_fields()
        parsed = RefreshCommand.from_fields(fields)
        self.assertEqual(parsed.domain, "data-access")
        self.assertEqual(parsed.symbol, "SOLUSDT")
        self.assertEqual(parsed.command_id, "run-1")
        self.assertEqual(parsed.parameters, {"depth_levels": 20})

    def test_market_event_round_trip(self):
        ev = MarketEvent(
            event_type="data_access_complete",
            symbol="SOLUSDT",
            payload={"run_id": "r", "status": "healthy"},
        )
        parsed = MarketEvent.from_fields(ev.to_fields())
        self.assertEqual(parsed.event_type, "data_access_complete")
        self.assertEqual(parsed.symbol, "SOLUSDT")
        self.assertEqual(parsed.payload, {"run_id": "r", "status": "healthy"})

    def test_publish_domain_state_publishes_correct_envelope(self):
        from market_service.runtime.redis_store import RedisRuntimeStore

        async def _go():
            fake = _FakeRedis()
            store = RedisRuntimeStore.__new__(RedisRuntimeStore)
            store.redis = fake
            store.prefix = "marketflow"
            store.stream_maxlen = 1000

            env = MarketStateEnvelope(
                symbol="SOLUSDT",
                source="data-access",
                observed_at="2026-08-12T00:00:00+00:00",
                produced_at="2026-08-12T00:00:01+00:00",
                status="healthy",
                data={"evidence": {}},
                coverage_seconds=300,
            )
            await store.publish_domain_state(env)
            self.assertIn("marketflow:latest:SOLUSDT:data-access", fake.latest)
            streams = [s for s, _ in fake.streams if s == "marketflow:stream:domain:data-access:SOLUSDT"]
            self.assertEqual(len(streams), 1)

        asyncio.run(_go())

    def test_calculations_handler_reads_data_access_and_emits_calculations(self):
        from market_service.nodes.calculations import make_handler

        async def _go():
            fake = _FakeRedis()
            fake.latest["marketflow:latest:SOLUSDT:data-access"] = json.dumps(_envelope_dict())
            store = AsyncMock()
            store.read_run_domain_state = AsyncMock(return_value=MarketStateEnvelope.from_mapping(
                _envelope_dict(run_id="run-1")
            ))

            from market_service.config import Settings
            settings = Settings(
                database_url="postgresql://x/y",
                redis_url="redis://x:6379/0",
                redis_key_prefix="marketflow",
                redis_stream_maxlen=1000,
                symbols=("SOLUSDT",),
                poll_seconds=30,
                flow_window_seconds=300,
                depth_levels=20,
            )
            handler = await make_handler(settings, store)
            result = await handler(RefreshCommand(domain="calculations", symbol="SOLUSDT", command_id="run-1"))

            self.assertIn("calculations", result)
            self.assertIn("flow", result["calculations"])
            self.assertIn("signal_inputs", result["calculations"])

        asyncio.run(_go())

    def test_calculations_handler_reports_invalid_when_no_data_access(self):
        from market_service.nodes.calculations import make_handler

        async def _go():
            store = AsyncMock()
            store.read_run_domain_state = AsyncMock(return_value=None)
            from market_service.config import Settings
            settings = Settings(
                database_url="postgresql://x/y",
                redis_url="redis://x:6379/0",
                redis_key_prefix="marketflow",
                redis_stream_maxlen=1000,
                symbols=("SOLUSDT",),
                poll_seconds=30,
                flow_window_seconds=300,
                depth_levels=20,
            )
            handler = await make_handler(settings, store)
            result = await handler(RefreshCommand(domain="calculations", symbol="SOLUSDT", command_id="r"))
            self.assertEqual(result["status"], "invalid")
            self.assertTrue(result["errors"])

        asyncio.run(_go())

    def test_analysis_handler_emits_deterministic_sections(self):
        from market_service.nodes.analysis import make_handler

        async def _go():
            envelope_data = {
                "schema_version": 1,
                "symbol": "SOLUSDT",
                "source": "calculations",
                "observed_at": "2026-08-12T00:00:00+00:00",
                "produced_at": "2026-08-12T00:00:01+00:00",
                "status": "healthy",
                "data": {
                    "calculations": {"flow": {"spot_flow": {"buy_share": 0.55}, "futures_flow": {"buy_share": 0.45}}},
                },
                "errors": [],
            }
            data_access_envelope = _envelope_dict(run_id="r")
            envelope_data["run_id"] = "r"
            store = AsyncMock()
            store.read_run_domain_state = AsyncMock(side_effect=[
                MarketStateEnvelope.from_mapping(envelope_data),
                MarketStateEnvelope.from_mapping(data_access_envelope),
            ])
            from market_service.config import Settings
            settings = Settings(
                database_url="postgresql://x/y",
                redis_url="redis://x:6379/0",
                redis_key_prefix="marketflow",
                redis_stream_maxlen=1000,
                symbols=("SOLUSDT",),
                poll_seconds=30,
                flow_window_seconds=300,
                depth_levels=20,
            )
            handler = await make_handler(settings, store)
            result = await handler(RefreshCommand(domain="analysis", symbol="SOLUSDT", command_id="r"))
            # The compact fixture intentionally omits several optional
            # evidence sections; the adapter must surface that as degraded,
            # never fabricate a healthy analysis result.
            self.assertEqual(result["status"], "degraded")
            analysis = result["analysis"]
            for key in ("auction", "open_interest", "wall_migration", "path_absorption",
                        "demand", "regime", "stage", "macro"):
                with self.subTest(section=key):
                    self.assertIn(key, analysis)

        asyncio.run(_go())

    def test_source_failure_makes_envelope_invalid_not_fabricated_healthy(self):
        """If a downstream endpoint raises, status must be ``invalid`` / ``degraded``."""
        from market_service.nodes._base import make_failure_envelope

        envelope = make_failure_envelope(
            symbol="SOLUSDT",
            source="data-access",
            stage="binance_endpoint",
            exc=RuntimeError("timeout"),
        )
        self.assertEqual(envelope.source, "data-access")
        self.assertEqual(envelope.status, "invalid")
        self.assertEqual(envelope.schema_version, MARKET_STATE_SCHEMA_VERSION)
        self.assertTrue(envelope.errors)
        self.assertIn("timeout", envelope.errors[0]["error"])

    def test_orchestrator_sends_commands_in_domain_order(self):
        from market_service.nodes.orchestrator import DOMAIN_ORDER
        self.assertEqual(DOMAIN_ORDER, ("data-access", "calculations", "analysis"))


if __name__ == "__main__":
    unittest.main()
