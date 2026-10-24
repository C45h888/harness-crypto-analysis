"""The store's namespace configuration and its strict write-side contract.

S1 of the read-plane integration: a Redis key family exists exactly once — as
a ``Namespace`` entry in ``runtime/redis_store.py``. Key builders delegate to
it, publishers are validated against it, retention and the schema stamp come
from it, and the retired families are listed so their leftovers can be dropped.

What these tests defend:

* no key string can drift from the configuration (builders == ``key()``);
* the worker surface is CONFIGURED (literal lists), not discovered at import;
* a writer that drifts from its contract FAILS AT THE WRITE (strict), instead
  of surfacing as a silent null three planes away;
* the retired patterns can never match a live namespace key.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import unittest

from market_service.runtime import redis_store as rs


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Records the calls the store makes; never talks to a server."""

    def __init__(self, scan_results: dict[str, list[str]] | None = None):
        self.calls: list[tuple] = []
        self._scan = scan_results or {}

    async def set(self, *args, **kwargs):
        self.calls.append(("set", args, kwargs))
        return True

    async def xadd(self, *args, **kwargs):
        self.calls.append(("xadd", args, kwargs))
        return "1-0"

    async def eval(self, *args, **kwargs):
        self.calls.append(("eval", args, kwargs))
        return "2-0"

    async def expire(self, *args, **kwargs):
        self.calls.append(("expire", args, kwargs))
        return True

    async def get(self, key):
        self.calls.append(("get", (key,), {}))
        return None

    async def xlen(self, key):
        return 0

    async def scan_iter(self, match=None, count=None):
        for key in self._scan.get(match, []):
            yield key

    async def unlink(self, *keys):
        self.calls.append(("unlink", keys, {}))
        return len(keys)

    async def aclose(self):
        return True


def _store(redis=None, **kwargs) -> rs.RedisRuntimeStore:
    store = rs.RedisRuntimeStore("redis://unused/0", "marketflow", **kwargs)
    store.redis = redis or _FakeRedis()
    return store


def _evidence(store) -> dict:
    return {
        "schema_version": 1, "symbol": "SOLUSDT", "venue": "futures",
        "evidence_id": "ev-1", "generated_at_ms": 1, "received_ts_ms": 1,
    }


# ---------------------------------------------------------------------------
# Key builders — one source of key shape
# ---------------------------------------------------------------------------

class KeyBuilderParityTests(unittest.TestCase):
    """Every builder returns exactly the configured key (no drift possible)."""

    CASES = (
        ("agent.artifacts", lambda s: s.agent_stream("sess", "memory"),
         dict(session="sess", artifact="memory")),
        ("derivatives.latest", lambda s: s.derivatives_key("sol"),
         dict(symbol="SOL")),
        ("derivatives.stream", lambda s: s.derivatives_stream("sol"),
         dict(symbol="SOL")),
        ("raw.latest", lambda s: s.raw_latest_key("sol"), dict(symbol="SOL")),
        ("raw.stream", lambda s: s.raw_stream("sol"), dict(symbol="SOL")),
        ("raw.dedupe", lambda s: s.raw_dedupe_key("sol", "123"),
         dict(symbol="SOL", observed_at_ms="123")),
        ("micro.raw", lambda s: s.microstructure_raw_stream("futures", "sol"),
         dict(venue="futures", symbol="SOL")),
        ("micro.events",
         lambda s: s.microstructure_event_stream("futures", "sol"),
         dict(venue="futures", symbol="SOL")),
        ("micro.ofi", lambda s: s.microstructure_ofi_stream("futures", "sol"),
         dict(venue="futures", symbol="SOL")),
        ("micro.book", lambda s: s.microstructure_book_key("futures", "sol"),
         dict(venue="futures", symbol="SOL")),
        ("micro.status", lambda s: s.microstructure_status_key("futures", "sol"),
         dict(venue="futures", symbol="SOL")),
        ("micro.status.events",
         lambda s: s.microstructure_status_stream("futures", "sol"),
         dict(venue="futures", symbol="SOL")),
        ("micro.evidence",
         lambda s: s.microstructure_evidence_key("futures", "sol"),
         dict(venue="futures", symbol="SOL")),
        ("inference.latest", lambda s: s.inference_latest_key("sol", "futures"),
         dict(symbol="SOL", venue="futures")),
        ("inference.stream", lambda s: s.inference_stream("sol", "futures"),
         dict(symbol="SOL", venue="futures")),
        ("poller.active", lambda s: s.poller_control_key(), {}),
        ("poller.status", lambda s: s.poller_status_key(), {}),
        ("substrate.tape", lambda s: s.substrate_latest_key("tape", "sol"),
         dict(symbol="SOL")),
        ("substrate.tape.events", lambda s: s.substrate_stream("tape", "sol"),
         dict(symbol="SOL")),
        ("substrate.tape.supervisor",
         lambda s: s.substrate_supervisor_key("tape", "sol"), dict(symbol="SOL")),
        ("analysis.regime", lambda s: s.analysis_latest_key("regime", "sol"),
         dict(symbol="SOL")),
        ("analysis.regime.events", lambda s: s.analysis_stream("regime", "sol"),
         dict(symbol="SOL")),
        ("analysis.regime.supervisor",
         lambda s: s.analysis_supervisor_key("regime", "sol"), dict(symbol="SOL")),
        ("governance.latest",
         lambda s: s.governance_latest_key("calculation", "tape", "sol"),
         dict(plane="calculation", name="tape", symbol="SOL")),
        ("governance.stream", lambda s: s.governance_stream("calculation", "sol"),
         dict(plane="calculation", symbol="SOL")),
        ("governance.denials",
         lambda s: s.governance_denial_stream("calculation", "sol"),
         dict(plane="calculation", symbol="SOL")),
        ("governance.supervisor",
         lambda s: s.governance_supervisor_key("calculation", "sol"),
         dict(plane="calculation", symbol="SOL")),
    )

    def test_every_builder_matches_its_namespace(self):
        store = _store()
        for namespace, build, ids in self.CASES:
            with self.subTest(namespace=namespace):
                self.assertEqual(build(store), store.key(namespace, **ids))

    def test_every_case_names_a_configured_namespace(self):
        for namespace, _build, _ids in self.CASES:
            self.assertIn(namespace, rs.NAMESPACES)

    def test_key_rejects_an_undeclared_namespace(self):
        with self.assertRaises(rs.StoreContractError):
            _store().key("micro.nope", symbol="SOL")


# ---------------------------------------------------------------------------
# The configured surface
# ---------------------------------------------------------------------------

class NamespaceConfigTests(unittest.TestCase):
    def test_every_namespace_declares_a_writer(self):
        for spec in rs.NAMESPACES.values():
            self.assertTrue(spec.writer, f"{spec.id} has no declared writer")

    def test_templates_only_use_known_tokens(self):
        allowed = {"symbol", "venue", "name", "plane", "session", "artifact",
                   "observed_at_ms", "owner", "subject_type", "subject_id",
                   "consumer", "event_id"}
        for spec in rs.NAMESPACES.values():
            tokens = {part.split("}")[0] for part in spec.template.split("{")[1:]}
            self.assertTrue(tokens <= allowed,
                            f"{spec.id} uses unknown tokens {tokens - allowed}")

    def test_templates_are_unique_per_namespace(self):
        seen: dict[str, str] = {}
        for spec in rs.NAMESPACES.values():
            key = (spec.template, spec.kind)
            self.assertNotIn(key, seen, f"{spec.id} duplicates {seen.get(key)}")
            seen[key] = spec.id

    def test_worker_surface_is_configured_not_discovered(self):
        """The worker names are literals in the store, pinned by this test."""
        from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY
        from market_service.substrate_worker import WORKER_REGISTRY

        self.assertEqual(tuple(sorted(rs.SUBSTRATE_NAMES)),
                         tuple(sorted(WORKER_REGISTRY)))
        self.assertEqual(tuple(sorted(rs.ANALYSIS_NAMES)),
                         tuple(sorted(ANALYSIS_WORKER_REGISTRY)))
        source = pathlib.Path(rs.__file__).read_text()
        self.assertNotIn("WORKER_REGISTRY", source,
                         "the store must not discover worker names at import")

    def test_every_worker_has_latest_stream_and_supervisor(self):
        for name in rs.SUBSTRATE_NAMES:
            for suffix in ("", ".events", ".supervisor"):
                self.assertIn(f"substrate.{name}{suffix}", rs.NAMESPACES)
        for name in rs.ANALYSIS_NAMES:
            for suffix in ("", ".events", ".supervisor"):
                self.assertIn(f"analysis.{name}{suffix}", rs.NAMESPACES)

    def test_retention_is_declared_where_it_differs_from_the_default(self):
        self.assertEqual(rs.NAMESPACES["derivatives.latest"].retention.ttl_s, 300)
        raw = rs.NAMESPACES["raw.stream"].retention
        self.assertEqual(raw.retention_ms, 43_200_000)
        self.assertEqual(raw.guardrail_maxlen, 20_000)


# ---------------------------------------------------------------------------
# Strict write-side contract
# ---------------------------------------------------------------------------

class ContractTests(unittest.TestCase):
    def test_missing_required_key_raises(self):
        store = _store()
        with self.assertRaises(rs.StoreContractError) as ctx:
            store._contract("micro.evidence", {"symbol": "SOLUSDT"})
        self.assertIn("venue", str(ctx.exception))

    def test_missing_schema_version_is_stamped(self):
        payload = _store()._contract(
            "micro.evidence",
            {"symbol": "SOLUSDT", "venue": "futures"})
        self.assertEqual(payload["schema_version"], 1)

    def test_schema_mismatch_raises(self):
        with self.assertRaises(rs.StoreContractError):
            _store()._contract("micro.evidence", {
                "symbol": "SOLUSDT", "venue": "futures",
                "schema_version": 999})

    def test_non_mapping_raises(self):
        with self.assertRaises(rs.StoreContractError):
            _store()._contract("micro.evidence", ["not", "an", "object"])

    def test_text_namespace_requires_a_non_empty_string(self):
        store = _store()
        self.assertEqual(store._contract("agent.artifacts", "{}"), "{}")
        with self.assertRaises(rs.StoreContractError):
            store._contract("agent.artifacts", "")

    def test_sequence_namespace_requires_a_non_empty_sequence(self):
        store = _store()
        self.assertEqual(store._contract("poller.active", ["SOLUSDT"]),
                         ["SOLUSDT"])
        with self.assertRaises(rs.StoreContractError):
            store._contract("poller.active", [])

    def test_worker_payload_contract(self):
        store = _store()
        with self.assertRaises(rs.StoreContractError):
            store._contract("substrate.tape", {"symbol": "SOLUSDT"})
        stamped = store._contract("substrate.tape", {
            "substrate": "tape", "symbol": "SOLUSDT", "computed_at_ms": 1})
        self.assertEqual(stamped["schema_version"], 2)


class PublisherContractTests(unittest.TestCase):
    """A drifting writer fails at the write, before Redis is touched."""

    def test_invalid_payload_raises_without_writing(self):
        redis = _FakeRedis()
        store = _store(redis)
        with self.assertRaises(rs.StoreContractError):
            asyncio.run(store.publish(
                "micro.events", {"previous": {}},
                ids={"venue": "futures", "symbol": "SOLUSDT"}, maxlen=10))
        self.assertEqual(redis.calls, [])

    def test_valid_payload_writes_the_configured_key(self):
        redis = _FakeRedis()
        store = _store(redis)
        payload = {"schema_version": 1, "event_type": "best_quote_transition",
                   "current": {"bid_price": "1"}, "previous": {}}
        asyncio.run(store.publish(
            "micro.events", payload,
            ids={"venue": "futures", "symbol": "SOLUSDT"}, maxlen=10))
        self.assertEqual(redis.calls[0][0], "xadd")
        self.assertEqual(
            redis.calls[0][1][0],
            store.key("micro.events", venue="futures", symbol="SOLUSDT"))

    def test_configured_ttl_wins_over_the_caller(self):
        redis = _FakeRedis()
        store = _store(redis)
        asyncio.run(store.publish(
            "derivatives.latest", {"observed_at_ms": 1},
            ids={"symbol": "SOLUSDT"}))
        # eval(script, numkeys, key1, key2, ...) — the script is the first arg
        script = redis.calls[0][1][0]
        self.assertIn("'EX', 300", script)

    def test_configured_raw_retention_is_used(self):
        redis = _FakeRedis()
        store = _store(redis, raw_retention_ms=1, raw_guardrail_maxlen=1)
        asyncio.run(store.publish(
            "raw.latest", {"observed_at_ms": 1, "spot": {}, "futures": {}},
            ids={"symbol": "SOLUSDT"}))
        # eval(script, keys, body, ts, guard_ttl, stream_body, maxlen, minid)
        args = redis.calls[0][1]
        self.assertEqual(args[-2], str(20_000))   # guardrail maxlen from config
        self.assertNotEqual(args[-1], "1")        # retention_ms from config


# ---------------------------------------------------------------------------
# Retired families
# ---------------------------------------------------------------------------

class RetiredNamespaceTests(unittest.TestCase):
    def _live_keys(self, store) -> list[str]:
        keys = []
        for spec in rs.NAMESPACES.values():
            ids = {"symbol": "SOLUSDT", "venue": "futures", "name": "tape",
                   "plane": "calculation", "session": "s", "artifact": "memory",
                   "observed_at_ms": "1", "owner": "sink",
                   "subject_type": "symbol", "subject_id": "SOLUSDT",
                   "consumer": "projection", "event_id": "e1"}
            keys.append(f"{store.prefix}:{spec.template.format(**ids)}")
        return keys

    def test_retired_patterns_never_match_a_live_key(self):
        from fnmatch import fnmatch

        store = _store()
        patterns = [p.format(prefix=store.prefix) for p in rs.RETIRED_KEY_PATTERNS]
        for key in self._live_keys(store):
            for pattern in patterns:
                self.assertFalse(fnmatch(key, pattern),
                                 f"{key} matched retired pattern {pattern}")

    def test_purge_dry_run_reports_without_deleting(self):
        retired = [f"marketflow:latest:SOLUSDT:collated"]
        redis = _FakeRedis({f"marketflow:latest:*:collated": retired})
        result = asyncio.run(_store(redis).purge_retired())
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["deleted"], 0)
        self.assertFalse(any(call[0] == "unlink" for call in redis.calls))

    def test_purge_execute_deletes_matched_keys(self):
        retired = ["marketflow:latest:SOLUSDT:collated"]
        redis = _FakeRedis({f"marketflow:latest:*:collated": retired})
        result = asyncio.run(_store(redis).purge_retired(dry_run=False))
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(any(call[0] == "unlink" for call in redis.calls))


# ---------------------------------------------------------------------------
# The telemetry plane is a namespace like any other
# ---------------------------------------------------------------------------

class TelemetryNamespaceTests(unittest.TestCase):
    """telemetry_kernel writes THROUGH the configuration, not beside it."""

    def _telemetry(self):
        from market_service.telemetry_kernel.contracts import TelemetryEvent
        from market_service.telemetry_kernel.store import TelemetryStore

        class _Stub:
            prefix = "marketflow"
            stream_maxlen = 47

            def __init__(self) -> None:
                self.redis = _FakeRedis()

        store = TelemetryStore(_Stub())
        event = TelemetryEvent.create(
            plane="calculation", owner="density", subject_type="symbol",
            subject_id="SOLUSDT", event_type="state_adjudicated",
            source_sequence=7, occurred_at_ms=1_700_000_000_000,
            state={"status": "ok"},
        )
        return store, event

    def test_every_telemetry_key_resolves_to_a_configured_namespace(self):
        from fnmatch import fnmatch

        store, event = self._telemetry()
        render = {token: "*" for token in (
            "symbol", "venue", "name", "plane", "session", "artifact",
            "observed_at_ms", "owner", "subject_type", "subject_id",
            "consumer", "event_id")}
        patterns = ["marketflow:" + spec.template.format(**render)
                    for spec in rs.NAMESPACES.values()]
        keys = [store.event_stream(event), store.latest_key(event),
                store._event_marker(event), store.health_key("poller"),
                store.cursor_key("projection")]
        for key in keys:
            self.assertTrue(any(fnmatch(key, p) for p in patterns),
                            f"{key} is outside the configured namespaces")
            self.assertTrue(key.startswith("marketflow:telemetry:"))

    def test_telemetry_store_composes_no_keys_of_its_own(self):
        source = (pathlib.Path(rs.__file__).parent.parent / "telemetry_kernel"
                  / "store.py").read_text()
        self.assertIn("namespace_key", source)
        self.assertNotIn('\":\".join((', source)
        self.assertNotIn("self.prefix}:", source)

    def test_marker_ttl_comes_from_config_undeclared_maxlen_from_caller(self):
        store, _event = self._telemetry()
        self.assertEqual(
            store.marker_ttl_s,
            rs.namespace_retention("telemetry.dedupe").ttl_s)
        self.assertEqual(store.stream_maxlen, 47)  # undeclared -> caller

    def test_publish_stamps_the_schema_and_validates_both_payloads(self):
        store, event = self._telemetry()
        projection = {
            "plane": event.plane, "owner": event.owner,
            "subject_type": event.subject_type, "subject_id": event.subject_id,
            "last_event_id": event.event_id,
            "last_event_at_ms": event.occurred_at_ms,
            "source_sequence": event.source_sequence,
            "state": dict(event.state),
        }
        asyncio.run(store.publish(event, projection))
        args = next(call[1] for call in store.redis.calls if call[0] == "eval")
        body = json.loads(args[5])
        projected = json.loads(args[6])
        self.assertEqual(body["schema_version"],
                         rs.SCHEMA_VERSION_REGISTRY["telemetry_event"])
        self.assertEqual(projected["schema_version"],
                         rs.SCHEMA_VERSION_REGISTRY["telemetry_event"])

        with self.assertRaises(rs.StoreContractError):
            asyncio.run(store.publish(event, {"plane": event.plane}))

    def test_telemetry_contracts_reject_drift(self):
        with self.assertRaises(rs.StoreContractError):
            rs.namespace_contract("telemetry.events", {"plane": "calculation"})
        with self.assertRaises(rs.StoreContractError):
            rs.namespace_contract("telemetry.latest", {})
        with self.assertRaises(rs.StoreContractError):
            rs.namespace_contract("telemetry.dedupe", "")
        self.assertEqual(
            rs.namespace_contract("telemetry.dedupe", "1"), "1")


if __name__ == "__main__":
    unittest.main()