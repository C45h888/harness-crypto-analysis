"""Tool-first invocation system tests — tools.py, dispatch, CLI routing."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from market_service.substrate_worker import WORKER_REGISTRY, tools
from tests.test_substrate_worker_core import _FakeRedis, _FakeStore


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _rows(n=2, start_ms=1_700_000_000_000):
    return [{"id": f"{start_ms + i}-0", "fields": {}} for i in range(n)]


class ToolRegistryTests(unittest.TestCase):
    def test_tool_names_cover_registry(self):
        self.assertEqual(
            sorted(t.split(".", 1)[1] for t in tools.invoke_tool_names()),
            sorted(WORKER_REGISTRY),
        )
        self.assertEqual(tools.READ_TOOL, "substrate.read")

    def test_dispatch_registry_covers_tools(self):
        from market_service.nooa_harness.inference.capability import CAPABILITIES
        from market_service.nooa_harness.inference.dispatch import TOOL_NAMES, TOOL_PHASE
        # Decoupled by design: worker fire-ticks are NOT agent tools. The
        # agent reads worker state through ns.read; invocation lives inside
        # the worker cadence (substrate_worker/tools.invoke).
        for tool in tools.invoke_tool_names():
            self.assertNotIn(tool, TOOL_NAMES, f"{tool} must not be an agent tool")
        self.assertNotIn("substrate.read", TOOL_NAMES)
        self.assertNotIn("substrate.invoke", TOOL_NAMES)
        self.assertIn("ns.read", TOOL_NAMES)
        self.assertEqual(TOOL_PHASE["ns.read"], "P3")
        self.assertIn("ns.read", CAPABILITIES)
        self.assertNotIn("substrate.read", CAPABILITIES)
        self.assertNotIn("substrate.invoke", CAPABILITIES)


class InvokeTests(unittest.TestCase):
    def test_invoke_cold_start_fires_and_reports(self):
        store = _FakeStore(_FakeRedis(rows=_rows()))
        report = _run(tools.invoke(store, "SOLUSDT", "density"))
        self.assertTrue(report["invoked"])
        self.assertEqual(report["fired"], 1)
        self.assertEqual(report["trigger_source"], "cold_start")
        self.assertTrue(report["available"])

    def test_invoke_unknown_is_structured(self):
        report = _run(tools.invoke(_FakeStore(_FakeRedis()), "SOLUSDT", "nope"))
        self.assertFalse(report["invoked"])
        self.assertIn("unknown substrate", report["error"])

    def test_invoke_many_reports_per_worker(self):
        store = _FakeStore(_FakeRedis(rows=_rows()))
        out = _run(tools.invoke_many(store, "SOLUSDT", ["density", "tape", "nope"]))
        self.assertEqual(out["symbol"], "SOLUSDT")
        self.assertEqual(out["invoked"], 2)
        self.assertEqual(len(out["reports"]), 3)

    def test_read_state_compact_and_full(self):
        payload = {"substrate": "tape", "symbol": "SOLUSDT", "status": "healthy",
                   "computed_at_ms": 1_700_000_001_000,
                   "trigger": {"source": "probe", "predicates": {}},
                   "output": {"spot_flow": {}}}
        store = _FakeStore(_FakeRedis(latest=payload))

        async def _per_substrate(substrate: str, symbol: str):
            return payload if substrate == "tape" else None

        store.read_substrate_latest = _per_substrate  # type: ignore[method-assign]
        compact = _run(tools.read_state(store, "SOLUSDT", substrates=["tape"]))
        entry = compact["substrates"]["tape"]
        self.assertEqual(
            set(entry), {"available", "status", "trigger_source", "age_ms",
                         "computed_at_ms"})
        self.assertEqual(entry["trigger_source"], "probe")
        full = _run(tools.read_state(store, "SOLUSDT", substrates=["tape"],
                                     mode="full"))
        self.assertEqual(full["substrates"]["tape"]["status"], "healthy")
        missing = _run(tools.read_state(store, "SOLUSDT", substrates=["nope"]))
        self.assertEqual(missing["substrates"]["nope"], {"available": False})

    def test_read_state_snapshot_covers_registry(self):
        snap = _run(tools.read_state(_FakeStore(_FakeRedis()), "SOLUSDT"))
        self.assertEqual(set(snap["substrates"]), set(WORKER_REGISTRY))
        self.assertEqual(snap["substrates"]["density"], {"available": False})


class DispatchToolTests(unittest.TestCase):
    def test_ns_read_covers_worker_state(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool
        payload = {"substrate": "tape", "symbol": "SOLUSDT", "status": "healthy",
                   "computed_at_ms": 1_700_000_001_000,
                   "trigger": {"source": "probe", "predicates": {}},
                   "output": {}}
        store = _FakeStore(_FakeRedis(latest=payload))
        result, audit = _run(execute_tool(store, "ns.read",
                                          {"symbol": "SOLUSDT",
                                           "namespace": "substrate.tape"}))
        self.assertEqual(audit["capability"], "ns.read")
        self.assertEqual(audit["result"], "ok")
        self.assertEqual(result["namespace"], "substrate.tape")
        self.assertEqual(result["data"]["status"], "healthy")

    def test_per_worker_invoke_is_not_an_agent_tool(self):
        """Fire-ticks live in the worker cadence, not on the agent plane."""
        from market_service.nooa_harness.inference.dispatch import execute_tool
        store = _FakeStore(_FakeRedis(rows=_rows()))
        result, audit = _run(execute_tool(store, "substrate.density",
                                          {"symbol": "SOLUSDT"}))
        self.assertIsNone(result)
        self.assertEqual(audit["result"], "denied")
        self.assertEqual(audit["capability"], "tool.unknown")

    def test_unknown_worker_denied(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool
        result, audit = _run(execute_tool(
            _FakeStore(_FakeRedis()), "substrate.nope", {"symbol": "SOLUSDT"}))
        self.assertIsNone(result)
        self.assertEqual(audit["result"], "denied")

    def test_out_of_scope_denied(self):
        from types import SimpleNamespace
        from market_service.nooa_harness.inference.dispatch import execute_tool
        result, audit = _run(execute_tool(_FakeStore(_FakeRedis()),
                                          "ns.read",
                                          {"symbol": "FAKEUSDT",
                                           "namespace": "substrate.tape"},
                                          settings=SimpleNamespace(symbols=("SOLUSDT",), venue="futures")))
        self.assertIsNone(result)
        self.assertEqual(audit["result"], "denied")


class HarnessRoutingTests(unittest.TestCase):
    def test_invoke_flag_is_gone_from_the_harness(self):
        """The harness reads; it has no authority to fire a calculation.

        Invocation lives with the inference plane, which requests it from the
        calculation container (tests/test_dispatch_control_transport.py).
        """
        from market_service.commands.harness import build_parser
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["SOLUSDT", "--invoke", "tape,density"])

    @patch("market_service.interaction_plane.cli._read_substrates",
           new_callable=AsyncMock)
    def test_substrate_read_routes_to_handler(self, mock_read):
        from market_service.commands.harness import main
        mock_read.return_value = (
            {"tool": "substrate.read", "status": "ok", "reason": None,
             "data": {"symbol": "SOLUSDT", "substrates": {}},
             "null_fields": [], "budget_receipt": {"truncated": False}},
            "redis",
        )
        rc = main(["SOLUSDT", "--substrate-read"])
        self.assertEqual(rc, 0)
        mock_read.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
