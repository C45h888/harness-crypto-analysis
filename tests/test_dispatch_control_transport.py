"""Inference dispatch reaches workers through the calculation plane.

The engine keeps every ``substrate.*`` tool it has — its authority to decide a
calculation must run is unchanged. What these tests pin is that the *execution
site* moved: the dispatch asks the calculation container over HTTP instead of
building a ``SubstrateWorkerCore`` inside the inference process, where it would
clobber the live container's supervisor key and steal its stream entries.

The outcome contract is deliberately unchanged — same reports, same denials —
so the engine's tool surface is provably untouched.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from market_service.substrate_worker.control_client import (
    CalcPlaneRejected,
    CalcPlaneUnreachable,
)
from tests.test_substrate_worker_core import _FakeRedis, _FakeStore


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_PLANE = "market_service.substrate_worker.control_client.request_invoke"
_REPORT = {
    "symbol": "SOLUSDT", "invoked": 1, "fired": 1,
    "reports": [{"substrate": "density", "symbol": "SOLUSDT", "invoked": True,
                 "fired": 1, "trigger_source": "cold_start", "status": "healthy",
                 "available": True}],
}


class TransportTests(unittest.TestCase):
    def test_named_worker_goes_through_the_calculation_plane(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool

        with patch(_PLANE, new_callable=AsyncMock) as plane:
            plane.return_value = _REPORT
            result, audit = _run(execute_tool(
                _FakeStore(_FakeRedis()), "substrate.density", {"symbol": "SOLUSDT"}))

        plane.assert_awaited_once()
        self.assertEqual(plane.await_args.args[0], "SOLUSDT")
        self.assertEqual(list(plane.await_args.args[1]), ["density"])
        # Outcome contract unchanged from the in-process era.
        self.assertEqual(audit["result"], "ok")
        self.assertTrue(result["invoked"])
        self.assertEqual(result["substrate"], "density")

    def test_never_builds_a_worker_in_this_process(self):
        """The whole point: substrate_tools.invoke* must not be reached."""
        from market_service.nooa_harness.inference.dispatch import execute_tool

        def _boom(*_a, **_kw):
            raise AssertionError("dispatch built a worker in the inference process")

        with patch(_PLANE, new_callable=AsyncMock) as plane, \
                patch("market_service.substrate_worker.tools.invoke", new=_boom), \
                patch("market_service.substrate_worker.tools.invoke_many", new=_boom):
            plane.return_value = _REPORT
            _run(execute_tool(_FakeStore(_FakeRedis()), "substrate.density",
                              {"symbol": "SOLUSDT"}))

    def test_invoke_all_passes_no_substrate_filter(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool

        with patch(_PLANE, new_callable=AsyncMock) as plane:
            plane.return_value = {"symbol": "SOLUSDT", "invoked": 12, "fired": 3,
                                  "reports": []}
            result, audit = _run(execute_tool(
                _FakeStore(_FakeRedis()), "substrate.invoke", {"symbol": "SOLUSDT"}))

        self.assertIsNone(plane.await_args.args[1])
        self.assertEqual(audit["result"], "ok")
        self.assertEqual(result["invoked"], 12)


class PlaneFailureTests(unittest.TestCase):
    def test_unreachable_plane_is_a_finding_not_a_fallback(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool

        with patch(_PLANE, new_callable=AsyncMock) as plane:
            plane.side_effect = CalcPlaneUnreachable("calculation plane unreachable at …")
            result, audit = _run(execute_tool(
                _FakeStore(_FakeRedis()), "substrate.density", {"symbol": "SOLUSDT"}))

        self.assertIsNone(result)
        self.assertEqual(audit["result"], "error")
        self.assertIn("unreachable", str(audit.get("detail")).lower())

    def test_rejected_request_is_denied(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool

        with patch(_PLANE, new_callable=AsyncMock) as plane:
            plane.side_effect = CalcPlaneRejected("unknown substrate worker(s): ['nope']")
            result, audit = _run(execute_tool(
                _FakeStore(_FakeRedis()), "substrate.density", {"symbol": "SOLUSDT"}))

        self.assertIsNone(result)
        self.assertEqual(audit["result"], "denied")


class UnchangedDenialTests(unittest.TestCase):
    """Denials resolved before transport must still resolve before transport."""

    def test_unknown_worker_denied_without_calling_the_plane(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool

        with patch(_PLANE, new_callable=AsyncMock) as plane:
            result, audit = _run(execute_tool(
                _FakeStore(_FakeRedis()), "substrate.nope", {"symbol": "SOLUSDT"}))

        plane.assert_not_awaited()
        self.assertIsNone(result)
        self.assertEqual(audit["result"], "denied")

    def test_out_of_scope_denied_without_calling_the_plane(self):
        from market_service.nooa_harness.inference.dispatch import execute_tool

        with patch(_PLANE, new_callable=AsyncMock) as plane:
            result, audit = _run(execute_tool(
                _FakeStore(_FakeRedis()), "substrate.tape", {"symbol": "FAKEUSDT"}))

        plane.assert_not_awaited()
        self.assertIsNone(result)
        self.assertEqual(audit["result"], "denied")


if __name__ == "__main__":
    unittest.main(verbosity=2)
