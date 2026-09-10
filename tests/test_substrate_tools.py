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
        for tool in tools.invoke_tool_names():
            self.assertIn(tool, TOOL_NAMES, f"{tool} missing from TOOL_NAMES")
            self.assertEqual(TOOL_PHASE[tool], "P3")
        self.assertIn("substrate.read", TOOL_NAMES)
        self.assertIn("substrate.invoke", TOOL_NAMES)
        self.assertIn("substrate.read", CAPABILITIES)
        self.assertIn("substrate.invoke", CAPABILITIES)


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
    def test_substrate_read_tool(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool
        payload = {"substrate": "tape", "symbol": "SOLUSDT", "status": "healthy",
                   "computed_at_ms": 1_700_000_001_000,
                   "trigger": {"source": "probe", "predicates": {}},
                   "output": {}}
        store = _FakeStore(_FakeRedis(latest=payload))
        result, audit = _run(execute_tool(store, "substrate.read",
                                          {"symbol": "SOLUSDT"}))
        self.assertEqual(audit["capability"], "substrate.read")
        self.assertEqual(audit["result"], "ok")
        self.assertIn("tape", result["substrates"])

    def test_per_worker_invoke_tool(self):
        """Same outcome contract as the in-process era, over the control plane.

        The engine's tool surface is unchanged; only the execution site moved
        into the calculation container (see tests/test_dispatch_control_transport.py).
        """
        from market_service.nooa_harness.inference.dispatch import execute_tool
        store = _FakeStore(_FakeRedis(rows=_rows()))
        with patch("market_service.substrate_worker.control_client.request_invoke",
                   new_callable=AsyncMock) as plane:
            plane.return_value = {
                "symbol": "SOLUSDT", "invoked": 1, "fired": 1,
                "reports": [{"substrate": "density", "symbol": "SOLUSDT",
                             "invoked": True, "fired": 1,
                             "trigger_source": "cold_start", "available": True}],
            }
            result, audit = _run(execute_tool(store, "substrate.density",
                                              {"symbol": "SOLUSDT"}))
        self.assertEqual(audit["result"], "ok")
        self.assertTrue(result["invoked"])
        self.assertEqual(result["substrate"], "density")

    def test_unknown_worker_denied(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool
        result, audit = _run(execute_tool(
            _FakeStore(_FakeRedis()), "substrate.nope", {"symbol": "SOLUSDT"}))
        self.assertIsNone(result)
        self.assertEqual(audit["result"], "denied")

    def test_out_of_scope_denied(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool
        result, audit = _run(execute_tool(_FakeStore(_FakeRedis()),
                                          "substrate.tape",
                                          {"symbol": "FAKEUSDT"}))
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

    @patch("market_service.commands.harness._read_substrates",
           new_callable=AsyncMock)
    def test_substrate_read_routes_to_handler(self, mock_read):
        from market_service.commands.harness import main
        mock_read.return_value = {"symbol": "SOLUSDT", "substrates": {}}
        rc = main(["SOLUSDT", "--substrate-read"])
        self.assertEqual(rc, 0)
        mock_read.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
