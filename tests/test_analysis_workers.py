"""Analysis worker plane tests — trigger semantics, keyspace, purity.

Pass 1 regression net for docs/ANALYSIS_WORKER_SPEC.md:
* deterministic trigger doctrine — probe-against-own-state (a substrate
  re-fire with no semantic change does NOT fire the analysis worker);
* plane seam — analysis workers bind the dependency's state stream and
  route own-state reads/publishes into the analysis: keyspace;
* worker purity — one analysis module per worker file, no Binance, no
  substrate_worker imports beyond the shared core.
"""

from __future__ import annotations

import asyncio
import ast
import importlib
import json
import time
import unittest
from pathlib import Path

from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY
from market_service.analysis_worker.regime_worker import RegimeWorker, compose_regime_input
from market_service.analysis_worker.wall_migration_worker import (
    MIN_WALL_QTY,
    evaluate_wall_conditions,
)
from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.substrate_worker.contracts import SubstrateStatePayload, TriggerDecision
from tests.test_substrate_worker_core import _FakeRedis


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


class _AnalysisFakeRedis(_FakeRedis):
    """Adds the analysis keyspace + derivative-cache surface to the shared fake."""

    def __init__(self, *, deps=None, derivative=None, analysis_latest=None):
        super().__init__(rows=None, latest=None)
        self.deps = deps or {}
        self.derivative = derivative
        self.analysis_latest = analysis_latest
        self.published: list[dict] = []

    async def eval(self, script, numkeys, *keys_and_args):
        # publish_analysis_state: SET latest + XADD stream
        body = keys_and_args[2]
        payload = body if isinstance(body, dict) else __import__("json").loads(
            body.decode() if isinstance(body, bytes) else body)
        self.published.append(payload)
        self.analysis_latest = payload
        self.stream.append(payload)
        return "1-1"


class _AnalysisFakeStore:
    """RedisRuntimeStore-shaped adapter with BOTH planes' keyspaces."""

    def __init__(self, redis):
        self.redis = redis

    # substrate plane (dependencies live here — always)
    def raw_stream(self, symbol):
        return f"mkt:stream:raw:{symbol}"

    def substrate_stream(self, substrate, symbol):
        return f"mkt:stream:substrate:{substrate}:{symbol}"

    def substrate_supervisor_key(self, substrate, symbol):
        return f"mkt:substrate:{substrate}:{symbol}:supervisor"

    async def read_substrate_latest(self, substrate, symbol):
        return self.redis.deps.get(substrate)

    async def read_derivative_evidence(self, symbol):
        return self.redis.derivative

    # analysis plane
    def analysis_stream(self, analysis, symbol):
        return f"mkt:stream:analysis:{analysis}:{symbol}"

    def analysis_latest_key(self, analysis, symbol):
        return f"mkt:latest:analysis:{analysis}:{symbol}"

    def analysis_supervisor_key(self, analysis, symbol):
        return f"mkt:analysis:{analysis}:{symbol}:supervisor"

    async def read_analysis_latest(self, analysis, symbol):
        return self.redis.analysis_latest

    async def publish_analysis_state(self, analysis, symbol, payload):
        self.redis.published.append(payload)
        self.redis.analysis_latest = payload
        return "1-1"


def _regime_deps(*, last_price=116.9, vwap=116.8, cvd=0.5, obi=0.3,
                 buy_sell=1.2, oi=8_200_000.0):
    """Dependency payloads composing an unambiguous TREND-UP regime."""
    return {
        "tape": {
            "substrate": "tape", "symbol": "SOLUSDT", "status": "healthy",
            "computed_at_ms": _now_ms() - 1_000,
            "output": {"futures_flow": {
                "last_price": last_price, "vwap": vwap, "cvd": cvd,
                "obi": obi, "buy_sell_ratio": buy_sell, "large_trades": [],
            }},
        },
        "oi": {
            "substrate": "oi", "symbol": "SOLUSDT", "status": "healthy",
            "computed_at_ms": _now_ms() - 1_000,
            "output": {"raw_open_interest": oi},
        },
    }


def _regime_derivative(funding_rate="0.0001"):
    return {
        "observed_at_ms": _now_ms() - 1_000,
        "futures": {
            "funding": {"lastFundingRate": funding_rate},
            "top_ls": [{"longAccount": 0.4, "longShortRatio": 0.66}],
            "global_ls": [{"longAccount": 0.45, "longShortRatio": 0.8}],
        },
    }


def _rows(n=1, start_ms=1_700_000_000_000):
    return [{"id": f"{start_ms + i}-0", "fields": {}} for i in range(n)]


def _prior_regime_state(verdict: str) -> dict:
    """A schema-versioned prior projection (real payload shape)."""
    from market_service.substrate_worker.contracts import TriggerDecision

    return SubstrateStatePayload.create(
        substrate="regime", symbol="SOLUSDT",
        output={"verdict": verdict},
        trigger=TriggerDecision(fired=True, source="probe"),
        computed_at_ms=_now_ms() - 2_000,
    ).to_dict()


class RegimeWorkerTests(unittest.TestCase):
    """Deterministic trigger semantics for the analysis plane's wake/fire split."""

    def test_cold_start_fires_on_first_trigger_arrival(self):
        store = _AnalysisFakeStore(_AnalysisFakeRedis(
            deps=_regime_deps(), derivative=_regime_derivative()))
        w = RegimeWorker(store, symbol="SOLUSDT")
        now = _now_ms()
        _run(w._handle_rows(_rows(), now))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.published[0]["trigger"]["source"], "cold_start")
        self.assertEqual(w.store.redis.published[0]["output"]["verdict"],
                         "TREND-UP (long build confirmed)")

    def test_verdict_flip_fires(self):
        prior = _prior_regime_state("CHOP / RANGE")
        store = _AnalysisFakeStore(_AnalysisFakeRedis(
            deps=_regime_deps(), derivative=_regime_derivative(),
            analysis_latest=prior))
        w = RegimeWorker(store, symbol="SOLUSDT")
        now = _now_ms()
        _run(w._handle_rows(_rows(), now))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 1)
        pub = w.store.redis.published[0]
        self.assertEqual(pub["trigger"]["source"], "probe")
        self.assertEqual(pub["trigger"]["predicates"]["regime_flip"]["from"],
                         "CHOP / RANGE")

    def test_same_verdict_does_not_fire(self):
        """Probe-against-own-state: substrate re-fire, same composed verdict → no fire."""
        store = _AnalysisFakeStore(_AnalysisFakeRedis(
            deps=_regime_deps(), derivative=_regime_derivative(),
            analysis_latest=_prior_regime_state("TREND-UP (long build confirmed)")))
        w = RegimeWorker(store, symbol="SOLUSDT")
        now = _now_ms()
        _run(w._handle_rows(_rows(), now))
        _run(asyncio.sleep(0.05))
        self.assertEqual(w.fired_count, 0)

    def test_trigger_stream_is_the_dependency_state_stream(self):
        store = _AnalysisFakeStore(_AnalysisFakeRedis())
        w = RegimeWorker(store, symbol="SOLUSDT")
        self.assertEqual(w._stream, store.substrate_stream("tape", "SOLUSDT"))

    def test_compose_null_discipline(self):
        self.assertIsNone(compose_regime_input({"substrate_dependencies": {}}))


class WallConditionTests(unittest.TestCase):
    """The wall-event vocabulary: formed / absorbed / migrated — pure + deterministic."""

    def _windows(self, *pairs):
        return [{"price": p, "qty": q, "window_qty": q, "notional": p * q}
                for p, q in pairs]

    def test_wall_formed_when_new_defended_level_appears(self):
        result = evaluate_wall_conditions(
            self._windows((116.5, 25_000.0)), {"keystone": 116.5}, {})
        formed = [e for e in result["events"] if e["event"] == "wall_formed"]
        self.assertEqual(len(formed), 1)
        self.assertEqual(formed[0]["level"], 116.5)

    def test_wall_absorbed_when_defended_level_collapses(self):
        prev = {"walls": {116.5: 25_000.0}, "keystone": {"keystone": 116.5}}
        result = evaluate_wall_conditions(
            self._windows((116.5, 8_000.0)), {"keystone": 116.5}, prev)
        absorbed = [e for e in result["events"] if e["event"] == "wall_absorbed"]
        self.assertEqual(len(absorbed), 1)
        self.assertAlmostEqual(absorbed[0]["prior"], 25_000.0)

    def test_keystone_migrated_event_beyond_width(self):
        prev = {"walls": {}, "keystone": {"keystone": 116.0}}
        result = evaluate_wall_conditions(
            self._windows((116.5, 25_000.0)), {"keystone": 116.25}, prev)
        migrated = [e for e in result["events"] if e["event"] == "keystone_migrated"]
        self.assertEqual(len(migrated), 1)

    def test_identical_state_fires_nothing(self):
        prev = {"walls": {116.5: 25_000.0}, "keystone": {"keystone": 116.5}}
        result = evaluate_wall_conditions(
            self._windows((116.5, 26_000.0)), {"keystone": 116.5}, prev)
        self.assertEqual(result["events"], [])

    def test_legacy_bare_float_keystone_prior_accepted(self):
        """Schema migration: projections that persisted keystone as a bare
        float (pre-fix shape) must be read as the prior level, not crash."""
        prev = {"walls": {116.5: 25_000.0}, "keystone": 116.5}
        result = evaluate_wall_conditions(
            self._windows((116.5, 25_000.0)), {"keystone": 116.5}, prev)
        self.assertEqual(result["events"], [])
        # And a travel beyond width still fires the migrated event.
        result = evaluate_wall_conditions(
            self._windows((116.5, 25_000.0)), {"keystone": 116.25}, prev)
        migrated = [e for e in result["events"] if e["event"] == "keystone_migrated"]
        self.assertEqual(len(migrated), 1)

    def test_persisted_keystone_shape_is_structured(self):
        prev = {"walls": {}, "keystone": {"keystone": 116.0}}
        result = evaluate_wall_conditions(
            self._windows((116.5, 25_000.0)), {"keystone": 116.25}, prev)
        self.assertEqual(result["keystone"], {"keystone": 116.25})

    def test_sub_threshold_window_is_not_a_wall(self):
        result = evaluate_wall_conditions(
            self._windows((116.5, MIN_WALL_QTY / 2)), {"keystone": 116.5}, {})
        self.assertEqual(result["events"], [])
        self.assertEqual(result["walls"], {})


class AnalysisKeyspaceTests(unittest.TestCase):
    """The analysis keyspace is separate from the substrate keyspace."""

    def test_key_shapes(self):
        store = RedisRuntimeStore("redis://localhost:6379/0", "marketflow", 10_000)
        self.assertEqual(store.analysis_stream("Regime", "solusdt"),
                         "marketflow:stream:analysis:regime:SOLUSDT")
        self.assertEqual(store.analysis_latest_key("regime", "SOLUSDT"),
                         "marketflow:latest:analysis:regime:SOLUSDT")
        self.assertEqual(store.analysis_supervisor_key("regime", "SOLUSDT"),
                         "marketflow:analysis:regime:SOLUSDT:supervisor")


class AnalysisWorkerPurityTests(unittest.TestCase):
    """AST-level purity pins (docs/ANALYSIS_WORKER_SPEC.md §9)."""

    WORKER_DIR = (Path(__file__).resolve().parent.parent
                  / "market_service" / "analysis_worker")

    def _worker_modules(self):
        return [
            p for p in sorted(self.WORKER_DIR.glob("*_worker.py"))
            if p.name != "__init__.py"
        ]

    def test_worker_imports_exactly_one_analysis_module(self):
        for path in self.WORKER_DIR.glob("*_worker.py"):
            tree = ast.parse(path.read_text())
            analysis_modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("market_service.analysis."):
                        analysis_modules.add(node.module)
            self.assertEqual(
                len(analysis_modules), 1,
                f"{path.name} must import exactly ONE analysis module, saw {analysis_modules}",
            )

    def test_worker_never_imports_binance_or_worker_plane_files(self):
        for path in self.WORKER_DIR.glob("*_worker.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                mod = node.module if isinstance(node, ast.ImportFrom) else None
                if mod is None and isinstance(node, ast.Import):
                    mod = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(
                        node.module.startswith("market_service.clients"),
                        f"{path.name} imports a transport client: {node.module}")
                    self.assertFalse(
                        "substrate_worker" in node.module
                        and not node.module.endswith(".core")
                        and "contracts" not in node.module,
                        f"{path.name} imports non-core substrate_worker: {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.startswith("market_service.clients"),
                            f"{path.name} imports {alias.name}")

    def test_registry_names_match_worker_names(self):
        for name, cls in ANALYSIS_WORKER_REGISTRY.items():
            self.assertEqual(cls.SUBSTRATE_NAME, name)
            self.assertEqual(cls.PLANE, "analysis")


if __name__ == "__main__":
    unittest.main()