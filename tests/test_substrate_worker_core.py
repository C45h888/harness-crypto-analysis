"""SubstrateWorkerCore contract tests — transport, fire orchestration, dedupe.

Uses the same FakeRedis pattern as test_wake_worker.py: the core is driven
through its loop with scripted stream reads, and every fire decision is
verified against the four-layer matrix:

    fire = (L1 arrival AND L2 probe) OR L3(staleness) OR L4(cold start)
           → dedupe → compute → persist (atomic)

A minimal in-test worker (``_ProbeWorker``) supplies the probe/compute hooks
so these tests pin the CORE, not the density substrate (pinned separately in
test_density_worker.py).
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from market_service.substrate_worker.contracts import (
    CadenceProfile,
    SubstrateStatePayload,
    TriggerDecision,
    bound_arrays,
)
from market_service.substrate_worker.core import SubstrateWorkerCore


class _FakeRedis:
    """Scripted Redis: consumed entries are returned once, then empty."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None,
                 latest: dict[str, Any] | None = None):
        self._pending = list(rows or [])
        self.latest: dict[str, Any] | None = latest
        self.stream: list[dict[str, Any]] = []
        self.supervisor: dict[str, str] = {}
        self.eval_calls: list[tuple] = []

    async def xgroup_create(self, stream, group, id=None, mkstream=False):
        return "OK"

    async def script_load(self, source):
        return "dedupe-sha"

    async def xreadgroup(self, group, consumer, streams, count=None,
                         block=None, noack=False):
        if not self._pending:
            await asyncio.sleep(0)
            return []
        stream_key = list(streams.keys())[0]
        batch, self._pending = self._pending, []
        entries = [
            (row["id"], {"payload": json.dumps(row.get("fields") or {})})
            for row in batch
        ]
        return [(stream_key, entries)]

    async def evalsha(self, sha, numkeys, *args):
        self.eval_calls.append(args)
        supervisor_key, candidate, cooldown_ms, high_water, source, now_ms = args
        key = supervisor_key.decode() if isinstance(supervisor_key, bytes) else supervisor_key
        hw = high_water.decode() if isinstance(high_water, bytes) else high_water
        src = source.decode() if isinstance(source, bytes) else source
        cand = candidate.decode() if isinstance(candidate, bytes) else candidate
        current = self.supervisor.get(key)
        if current:
            dec = json.loads(current) if isinstance(current, str) else current
            if (dec.get("high_water") == hw and dec.get("trigger_source") == src):
                return "0"
        self.supervisor[key] = cand
        return "1"

    async def setex(self, key, ttl, value):
        self.supervisor[key.decode() if isinstance(key, bytes) else key] = value

    async def get(self, key):
        key = key.decode() if isinstance(key, bytes) else key
        return self.supervisor.get(key)

    async def eval(self, script, numkeys, *keys_and_args):
        # publish_substrate_state: SET latest + XADD stream
        latest_key, stream_key = keys_and_args[0], keys_and_args[1]
        body = keys_and_args[5] if len(keys_and_args) > 5 else keys_and_args[-2]
        payload = json.loads(body.decode() if isinstance(body, bytes) else body)
        self.latest = payload
        self.stream.append(payload)
        return "1-1"

    async def xrange(self, key, min=None, max=None):
        return []

    async def xread(self, streams, count=None, block=None):
        return []

    async def xrevrange(self, key, count=None):
        return []


class _FakeStore:
    """RedisRuntimeStore-shaped adapter over _FakeRedis (test seam)."""

    def __init__(self, redis: _FakeRedis):
        self.redis = redis

    def raw_stream(self, symbol):
        return f"mkt:stream:raw:{symbol}"

    def substrate_supervisor_key(self, substrate, symbol):
        return f"mkt:substrate:{substrate}:{symbol}:supervisor"

    def substrate_latest_key(self, substrate, symbol):
        return f"mkt:latest:substrate:{substrate}:{symbol}"

    def substrate_stream(self, substrate, symbol):
        return f"mkt:stream:substrate:{substrate}:{symbol}"

    async def read_substrate_latest(self, substrate, symbol):
        return self.redis.latest

    async def read_substrate_history(self, substrate, symbol, count=100):
        return list(reversed(self.redis.stream))[:count]

    async def read_substrate_history_count(self, substrate, symbol):
        return len(self.redis.stream)

    async def publish_substrate_state(self, substrate, symbol, payload):
        self.redis.latest = payload
        self.redis.stream.append(payload)
        return "1-1"

    async def read_raw_latest(self, symbol):
        return None


class _ProbeWorker(SubstrateWorkerCore):
    """Test worker: probe scripted per-test via ``self._decision``."""

    SUBSTRATE_NAME = "probe"
    INPUT_STREAMS = ("raw",)

    def __init__(self, *args, decision=None, computed=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._decision = decision or TriggerDecision(fired=True, source="probe")
        self._computed = computed if computed is not None else {"metric": 1.0}
        self.probe_calls: list[tuple] = []
        self.compute_calls = 0

    def probe(self, window, last_state, now_ms):
        self.probe_calls.append((window, last_state, now_ms))
        return self._decision

    def compute(self, evidence, depth):
        self.compute_calls += 1
        return dict(self._computed)


def _worker(rows=None, latest=None, decision=None, computed=None, **kw) -> _ProbeWorker:
    store = _FakeStore(_FakeRedis(rows=rows, latest=latest))
    kwargs = {"cooldown_s": 0, "staleness_s": 120, **kw}
    return _ProbeWorker(store, symbol="SOLUSDT", decision=decision,
                        computed=computed, **kwargs)


def _rows(n=1, start_ms=1_700_000_000_000):
    return [{"id": f"{start_ms + i}-0", "fields": {}} for i in range(n)]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _NogroupRedis(_FakeRedis):
    """Fails the first XREADGROUP with NOGROUP, then behaves normally."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.group_creates = 0
        self._nogroup_fired = False

    async def xgroup_create(self, stream, group, id=None, mkstream=False):
        self.group_creates += 1
        return "OK"

    async def xreadgroup(self, group, consumer, streams, count=None,
                         block=None, noack=False):
        if not self._nogroup_fired:
            self._nogroup_fired = True
            raise _NogroupError(
                f"NOGROUP No such key '{list(streams)[0]}' "
                f"or consumer group '{group}' in XREADGROUP with GROUP option")
        return await super().xreadgroup(group, consumer, streams, count=count,
                                        block=block, noack=noack)


class _NogroupError(Exception):
    """Test double for the Redis NOGROUP response error (no redis dep)."""


class _WsStore(_FakeStore):
    """Fake store with the microstructure WS surface attached."""
    def microstructure_event_stream(self, venue, symbol):
        return f"mkt:stream:micro:{venue}:{symbol}:events"

    def microstructure_status_stream(self, venue, symbol):
        return f"mkt:stream:micro:{venue}:{symbol}:status"


class _WsProbeWorker(_ProbeWorker):
    SUBSTRATE_NAME = "wsprobe"
    INPUT_STREAMS = ("raw", "microstructure")
    CADENCE = CadenceProfile(cooldown_s=0, staleness_s=3600, ws_input=True)


class GroupSelfHealTests(unittest.TestCase):
    def test_main_read_heals_nogroup_once(self):
        store = _FakeStore(_NogroupRedis())
        w = _ProbeWorker(store, symbol="SOLUSDT", cooldown_s=0, staleness_s=120)
        rows = _run(w._read_once(block_ms=1))
        self.assertEqual(rows, [])
        # group re-created (main + ws-absent) then the retry read cleanly
        self.assertGreaterEqual(store.redis.group_creates, 1)
        self.assertIsNone(w._last_error)  # healed: no standing error

    def test_ws_read_heals_nogroup_and_logs_once(self):
        store = _WsStore(_NogroupRedis())
        w = _WsProbeWorker(store, symbol="SOLUSDT", cooldown_s=0, staleness_s=3600)
        self.assertIsNotNone(w._ws_stream)
        ws_rows, recovery = _run(w._read_ws_once(block_ms=1))
        self.assertEqual(ws_rows, [])
        self.assertFalse(recovery)
        self.assertIsNone(w._last_error)  # healed on retry
        self.assertGreaterEqual(store.redis.group_creates, 1)

    def test_repeated_nogroup_degrades_with_error(self):
        class _AlwaysNogroup(_NogroupRedis):
            async def xreadgroup(self, group, consumer, streams, count=None,
                                 block=None, noack=False):
                raise _NogroupError("NOGROUP gone")
        store = _FakeStore(_AlwaysNogroup())
        w = _ProbeWorker(store, symbol="SOLUSDT", cooldown_s=0, staleness_s=120)
        rows = _run(w._read_once(block_ms=1))
        self.assertEqual(rows, [])
        self.assertIsNotNone(w._last_error)
        self.assertIn("NOGROUP", w._last_error)


class StarvationCheckTests(unittest.TestCase):
    def test_flags_live_never_fired_with_error(self):
        from market_service.substrate_worker.healthcheck import check_starvation
        redis = _FakeRedis()
        store = _FakeStore(redis)
        beat = json.dumps({"state": "running", "substrate": "probe",
                           "symbol": "SOLUSDT", "fired": 0,
                           "last_error": "ws read: NOGROUP gone"})
        redis.supervisor[store.substrate_supervisor_key("probe", "SOLUSDT")] = beat
        w = _ProbeWorker(store, symbol="SOLUSDT")
        flagged = _run(check_starvation(store, [w]))
        self.assertEqual(len(flagged), 1)
        self.assertIn("probe:SOLUSDT", flagged[0])

    def test_ignores_fresh_and_fired_workers(self):
        from market_service.substrate_worker.healthcheck import check_starvation
        redis = _FakeRedis()
        store = _FakeStore(redis)
        fresh = json.dumps({"state": "running", "fired": 0, "last_error": None})
        fired = json.dumps({"state": "running", "fired": 3, "last_error": "old"})
        redis.supervisor[store.substrate_supervisor_key("probe", "SOLUSDT")] = fresh
        w = _ProbeWorker(store, symbol="SOLUSDT")
        self.assertEqual(_run(check_starvation(store, [w])), [])
        redis.supervisor[store.substrate_supervisor_key("probe", "SOLUSDT")] = fired
        self.assertEqual(_run(check_starvation(store, [w])), [])


class CoreFireMatrixTests(unittest.TestCase):
    """fire = (L1 AND L2) OR L3(staleness) OR L4(cold start)."""

    def test_cold_start_fires_on_first_arrival_without_prior_state(self):
        w = _worker(rows=_rows())
        fired = asyncio.get_event_loop_policy().new_event_loop()
        try:
            count = fired.run_until_complete(asyncio.wait_for(w.run_forever(), timeout=0.5))
        except asyncio.TimeoutError:
            count = w.fired_count
        finally:
            fired.stop()
        self.assertGreaterEqual(w.fired_count, 1)

    def test_quiet_market_with_fresh_state_does_not_fire(self):
        import time
        fresh_state = SubstrateStatePayload.create(
            substrate="probe", symbol="SOLUSDT",
            output={"metric": 1.0}, trigger=TriggerDecision(fired=True, source="probe"),
            computed_at_ms=int(time.time() * 1000) - 1_000,  # 1s old = fresh
        ).to_dict()
        w = _worker(rows=_rows(), latest=fresh_state)
        # probe returns not-fired
        w._decision = TriggerDecision(fired=False, source="probe")
        loop = asyncio.new_event_loop()
        try:
            asyncio.run_coroutine_threadsafe if False else None
            loop.run_until_complete(asyncio.wait_for(w.run_forever(), timeout=0.4))
        except asyncio.TimeoutError:
            pass
        finally:
            loop.stop()
        self.assertEqual(w.fired_count, 0)

    def test_probe_fire_persists_payload(self):
        import time as _time
        state = SubstrateStatePayload.create(
            substrate="probe", symbol="SOLUSDT",
            output={"metric": 1.0}, trigger=TriggerDecision(fired=True, source="probe"),
            computed_at_ms=int(_time.time() * 1000) - 1_000,  # fresh: probe path, not staleness
        ).to_dict()
        w = _worker(rows=_rows(), latest=state)
        w._decision = TriggerDecision(
            fired=True, source="probe",
            predicates={"keystone_delta": {"from": 1.0, "to": 2.0, "threshold": 0.20}},
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.wait_for(w.run_forever(), timeout=0.5))
        except asyncio.TimeoutError:
            pass
        finally:
            loop.stop()
        self.assertGreaterEqual(w.fired_count, 1)
        self.assertGreaterEqual(w.compute_calls, 1)
        persisted = w.store.redis.latest
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["substrate"], "probe")
        self.assertEqual(persisted["symbol"], "SOLUSDT")
        self.assertEqual(persisted["trigger"]["source"], "probe")
        self.assertIn("keystone_delta", persisted["trigger"]["predicates"])
        self.assertEqual(persisted["provenance"], {"substrates": ["probe"]})

    def test_staleness_override_fires_when_no_rows_arrive(self):
        old_state = SubstrateStatePayload.create(
            substrate="probe", symbol="SOLUSDT",
            output={"metric": 1.0}, trigger=TriggerDecision(fired=True, source="probe"),
            computed_at_ms=1_000,  # ancient
        ).to_dict()
        w = _worker(rows=[], latest=old_state, staleness_s=0)
        w._decision = TriggerDecision(fired=False, source="probe")
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.wait_for(w.run_forever(), timeout=0.5))
        except asyncio.TimeoutError:
            pass
        finally:
            loop.stop()
        self.assertGreaterEqual(w.fired_count, 1)

    def test_no_state_and_no_rows_stays_dormant(self):
        w = _worker(rows=[], latest=None)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.wait_for(w.run_forever(), timeout=0.4))
        except asyncio.TimeoutError:
            pass
        finally:
            loop.stop()
        self.assertEqual(w.fired_count, 0)
        self.assertIsNone(w.store.redis.latest)


class CoreDedupeTests(unittest.TestCase):
    def test_dedupe_collapses_same_condition(self):
        redis = _FakeRedis()
        store = _FakeStore(redis)
        w = _ProbeWorker(store, symbol="SOLUSDT", cooldown_s=0)
        loop = asyncio.new_event_loop()
        try:
            first = loop.run_until_complete(
                w._dedupe(TriggerDecision(fired=True, source="probe"), "100-0", 5_000))
            second = loop.run_until_complete(
                w._dedupe(TriggerDecision(fired=True, source="probe"), "100-0", 6_000))
            third = loop.run_until_complete(
                w._dedupe(TriggerDecision(fired=True, source="probe"), "200-0", 7_000))
        finally:
            loop.close()
        self.assertTrue(first)
        self.assertFalse(second)   # same high-water + same source → collapsed
        self.assertTrue(third)     # high-water advanced → allowed


class CoreContractTests(unittest.TestCase):
    def test_worker_without_substrate_name_rejected(self):
        class _Anon(SubstrateWorkerCore):
            pass

        with self.assertRaises(ValueError):
            _Anon(_FakeStore(_FakeRedis()), symbol="SOLUSDT")

    def test_worker_requires_raw_stream_in_phase1(self):
        class _WsOnly(SubstrateWorkerCore):
            SUBSTRATE_NAME = "ws_only"
            INPUT_STREAMS = ("microstructure",)

        with self.assertRaises(ValueError):
            _WsOnly(_FakeStore(_FakeRedis()), symbol="SOLUSDT")

    def test_supervisor_heartbeat_written(self):
        w = _worker(rows=_rows())
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(w._tick(1_700_000_000_000))
        finally:
            loop.close()
        key = w._supervisor_key
        heartbeat = json.loads(w.store.redis.supervisor[key])
        self.assertEqual(heartbeat["substrate"], "probe")
        self.assertEqual(heartbeat["state"], "running")


class PayloadContractTests(unittest.TestCase):
    def test_bound_arrays_truncates_explicitly(self):
        value = list(range(200))
        out = bound_arrays(value, cap=128)
        self.assertEqual(len(out), 129)
        self.assertEqual(out[-1], "__truncated__")

    def test_payload_roundtrip(self):
        payload = SubstrateStatePayload.create(
            substrate="density", symbol="solusdt",
            output={"fut_keystone": {"keystone": 148.2}},
            trigger=TriggerDecision(fired=True, source="cold_start",
                                    predicates={"consumed_entries": 3}),
            freshness={"window_minutes": 15},
            observed_at_ms=1_700_000_000_000,
            computed_at_ms=1_700_000_000_050,
        )
        restored = SubstrateStatePayload.from_dict(payload.to_dict())
        self.assertEqual(restored.substrate, "density")
        self.assertEqual(restored.symbol, "SOLUSDT")
        self.assertEqual(restored.trigger["source"], "cold_start")

    def test_invalid_status_rejected(self):
        bad = {
            "substrate": "density", "symbol": "SOLUSDT", "status": "bogus",
            "computed_at_ms": 1, "trigger": {"source": "probe"}, "output": {},
        }
        with self.assertRaises(ValueError):
            SubstrateStatePayload.from_dict(bad).to_dict()


if __name__ == "__main__":
    unittest.main(verbosity=2)