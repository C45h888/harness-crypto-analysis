"""Control-client contract — how callers reach the calculation plane.

``control_client`` is the ONE implementation of "ask the calculation plane to
fire a worker", shared by the inference dispatch and the inner CLI. It exists
so no process outside ``substrate_worker/`` ever builds a
``SubstrateWorkerCore`` of its own.

The contract these tests pin:

1. A successful request returns the plane's report verbatim.
2. An unreachable plane raises ``CalcPlaneUnreachable`` — callers turn that
   into a structured finding. It must NEVER silently degrade to in-process
   invocation, which is the collision the seam exists to prevent.
3. A 4xx from the plane raises ``CalcPlaneRejected`` carrying the plane's own
   message, so a bad request stays distinguishable from a dormant worker.
4. The base URL comes from ``CALC_CONTROL_URL``, defaulting to the compose
   service name.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from market_service.substrate_worker.control_client import (
    CalcPlaneRejected,
    CalcPlaneUnreachable,
    control_base_url,
    request_invoke,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _stub_plane(handler):
    app = web.Application()
    app.router.add_post("/invoke", handler)
    return TestServer(app)


class RequestInvokeTests(unittest.TestCase):
    def test_returns_plane_report_verbatim(self):
        seen: dict = {}

        async def _handler(request):
            seen.update(await request.json())
            return web.json_response(
                {"symbol": "SOLUSDT", "invoked": 1, "fired": 1,
                 "reports": [{"substrate": "density", "fired": 1}]})

        async def _call():
            server = _stub_plane(_handler)
            await server.start_server()
            try:
                return await request_invoke(
                    "SOLUSDT", ["density"], base_url=str(server.make_url("")).rstrip("/"))
            finally:
                await server.close()

        result = _run(_call())
        self.assertEqual(result["invoked"], 1)
        self.assertEqual(result["reports"][0]["substrate"], "density")
        self.assertEqual(seen["symbol"], "SOLUSDT")
        self.assertEqual(seen["substrates"], ["density"])

    def test_unreachable_plane_raises_never_falls_back(self):
        """A dead plane is a finding, not a licence to invoke in-process."""
        with self.assertRaises(CalcPlaneUnreachable):
            # Port 1 is reserved and never listening.
            _run(request_invoke("SOLUSDT", ["density"],
                                base_url="http://127.0.0.1:1", timeout_s=1.0))

    def test_rejection_carries_the_plane_message(self):
        async def _handler(_request):
            return web.json_response(
                {"error": "unknown substrate worker(s): ['nope']"}, status=400)

        async def _call():
            server = _stub_plane(_handler)
            await server.start_server()
            try:
                return await request_invoke(
                    "SOLUSDT", ["nope"], base_url=str(server.make_url("")).rstrip("/"))
            finally:
                await server.close()

        with self.assertRaises(CalcPlaneRejected) as ctx:
            _run(_call())
        self.assertIn("nope", str(ctx.exception))


class BaseUrlTests(unittest.TestCase):
    def test_defaults_to_compose_service_name(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("CALC_CONTROL_URL", None)
            self.assertEqual(control_base_url(), "http://calculation:8041")

    def test_env_override_wins(self):
        with patch.dict("os.environ", {"CALC_CONTROL_URL": "http://127.0.0.1:8041"}):
            self.assertEqual(control_base_url(), "http://127.0.0.1:8041")


if __name__ == "__main__":
    unittest.main(verbosity=2)
