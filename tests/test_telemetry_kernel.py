"""Behavioral contracts for the out-of-band governed telemetry kernel."""

from __future__ import annotations

import asyncio
import unittest

from market_service.telemetry_kernel import (
    TelemetryEvent,
    TelemetryKernel,
    reduce_projection,
)


class _Store:
    def __init__(self, *, fail_persist: int = 0, fail_publish: int = 0):
        self.fail_persist = fail_persist
        self.fail_publish = fail_publish
        self.persisted: dict[str, dict] = {}
        self.published: list[dict] = []
        self.redis_event_ids: set[str] = set()
        self.projections: dict[tuple[str, str, str, str], dict] = {}
        self.health: dict[str, dict] = {}
        self.cursors: dict[str, dict] = {}

    async def persist(self, event) -> bool:
        if self.fail_persist:
            self.fail_persist -= 1
            raise ConnectionError("telemetry pg unavailable")
        payload = event.to_dict()
        if payload["event_id"] in self.persisted:
            return False
        self.persisted[payload["event_id"]] = payload
        return True

    async def publish(self, event, projection) -> str:
        if self.fail_publish:
            self.fail_publish -= 1
            raise ConnectionError("telemetry redis unavailable")
        if event.event_id in self.redis_event_ids:
            return "duplicate"
        self.redis_event_ids.add(event.event_id)
        self.published.append(event.to_dict())
        self.projections[(
            event.plane, event.owner, event.subject_type, event.subject_id,
        )] = projection
        return f"{event.occurred_at_ms}-0"

    async def read_events(
        self, plane: str, owner: str, subject_type: str, subject_id: str,
        *, limit: int,
    ) -> list[dict]:
        return [
            event for event in self.persisted.values()
            if (event["plane"], event["owner"], event["subject_type"], event["subject_id"])
            == (plane, owner, subject_type, subject_id)
        ][:limit]

    async def read_projection(self, plane, owner, subject_type, subject_id):
        return self.projections.get((plane, owner, subject_type, subject_id))

    async def replace_projection(self, projection: dict) -> None:
        key = (
            projection["plane"], projection["owner"],
            projection["subject_type"], projection["subject_id"],
        )
        self.projections[key] = projection

    async def write_health(self, owner: str, payload: dict) -> None:
        self.health[owner] = payload

    async def read_health(self, owner: str):
        return self.health.get(owner)

    async def write_cursor(self, consumer: str, cursor: dict) -> None:
        self.cursors[consumer] = cursor

    async def read_cursor(self, consumer: str):
        return self.cursors.get(consumer)


class TelemetrySchemaTests(unittest.TestCase):
    def test_schema_registry_owns_telemetry_event_version(self):
        from market_service.runtime.contracts import SCHEMA_VERSION_REGISTRY
        from market_service.telemetry_kernel.contracts import TELEMETRY_EVENT_SCHEMA_VERSION

        self.assertEqual(TELEMETRY_EVENT_SCHEMA_VERSION, 1)
        self.assertEqual(SCHEMA_VERSION_REGISTRY["telemetry_event"], 1)

    def test_pg_migration_and_init_schema_declare_separate_event_ledger(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        migration = (root / "alembic/versions/0018_runtime_telemetry_event.py").read_text()
        init_schema = (root / "db/init/001_schema.sql").read_text()
        self.assertIn('revision = "0018_runtime_telemetry_event"', migration)
        # The telemetry ledger chains onto the canonical-output head: one
        # linear history, never a second branch off 0016.
        self.assertIn('down_revision = "0017_canonical_output"', migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS runtime_telemetry_event", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS runtime_telemetry_event", init_schema)
        self.assertIn("ON CONFLICT (event_id) DO NOTHING", (
            root / "market_service/runtime/postgres_store.py").read_text())


class TelemetryEventContractTests(unittest.TestCase):
    def test_identity_is_stable_for_a_retried_state_emission(self):
        kwargs = dict(
            plane="calculation",
            owner="density",
            subject_type="symbol",
            subject_id="SOLUSDT",
            event_type="state_adjudicated",
            source_sequence=1700000000123,
            occurred_at_ms=1700000000123,
            state={"status": "healthy", "schema_version": 2},
            source_ref="1700000000123-0",
        )
        first = TelemetryEvent.create(**kwargs)
        retry = TelemetryEvent.create(**kwargs)
        self.assertEqual(first.event_id, retry.event_id)
        self.assertEqual(TelemetryEvent.from_mapping(first.to_dict()), first)

    def test_rejects_an_event_type_outside_its_plane_contract(self):
        with self.assertRaises(ValueError):
            TelemetryEvent.create(
                plane="agentic",
                owner="cycle_controller",
                subject_type="cycle",
                subject_id="wake-1",
                event_type="state_adjudicated",
                source_sequence=1,
                occurred_at_ms=1,
                state={"status": "healthy"},
            )

    def test_rejects_raw_evidence_and_narration_from_pure_state_emissions(self):
        for key in ("raw_evidence", "prompt", "interpretation", "api_key"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                TelemetryEvent.create(
                    plane="agentic",
                    owner="cycle_controller",
                    subject_type="cycle",
                    subject_id="wake-1",
                    event_type="agent_loop_transition",
                    source_sequence=1,
                    occurred_at_ms=1,
                    state={key: "must-not-enter-telemetry"},
                )


class _Pool:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []
        self.query = None
        self.args = None

    async def fetchrow(self, query, *args):
        self.query, self.args = query, args
        return self.row

    async def fetch(self, query, *args):
        self.query, self.args = query, args
        return self.rows


class _Redis:
    def __init__(self):
        self.eval_args = None
        self.values = {}

    async def eval(self, script, numkeys, *args):
        self.eval_args = (script, numkeys, args)
        return "1700000000000-0"

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


class TelemetryRedisNamespaceTests(unittest.TestCase):
    def test_store_uses_only_the_telemetry_key_namespace(self):
        from market_service.telemetry_kernel import TelemetryStore

        event = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=2, occurred_at_ms=20, state={"status": "healthy"},
        )
        redis = _Redis()

        class _RedisRuntimeStore:
            prefix = "marketflow"
            stream_maxlen = 47
            def __init__(self):
                self.redis = redis

        store = TelemetryStore(_RedisRuntimeStore())
        stream = store.event_stream(event)
        latest = store.latest_key(event)

        asyncio.run(store.publish(event, {
            "plane": event.plane, "owner": event.owner,
            "subject_type": event.subject_type, "subject_id": event.subject_id,
            "last_event_id": event.event_id, "last_event_at_ms": event.occurred_at_ms,
            "source_sequence": event.source_sequence, "state": event.state,
        }))

        self.assertTrue(stream.startswith("marketflow:telemetry:events:"))
        self.assertTrue(latest.startswith("marketflow:telemetry:latest:"))
        self.assertEqual(redis.eval_args[1], 3)
        self.assertIn("47", redis.eval_args[2])
        self.assertNotIn("marketflow:latest:substrate:", stream)


class TelemetryPostgresStoreTests(unittest.TestCase):
    def test_event_insert_is_idempotent_and_uses_its_own_table(self):
        from market_service.runtime.postgres_store import PostgresRuntimeStore

        event = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=2, occurred_at_ms=20, state={"status": "healthy"},
        )
        pool = _Pool(row={"event_id": event.event_id})
        pg = PostgresRuntimeStore.__new__(PostgresRuntimeStore)
        pg.pool = pool

        inserted = asyncio.run(pg.record_telemetry_event(event.to_dict()))

        self.assertTrue(inserted)
        self.assertIn("runtime_telemetry_event", pool.query)
        self.assertIn("ON CONFLICT (event_id) DO NOTHING", pool.query)
        self.assertEqual(pool.args[0], event.event_id)

    def test_telemetry_history_reads_in_deterministic_order(self):
        import json
        from market_service.runtime.postgres_store import PostgresRuntimeStore

        row = {
            "event_id": "evt-1", "schema_version": 1,
            "plane": "calculation", "owner": "density",
            "subject_type": "symbol", "subject_id": "SOLUSDT",
            "event_type": "state_adjudicated", "source_sequence": 2,
            "occurred_at_ms": 20, "source_ref": None,
            "correlation_id": None, "parent_event_id": None,
            "state": json.dumps({"status": "healthy"}),
        }
        pool = _Pool(rows=[row])
        pg = PostgresRuntimeStore.__new__(PostgresRuntimeStore)
        pg.pool = pool

        events = asyncio.run(pg.read_telemetry_events(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", limit=10,
        ))

        self.assertEqual(events[0]["state"], {"status": "healthy"})
        self.assertIn("ORDER BY occurred_at_ms, source_sequence, event_id", pool.query)


class TelemetryKernelTests(unittest.TestCase):
    def test_emission_is_persisted_and_projected_idempotently(self):
        store = _Store()
        kernel = TelemetryKernel(store)
        event = TelemetryEvent.create(
            plane="agentic",
            owner="cycle_controller",
            subject_type="cycle",
            subject_id="wake-1",
            event_type="agent_loop_transition",
            source_sequence=4,
            occurred_at_ms=1700000000004,
            state={
                "event": "open_subloop",
                "before": {"loop": "context", "sub_loop": "framing"},
                "after": {"loop": "context", "sub_loop": "sourcing"},
                "allowed": True,
            },
            correlation_id="wake-1",
        )

        result = asyncio.run(kernel.emit(event))
        duplicate = asyncio.run(kernel.emit(event))

        self.assertTrue(result.durable)
        self.assertTrue(result.projected)
        self.assertEqual(len(store.persisted), 1)
        self.assertEqual(len(store.published), 1)
        self.assertEqual(duplicate.event_id, result.event_id)

    def test_telemetry_sink_failure_does_not_change_the_governed_state(self):
        store = _Store(fail_persist=5)
        kernel = TelemetryKernel(store, retries=2, retry_delay_s=0)
        event = TelemetryEvent.create(
            plane="calculation",
            owner="density",
            subject_type="symbol",
            subject_id="SOLUSDT",
            event_type="state_adjudicated",
            source_sequence=5,
            occurred_at_ms=1700000000005,
            state={"status": "healthy", "schema_version": 2},
        )

        result = asyncio.run(kernel.emit(event))

        self.assertFalse(result.durable)
        self.assertTrue(result.projected)
        self.assertEqual(len(store.published), 1)
        self.assertEqual(kernel.health()["persist_failures"], 1)

    def test_projection_replay_is_deterministic_and_uses_latest_source_order(self):
        older = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=8, occurred_at_ms=100,
            state={"status": "degraded"},
        )
        newer = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=9, occurred_at_ms=101,
            state={"status": "healthy"},
        )
        first = reduce_projection(None, older)
        second = reduce_projection(first, newer)
        replay = reduce_projection(None, older)
        replay = reduce_projection(replay, newer)
        late_old = reduce_projection(second, older)

        self.assertEqual(second, replay)
        self.assertEqual(second["state"], {"status": "healthy"})
        self.assertEqual(late_old, second)

    def test_rebuild_replays_durable_events_into_redis_projection(self):
        store = _Store()
        kernel = TelemetryKernel(store)
        first = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=1, occurred_at_ms=10, state={"status": "degraded"},
        )
        second = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=2, occurred_at_ms=20, state={"status": "healthy"},
        )
        asyncio.run(store.persist(first))
        asyncio.run(store.persist(second))

        projection = asyncio.run(kernel.rebuild(
            "calculation", "density", "symbol", "SOLUSDT"))

        self.assertEqual(projection["state"], {"status": "healthy"})
        self.assertEqual(projection["event_count"], 2)
        self.assertIn(
            ("calculation", "density", "symbol", "SOLUSDT"), store.projections)


class TelemetryBoundaryContractTests(unittest.TestCase):
    """The kernel cannot reach a canonical keyspace or gate a verdict."""

    _CANONICAL_MARKERS = (
        "marketflow:latest:substrate",
        "marketflow:latest:analysis",
        "marketflow:governance",
        "inference_artifact",
        "market_run",
        "publish_substrate_state",
        "publish_inference_artifact",
    )

    def _telemetry_sources(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        paths = list((root / "market_service/telemetry_kernel").glob("*.py"))
        paths += [
            root / "market_service/nooa_harness/engine/telemetry.py",
            root / "market_service/commands/telemetry.py",
        ]
        return {path.name: path.read_text() for path in paths}

    def test_kernel_never_writes_a_canonical_keyspace_or_ledger(self):
        for name, source in self._telemetry_sources().items():
            with self.subTest(module=name):
                for marker in self._CANONICAL_MARKERS:
                    self.assertNotIn(marker, source)

    def test_a_failing_sink_cannot_break_a_governed_state_emission(self):
        from types import SimpleNamespace
        from market_service.substrate_worker.core.base import SubstrateBase

        class _DeadSink:
            async def emit_state(self, **kwargs):
                raise RuntimeError("telemetry sink down")

        worker = SimpleNamespace(
            telemetry=_DeadSink(), SUBSTRATE_NAME="density",
            _plane="calculation", symbol="SOLUSDT",
        )

        # Must return normally: an observational mirror never gates a fire.
        asyncio.run(SubstrateBase._emit_telemetry_state(
            worker, "state_adjudicated", {"status": "healthy"},
            source_sequence=1, occurred_at_ms=1,
        ))


class TelemetryAdmissionMatrixTests(unittest.TestCase):
    """Phase 1: declared owner→event permissions, not inferred provenance."""

    def test_every_registered_worker_owner_is_admitted(self):
        from market_service.telemetry_kernel.contracts import (
            ANALYSIS_OWNERS,
            CALCULATION_OWNERS,
        )

        self.assertEqual(len(CALCULATION_OWNERS), 12)
        self.assertEqual(len(ANALYSIS_OWNERS), 6)
        for owner, plane in (
            *((name, "calculation") for name in CALCULATION_OWNERS),
            *((name, "analysis") for name in ANALYSIS_OWNERS),
        ):
            with self.subTest(owner=owner):
                event = TelemetryEvent.create(
                    plane=plane, owner=owner, subject_type="symbol",
                    subject_id="SOLUSDT", event_type="state_adjudicated",
                    source_sequence=1, occurred_at_ms=1,
                    state={"status": "healthy"},
                )
                self.assertEqual(event.owner, owner)

    def test_undeclared_owner_is_refused_at_admission(self):
        with self.assertRaises(ValueError) as caught:
            TelemetryEvent.create(
                plane="calculation", owner="mystery_worker",
                subject_type="symbol", subject_id="SOLUSDT",
                event_type="state_adjudicated", source_sequence=1,
                occurred_at_ms=1, state={},
            )
        self.assertIn("not declared", str(caught.exception))

    def test_owner_cannot_emit_on_another_owners_plane(self):
        with self.assertRaises(ValueError) as caught:
            TelemetryEvent.create(
                plane="analysis", owner="density", subject_type="symbol",
                subject_id="SOLUSDT", event_type="state_adjudicated",
                source_sequence=1, occurred_at_ms=1, state={},
            )
        self.assertIn("emits on plane", str(caught.exception))

    def test_agentic_owner_cannot_emit_a_worker_state_event(self):
        with self.assertRaises(ValueError):
            TelemetryEvent.create(
                plane="agentic", owner="cycle_controller",
                subject_type="cycle", subject_id="c1",
                event_type="state_adjudicated", source_sequence=1,
                occurred_at_ms=1, state={},
            )

    def test_idempotency_key_is_the_stable_event_identity(self):
        kwargs = dict(
            plane="capture", owner="capture", subject_type="symbol",
            subject_id="SOLUSDT", event_type="capture_status",
            source_sequence=7, occurred_at_ms=7,
            state={"from_state": "gap", "to_state": "running"},
        )
        once = TelemetryEvent.create(**kwargs)
        retried = TelemetryEvent.create(**kwargs)
        self.assertEqual(once.idempotency_key, retried.idempotency_key)
        self.assertEqual(once.idempotency_key, once.event_id)


class TelemetryHealthSurfaceTests(unittest.TestCase):
    """Phase 2: health/cursor families live outside the event path."""

    def _store(self, redis):
        from market_service.telemetry_kernel import TelemetryStore

        class _RedisRuntimeStore:
            prefix = "nofx"
            stream_maxlen = 100
            def __init__(self):
                self.redis = redis

        return TelemetryStore(_RedisRuntimeStore())

    def test_health_and_cursor_keys_use_only_the_telemetry_prefix(self):
        redis = _Redis()
        store = self._store(redis)
        self.assertEqual(store.health_key("cycle_controller"),
                         "nofx:telemetry:health:cycle_controller")
        self.assertEqual(store.cursor_key("projection"),
                         "nofx:telemetry:cursor:projection")

    def test_health_write_is_direct_and_never_touches_the_event_stream(self):
        redis = _Redis()
        store = self._store(redis)
        asyncio.run(store.write_health("poller", {"status": "degraded"}))
        asyncio.run(store.write_cursor("projection", {"cursor_event_id": "e1"}))

        self.assertIsNone(redis.eval_args)  # no event pipeline involved
        self.assertEqual(
            asyncio.run(store.read_health("poller")), {"status": "degraded"})
        self.assertEqual(
            asyncio.run(store.read_cursor("projection")),
            {"cursor_event_id": "e1"})

    def test_degraded_delivery_is_surfaced_on_health_not_hidden(self):
        store = _Store(fail_persist=5)
        kernel = TelemetryKernel(store, retries=1, retry_delay_s=0)
        event = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=3, occurred_at_ms=3, state={"status": "healthy"},
        )

        asyncio.run(kernel.emit(event))

        self.assertEqual(store.health["density"]["status"], "degraded")
        self.assertEqual(store.health["density"]["persist_failures"], 1)

    def test_kernel_lifecycle_is_local_and_never_recursive(self):
        store = _Store()
        kernel = TelemetryKernel(store)

        payload = asyncio.run(kernel.record_lifecycle("started", plane="boot"))

        self.assertEqual(payload["lifecycle"], "started")
        self.assertEqual(store.health["kernel"]["lifecycle"], "started")
        # A lifecycle record is health, never an emitted event.
        self.assertEqual(store.published, [])
        self.assertEqual(store.persisted, {})


class CaptureTelemetryTests(unittest.TestCase):
    """Phase 3: capture transitions are mirrored after the state machine."""

    class _CaptureStore:
        def __init__(self):
            self.status_calls = 0
            self.transition_calls = 0

        async def set_microstructure_status(self, venue, symbol, payload):
            self.status_calls += 1

        async def publish_microstructure_status_transition(
            self, venue, symbol, payload, *, maxlen,
        ):
            self.transition_calls += 1
            return f"{payload['updated_at_ms']}-0"

    def _capture(self):
        from market_service.microstructure.capture import (
            BinanceSpotDepthCapture,
            MicrostructureSettings,
        )

        capture = BinanceSpotDepthCapture(MicrostructureSettings(
            redis_url="redis://localhost:6379/0", redis_prefix="marketflow",
            symbol="SOLUSDT", venue="futures",
        ))
        capture.store = self._CaptureStore()
        capture.telemetry = TelemetryKernel(_Store())
        capture._telemetry_pg = None
        return capture

    def test_capture_emits_one_receipt_per_transition_only(self):
        capture = self._capture()
        kernel = capture.telemetry
        sink = kernel.store

        asyncio.run(capture._status("connected"))
        asyncio.run(capture._status("connected"))
        asyncio.run(capture._status("gap", error="depth sequence gap"))

        self.assertEqual(len(sink.published), 2)
        self.assertEqual(
            [e["state"]["to_state"] for e in sink.published],
            ["connected", "gap"],
        )
        self.assertEqual(sink.published[1]["state"]["from_state"], "connected")
        self.assertEqual(sink.published[1]["event_type"], "capture_status")
        self.assertEqual(sink.published[1]["plane"], "capture")
        self.assertEqual(sink.published[1]["source_ref"] is not None, True)


class AgenticTelemetryTests(unittest.TestCase):
    """Phase 4: cadence and loop transitions are recorded AFTER adjudication."""

    def _engine(self, store):
        from types import SimpleNamespace

        return SimpleNamespace(
            store=SimpleNamespace(), postgres=None, session_id="session-1",
            _telemetry_kernel=TelemetryKernel(store),
        )

    def _ctx(self):
        from types import SimpleNamespace
        from market_service.nooa_harness.engine.controller import GovernanceRecord

        transition = GovernanceRecord(
            kind="open_subloop", target="sourcing", allowed=True,
            reason="successor accepted", before_loop="context",
            before_sub_loop="framing", after_loop="context",
            after_sub_loop="sourcing",
        )
        denial = GovernanceRecord(
            kind="enter_loop", target="reasoning", allowed=False,
            reason="illegal move", before_loop="context",
            before_sub_loop=None, after_loop="context", after_sub_loop=None,
        )
        return SimpleNamespace(
            controller=SimpleNamespace(
                transitions=(transition, denial), terminal=None,
            ),
            capability_log=[{
                "capability": "engine.cadence",
                "result": "ok",
                "detail": {
                    "interval_seconds": 10, "window_minutes": 30,
                    "horizon_ms": 300000, "regime": "native",
                    "provenance": "cli_approved",
                },
                "governance": "cadence bound: cli-approved, in-vocabulary",
            }],
            wake=SimpleNamespace(wake_id="wake-1"),
        )

    def _artifact(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            artifact_id="artifact-1",
            generated_at="2026-10-19T10:00:00+00:00",
            status="provisional",
        )

    def _emit(self, engine, ctx, artifact, meta):
        from market_service.nooa_harness.engine.telemetry import (
            emit_cycle_telemetry,
        )

        asyncio.run(emit_cycle_telemetry(engine, ctx, artifact, meta))

    def test_cycle_emits_cadence_transitions_and_outcome(self):
        sink = _Store()
        engine = self._engine(sink)
        ctx = self._ctx()

        self._emit(engine, ctx, self._artifact(), {
            "terminal": "settled", "gate": "green", "llm_calls": 4,
            "tool_rounds": 2, "repairs": 0,
            "persistence": {
                "postgres": True, "redis": True, "final": {"postgres": True},
                "postgres_error": "x" * 5000,
            },
        })

        by_type: dict[str, list[dict]] = {}
        for event in sink.published:
            by_type.setdefault(event["event_type"], []).append(event)

        self.assertEqual(len(by_type["agent_cadence"]), 1)
        self.assertEqual(len(by_type["agent_loop_transition"]), 2)
        self.assertEqual(len(by_type["agent_cycle"]), 1)

        cadence = by_type["agent_cadence"][0]["state"]
        self.assertTrue(cadence["adopted"])
        self.assertEqual(cadence["provenance"], "cli_approved")
        self.assertEqual(cadence["interval_seconds"], 10)

        transitions = by_type["agent_loop_transition"]
        self.assertEqual([t["state"]["before_sub_loop"] for t in transitions],
                         ["framing", None])
        self.assertEqual([t["state"]["allowed"] for t in transitions],
                         [True, False])
        self.assertEqual([t["source_sequence"] for t in transitions], [0, 1])

        cycle = by_type["agent_cycle"][0]["state"]
        self.assertEqual(cycle["terminal"], "settled")
        self.assertEqual(cycle["status"], "provisional")
        self.assertEqual(cycle["transitions"], 2)
        self.assertEqual(cycle["denials"], 1)
        self.assertTrue(cycle["durable"]["postgres"])
        # Error TEXT never enters telemetry — only bounded error KINDS.
        self.assertEqual(cycle["durable"]["error_kinds"], ["postgres_error"])
        self.assertNotIn("postgres_error", cycle["durable"])

    def test_reemitting_a_cycle_collapses_on_identity(self):
        sink = _Store()
        engine = self._engine(sink)
        ctx = self._ctx()
        artifact = self._artifact()
        meta = {"terminal": "settled", "gate": "green"}

        self._emit(engine, ctx, artifact, meta)
        first_ids = {event["event_id"] for event in sink.published}
        self._emit(engine, ctx, artifact, meta)

        self.assertEqual(len(sink.persisted), 4)
        self.assertEqual({event["event_id"] for event in sink.published}, first_ids)

    def test_cadence_refusal_is_recorded_as_a_finding(self):
        sink = _Store()
        engine = self._engine(sink)
        ctx = self._ctx()
        ctx.capability_log = [{
            "capability": "engine.cadence.refused",
            "result": "denied",
            "detail": {
                "reasons": ["cadence approval requires the CLI channel"],
                "provenance": "unapproved_source",
            },
            "governance": "cadence refused: not cli-approved",
        }]

        self._emit(engine, ctx, self._artifact(), {"terminal": "settled"})

        cadence = [e for e in sink.published if e["event_type"] == "agent_cadence"]
        self.assertEqual(len(cadence), 1)
        self.assertFalse(cadence[0]["state"]["adopted"])
        self.assertEqual(cadence[0]["state"]["provenance"], "unapproved_source")

    def test_canonical_output_identity_is_accepted_as_the_cycle_subject(self):
        from types import SimpleNamespace

        from market_service.nooa_harness.engine.telemetry import _artifact_id

        canonical = SimpleNamespace(run_id="run-9", generated_at="2026-10-19T10:00:00+00:00")
        legacy = SimpleNamespace(artifact_id="artifact-1")
        self.assertEqual(_artifact_id(canonical), "run-9")
        self.assertEqual(_artifact_id(legacy), "artifact-1")
        self.assertEqual(_artifact_id(SimpleNamespace()), "unknown")

    def test_no_prompt_prose_or_raw_evidence_enters_the_agentic_mirror(self):
        sink = _Store()
        self._emit(self._engine(sink), self._ctx(), self._artifact(), {
            "terminal": "settled",
        })

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)

        forbidden = {
            "prompt", "system_prompt", "user_prompt", "narrative",
            "interpretation", "raw_evidence", "llm_output", "tool_args",
        }
        for event in sink.published:
            with self.subTest(event_type=event["event_type"]):
                self.assertEqual(set(keys(event["state"])) & forbidden, set())


if __name__ == "__main__":
    unittest.main()
