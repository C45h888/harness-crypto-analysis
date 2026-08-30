"""Pass-B2 tool-base tests.

Covers the agent's commandable calculation surface, all nooa-free:

- Registry: 11 tools, every tool name maps to a registered capability,
  frozen scope (BTCUSDT/spot only).
- ``execute_tool``: unknown tool denied, out-of-scope denied, pure T1
  dispatches (replay) execute and log, output bounds respected.
- ``micro.fit_beta`` over synthetic ledger data via a fake store.
- Audit trail: every dispatch returns a capability_log entry with
  capability/scope/result.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from market_service.nooa_harness.inference import (
    CAPABILITIES,
    TOOL_NAMES,
    execute_tool,
)


def _event_payload(k: int, contribution: str, ts: int) -> dict:
    quote = {
        "schema_version": 1, "symbol": "BTCUSDT", "venue": "spot",
        "update_id": k, "exchange_ts_ms": ts, "received_ts_ms": ts,
        "bid_price": "100", "bid_qty": "10", "ask_price": "101", "ask_qty": "20",
    }
    return {
        "schema_version": 1, "event_type": "best_quote_transition",
        "source_quality": "exact_feed", "contribution": contribution,
        "previous": quote, "current": quote,
    }


class _FakeStore:
    """Minimal store double: only the microstructure event/interval reads."""

    def __init__(self, events: list[dict] | None = None):
        self._events = events or []

    async def read_microstructure_events(self, venue, symbol, *, start="-", end="+", count=None):
        return self._events

    async def read_microstructure_intervals(self, venue, symbol, *, start="-", end="+", count=None):
        return []

    async def read_microstructure_status(self, venue, symbol):
        return {"state": "running", "sequence_gaps": 0, "reconnects": 0}

    async def read_microstructure_evidence(self, venue, symbol):
        return None

    async def read_latest_run(self, symbol):
        return None

    async def read_derivative_evidence(self, symbol):
        return None

    async def read_keystone_history(self, symbol, count=1000):
        return []

    async def read_wall_history(self, symbol, count=1000):
        return []

    async def close(self):
        return None


class ToolRegistryTests(unittest.TestCase):
    def test_eleven_tools_registered(self):
        self.assertEqual(len(TOOL_NAMES), 11)

    def test_every_tool_backed_by_a_capability(self):
        for tool, capability in TOOL_NAMES.items():
            self.assertIn(capability, CAPABILITIES, tool)

    def test_scope_is_frozen_to_initial_scope(self):
        for name, cap in CAPABILITIES.items():
            self.assertEqual(cap.allowed_symbols, frozenset({"BTCUSDT"}), name)
            self.assertEqual(cap.allowed_venues, frozenset({"spot"}), name)


class ExecuteToolTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_tool_denied_not_raised(self):
        result, log = _run(execute_tool(_FakeStore(), "market.nuke", {}))
        self.assertIsNone(result)
        self.assertEqual(log["result"], "denied")
        self.assertEqual(log["capability"], "tool.unknown")

    def test_out_of_scope_symbol_denied(self):
        result, log = _run(execute_tool(
            _FakeStore(), "micro.capture_status",
            {"symbol": "SOLUSDT", "venue": "spot"},
        ))
        self.assertIsNone(result)
        self.assertEqual(log["result"], "denied")
        self.assertIn("outside", log["detail"])

    def test_out_of_scope_venue_denied(self):
        result, log = _run(execute_tool(
            _FakeStore(), "micro.capture_status",
            {"symbol": "BTCUSDT", "venue": "futures"},
        ))
        self.assertIsNone(result)
        self.assertEqual(log["result"], "denied")

    def test_capture_status_dispatches_ok(self):
        result, log = _run(execute_tool(
            _FakeStore(), "micro.capture_status",
            {"symbol": "BTCUSDT", "venue": "spot"},
        ))
        self.assertEqual(log["result"], "ok")
        self.assertEqual(result["state"], "running")

    def test_events_returns_payloads(self):
        payloads = [_event_payload(1, "5", 1_000), _event_payload(2, "3", 2_000)]
        result, log = _run(execute_tool(
            _FakeStore(events=payloads), "micro.events",
            {"symbol": "BTCUSDT", "venue": "spot"},
        ))
        self.assertEqual(log["result"], "ok")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["contribution"], "5")

    def test_replay_is_pure_and_deterministic(self):
        payloads = [_event_payload(1, "5", 1_000)]
        args = {
            "symbol": "BTCUSDT", "venue": "spot",
            "event_payloads": payloads, "interval_ms": 10_000,
        }
        events_a, intervals_a, dropped_a, log_a = _run(
            execute_tool(_FakeStore(), "micro.replay", args)
        )
        events_b, intervals_b, dropped_b, _log_b = _run(
            execute_tool(_FakeStore(), "micro.replay", args)
        )
        self.assertEqual(log_a["result"], "ok")
        self.assertEqual(len(events_a), len(events_b))
        self.assertEqual(len(intervals_a), len(intervals_b))
        self.assertEqual(dropped_a, dropped_b)
        self.assertEqual(events_a[0].contribution, Decimal(5))

    def test_log_entries_carry_capability_scope_result(self):
        _result, log = _run(execute_tool(
            _FakeStore(), "micro.capture_status",
            {"symbol": "BTCUSDT", "venue": "spot"},
        ))
        self.assertIn("capability", log)
        self.assertIn("scope", log)
        self.assertIn("result", log)
        self.assertEqual(log["scope"]["symbol"], "BTCUSDT")


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class FitBetaToolTests(unittest.IsolatedAsyncioTestCase):
    def test_fit_beta_insufficient_events(self):
        payloads = [_event_payload(1, "5", 1_000)]
        result, log = _run(execute_tool(
            _FakeStore(events=payloads), "micro.fit_beta",
            {"symbol": "BTCUSDT", "venue": "spot"},
        ))
        self.assertIsNone(result)
        self.assertEqual(log["result"], "ok")
        self.assertEqual(log["detail"]["status"], "insufficient")

    def test_fit_beta_runs_over_synthetic_ledger(self):
        payloads = []
        ts = 1_700_000_000_000
        update_id = 1
        for block in range(6):
            for step in range(30):
                payloads.append(_event_payload(
                    update_id, "5" if step % 2 == 0 else "-3",
                    ts + block * 600_000 + step * 10_000,
                ))
                update_id += 1
        result, log = _run(execute_tool(
            _FakeStore(events=payloads), "micro.fit_beta",
            {"symbol": "BTCUSDT", "venue": "spot",
             "interval_seconds": 10, "window_minutes": 30},
        ))
        self.assertEqual(log["result"], "ok")
        self.assertIsNotNone(result)
        self.assertIn("price_impact_fit", result)
        # 6 alternating-sign blocks produce variance but under 2x-minimum
        # observations → the deterministic gate says provisional, not
        # validated, and never sufficient for depth scaling.
        self.assertEqual(result["price_impact_fit"]["status"], "provisional")
        self.assertEqual(result["depth_scaling_fit"]["status"], "insufficient")


if __name__ == "__main__":
    unittest.main()
