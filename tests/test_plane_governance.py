"""Plane governance tests — two deterministic legality engines, no workers.

Contract under test (docs/PLANE_GOVERNANCE_SPEC.md):

* the calc + analysis FSMs are two SEPARATE engines, each the sole legality
  authority of its plane;
* authority is derived from the plane's own declarations (manifest), never
  mirrored;
* the FSMs judge the outputs that live in Redis and gate the PG write;
* every verdict is reasoned (rational boundary) — a bare denial is impossible;
* governance has NO contractual connection to the workers;
* governance is deterministic — no timers, no sleeps, no scheduled tasks.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from market_service.governance import (
    AnalysisGovernanceFSM,
    CalculationGovernanceFSM,
    GovernanceEvent,
    GovernanceGate,
    GovernanceLedger,
    GovernanceObservation,
    GovernancePlane,
    GovernanceVerdict,
    build_analysis_governor,
    build_calculation_governor,
)
from market_service.governance.manifest import (
    build_analysis_manifest,
    build_calculation_manifest,
    known_graph_sections,
    plane_status_vocabulary,
)
from market_service.substrate_worker.contracts import TriggerDecision

GOVERNANCE_DIR = pathlib.Path(__file__).resolve().parent.parent / "market_service" / "governance"


# ---------------------------------------------------------------------------
# Fakes — Redis + PG surfaces the governor touches (duck-typed, no real I/O)
# ---------------------------------------------------------------------------


class _FakeStore:

    async def read(self, namespace, *, ids=None, count=None, newest_first=True, start="-", end="+"):
        from tests._singular_compat import fake_read
        return await fake_read(self, namespace, ids=ids, count=count, newest_first=newest_first, start=start, end=end)

    async def publish(self, namespace, payload, *, ids=None, **kw):
        from tests._singular_compat import fake_publish
        return await fake_publish(self, namespace, payload, ids=ids, **kw)


    def ns(self, namespace):
        from tests._singular_compat import fake_ns
        return fake_ns(namespace)

    def key(self, namespace, **ids):
        try:
            inner = ":".join(f"{k}={v}" for k, v in sorted(ids.items()))
        except Exception:
            inner = ""
        return f"fake:{namespace}:{inner}"
    def __init__(self, latest: dict | None = None):
        self._latest = latest
        self.published: list[tuple] = []

    async def read_substrate_latest(self, substrate, symbol):
        return self._latest

    async def read_analysis_latest(self, analysis, symbol):
        return self._latest

    async def publish_governance_decision(self, plane, substrate, symbol, payload):
        self.published.append((plane, substrate, symbol, payload))
        return "1-1"


class _FakePg:
    def __init__(self):
        self.rows: list[dict] = []

    async def record_governance_decision(self, record):
        self.rows.append(record)
        return len(self.rows)


def _calc_fsm() -> CalculationGovernanceFSM:
    fsm = CalculationGovernanceFSM(
        build_calculation_manifest(),
        known_sections=known_graph_sections(),
        status_vocabulary=plane_status_vocabulary(),
    )
    fsm.self_check()
    return fsm


def _analysis_fsm() -> AnalysisGovernanceFSM:
    fsm = AnalysisGovernanceFSM(
        build_analysis_manifest(),
        known_sections=known_graph_sections(),
        status_vocabulary=plane_status_vocabulary(),
    )
    fsm.self_check()
    return fsm


_STATUS_FAMILY = frozenset({"healthy", "degraded", "insufficient_data"})


def _obs(plane, substrate="tape", keyspace="substrate", **kw) -> GovernanceObservation:
    if "declared_inputs" not in kw:
        manifests = (build_calculation_manifest()
                     if plane is GovernancePlane.CALCULATION
                     else build_analysis_manifest())
        manifest = manifests.get(substrate)
        if manifest is not None:
            kw["declared_inputs"] = manifest.declared_inputs
    base = dict(
        plane=plane, substrate=substrate, keyspace=keyspace,
        declared_inputs=(), status="healthy", schema_version=2,
        trigger_source="probe", computed_at_ms=1_000,
    )
    base.update(kw)
    return GovernanceObservation(**base)


# ---------------------------------------------------------------------------
# Manifest derivation — authority comes from the plane, never re-typed
# ---------------------------------------------------------------------------


class ManifestDerivationTests(unittest.TestCase):
    def test_calculation_manifest_covers_every_registered_substrate(self):
        from market_service.substrate_worker import WORKER_REGISTRY

        manifest = build_calculation_manifest()
        self.assertEqual(set(manifest), set(WORKER_REGISTRY))
        for name, m in manifest.items():
            self.assertEqual(m.plane, GovernancePlane.CALCULATION)
            self.assertEqual(m.writes_keyspace, "substrate")
            self.assertTrue(m.is_valid(), name)

    def test_analysis_manifest_covers_every_registered_analysis(self):
        from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY

        manifest = build_analysis_manifest()
        self.assertEqual(set(manifest), set(ANALYSIS_WORKER_REGISTRY))
        for name, m in manifest.items():
            self.assertEqual(m.plane, GovernancePlane.ANALYSIS)
            self.assertEqual(m.writes_keyspace, "analysis")
            self.assertTrue(m.is_valid(), name)

    def test_analysis_manifest_declares_substrate_evidence_layer(self):
        for name, m in build_analysis_manifest().items():
            self.assertTrue(
                m.primary_stream.startswith("substrate:"),
                f"{name}: {m.primary_stream}",
            )

    def test_manifest_mirrors_declared_worker_dependencies(self):
        """The signals worker's one declared cross-worker dep must appear."""
        manifest = build_calculation_manifest()
        self.assertEqual(manifest["signals"].dependencies, ("tape",))
        # and the analysis plane's declared deps are mirrored, not invented
        analysis = build_analysis_manifest()
        self.assertEqual(analysis["wall_migration"].dependencies,
                         ("density", "tape", "delta", "oi"))

    def test_manifest_cadence_mirrors_cadence_profile(self):
        manifest = build_calculation_manifest()
        self.assertEqual(manifest["signals"].cooldown_s, 15)
        self.assertEqual(manifest["signals"].staleness_s, 120)


# ---------------------------------------------------------------------------
# Rational boundary — every verdict is reasoned
# ---------------------------------------------------------------------------


class RationalBoundaryTests(unittest.TestCase):
    def test_bare_verdict_is_impossible(self):
        with self.assertRaises(ValueError):
            GovernanceVerdict(True, "")
        with self.assertRaises(ValueError):
            GovernanceVerdict(True, "   ")
        with self.assertRaises(ValueError):
            GovernanceVerdict(True, "because", rule="")

    def test_allow_and_deny_carry_rule_and_reason(self):
        allowed = GovernanceVerdict.allow("allow", "fine")
        denied = GovernanceVerdict.deny("keyspace-trespass", "wrong keyspace")
        for v in (allowed, denied):
            self.assertTrue(v.rule)
            self.assertTrue(v.reason)
            self.assertIn("rule", v.to_dict())


# ---------------------------------------------------------------------------
# Calculation FSM — sole legality authority of the calc plane
# ---------------------------------------------------------------------------


class CalculationFSMTests(unittest.TestCase):
    def setUp(self):
        self.fsm = _calc_fsm()

    def decide(self, obs, gate, substrate="tape"):
        return self.fsm.decide(obs, GovernanceEvent(gate, substrate))

    def test_healthy_fire_is_legal(self):
        v = self.decide(_obs(GovernancePlane.CALCULATION), GovernanceGate.FIRE)
        self.assertTrue(v.allowed, v.reason)
        self.assertEqual(v.rule, "allow")

    def test_healthy_publish_is_legal(self):
        v = self.decide(_obs(GovernancePlane.CALCULATION), GovernanceGate.PUBLISH)
        self.assertTrue(v.allowed, v.reason)

    def test_pg_write_requires_trigger_source(self):
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, trigger_source=None),
            GovernanceGate.PG_WRITE,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "trigger-missing")
        self.assertTrue(v.remedy)

    def test_unknown_substrate_denied(self):
        v = self.decide(_obs(GovernancePlane.CALCULATION), GovernanceGate.FIRE,
                        substrate="not_a_substrate")
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "unknown-substrate")

    def test_keyspace_trespass_denied(self):
        """A calc substrate may only write the substrate keyspace."""
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, keyspace="analysis"),
            GovernanceGate.PUBLISH,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "keyspace-trespass")

    def test_fabricated_degradation_denied(self):
        """insufficient_data without named missing_inputs is not evidence."""
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, status="insufficient_data",
                 missing_inputs=()),
            GovernanceGate.PUBLISH,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "output-fabricated")

    def test_named_degradation_allowed(self):
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, status="insufficient_data",
                 missing_inputs=("derivative_cache_missing_or_stale",)),
            GovernanceGate.PUBLISH,
        )
        self.assertTrue(v.allowed, v.reason)

    def test_status_drift_denied(self):
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, status="banana"),
            GovernanceGate.PUBLISH,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "status-invalid")

    def test_schema_drift_denied(self):
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, schema_version=99),
            GovernanceGate.PUBLISH,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "schema-drift")

    def test_out_of_order_write_denied(self):
        """A projection must never move backwards in Redis."""
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, previous_computed_at_ms=5_000,
                 computed_at_ms=4_000),
            GovernanceGate.PUBLISH,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "out-of-order-write")

    def test_undeclared_input_denied(self):
        """A worker whose runtime binding diverges from the authority record
        (``binding-drift``) may not fire — this catches a changed declaration,
        a governor attached to the wrong worker class, and a stale manifest."""
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, declared_inputs=("raw", "binance")),
            GovernanceGate.FIRE,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "binding-drift")

    def test_binding_drift_is_symmetric(self):
        """A missing declared input is drift too (not just an extra one)."""
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, declared_inputs=()),
            GovernanceGate.FIRE,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "binding-drift")

    def test_read_failure_fails_closed(self):
        """An unreadable history is not an absent history."""
        v = self.decide(
            _obs(GovernancePlane.CALCULATION, read_failed=True),
            GovernanceGate.PUBLISH,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "observation-unavailable")

    def test_analysis_substrate_is_out_of_scope(self):
        """Each FSM is scoped to its own plane: an analysis name is unknown
        to the calculation FSM (it can never grant it authority)."""
        v = self.decide(_obs(GovernancePlane.CALCULATION), GovernanceGate.FIRE,
                        substrate="regime")
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "unknown-substrate")

    def test_plane_mismatch_rule_is_reachable(self):
        """A mis-registered manifest (wrong plane) is denied, not trusted."""
        from market_service.governance.contracts import SubstrateManifest

        stray = {
            "regime": SubstrateManifest(
                plane=GovernancePlane.ANALYSIS, substrate="regime",
                primary_stream="substrate:tape", writes_keyspace="analysis",
                status_family=_STATUS_FAMILY,
            )
        }
        fsm = CalculationGovernanceFSM(stray)
        v = fsm.decide(
            _obs(GovernancePlane.CALCULATION, substrate="regime"),
            GovernanceEvent(GovernanceGate.FIRE, "regime"),
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "plane-mismatch")


# ---------------------------------------------------------------------------
# Analysis FSM — sole legality authority of the analysis plane
# ---------------------------------------------------------------------------


class AnalysisFSMTests(unittest.TestCase):
    def setUp(self):
        self.fsm = _analysis_fsm()

    def decide(self, obs, gate, substrate="regime"):
        return self.fsm.decide(obs, GovernanceEvent(gate, substrate))

    def test_healthy_publish_is_legal(self):
        v = self.decide(
            _obs(GovernancePlane.ANALYSIS, substrate="regime",
                 keyspace="analysis"),
            GovernanceGate.PUBLISH,
        )
        self.assertTrue(v.allowed, v.reason)

    def test_calc_keyspace_trespass_denied(self):
        v = self.decide(
            _obs(GovernancePlane.ANALYSIS, substrate="regime",
                 keyspace="substrate"),
            GovernanceGate.PUBLISH,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "keyspace-trespass")

    def test_calculation_substrate_is_out_of_scope(self):
        """Each FSM is scoped to its own plane: a calc name is unknown to the
        analysis FSM."""
        v = self.decide(_obs(GovernancePlane.ANALYSIS), GovernanceGate.FIRE,
                        substrate="tape")
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "unknown-substrate")

    def test_substrate_root_rule_reachable(self):
        """The plane rule must exist, be named, and be reachable by a
        manifest that declares no substrate root."""
        from market_service.governance.contracts import SubstrateManifest

        broken = {
            "rogue": SubstrateManifest(
                plane=GovernancePlane.ANALYSIS, substrate="rogue",
                primary_stream="raw", writes_keyspace="analysis",
                input_streams=("raw",), status_family=_STATUS_FAMILY,
            )
        }
        fsm = AnalysisGovernanceFSM(broken)
        v = fsm.decide(
            _obs(GovernancePlane.ANALYSIS, substrate="rogue", keyspace="analysis",
                 declared_inputs=("raw",)),
            GovernanceEvent(GovernanceGate.FIRE, "rogue"),
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "analysis-no-substrate-root")

    def test_analysis_rule_is_declared(self):
        self.assertIn("analysis-no-substrate-root", self.fsm.rule_ids())


# ---------------------------------------------------------------------------
# Governor — the I/O adapter (reads Redis, records the ledger)
# ---------------------------------------------------------------------------


class GovernorTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_payload_allowed_and_recorded(self):
        store, pg = _FakeStore(), _FakePg()
        gov = build_calculation_governor(store, pg)
        payload = {
            "status": "healthy", "schema_version": 2, "missing_inputs": [],
            "trigger": {"source": "probe"}, "computed_at_ms": 2_000,
        }
        v = await gov.authorize_pg_write(
            substrate="tape", symbol="SOLUSDT", payload=payload,
        )
        self.assertTrue(v.allowed, v.reason)
        self.assertEqual(len(store.published), 1)
        self.assertEqual(len(pg.rows), 1)
        self.assertEqual(pg.rows[0]["plane"], "calculation")

    async def test_denied_payload_is_recorded_too(self):
        store, pg = _FakeStore(), _FakePg()
        gov = build_calculation_governor(store, pg)
        payload = {
            "status": "insufficient_data", "schema_version": 2,
            "missing_inputs": [], "trigger": {"source": "probe"},
            "computed_at_ms": 2_000,
        }
        v = await gov.authorize_publish(
            substrate="tape", symbol="SOLUSDT", payload=payload,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "output-fabricated")
        # a denial is evidence about the system: it must be durable
        self.assertEqual(len(pg.rows), 1)
        self.assertFalse(pg.rows[0]["allowed"])
        self.assertEqual(pg.rows[0]["rule"], "output-fabricated")

    async def test_observation_reads_previous_redis_projection(self):
        store = _FakeStore(latest={
            "status": "healthy", "schema_version": 2, "computed_at_ms": 9_000,
        })
        gov = build_calculation_governor(store)
        obs = await gov.observe(substrate="tape", symbol="SOLUSDT")
        self.assertTrue(obs.latest_present)
        self.assertEqual(obs.previous_status, "healthy")
        self.assertEqual(obs.previous_computed_at_ms, 9_000)
        self.assertEqual(obs.keyspace, "substrate")

    async def test_analysis_governor_routes_analysis_keyspace(self):
        store = _FakeStore()
        gov = build_analysis_governor(store)
        obs = await gov.observe(substrate="regime", symbol="SOLUSDT")
        self.assertEqual(obs.plane, GovernancePlane.ANALYSIS)
        self.assertEqual(obs.keyspace, "analysis")

    async def test_unreadable_history_fails_closed_not_open(self):
        """A Redis read error is NOT treated as "no history": the FSM cannot
        prove the output is in order, so it refuses (observed-unavailable)."""

        class _Exploding(_FakeStore):
            async def read_substrate_latest(self, substrate, symbol):
                raise RuntimeError("redis down")

        gov = build_calculation_governor(_Exploding())
        payload = {
            "status": "healthy", "schema_version": 2, "missing_inputs": [],
            "trigger": {"source": "staleness"}, "computed_at_ms": 2_000,
        }
        v = await gov.authorize_publish(
            substrate="tape", symbol="SOLUSDT", payload=payload,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "observation-unavailable")

    async def test_fire_gate_reads_no_history(self):
        """The FIRE gate reasons about the binding only; a Redis outage must
        not block it (it reads no previous projection)."""

        class _Exploding(_FakeStore):
            async def read_substrate_latest(self, substrate, symbol):
                raise RuntimeError("redis down")

        gov = build_calculation_governor(_Exploding())
        v = await gov.authorize_fire(substrate="tape", symbol="SOLUSDT")
        self.assertTrue(v.allowed, v.reason)


# ---------------------------------------------------------------------------
# Ledger — Redis + PG, never raises
# ---------------------------------------------------------------------------


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ledger_degrades_without_redis_surface(self):
        class _NoLedger:
            async def read_substrate_latest(self, s, sym):
                return None

        ledger = GovernanceLedger(_NoLedger())
        v = GovernanceVerdict.deny("keyspace-trespass", "wrong keyspace")
        entry = await ledger.record(
            plane="calculation", substrate="tape", symbol="SOLUSDT",
            gate="publish", verdict=v,
        )
        self.assertIsNone(entry)  # honest degradation, not a crash
        inspect = await ledger.inspect("calculation", "SOLUSDT")
        self.assertFalse(inspect["available"])

    async def test_ledger_records_findings_and_observation(self):
        store, pg = _FakeStore(), _FakePg()
        ledger = GovernanceLedger(store, pg)
        v = GovernanceVerdict.deny(
            "output-fabricated", "no inputs named", findings=("a", "b"))
        obs = _obs(GovernancePlane.CALCULATION)
        await ledger.record(
            plane="calculation", substrate="tape", symbol="solusdt",
            gate="publish", verdict=v, observation=obs,
        )
        row = pg.rows[0]
        self.assertEqual(row["symbol"], "SOLUSDT")
        self.assertEqual(row["findings"], ["a", "b"])
        self.assertEqual(row["observation"]["substrate"], "tape")


# ---------------------------------------------------------------------------
# Structural invariants — the doctrine, pinned mechanically
# ---------------------------------------------------------------------------


class GovernancePurityTests(unittest.TestCase):
    def _modules(self):
        return sorted(p for p in GOVERNANCE_DIR.glob("*.py"))

    def test_governance_modules_exist(self):
        names = {p.name for p in self._modules()}
        for required in ("contracts.py", "fsm.py", "calculation_fsm.py",
                         "analysis_fsm.py", "gates.py", "ledger.py",
                         "manifest.py"):
            self.assertIn(required, names)

    def test_no_governance_module_imports_a_worker_or_redis_store(self):
        """Doctrine: no contractual connection to the workers.

        ``manifest.py`` is the ONE documented derivation seam — it reads the
        plane's DECLARATIONS (registers) and nothing else.
        """
        offenders: list[str] = []
        for path in self._modules():
            if path.name == "manifest.py":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if any(bad in name for bad in (
                        "substrate_worker", "analysis_worker", "redis_store",
                        "postgres_store",
                    )):
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], f"governance touched the plane: {offenders}")

    def test_manifest_seam_reads_declarations_only(self):
        """The derivation seam must never instantiate or call a worker."""
        tree = ast.parse((GOVERNANCE_DIR / "manifest.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                target = getattr(fn, "id", None) or getattr(fn, "attr", None)
                self.assertNotIn(
                    target, ("__call__",), "manifest must not invoke a worker")

    def test_no_timer_logic_anywhere_in_governance(self):
        """Lazy design: no sleeps, no scheduled tasks, no timer loops."""
        banned = ("sleep", "call_later", "call_at", "Timer", "ensure_future",
                  "create_task", "run_forever", "run_until_complete")
        offenders: list[str] = []
        for path in self._modules():
            source = path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                fn = None
                if isinstance(node, ast.Call):
                    fn = node.func
                elif isinstance(node, ast.Attribute):
                    fn = node
                if fn is not None:
                    attr = getattr(fn, "attr", None)
                    if attr in banned:
                        offenders.append(f"{path.name}: {attr}")
        self.assertEqual(offenders, [], f"timer logic found: {offenders}")

    def test_both_planes_self_check(self):
        """The constitution is not silently dead."""
        calc = _calc_fsm()
        analysis = _analysis_fsm()
        for fsm in (calc, analysis):
            self.assertTrue(fsm.substrates())
            self.assertTrue(fsm.rule_ids())
            self.assertIn("allow", fsm.rule_ids())

    def test_unknown_plane_rule_is_impossible(self):
        for fsm in (_calc_fsm(), _analysis_fsm()):
            for rid in fsm.PLANE_RULE_IDS:
                self.assertTrue(str(rid).strip())

    def test_render_is_viewable(self):
        for fsm in (_calc_fsm(), _analysis_fsm()):
            rendered = fsm.render()
            self.assertIn(fsm.PLANE.value, rendered)
            self.assertIn("rules:", rendered)


# ---------------------------------------------------------------------------
# Core seam — the worker consults the governor, and only the governor
# ---------------------------------------------------------------------------


class _RecordingGovernor:
    """Duck-typed governor: records calls, answers per policy."""

    def __init__(self, *, allowed=True, raises=False):
        self.allowed = allowed
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def authorize_fire(self, **kw):
        return await self._answer("fire", kw)

    async def authorize_publish(self, **kw):
        return await self._answer("publish", kw)

    async def authorize_pg_write(self, **kw):
        return await self._answer("pg_write", kw)

    async def _answer(self, gate, kw):
        self.calls.append((gate, kw))
        if self.raises:
            raise RuntimeError("governor exploded")
        if self.allowed:
            return GovernanceVerdict.allow("allow", f"{gate} is legal")
        return GovernanceVerdict.deny("test-denial", f"{gate} denied by policy")


def _worker(governor):
    from tests.test_substrate_worker_core import _FakeRedis, _FakeStore
    from market_service.substrate_worker import TapeWorker

    return TapeWorker(
        _FakeStore(_FakeRedis()), symbol="SOLUSDT", governor=governor,
    )


class CoreSeamTests(unittest.IsolatedAsyncioTestCase):
    """The worker is gated by duck-typed primitive calls — no governance import."""

    async def test_ungoverned_worker_is_byte_identical(self):
        worker = _worker(None)
        self.assertTrue(await worker._govern("fire"))
        self.assertTrue(await worker._govern("publish"))
        self.assertTrue(await worker._govern("pg_write"))

    async def test_allowed_governor_is_consulted_with_primitives(self):
        governor = _RecordingGovernor(allowed=True)
        worker = _worker(governor)
        self.assertTrue(await worker._govern("fire"))
        gate, kw = governor.calls[0]
        self.assertEqual(gate, "fire")
        self.assertEqual(kw["substrate"], "tape")
        self.assertEqual(kw["symbol"], "SOLUSDT")
        self.assertEqual(kw["inputs"], ("raw", "microstructure"))

    async def test_denied_governor_records_dormancy(self):
        governor = _RecordingGovernor(allowed=False)
        worker = _worker(governor)
        self.assertFalse(await worker._govern("publish", {"status": "healthy"}))
        self.assertIn("governance:test-denial", worker._last_dormant_reason or "")

    async def test_governor_error_fails_closed(self):
        governor = _RecordingGovernor(raises=True)
        worker = _worker(governor)
        self.assertFalse(await worker._govern("pg_write"))
        self.assertIn("governance", worker._last_error or "")

    async def test_missing_authorize_method_fails_closed(self):
        class _Partial:
            async def authorize_fire(self, **kw):
                return GovernanceVerdict.allow("allow", "ok")

        worker = _worker(_Partial())
        self.assertTrue(await worker._govern("fire"))
        self.assertFalse(await worker._govern("publish"))


class LedgerAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ledger_counts_failures_instead_of_hiding_them(self):
        """A silent audit is no audit: write failures are counted."""

        class _NoLedger:
            async def read_substrate_latest(self, s, sym):
                return None

        ledger = GovernanceLedger(_NoLedger())
        v = GovernanceVerdict.allow("allow", "fine")
        await ledger.record(
            plane="calculation", substrate="tape", symbol="SOLUSDT",
            gate="publish", verdict=v,
        )
        stats = ledger.stats()
        self.assertEqual(stats["decisions"], 1)
        self.assertEqual(stats["redis_failures"], 1)
        self.assertFalse(stats["durable"])
        self.assertIsNotNone(stats["last_error"])

    async def test_ledger_counts_allowed_and_denied(self):
        store, pg = _FakeStore(), _FakePg()
        ledger = GovernanceLedger(store, pg)
        await ledger.record(
            plane="calculation", substrate="tape", symbol="SOLUSDT",
            gate="publish", verdict=GovernanceVerdict.allow("allow", "ok"),
        )
        await ledger.record(
            plane="calculation", substrate="tape", symbol="SOLUSDT",
            gate="publish", verdict=GovernanceVerdict.deny("keyspace-trespass", "no"),
        )
        stats = ledger.stats()
        self.assertEqual((stats["allowed"], stats["denied"]), (1, 1))
        self.assertEqual(stats["pg_written"], 2)
        self.assertTrue(stats["durable"])

    async def test_latest_without_substrate_reads_the_stream_head(self):
        """`GET /governance?symbol=` must work with no substrate given
        (previously it read a literal `*` key that nothing writes)."""
        import json as _json

        from market_service.runtime.redis_store import RedisRuntimeStore

        store = RedisRuntimeStore("redis://localhost:1/0")

        class _R:
            async def xrevrange(self, key, count=1):
                return [("1-1", {
                    "payload": _json.dumps({"plane": "calculation", "allowed": True,
                                            "substrate": "tape"}),
                })]

        store.redis = _R()
        got = await store.read("governance.latest", ids={"plane": "calculation", "symbol": "SOLUSDT"})
        self.assertIsNotNone(got)
        self.assertTrue(got["allowed"])
        self.assertEqual(got["substrate"], "tape")


class LedgerNameTests(unittest.TestCase):
    """The two ledgers' substrate naming cannot drift (one derivation)."""

    def test_ledger_substrate_name_owner(self):
        from market_service.substrate_worker.contracts import ledger_substrate_name

        self.assertEqual(ledger_substrate_name("calculation", "Tape"), "tape")
        self.assertEqual(
            ledger_substrate_name("analysis", "Regime"), "analysis:regime")


class StatusVocabularyTests(unittest.TestCase):
    """CHARTER: consume owners by IMPORT, never by mirrored constants."""

    def test_status_vocabulary_is_the_owner(self):
        from market_service.substrate_worker.contracts import PAYLOAD_STATUSES

        self.assertEqual(plane_status_vocabulary(), tuple(PAYLOAD_STATUSES))
        self.assertNotIn("PAYLOAD_STATUSES", dir(
            __import__("market_service.governance.contracts", fromlist=["x"])))

    def test_manifests_carry_the_owned_status_family(self):
        from market_service.substrate_worker.contracts import PAYLOAD_STATUSES

        for manifest in (*build_calculation_manifest().values(),
                         *build_analysis_manifest().values()):
            self.assertEqual(manifest.status_family, frozenset(PAYLOAD_STATUSES))

    def test_self_check_rejects_a_borrowed_status_word(self):
        from market_service.governance.contracts import SubstrateManifest

        stray = {"x": SubstrateManifest(
            plane=GovernancePlane.CALCULATION, substrate="x",
            primary_stream="raw", writes_keyspace="substrate",
            status_family=frozenset({"healthy", "not_a_status"}),
        )}
        fsm = CalculationGovernanceFSM(
            stray, status_vocabulary=plane_status_vocabulary())
        with self.assertRaises(ValueError):
            fsm.self_check()


class FireCycleDisciplineTests(unittest.IsolatedAsyncioTestCase):
    """H1: a denial must not burn dedupe/cooldown/counter.
    H2: one ledger row per fire cycle."""

    async def test_denied_fire_does_not_burn_dedupe_cooldown_or_count(self):
        governor = _RecordingGovernor(allowed=False)
        worker = _worker(governor)
        before = worker.fired_count
        decision = TriggerDecision(fired=True, source="probe")
        await worker._fire(
            decision, None, 1_700_000_000_000, high_water="1-1")
        self.assertEqual(worker.fired_count, before)
        self.assertIsNone(worker._last_fire_ms)
        self.assertEqual(worker._tasks, set())
        self.assertIn("governance", worker._last_dormant_reason or "")

    async def test_denied_fire_never_touches_the_supervisor_key(self):
        """The dedupe Lua runs AFTER the gate, so a denial leaves no trace
        that could suppress the retry of the same condition."""
        governor = _RecordingGovernor(allowed=False)
        worker = _worker(governor)
        calls: list[str] = []

        async def _script(*a, **k):  # pragma: no cover - must not be reached
            calls.append("registered")

        worker._register_scripts = _script
        await worker._fire(
            TriggerDecision(fired=True, source="probe"), None,
            1_700_000_000_000, high_water="1-1")
        self.assertEqual(calls, [])

    async def test_one_ledger_row_per_fire_cycle(self):
        from tests.test_substrate_worker_core import _FakeRedis, _FakeStore
        from market_service.substrate_worker import TapeWorker

        store, pg = _FakeStore(_FakeRedis()), _FakePg()
        governor = build_calculation_governor(store, pg)
        worker = TapeWorker(store, symbol="SOLUSDT", governor=governor)
        payload = {
            "status": "healthy", "schema_version": 2, "missing_inputs": [],
            "trigger": {"source": "probe"}, "computed_at_ms": 2_000,
        }
        # A fire cycle that reaches PG_WRITE writes exactly ONE ledger row.
        self.assertTrue(await worker._govern("fire"))
        self.assertTrue(await worker._govern("publish", payload, record_allow=False))
        self.assertTrue(await worker._govern("pg_write", payload, record_allow=True))
        self.assertEqual(len(pg.rows), 1)
        self.assertTrue(pg.rows[0]["allowed"])
        self.assertEqual(pg.rows[0]["gate"], "pg_write")

    async def test_denial_records_exactly_one_row(self):
        from tests.test_substrate_worker_core import _FakeRedis, _FakeStore
        from market_service.substrate_worker import TapeWorker

        store, pg = _FakeStore(_FakeRedis()), _FakePg()
        governor = build_calculation_governor(store, pg)
        worker = TapeWorker(store, symbol="SOLUSDT", governor=governor)
        bad = {
            "status": "insufficient_data", "schema_version": 2,
            "missing_inputs": [], "trigger": {"source": "probe"},
            "computed_at_ms": 2_000,
        }
        self.assertFalse(await worker._govern("publish", bad, record_allow=False))
        self.assertEqual(len(pg.rows), 1)
        self.assertFalse(pg.rows[0]["allowed"])
        self.assertEqual(pg.rows[0]["rule"], "output-fabricated")


if __name__ == "__main__":
    unittest.main()
