"""Slice-1 wake-worker tests (nooa-free, no Redis required).

Covers the event-driven wake worker bounded on the Redis plane:

- ``make_wake_envelope`` — the transport-free typed assertion (fresh
  predicates, deterministic wake id, validate at create).
- Fire gate via ``_handle_delta``: event_delta over the artifact high-water
  fires; recovery (state transition) fires; cold start (no artifact) fires;
  unestablished capture never fires; stale replay never re-fires.
- Dedupe: identical conditions collapse, distinct conditions pass.
- Timestamp-water gate: old stream entries do not re-fire.
- Supervisor started/stopped lifecycle; dispatcher Task invocation.
Uses a fake store exposing only the surfaces the worker touches.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

from market_service.nooa_harness.inference import WakeConfig
from market_service.nooa_harness.wake_worker import (
    _STREAM_MAXLEN,
    READ_BLOCK_MS,
    SUPERVISOR_MS,
    TICK_RESET_MS,
    WakeSupervisor,
    WakeSupervisorConfig,
    _StatusPayload,
    _high_water_from_artifact,
    is_recoverable_redis_exc,
    make_wake_envelope,
)

NOW_MS = 1_700_000_000_000  # fixed "now" for determinism


class _FakeStatusKey:
    def __init__(self, payload: dict | None):
        self.payload = payload

    async def get(self, _key):
        return json.dumps(self.payload) if self.payload is not None else None


class _FakeRedis:
    """Minimal in-memory fake for the store surfaces the worker touches."""

    def __init__(
        self,
        *,
        status: dict | None = None,
        artifact: dict | None = None,
        event_len: int = 0,
        status_stream_state: str | None = None,
    ):
        self.status = status
        self.artifact = artifact
        self.event_len = event_len
        self.status_stream_state = status_stream_state
        self.deleted_keys: list = []
        self.supervisor_value: bytes | None = None
        self.script_sha: str | None = None
        self.evalsha_result: str = "1"  # default: allow
        self.evalsha_calls: list = []
        # Deterministic Lua CLI: 1st call writes (allow), identical 2nd call
        # (same condition json) returns 0 (collapse).
        self._last_evalsha_args: tuple | None = None

    async def get(self, key):
        if key.endswith(self._status_key_suffix()):
            return json.dumps(self.status) if self.status is not None else None
        return None

    def _status_key_suffix(self):
        return "status"

    # ---- store methods the worker calls ---------------------------------
    async def read_microstructure_status(self, venue, symbol):
        return dict(self.status) if self.status else None

    async def read_microstructure_events(self, venue, symbol, count=None):
        return []

    async def read_latest_inference_artifact(self, symbol, venue=None):
        return dict(self.artifact) if self.artifact else None

    async def _xlen_fake(self, key):
        return self.event_len

    @property
    def xlen(self):
        return self._xlen_fake

    async def setex(self, key, ttl, value):
        self.supervisor_value = value.encode("utf-8") if isinstance(value, str) else value

    async def set(self, key, value, **kwargs):
        self.supervisor_value = value.encode("utf-8") if isinstance(value, str) else value

    async def delete(self, key):
        self.deleted_keys.append(key)
        self.supervisor_value = None

    async def script_load(self, source):
        self.script_sha = "sha-fake"
        return "sha-fake"

    async def evalsha(self, sha, numkeys, *args):
        self.evalsha_calls.append(args)
        # If an explicit result is set (test override), honor it.
        if self.evalsha_result != "1":
            return self.evalsha_result
        # Deterministic: first call (new condition) writes+allows; a repeat
        # of the exact same condition collapses to 0.
        if self._last_evalsha_args == args:
            return "0"
        self._last_evalsha_args = args
        return "1"

    async def xgroup_create(self, stream, group, id=None, mkstream=False):
        return True

    async def xgroup_create_mkstream(self, stream, group, id=None):
        return True

    async def xread(self, *args, **kwargs):
        """Blocking read of the status-transition stream (tail ``$``)."""
        return []

    async def xreadgroup(self, *args, **kwargs):
        """Blocking consumer-group read of the event stream."""
        return []

    # status stream method the worker calls via store
    async def xadd(self, key, fields, **kwargs):
        return "1-1"


class _FakeStore:
    """Adapter exposing RedisRuntimeStore-shaped properties from _FakeRedis."""

    def __init__(self, *, status=None, artifact=None, event_len=0, status_stream_state=None):
        self.redis = _FakeRedis(
            status=status, artifact=artifact,
            event_len=event_len, status_stream_state=status_stream_state,
        )
        self.prefix = "marketflow"
        self.stream_maxlen = 10_000
        self.symbol = "BTCUSDT"
        self.venue = "spot"

    def microstructure_status_key(self, venue, symbol):
        return f"{self.prefix}:latest:microstructure:{venue}:{symbol}:status"

    def microstructure_event_stream(self, venue, symbol):
        return f"{self.prefix}:stream:microstructure:events:{venue}:{symbol}"

    def microstructure_status_stream(self, venue, symbol):
        return f"{self.prefix}:stream:microstructure:status:{venue}:{symbol}"

    def inference_wake_supervisor_key(self, symbol, venue="spot"):
        return f"{self.prefix}:state:inference:wake:{venue}:{symbol}:supervisor"

    def inference_wake_stream(self, symbol, venue="spot"):
        return f"{self.prefix}:stream:inference:wake:{venue}:{symbol}"

    async def read_microstructure_status(self, venue, symbol):
        return dict(self.redis.status) if self.redis.status else None

    async def read_latest_inference_artifact(self, symbol, venue=None):
        return dict(self.redis.artifact) if self.redis.artifact else None

    async def read_microstructure_events(self, venue, symbol, count=None):
        return []

    async def close(self):
        return None


def _dt_iso(ms: int) -> str:
    """ISO-8601 string at epoch-millis ``ms`` (UTC, with +00:00)."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1_000, tz=timezone.utc).isoformat()


def _make_status(state="running", events=2_000, **overrides) -> dict:
    payload = {
        "schema_version": 1,
        "symbol": "BTCUSDT", "venue": "spot",
        "state": state,
        "updated_at_ms": NOW_MS,
        "messages": 100,
        "best_quote_events": events,
        "completed_ofi_intervals": 50,
        "ofi_interval_seconds": 10,
        "sequence_gaps": 0,
        "reconnects": 0,
        "last_update_id": 1234,
        "error": None,
    }
    payload.update(overrides)
    return payload


def _make_artifact(events_total=1_000, completed_at=None, **overrides) -> dict:
    # completed_at defaults to 60s BEFORE NOW_MS so the worker's cooldown
    # gate (completed must be older than now - cooldown) passes.
    default_completed = _dt_iso(NOW_MS - 120_000)
    artifact = {
        "schema_version": 1,
        "artifact_id": "art-1",
        "symbol": "BTCUSDT", "venue": "spot",
        "generated_at": default_completed,
        "completed_at": completed_at or default_completed,
        "status": "validated",
        "window_minutes": 30, "interval_seconds": 10,
        "deterministic_state": {
            "coverage": {"events_total": events_total},
        },
        "capability_log": [], "input_hash": "abc", "model_version": "v1",
        "interpretation": None, "session_id": None, "errors": [],
    }
    artifact.update(overrides)
    return artifact


class _DeterministicSupervisor(WakeSupervisor):
    """WakeSupervisor with a fixed clock + no-op blocking reads."""

    def __init__(self, store, *, dispatcher=None, **kwargs):
        super().__init__(store, symbol="BTCUSDT", venue="spot", **kwargs)
        self._now_value = NOW_MS
        self.dispatcher = dispatcher or AsyncMock(return_value={"fired": True})
        self._event_rows: list = []
        self._status_rows: list = []

    def _now(self):
        return self._now_value

    def set_now(self, ms):
        self._now_value = ms

    async def _read_once(self, block_ms=None):
        rows = list(self._event_rows)
        self._event_rows = []
        return rows

    async def _read_status_once(self, block_ms=None):
        rows = list(self._status_rows)
        self._status_rows = []
        return rows

    def push_event(self, entry_id: str, fields: dict | None = None):
        self._event_rows.append({"id": entry_id, "fields": fields or {}})

    def push_status(self, entry_id: str, fields: dict | None = None):
        self._status_rows.append({"id": entry_id, "fields": fields or {}})


def _mk_supervisor(store, **kwargs):
    sup = _DeterministicSupervisor(
        store,
        wake_config=WakeConfig(
            event_delta_threshold=1_800,
            cooldown_seconds=60,
        ),
        **kwargs,
    )
    sup._dedupe_lua_sha = "sha-fake"  # skip script registration in tests
    return sup


class MakeWakeEnvelopeTests(unittest.TestCase):
    def test_builds_typed_assertion_with_fresh_predicates(self):
        env = make_wake_envelope(
            symbol="btcusdt", venue="spot", trigger_source="watcher",
            status_state="running", events_total=2_500, high_water=1_000,
            last_artifact_completed_at_ms=1_700_000_000_000 - 600_000,
            now_ms=NOW_MS,
            predicates={"event_delta": {"new_events": 1_500, "threshold": 1_800}},
            water_age_ms=1_200,
        )
        self.assertEqual(env.symbol, "BTCUSDT")
        self.assertEqual(env.trigger_source, "watcher")
        self.assertIn("event_delta", env.predicates_fired)
        self.assertEqual(env.counter_snapshot["events_total"], 2_500)
        self.assertEqual(env.counter_snapshot["water_age_ms"], 1_200)
        self.assertEqual(env.high_water["events_total"], 1_000)
        self.assertTrue(env.wake_id.startswith("wake-"))
        env.validate()

    def test_round_trip_preserves_condition_hash(self):
        env = make_wake_envelope(
            symbol="BTCUSDT", venue="spot", trigger_source="watcher",
            status_state="running", events_total=2_500, high_water=1_000,
            last_artifact_completed_at_ms=None, now_ms=NOW_MS,
            predicates={"event_delta": {"new_events": 1_500, "threshold": 1_800}},
            water_age_ms=1_200,
        )
        restored = type(env).from_mapping(env.to_dict())
        self.assertEqual(restored.wake_id, env.wake_id)
        self.assertEqual(restored.predicates_fired, env.predicates_fired)
        self.assertEqual(restored.counter_snapshot, env.counter_snapshot)

    def test_invalid_trigger_source_rejected(self):
        with self.assertRaises(ValueError):
            make_wake_envelope(
                symbol="BTCUSDT", venue="spot", trigger_source="aliens",
                status_state="running", events_total=1, high_water=None,
                last_artifact_completed_at_ms=None, now_ms=NOW_MS,
                predicates={"cold_start": {}}, water_age_ms=None,
            )


class FireGateTests(unittest.IsolatedAsyncioTestCase):
    async def _fire(self, store, *, events=None, status_rows=None, **sup_kwargs):
        sup = _mk_supervisor(store, **sup_kwargs)
        await sup._register_scripts()
        if events:
            for eid, fields in events:
                sup.push_event(eid, fields)
        if status_rows:
            for sid, fields in status_rows:
                sup.push_status(sid, fields)
        await sup._handle_delta(status_rows or [], events or [], NOW_MS)
        # Let the fired engine Task (create_task) settle before asserting
        # the dispatcher was invoked.
        for task in list(sup._tasks):
            await task
        return sup

    async def test_event_delta_over_high_water_fires(self):
        store = _FakeStore(
            status=_make_status(events=3_000),
            artifact=_make_artifact(events_total=1_000),
            event_len=3_000,
        )
        sup = await self._fire(
            store, events=[("1700000001000-0", {"payload": "{}"})],
        )
        self.assertGreater(sup.fired_count, 0)
        self.assertEqual(sup._last_fired_events_total, 3_000)
        sup.dispatcher.assert_awaited()

    async def test_cold_start_fires_without_artifact(self):
        store = _FakeStore(status=_make_status(events=400), event_len=400)
        sup = await self._fire(
            store, events=[("1700000001000-0", {"payload": "{}"})],
        )
        self.assertGreater(sup.fired_count, 0)

    async def test_capture_recovery_fires_on_state_transition(self):
        store = _FakeStore(
            status=_make_status(state="running", events=1_100),
            artifact=_make_artifact(events_total=1_000),
            event_len=1_100,
        )
        sup = await self._fire(
            store,
            # transition entry: gap -> running (from_state gap)
            status_rows=[("1700000002000-0", {"from_state": "gap", "to_state": "running"})],
        )
        self.assertGreater(sup.fired_count, 0)

    async def test_never_fires_when_capture_not_established(self):
        for state in ("starting", "stopped"):
            with self.subTest(state=state):
                store = _FakeStore(status=_make_status(state=state, events=3_000))
                sup = await self._fire(store)
                self.assertEqual(sup.fired_count, 0)
                sup.dispatcher.assert_not_awaited()

    async def test_cooldown_defers_fire(self):
        store = _FakeStore(
            status=_make_status(events=2_500),
            artifact=_make_artifact(events_total=1_000),
        )
        sup = _mk_supervisor(store)
        sup._last_fire_started_ms = NOW_MS  # fired this exact instant
        sup._last_fired_events_total = 2_500
        sup.push_event(("1700000001000-0", {"payload": "{}"}))
        await sup._handle_delta([], sup._event_rows.pop(0) and [("1700000001000-0", {"payload": "{}"})] or [], NOW_MS)
        # cooldown (60s) not elapsed → deferred
        self.assertEqual(sup.fired_count, 0)
        sup.dispatcher.assert_not_awaited()

    async def test_stale_replay_does_not_refire(self):
        # An old stream entry (age > 15s) with an established artifact must
        # not re-fire a stale delta.
        store = _FakeStore(
            status=_make_status(events=2_500),
            artifact=_make_artifact(events_total=1_000, completed_at="2026-08-28T00:00:30+00:00"),
        )
        sup = await self._fire(
            store, events=[("1699999980000-0", {"payload": "{}"})],  # 20s old
        )
        self.assertEqual(sup.fired_count, 0)


class DedupeTests(unittest.IsolatedAsyncioTestCase):
    async def _sup(self, store):
        sup = _mk_supervisor(store)
        await sup._register_scripts()
        return sup

    async def test_identical_conditions_collapse(self):
        store = _FakeStore(
            status=_make_status(events=2_500),
            artifact=_make_artifact(events_total=1_000),
        )
        sup = await self._sup(store)
        # First dedupe for these exact conditions is allowed, the second is
        # collapsed because evalsha returns 0 for an identical state row.
        model_first = {
            "status_state": "running", "events_total": 2_500,
            "last_artifact_completed_ms": None, "now_ms": NOW_MS,
            "predicates": {"event_delta": {"new_events": 1_500}},
            "high_water": 1_000,
        }
        first = await sup._dedupe(**model_first)
        self.assertTrue(first["allowed"])
        second = await sup._dedupe(**model_first)
        self.assertFalse(second["allowed"])

    async def test_distinct_conditions_pass(self):
        store = _FakeStore(status=_make_status(events=3_000))
        sup = await self._sup(store)
        base = {
            "status_state": "running", "last_artifact_completed_ms": None,
            "now_ms": NOW_MS, "high_water": None,
        }
        a = await sup._dedupe(**base, events_total=2_000,
                             predicates={"event_delta": {"new_events": 500}})
        b = await sup._dedupe(**base, events_total=3_000,
                              predicates={"event_delta": {"new_events": 1_500}})
        self.assertTrue(a["allowed"])
        self.assertTrue(b["allowed"])

    async def test_monotonic_skip_when_last_fired_ahead(self):
        store = _FakeStore(
            status=_make_status(events=2_500),
            artifact=_make_artifact(events_total=1_000),
        )
        sup = await self._sup(store)
        # Fake evalsha returns 0 (dedupe rejects): last fired already ahead.
        sup.store.redis.evalsha_result = "0"
        await sup._register_scripts()
        allowed = await sup._dedupe(
            status_state="running", events_total=2_500,
            last_artifact_completed_ms=None, now_ms=NOW_MS,
            predicates={"event_delta": {}}, high_water=1_000,
        )
        self.assertFalse(allowed["allowed"])


class HighWaterTests(unittest.TestCase):
    def test_reads_events_total_from_artifact_coverage(self):
        self.assertEqual(_high_water_from_artifact(_make_artifact(events_total=777)), 777)

    def test_none_when_no_artifact_or_no_coverage(self):
        self.assertIsNone(_high_water_from_artifact(None))
        # An artifact WITHOUT a coverage.events_total field (schema with no
        # coverage) yields None — cold start, nothing consumed yet.
        bare = _make_artifact()
        bare.pop("deterministic_state", None)
        self.assertIsNone(_high_water_from_artifact(bare))


class SupervisorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_round_trip(self):
        store = _FakeStore(status=_make_status(events=100))
        sup = _mk_supervisor(store)
        await sup.start()
        await sup.stop()
        self.assertFalse(sup._running)
        # The worker must NOT delete the capture-owned status stream.
        self.assertEqual(store.redis.deleted_keys, [])

    async def test_stop_invokes_on_stop_callback(self):
        store = _FakeStore(status=_make_status(events=100))
        closed: list[str] = []

        async def _close():
            closed.append("closed")

        sup = _mk_supervisor(store)
        sup._on_stop = _close
        await sup.start()
        await sup.stop()
        self.assertEqual(closed, ["closed"])


if __name__ == "__main__":
    unittest.main()