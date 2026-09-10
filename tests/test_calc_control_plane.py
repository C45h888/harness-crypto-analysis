"""Calculation control-plane contract — the sanctioned invocation seam.

``POST /invoke`` on the calculation container is the ONLY way a process
outside ``market_service/substrate_worker/`` may request a fire-tick, because
it runs in the workers' own process (own consumer groups, own supervisor
keys). These tests pin the contract that makes it usable as that seam:

1. It threads the durable ledger through — an HTTP-triggered fire writes
   Postgres exactly like an in-process one, never Redis-only.
2. It validates the request — a bad symbol or an unknown substrate name is a
   4xx the caller can distinguish from "worker dormant".
3. It binds an address reachable from other containers.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from market_service.substrate_worker.dain_container import CalcHttpApp
from tests.test_substrate_worker_core import _FakeRedis, _FakeStore


def _rows(n=1, start_ms=1_700_000_000_000):
    return [{"id": f"{start_ms + i}-0", "fields": {}} for i in range(n)]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _SpyPgStore:
    """Stand-in for PostgresRuntimeStore — records that it was handed over."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class InvokeLedgerPassthroughTests(unittest.TestCase):
    """The durable ledger must ride the HTTP path, not just the CLI path."""

    def test_invoke_passes_pg_store_and_strict_to_tools(self):
        store = _FakeStore(_FakeRedis(rows=_rows()))
        pg = _SpyPgStore()
        app = CalcHttpApp(store, [], pg_store=pg, pg_strict=True)

        captured: dict = {}

        async def _fake_invoke_many(_store, symbol, substrates, **kwargs):
            captured.update({"symbol": symbol, "substrates": substrates, **kwargs})
            return {"symbol": symbol, "invoked": 1, "fired": 1, "reports": []}

        async def _call():
            with patch("market_service.substrate_worker.tools.invoke_many",
                       new=_fake_invoke_many):
                async with TestClient(TestServer(app.app)) as client:
                    return await client.post(
                        "/invoke",
                        json={"symbol": "SOLUSDT", "substrates": ["density"]},
                    )

        resp = _run(_call())
        self.assertEqual(resp.status, 200)
        self.assertIs(captured.get("pg_store"), pg)
        self.assertTrue(captured.get("pg_strict"))


class InvokeValidationTests(unittest.TestCase):
    """Bad requests are 4xx — distinguishable from a dormant worker."""

    def _post(self, body: dict):
        store = _FakeStore(_FakeRedis(rows=_rows()))
        app = CalcHttpApp(store, [], pg_store=None, pg_strict=False)

        async def _call():
            async with TestClient(TestServer(app.app)) as client:
                return await client.post("/invoke", json=body)

        return _run(_call())

    def test_missing_symbol_is_rejected(self):
        """No hardcoded SOLUSDT fallback — an unaddressed request is a 400."""
        resp = self._post({"substrates": ["density"]})
        self.assertEqual(resp.status, 400)

    def test_unknown_substrate_is_rejected(self):
        """An unregistered name is a bad request, not a per-worker error."""
        resp = self._post({"symbol": "SOLUSDT", "substrates": ["nope"]})
        self.assertEqual(resp.status, 400)

    def test_known_substrate_is_accepted(self):
        resp = self._post({"symbol": "SOLUSDT", "substrates": ["density"]})
        self.assertEqual(resp.status, 200)


class ControlPlaneBindTests(unittest.TestCase):
    """The plane must be reachable from other containers on the compose net."""

    def test_binds_all_interfaces_not_container_loopback(self):
        import inspect

        from market_service.substrate_worker import dain_container

        source = inspect.getsource(dain_container.run_container)
        self.assertIn('"0.0.0.0"', source)
        self.assertNotIn('"127.0.0.1"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
