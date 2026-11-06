"""Vector-3: FSM-governed compute slots — FIFO fair queue + plane-wide bound.

The FSM owns the number (compute_concurrency), the governor enforces it
against one process-wide fair semaphore per plane, and workers hold a slot
across backfills and fire computes. Cold starts therefore queue in batches
of the FSM's number instead of stampeding 12-wide.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from market_service.governance.contracts import GovernancePlane
from market_service.governance import gates as gates_module
from market_service.governance.gates import GovernanceGovernor, _FairSemaphore


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubFSM:
    PLANE = GovernancePlane.CALCULATION

    def __init__(self, slots: int = 2):
        self._slots = slots

    def compute_concurrency(self) -> int:
        return self._slots


class FairSemaphoreTests(unittest.TestCase):
    def test_fifo_grant_order(self):
        async def _main():
            sem = _FairSemaphore(1)
            order: list[int] = []

            async def waiter(i: int):
                await sem.acquire()
                order.append(i)
                await asyncio.sleep(0.01)
                sem.release()

            await asyncio.gather(*[waiter(i) for i in range(4)])
            return order

        self.assertEqual(_run(_main()), [0, 1, 2, 3])

    def test_limit_never_exceeded(self):
        async def _main():
            sem = _FairSemaphore(2)
            peak = 0

            async def waiter():
                nonlocal peak
                await sem.acquire()
                peak = max(peak, sem.in_use)
                await asyncio.sleep(0.01)
                sem.release()

            await asyncio.gather(*[waiter() for _ in range(6)])
            return peak

        self.assertLessEqual(_run(_main()), 2)

    def test_cancelled_waiter_withdraws(self):
        async def _main():
            sem = _FairSemaphore(1)
            await sem.acquire()  # main holds the only slot
            t = asyncio.ensure_future(sem.acquire())
            await asyncio.sleep(0)
            self.assertEqual(sem.queued, 1)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            sem.release()
            return sem.in_use, sem.queued

        in_use, queued = _run(_main())
        self.assertEqual((in_use, queued), (0, 0))


class GovernorSlotTests(unittest.TestCase):
    def setUp(self):
        gates_module._COMPUTE_SLOTS.clear()
        self.addCleanup(gates_module._COMPUTE_SLOTS.clear)

    def test_acquire_release_status(self):
        async def _main():
            gov = GovernanceGovernor(_StubFSM(2), None)
            self.assertEqual(
                gov.compute_slot_status(), {"limit": 2, "in_use": 0, "queued": 0, "lease_s": 120, "lease_violations": 0, "oldest_hold_s": 0})
            await gov.acquire_compute_slot(symbol="SOLUSDT", substrate="oi")
            self.assertEqual(
                gov.compute_slot_status(), {"limit": 2, "in_use": 1, "queued": 0, "lease_s": 120, "lease_violations": 0, "oldest_hold_s": 0})
            await gov.release_compute_slot(symbol="SOLUSDT", substrate="oi")
            return gov.compute_slot_status()

        self.assertEqual(
            _run(_main()), {"limit": 2, "in_use": 0, "queued": 0, "lease_s": 120, "lease_violations": 0, "oldest_hold_s": 0})

    def test_second_governor_instance_shares_plane_slot(self):
        async def _main():
            gov1 = GovernanceGovernor(_StubFSM(2), None)
            gov2 = GovernanceGovernor(_StubFSM(2), None)
            await gov1.acquire_compute_slot(symbol="S", substrate="a")
            await gov2.acquire_compute_slot(symbol="S", substrate="b")
            # Plane-wide bound reached: a third acquire must wait.
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    gov1.acquire_compute_slot(symbol="S", substrate="c"),
                    timeout=0.05)
            await gov1.release_compute_slot(symbol="S", substrate="a")
            await gov2.release_compute_slot(symbol="S", substrate="b")
            return gov1.compute_slot_status()

        self.assertEqual(
            _run(_main()), {"limit": 2, "in_use": 0, "queued": 0, "lease_s": 120, "lease_violations": 0, "oldest_hold_s": 0})

    def test_env_override_wins_over_fsm(self):
        async def _main():
            with patch.dict(os.environ, {"SUBSTRATE_COMPUTE_SLOTS": "1"}):
                gov = GovernanceGovernor(_StubFSM(99), None)
                return gov.compute_slot_status()["limit"]

        self.assertEqual(_run(_main()), 1)

    def test_legacy_fsm_without_hook_falls_back(self):
        class _NoHook:
            PLANE = GovernancePlane.ANALYSIS

        async def _main():
            gov = GovernanceGovernor(_NoHook(), None)
            return gov.compute_slot_status()["limit"]

        self.assertEqual(_run(_main()), 2)


class WorkerSlotWiringTests(unittest.TestCase):
    def setUp(self):
        gates_module._COMPUTE_SLOTS.clear()
        self.addCleanup(gates_module._COMPUTE_SLOTS.clear)

    def _bare_worker(self, governor):
        from market_service.substrate_worker.oi_worker import OiWorker
        worker = OiWorker.__new__(OiWorker)
        worker.symbol = "SOLUSDT"
        worker.governor = governor
        return worker

    def test_slot_held_across_guarded_section(self):
        async def _main():
            gov = GovernanceGovernor(_StubFSM(2), None)
            worker = self._bare_worker(gov)
            async with worker._compute_slot("compute"):
                held = gov.compute_slot_status()
                self.assertEqual(held, {"limit": 2, "in_use": 1, "queued": 0, "lease_s": 120, "lease_violations": 0, "oldest_hold_s": 0})
            return gov.compute_slot_status()

        self.assertEqual(
            _run(_main()), {"limit": 2, "in_use": 0, "queued": 0, "lease_s": 120, "lease_violations": 0, "oldest_hold_s": 0})

    def test_ungoverned_worker_runs_unbounded(self):
        async def _main():
            worker = self._bare_worker(None)
            async with worker._compute_slot("compute"):
                return True

        self.assertTrue(_run(_main()))

    def test_backfill_holds_slot_during_widened_build(self):
        from unittest.mock import patch as _patch
        from market_service.substrate_worker import evidence as evidence_module

        async def _main():
            from market_service.substrate_worker.migration_worker import MigrationWorker
            from market_service.runtime.horizons import new_fold_state
            gov = GovernanceGovernor(_StubFSM(2), None)
            worker = MigrationWorker.__new__(MigrationWorker)
            worker.symbol = "SOLUSDT"
            worker.horizons = ("1h", "4h")
            worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
            worker._horizons_backfilled = False
            worker._horizons_rebuilt_from = None
            worker._plane = "substrate"
            worker.pg_store = None
            worker.store = object()
            worker.governor = gov
            worker._last_error = None
            seen = {}

            async def _fake_build(redis, symbol, minutes):
                seen["during"] = gov.compute_slot_status()
                self.assertEqual(minutes, 240)
                return {"spot": {"trades_normalized": []},
                        "futures": {"trades_normalized": []}}

            with _patch.object(evidence_module, "build_raw_window", _fake_build):
                await worker._backfill_horizons()
            return worker, seen

        worker, seen = _run(_main())
        self.assertTrue(worker._horizons_backfilled)
        # The widened build ran while holding one of the plane's 2 slots.
        self.assertEqual(seen["during"]["in_use"], 1)

    def test_heartbeat_carries_slot_status(self):
        import json

        class _SetexRedis:
            def __init__(self):
                self.written: dict[str, str] = {}

            async def setex(self, key, ttl, value):
                self.written[key.decode() if isinstance(key, bytes) else key] = value

        async def _main():
            gov = GovernanceGovernor(_StubFSM(2), None)
            worker = self._bare_worker(gov)
            worker._last_fire_ms = None
            worker._fired = 0
            worker._last_error = None
            worker._last_dormant_reason = None
            worker._ws_stream = None
            worker.supervisor_ms = 12_000
            worker._supervisor_key = "k"
            worker._redis = _SetexRedis()
            worker._ticks = 0
            worker._liveness = False
            await worker._tick(1_700_000_000_000)
            return json.loads(worker._redis.written["k"])

        heartbeat = _run(_main())
        self.assertEqual(
            heartbeat["compute_slots"], {"limit": 2, "in_use": 0, "queued": 0, "lease_s": 120, "lease_violations": 0, "oldest_hold_s": 0})
