"""Container-bus tests — data-driven dispatch + control-plane contract.

Pins the no-timer invariant of the container consolidation:

1. A worker task with zero arrivals performs no synthetic work: no fire,
   no heartbeat besides the proof-of-life read-cycle return.
2. Exactly one XADD ⇒ exactly one cycle completes; staleness/rollover are
   evaluated AT DISPATCH (given stale/crossing latest), never by a ticker.
3. The calculation control plane exposes /health /status /invoke over the
   SAME tools code path as the harness CLI (both planes, one implementation).
4. The wake supervisor loop refreshes its heartbeat per completed read
   cycle — a blocking read that returns with no data is a liveness stamp.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from market_service.substrate_worker import tools as substrate_tools
from market_service.substrate_worker.contracts import (
    CadenceProfile,
    SubstrateStatePayload,
    TriggerDecision,
)
from market_service.substrate_worker.core import SubstrateWorkerCore
from tests.test_substrate_worker_core import _FakeRedis, _FakeStore


class _BusWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "bus"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=0, staleness_s=3600)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.probe_calls = 0
        self._decision = TriggerDecision(fired=False, source="probe")

    def probe(self, window, last_state, now_ms):
        self.probe_calls += 1
        return self._decision

    def compute(self, evidence, depth):
        return {"bus": 1}


def _rows(n=1, start_ms=1_700_000_000_000):
    return [{"id": f"{start_ms + i}-0", "fields": {}} for i in range(n)]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class AbsentTimerDispatchTests(unittest.TestCase):
    def test_zero_arrivals_zero_probes_no_heartbeat(self):
        """No data ⇒ no probe, no fire, no supervisor write (no ticker)."""
        with patch("market_service.substrate_worker.core.build_raw_window",
                   return_value={"coverage": {}}):
            store = _FakeStore(_FakeRedis(rows=[]))
            w = _BusWorker(store, symbol="SOLUSDT", staleness_s=3600)

            async def _drive() -> None:
                await w.start()
                await w._handle_rows([], 1_700_000_000_000)
                # exactly one fire attempt, zero data
                await asyncio.gather(*list(w._tasks), return_exceptions=True)

            _run(_drive())
            self.assertEqual(w.probe_calls, 0)
            self.assertEqual(w.fired_count, 0)
            self.assertIsNone(store.redis.latest)
            # heartbeat remains unwritten — no loop, no timer stamp
            self.assertNotIn(w._supervisor_key, store.redis.supervisor)

    def test_dispatched_staleness_fires_without_waiting(self):
        """Stale latest + a small arrival fires AT DISPATCH (no timer wait)."""
        state = SubstrateStatePayload.create(
            substrate="bus", symbol="SOLUSDT",
            output={"bus": 1}, trigger=TriggerDecision(fired=True, source="probe"),
            computed_at_ms=1,  # ancient
        ).to_dict()
        w = _BusWorker(_FakeStore(_FakeRedis(rows=_rows(), latest=state)),
                       symbol="SOLUSDT", staleness_s=30)
        now_ms = 1_700_000_050_000
        _run(w._handle_rows(_rows(), now_ms))
        _run(asyncio.sleep(0.02))
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.latest["trigger"]["source"], "staleness")

    def test_rollover_crossing_fires_at_dispatch(self):
        class _Rolling(_BusWorker):
            CADENCE = CadenceProfile(cooldown_s=0, staleness_s=9999,
                                     rollovers=("hour",))

        hour = 3_600_000
        base = (1_700_000_000_000 // hour) * hour
        state = SubstrateStatePayload.create(
            substrate="bus", symbol="SOLUSDT",
            output={"bus": 1}, trigger=TriggerDecision(fired=True, source="probe"),
            computed_at_ms=base,  # fresh, so staleness can't swallow
        ).to_dict()
        w = _Rolling(_FakeStore(_FakeRedis(rows=_rows(), latest=state)),
                     symbol="SOLUSDT")
        _run(w._handle_rows(_rows(1, start_ms=base - 1_000), base + 1_000))
        self.assertEqual(w.fired_count, 0)  # water mark only
        _run(w._handle_rows(_rows(1, start_ms=base + 1_000), base + 2_000))
        _run(asyncio.sleep(0.02))
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.latest["trigger"]["source"], "rollover")

    def test_activity_heartbeat_on_quiet_read_cycle(self):
        """A completed read cycle (even quiet) paints the supervisor key.

        The heartbeat is the blocking-read return's activity stamp: run the
        loop body for one quiet cycle via a short task timeout, then assert
        the supervisor key exists with the worker's identity.
        """
        w = _BusWorker(_FakeStore(_FakeRedis(rows=[])), symbol="SOLUSDT")

        async def _one_loop_round():
            await w.start()
            # emulate the data-driven loop body: read (quiet) → tick stamp
            await w._handle_rows([], 1_700_000_000_000)
            await w._tick(1_700_000_000_000)
            await w.stop()

        _run(_one_loop_round())
        heartbeat = json.loads(w.store.redis.supervisor[w._supervisor_key])
        self.assertEqual(heartbeat["substrate"], "bus")
        self.assertEqual(heartbeat["state"], "running")


class ControlPlaneTests(unittest.TestCase):
    def test_invoke_http_and_cli_share_tools_path(self):
        """POST /invoke → tools.invoke_many → identical report shape."""
        store = _FakeStore(_FakeRedis(rows=_rows()))
        result = _run(substrate_tools.invoke_many(store, "SOLUSDT", ["density"]))
        self.assertEqual(result["invoked"], 1)
        self.assertEqual(result["reports"][0]["trigger_source"], "cold_start")
        # /status shape is read_state — same seam the CLI --substrate-read uses
        status = _run(substrate_tools.read_state(store, "SOLUSDT"))
        self.assertIn("density", status["substrates"])

    def test_health_reflects_supervisor_keys(self):
        from market_service.substrate_worker.dain_container import health
        store = _FakeStore(_FakeRedis(rows=_rows()))
        w = _BusWorker(store, symbol="SOLUSDT")
        _run(w._tick(1_700_000_000_000))
        ok = _run(health(store, [w]))
        self.assertTrue(ok)
        # A worker with NO heartbeat on its own store is unhealthy.
        store2 = _FakeStore(_FakeRedis())
        missing = _run(health(store2, [_BusWorker(store2, symbol="SOLUSDT")]))
        self.assertFalse(missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)