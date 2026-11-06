"""Vector 5, part 1: artifact-once, PG decoupling, breaker, slot lease.

- ArtifactBundle binding: probe-stage derivations are reusable only on
  input-identity match; anything else recomputes (never a stale reuse).
- Delta reference conversion: one _delta_of evaluation serves decide seed +
  probe + compute (was: up to 3 evaluations per firing tick).
- PG decoupling: only horizon workers keep a live pg_store.
- Breaker: consecutive PG failures park PG-dependent fires dormant.
- Lease: slot overruns are counted on the heartbeat, never preempted.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from market_service.governance import gates as gates_module
from market_service.governance.gates import GovernanceGovernor, PgCircuitBreaker
from market_service.substrate_worker import delta_worker as delta_module
from market_service.substrate_worker.contracts import (
    ArtifactBundle,
    artifact_binding,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


BOOK_WINDOW = {
    "_artifact_nonce": 5,
    "futures": {
        "order_book": {
            "bids": [[148.20, 900.0], [148.15, 500.0], [148.10, 400.0]],
            "asks": [[148.32, 600.0], [148.40, 800.0]],
        },
        "trades_normalized": [
            {"id": i, "ts": 1_700_000_000_000 + i * 45_000, "price": 148.25,
             "qty": 2.0, "is_buyer_maker": bool(i % 3)}
            for i in range(20)
        ],
    },
    "spot": {
        "order_book": {
            "bids": [[148.22, 300.0], [148.10, 120.0]],
            "asks": [[148.30, 250.0]],
        },
        "trades_normalized": [
            {"id": 100 + i, "ts": 1_700_000_000_000 + i * 90_000,
             "price": 148.26, "qty": 1.0, "is_buyer_maker": bool(i % 2)}
            for i in range(10)
        ],
    },
}


class BundleTests(unittest.TestCase):
    def test_usable_only_on_producer_and_binding_match(self):
        bundle = ArtifactBundle("delta", "SOLUSDT|delta|5", {"delta": {"a": 1}})
        self.assertTrue(bundle.usable_for("delta", "SOLUSDT|delta|5"))
        self.assertFalse(bundle.usable_for("delta", "SOLUSDT|delta|6"))
        self.assertFalse(bundle.usable_for("tape", "SOLUSDT|delta|5"))
        self.assertFalse(ArtifactBundle("", "", {}).usable_for("", ""))

    def test_binding_format(self):
        self.assertEqual(artifact_binding("solusdt", "delta", 7), "SOLUSDT|delta|7")

    def test_stamp_unique_per_build(self):
        from market_service.substrate_worker import evidence as ev
        first, second = ev._stamp({}), ev._stamp({})
        self.assertNotEqual(first["_artifact_nonce"], second["_artifact_nonce"])


class DeltaArtifactOnceTests(unittest.TestCase):
    def _bare_delta(self):
        worker = delta_module.DeltaWorker.__new__(delta_module.DeltaWorker)
        worker.symbol = "SOLUSDT"
        return worker

    def test_one_derivation_serves_probe_and_compute(self):
        worker = self._bare_delta()
        real = delta_module._delta_of
        calls = 0

        def _counting(window):
            nonlocal calls
            calls += 1
            return real(window)

        with patch.object(delta_module, "_delta_of", _counting):
            current = delta_module._delta_of(dict(BOOK_WINDOW))
            self.assertIsNotNone(current)
            self.assertEqual(calls, 1)
            bundle = ArtifactBundle(
                "delta",
                artifact_binding("SOLUSDT", "delta",
                                 int(BOOK_WINDOW["_artifact_nonce"])),
                {"delta": current},
            )
            worker.probe(dict(BOOK_WINDOW), None, 1_700_000_001_000, seed=current)
            self.assertEqual(calls, 1)
            out = worker.compute(dict(BOOK_WINDOW), 20, artifacts=bundle)
            self.assertEqual(calls, 1)
            self.assertIn("state", out)

    def test_legacy_path_recomputes_identically(self):
        worker = self._bare_delta()
        real = delta_module._delta_of
        calls = 0

        def _counting(window):
            nonlocal calls
            calls += 1
            return real(window)

        with patch.object(delta_module, "_delta_of", _counting):
            out_legacy = worker.compute(dict(BOOK_WINDOW), 20)
            legacy_calls = calls
            current = delta_module._delta_of(dict(BOOK_WINDOW))
            bundle = ArtifactBundle(
                "delta",
                artifact_binding("SOLUSDT", "delta",
                                 int(BOOK_WINDOW["_artifact_nonce"])),
                {"delta": current},
            )
            out_bundle = worker.compute(dict(BOOK_WINDOW), 20, artifacts=bundle)
        self.assertGreaterEqual(legacy_calls, 1)
        self.assertEqual(out_legacy, out_bundle)

    def test_foreign_bundle_recomputes(self):
        worker = self._bare_delta()
        real = delta_module._delta_of
        calls = 0

        def _counting(window):
            nonlocal calls
            calls += 1
            return real(window)

        foreign = ArtifactBundle("tape", "SOLUSDT|tape|9", {"delta": {"state": "x"}})
        with patch.object(delta_module, "_delta_of", _counting):
            out = worker.compute(dict(BOOK_WINDOW), 20, artifacts=foreign)
            self.assertEqual(calls, 1)
            self.assertIn("state", out)

    def test_no_nonce_recomputes(self):
        worker = self._bare_delta()
        real = delta_module._delta_of
        calls = 0

        def _counting(window):
            nonlocal calls
            calls += 1
            return real(window)

        no_nonce = dict(BOOK_WINDOW)
        del no_nonce["_artifact_nonce"]
        bundle = ArtifactBundle("delta", "SOLUSDT|delta|5", {"delta": {"state": "x"}})
        with patch.object(delta_module, "_delta_of", _counting):
            worker.compute(no_nonce, 20, artifacts=bundle)
            self.assertEqual(calls, 1)


class _StubFSM:
    from market_service.governance.contracts import GovernancePlane
    PLANE = GovernancePlane.CALCULATION

    def compute_concurrency(self) -> int:
        return 2


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self._saved = (gates_module._PG_BREAKER.failures,
                       gates_module._PG_BREAKER.opened_at_ms,
                       gates_module._PG_BREAKER.trial_in_flight)
        gates_module._PG_BREAKER.record_success()

    def tearDown(self):
        failures, opened, trial = self._saved
        gates_module._PG_BREAKER.failures = failures
        gates_module._PG_BREAKER.opened_at_ms = opened
        gates_module._PG_BREAKER.trial_in_flight = trial

    def test_closed_never_skips(self):
        breaker = PgCircuitBreaker()
        self.assertFalse(breaker.should_skip(1_000))

    def test_three_failures_open_then_trial_then_close(self):
        breaker = PgCircuitBreaker()
        t = 1_700_000_000_000
        breaker.record_failure(t)
        breaker.record_failure(t)
        self.assertFalse(breaker.should_skip(t))
        breaker.record_failure(t)
        self.assertTrue(breaker.should_skip(t + 1_000))
        # Cooldown lapses: exactly one trial passes, the rest keep skipping.
        self.assertFalse(breaker.should_skip(t + 61_000))
        self.assertTrue(breaker.should_skip(t + 61_000))
        # Trial success closes the breaker.
        breaker.record_success()
        self.assertFalse(breaker.should_skip(t + 62_000))

    def test_trial_failure_reopens(self):
        breaker = PgCircuitBreaker()
        t = 1_700_000_000_000
        for _ in range(3):
            breaker.record_failure(t)
        self.assertFalse(breaker.should_skip(t + 61_000))  # trial
        breaker.record_failure(t + 61_000)
        self.assertTrue(breaker.should_skip(t + 61_001))

    def test_fire_stands_down_on_open_breaker(self):
        from market_service.substrate_worker.contracts import TriggerDecision
        from market_service.substrate_worker.migration_worker import MigrationWorker
        worker = MigrationWorker.__new__(MigrationWorker)
        worker.SUBSTRATE_NAME = "migration"
        worker.symbol = "SOLUSDT"
        worker.pg_store = object()
        worker.governor = type("G", (), {
            "pg_circuit_skip": lambda self, now: True})()
        worker._last_dormant_reason = None
        worker._fired = 0
        decision = TriggerDecision(fired=True, source="probe")

        async def _main():
            await worker._fire(decision, None, 1_700_000_001_000, high_water="w")

        _run(_main())
        self.assertEqual(worker._last_dormant_reason, "pg_circuit_open")
        self.assertEqual(worker._fired, 0)


class PgAbortTests(unittest.TestCase):
    def setUp(self):
        gates_module._PG_BREAKER.record_success()
        self.addCleanup(gates_module._PG_BREAKER.record_success)

    def _bare_migration(self, pg_store):
        from market_service.substrate_worker.migration_worker import MigrationWorker
        from market_service.runtime.horizons import new_fold_state
        worker = MigrationWorker.__new__(MigrationWorker)
        worker.SUBSTRATE_NAME = "migration"
        worker.symbol = "SOLUSDT"
        worker.horizons = ("1h", "4h")
        worker.pg_store = pg_store
        worker.pg_strict = True
        worker.governor = None
        worker.dispatcher = None
        worker.telemetry = None
        worker.depth = None
        worker.window_minutes = 15
        worker._PG_INSERT_TIMEOUT_S = 10.0
        worker.compute = lambda evidence, depth, artifacts=None: {"m": 1.0}
        worker._fire_lock = asyncio.Semaphore(1)
        worker._compute_slot = None  # replaced below (needs loop-bound ctx)
        worker._pending = None
        worker._horizons_rebuilt_from = None
        worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
        worker._last_fire_ms = None
        worker._last_error = None
        worker._last_dormant_reason = None
        worker._fired = 0
        return worker

    def test_strict_pg_abort_rearms_cooldown(self):
        import contextlib
        from market_service.substrate_worker.contracts import TriggerDecision

        class _FailPg:
            async def record_substrate_state(self, *a, **kw):
                raise ConnectionError("pg down")

        worker = self._bare_migration(_FailPg())
        worker._plane = "substrate"
        recorded = []

        async def _allow(**kw):
            return type("V", (), {"allowed": True})()

        worker.governor = type("G", (), {
            "pg_record_failure": lambda self, now: recorded.append(now),
            "pg_record_success": lambda self: recorded.append("ok"),
            "authorize_publish": lambda self, **kw: _allow(**kw),
            "authorize_pg_write": lambda self, **kw: _allow(**kw)})()

        @contextlib.asynccontextmanager
        async def _no_slot(reason):
            yield

        worker._compute_slot = _no_slot
        decision = TriggerDecision(fired=True, source="probe")
        now_ms = 1_700_000_001_000

        async def _main():
            await worker._fire_guarded(decision, None, now_ms,
                                       evidence={"_artifact_nonce": 3})

        _run(_main())
        # Failure quiets the worker: the cooldown gate now suppresses the
        # next probe fire, and the breaker counted the failure.
        self.assertEqual(worker._last_fire_ms, now_ms)
        self.assertEqual(worker._last_dormant_reason, "pg_insert_failed")
        self.assertEqual(recorded, [now_ms])


class PgDecoupleTests(unittest.TestCase):
    def test_only_horizon_workers_keep_pg(self):
        from tests.test_substrate_worker_core import _FakeRedis, _FakeStore
        from market_service.substrate_worker.oi_worker import OiWorker
        from market_service.substrate_worker.density_worker import DensityWorker
        from market_service.substrate_worker.migration_worker import MigrationWorker
        from market_service.substrate_worker.tape_worker import TapeWorker
        from market_service.substrate_worker.technicals_worker import TechnicalsWorker
        pg = object()
        oi = OiWorker(_FakeStore(_FakeRedis()), symbol="SOLUSDT", pg_store=pg)
        self.assertIsNone(oi.pg_store)
        for cls in (DensityWorker, MigrationWorker, TapeWorker, TechnicalsWorker):
            w = cls(_FakeStore(_FakeRedis()), symbol="SOLUSDT", pg_store=pg)
            self.assertIs(w.pg_store, pg)


class LeaseTests(unittest.TestCase):
    def test_overrun_counted_on_heartbeat(self):
        async def _main():
            gov = GovernanceGovernor(_StubFSM(), None)
            with patch.object(GovernanceGovernor, "_slot_lease_s",
                              new=staticmethod(lambda: 0)):
                await gov.acquire_compute_slot(symbol="S", substrate="x")
                await asyncio.sleep(0.02)
                await gov.release_compute_slot(symbol="S", substrate="x")
                return gov.compute_slot_status()

        status = _run(_main())
        self.assertEqual(status["lease_violations"], 1)
        self.assertGreaterEqual(status["oldest_hold_s"], 0)
