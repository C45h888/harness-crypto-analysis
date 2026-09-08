"""Cadence interaction tests — Phase 2 core upgrades (Task 5, core matrix).

Pins the decided cadence doctrine against the core's fire orchestration:

* staleness bypasses cooldown (the Phase 1 bug);
* rollover crossing fires on a quiet probe;
* the min-data gate blocks a thin book (dormant + supervisor reason);
* schema-version mismatch cold-starts instead of raising;
* dedupe distinguishes trigger sources;
* WS status recovery fires (L4).

Reuses the FakeRedis/FakeStore seam from test_substrate_worker_core.py.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from market_service.substrate_worker.contracts import (
    SUBSTRATE_STATE_SCHEMA_VERSION,
    CadenceProfile,
    SubstrateStatePayload,
    TriggerDecision,
)
from market_service.substrate_worker.core import SubstrateWorkerCore
from tests.test_substrate_worker_core import _FakeRedis, _FakeStore


class _CadenceWorker(SubstrateWorkerCore):
    """Scripted-probe worker with a configurable CADENCE profile."""

    SUBSTRATE_NAME = "cadence"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=30, staleness_s=120)

    def __init__(self, *args, decision=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._decision = decision or TriggerDecision(fired=False, source="probe")
        self.compute_calls = 0

    def probe(self, window, last_state, now_ms):
        return self._decision

    def compute(self, evidence, depth):
        self.compute_calls += 1
        return {"metric": 1.0}


class _WsWorker(_CadenceWorker):
    SUBSTRATE_NAME = "cadence_ws"
    INPUT_STREAMS = ("raw", "microstructure")
    CADENCE = CadenceProfile(cooldown_s=5, staleness_s=60, ws_input=True)


def _make_worker(cls=_CadenceWorker, rows=None, latest=None, **kw):
    store = _FakeStore(_FakeRedis(rows=rows, latest=latest))
    return cls(store, symbol="SOLUSDT", **kw)


def _rows(n=1, start_ms=1_700_000_000_000):
    return [{"id": f"{start_ms + i}-0", "fields": {}} for i in range(n)]


def _fresh_state(**kw):
    return SubstrateStatePayload.create(
        substrate="cadence", symbol="SOLUSDT",
        output={"metric": 1.0}, trigger=TriggerDecision(fired=True, source="probe"),
        computed_at_ms=int(time.time() * 1000) - 1_000, **kw,
    ).to_dict()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class StalenessBypassTests(unittest.TestCase):
    def test_staleness_fire_ignores_cooldown(self):
        old = SubstrateStatePayload.create(
            substrate="cadence", symbol="SOLUSDT",
            output={"metric": 1.0}, trigger=TriggerDecision(fired=True, source="probe"),
            computed_at_ms=1_000,
        ).to_dict()
        w = _make_worker(rows=_rows(), latest=old)
        now_ms = int(time.time() * 1000)
        w._last_fire_ms = now_ms  # just fired: probe path would be gated
        _run(w._handle_rows(_rows(), now_ms))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.latest["trigger"]["source"], "staleness")

    def test_probe_fire_respects_cooldown(self):
        w = _make_worker(rows=_rows(), latest=_fresh_state())
        w._decision = TriggerDecision(fired=True, source="probe")
        now_ms = int(time.time() * 1000)
        w._last_fire_ms = now_ms
        _run(w._handle_rows(_rows(), now_ms))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 0)


class RolloverTests(unittest.TestCase):
    def test_hour_boundary_fires_on_quiet_probe(self):
        class _Hourly(_CadenceWorker):
            CADENCE = CadenceProfile(cooldown_s=300, staleness_s=3600,
                                     rollovers=("hour",))

        hour = 3_600_000
        base = (1_700_000_000_000 // hour) * hour
        w = _make_worker(cls=_Hourly, latest=_fresh_state())
        now_ms = int(time.time() * 1000)
        _run(w._handle_rows(_rows(1, start_ms=base - 10_000), now_ms))
        self.assertEqual(w.fired_count, 0)  # first batch: water mark only
        _run(w._handle_rows(_rows(1, start_ms=base + 10_000), now_ms + 1_000))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.latest["trigger"]["source"], "rollover")

    def test_same_bucket_does_not_fire(self):
        class _Hourly(_CadenceWorker):
            CADENCE = CadenceProfile(cooldown_s=300, staleness_s=3600,
                                     rollovers=("hour",))

        hour = 3_600_000
        base = (1_700_000_000_000 // hour) * hour
        w = _make_worker(cls=_Hourly, latest=_fresh_state())
        now_ms = int(time.time() * 1000)
        _run(w._handle_rows(_rows(1, start_ms=base + 1_000), now_ms))
        _run(w._handle_rows(_rows(1, start_ms=base + 2_000), now_ms + 1_000))
        self.assertEqual(w.fired_count, 0)


class MinDataGateTests(unittest.TestCase):
    def test_thin_book_blocks_probe_fire(self):
        class _Strict(_CadenceWorker):
            CADENCE = CadenceProfile(cooldown_s=0, staleness_s=3600,
                                     min_book_depth=10, min_trade_count=5)

        thin = {"futures": {"order_book": {"bids": [[1.0, 2.0]], "asks": []}},
                "spot": {"order_book": {}},
                "coverage": {"spot_trades": {"trade_count": 0},
                             "futures_trades": {"trade_count": 0}}}
        w = _make_worker(cls=_Strict, latest=_fresh_state())
        w._decision = TriggerDecision(fired=True, source="probe")
        now_ms = int(time.time() * 1000)
        with patch("market_service.substrate_worker.core.build_raw_window",
                   return_value=thin):
            _run(w._handle_rows(_rows(), now_ms))
        self.assertEqual(w.fired_count, 0)
        self.assertIn("insufficient_inputs", w._last_dormant_reason or "")
        _run(w._tick(now_ms))
        import json
        heartbeat = json.loads(w.store.redis.supervisor[w._supervisor_key])
        self.assertIn("insufficient_inputs", heartbeat["dormant_reason"] or "")


class VersionMismatchTests(unittest.TestCase):
    def test_future_schema_version_cold_starts(self):
        stale_shape = _fresh_state()
        stale_shape["schema_version"] = SUBSTRATE_STATE_SCHEMA_VERSION + 99
        w = _make_worker(rows=_rows(), latest=stale_shape)
        now_ms = int(time.time() * 1000)
        _run(w._handle_rows(_rows(), now_ms))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.latest["trigger"]["source"], "cold_start")
        self.assertEqual(
            w.store.redis.latest["schema_version"], SUBSTRATE_STATE_SCHEMA_VERSION,
        )


class DedupeSourceTests(unittest.TestCase):
    def test_same_water_different_source_is_allowed(self):
        w = _make_worker()
        first = _run(w._dedupe(TriggerDecision(fired=True, source="probe"), "100-0", 5_000))
        second = _run(w._dedupe(TriggerDecision(fired=True, source="staleness"), "100-0", 6_000))
        self.assertTrue(first)
        self.assertTrue(second)  # source differs → not a duplicate


class RecoveryTests(unittest.TestCase):
    def test_recovery_flag_fires_without_rows(self):
        w = _make_worker(cls=_WsWorker, latest=_fresh_state())
        w.SUBSTRATE_NAME = "cadence"  # align payload substrate with the fresh state
        now_ms = int(time.time() * 1000)
        _run(w._handle_rows([], now_ms, recovery=True))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.latest["trigger"]["source"], "recovery")

    def test_status_transition_parsing(self):
        w = _make_worker(cls=_WsWorker)
        self.assertIsNotNone(w._status_transition_recovery(
            {"from_state": "gap", "state": "running"}))
        self.assertIsNotNone(w._status_transition_recovery(
            {"from": "reconnecting", "to_state": "connected"}))
        self.assertIsNone(w._status_transition_recovery(
            {"from_state": "running", "state": "running"}))
        self.assertIsNone(w._status_transition_recovery({}))

    def test_entry_ms_parsing(self):
        w = _make_worker()
        self.assertEqual(w._entry_ms("1700000000000-0"), 1_700_000_000_000)
        self.assertIsNone(w._entry_ms("not-an-id"))


class DependencyDormantTests(unittest.TestCase):
    def test_missing_tape_keeps_signals_dormant(self):
        from market_service.substrate_worker.contracts import (
            SubstrateStatePayload,
            TriggerDecision,
        )
        from market_service.substrate_worker.signals_worker import SignalsWorker

        prior = SubstrateStatePayload.create(
            substrate="signals", symbol="SOLUSDT",
            output={"signals": [], "snapshot": {
                "spot_buy_share": 0.5, "futures_buy_share": 0.5,
                "spot_obi_top_n": 0.0, "open_interest": 1_000_000.0}},
            trigger=TriggerDecision(fired=True, source="cold_start"),
            computed_at_ms=int(time.time() * 1000) - 1_000,
        ).to_dict()
        store = _FakeStore(_FakeRedis(latest=prior))

        async def _per_substrate(substrate: str, symbol: str):
            return prior if substrate == "signals" else None

        store.read_substrate_latest = _per_substrate  # type: ignore[method-assign]
        w = SignalsWorker(store, symbol="SOLUSDT")
        now_ms = int(time.time() * 1000)
        window = {
            "futures": {"open_interest": {"open_interest": 1_000_000.0}},
            "coverage": {},
        }
        with patch("market_service.substrate_worker.core.build_raw_window",
                   return_value=window):
            _run(w._handle_rows(
                [{"id": f"{now_ms}-0", "fields": {}}], now_ms))
        self.assertEqual(w.fired_count, 0)
        self.assertIn("dependency_missing", w._last_dormant_reason or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
