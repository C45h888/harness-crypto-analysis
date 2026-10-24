"""Plane A (identity), B (data) and E (memory) contracts.

These pin the three seams the runtime audit found half-built:

A — the venue half of identity is refused before a cycle is constructed.
B — durable reads are keyed by ``(symbol, venue)`` like the Redis projections.
E — memory identity (session + segment) is derivable by an operator, prior
    gate outcomes reach the agent as a non-citable block, and the agent's
    memory seam always carries the task segment.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import types
import unittest
import uuid

from click.testing import CliRunner

from market_service.nooa_harness import inference_runner
from market_service.nooa_harness.engine import ledger as ledger_mod
from market_service.nooa_harness.engine.core import context as context_mod
from market_service.nooa_harness.engine.core.context import _EngineTransport
from market_service.nooa_harness.engine.core.driver import InferenceEngine
from market_service.nooa_harness.memory import session_id_for, task_segment_id
from market_service.runtime.postgres_store import PostgresRuntimeStore


# ---------------------------------------------------------------------------
# Plane A — venue is identity, and identity is refused before any work
# ---------------------------------------------------------------------------

class _EngineBuilt(Exception):
    """Sentinel: reaching engine construction means the gate did not refuse."""


class VenueRefusalTests(unittest.TestCase):
    def setUp(self):
        import os
        self._env = {k: os.environ.get(k) for k in
                     ("MICROSTRUCTURE_VENUE", "SYMBOLS", "DATABASE_URL")}
        os.environ["MICROSTRUCTURE_VENUE"] = "futures"
        os.environ["SYMBOLS"] = "SOLUSDT"
        os.environ["DATABASE_URL"] = "postgresql://localhost:5432/test"

    def tearDown(self):
        import os
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_venue_mismatch_refuses_before_engine_construction(self):
        built: list[dict] = []

        async def _build(*_args, **kwargs):
            built.append(kwargs)
            raise _EngineBuilt

        original = inference_runner._build_engine
        inference_runner._build_engine = _build
        try:
            out = asyncio.run(inference_runner.run_inference_once(
                "SOLUSDT", venue="spot", force=True))
        finally:
            inference_runner._build_engine = original

        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "venue_not_captured")
        self.assertEqual(out["venue"], "spot")
        self.assertEqual(out["configured_venue"], "futures")
        self.assertEqual(out["captured_venues"], ["futures"])
        self.assertEqual(built, [], "engine was built for an uncaptured venue")

    def test_canonical_venue_reaches_engine_construction(self):
        built: list[dict] = []

        async def _build(*_args, **kwargs):
            built.append(kwargs)
            raise _EngineBuilt

        original = inference_runner._build_engine
        inference_runner._build_engine = _build
        try:
            with self.assertRaises(_EngineBuilt):
                asyncio.run(inference_runner.run_inference_once(
                    "SOLUSDT", venue="perps", force=True))
        finally:
            inference_runner._build_engine = original

        self.assertEqual(built[0]["venue"], "futures",
                         "perps must normalise to the captured venue")


# ---------------------------------------------------------------------------
# Plane B — durable reads are venue-scoped, like every live projection
# ---------------------------------------------------------------------------

class _FakePool:
    def __init__(self, row=None, rows=None):
        self.calls: list[tuple[str, tuple]] = []
        self._row = row
        self._rows = rows or []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return self._row

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self._rows


def _store(pool) -> PostgresRuntimeStore:
    store = PostgresRuntimeStore("postgresql://unused/0")
    store.pool = pool
    return store


class CanonicalReadVenueTests(unittest.TestCase):
    def test_latest_by_symbol_filters_venue(self):
        pool = _FakePool(row={"run_id": "00000000-0000-0000-0000-000000000001"})
        store = _store(pool)
        asyncio.run(store.read_canonical_output("solusdt", venue="futures"))
        sql, params = pool.calls[0]
        self.assertIn("symbol = $1 AND venue = $2", sql)
        self.assertEqual(params, ("SOLUSDT", "futures"))

    def test_latest_by_symbol_without_venue_stays_symbol_scoped(self):
        pool = _FakePool(row={"run_id": "00000000-0000-0000-0000-000000000001"})
        store = _store(pool)
        asyncio.run(store.read_canonical_output("SOLUSDT"))
        sql, params = pool.calls[0]
        self.assertNotIn("venue =", sql)
        self.assertEqual(params, ("SOLUSDT",))

    def test_exact_run_id_is_global_by_contract(self):
        pool = _FakePool(row={"run_id": "00000000-0000-0000-0000-000000000001"})
        store = _store(pool)
        run_id = "00000000-0000-0000-0000-000000000001"
        asyncio.run(store.read_canonical_output(run_id=run_id, venue="futures"))
        sql, params = pool.calls[0]
        self.assertIn("WHERE run_id = $1", sql)
        self.assertNotIn("venue =", sql)
        self.assertEqual(params, (uuid.UUID(run_id),))

    def test_history_filters_venue(self):
        pool = _FakePool(rows=[])
        store = _store(pool)
        asyncio.run(store.read_canonical_history("solusdt", 5, venue="futures"))
        sql, params = pool.calls[0]
        self.assertIn("symbol = $1 AND venue = $2", sql)
        self.assertEqual(params, ("SOLUSDT", "futures", 5))


class _FakePgPrior:
    def __init__(self, record=None):
        self.record = record
        self.calls: list[dict] = []

    async def read_canonical_output(self, symbol=None, run_id=None, venue=None):
        self.calls.append({"symbol": symbol, "run_id": run_id, "venue": venue})
        return self.record


class _FakeStorePrior:
    def __init__(self, record=None):
        self.record = record
        self.calls: list[dict] = []

    async def read_latest_canonical_output(self, symbol, venue):
        self.calls.append({"symbol": symbol, "venue": venue})
        return self.record

    async def read(self, namespace, *, ids=None, count=None, newest_first=True, start="-", end="+"):
        ids = ids or {}
        self.calls.append({"symbol": ids.get("symbol"), "venue": ids.get("venue")})
        return self.record


def _prior_engine(*, venue="spot", record=None):
    """Driver methods bound to a tiny stand-in (no engine construction)."""
    pg = _FakePgPrior(record)
    store = _FakeStorePrior(None)
    holder = types.SimpleNamespace(
        postgres=pg, store=store, symbol="SOLUSDT", venue=venue,
    )
    holder._prior_state = InferenceEngine._prior_state
    holder._prior_canonical_record = (
        InferenceEngine._prior_canonical_record.__get__(holder))
    return holder, pg, store


class PriorRecordVenueTests(unittest.TestCase):
    def test_engine_prior_read_uses_the_cycle_venue(self):
        holder, pg, _store_ = _prior_engine(venue="spot")
        asyncio.run(holder._prior_canonical_record())
        self.assertEqual(pg.calls[0]["venue"], "spot",
                         "a spot cycle must not recall a futures run")

    def test_redis_fallback_is_venue_scoped(self):
        holder, _pg, store = _prior_engine(venue="futures")
        asyncio.run(holder._prior_canonical_record())
        self.assertEqual(store.calls[0]["venue"], "futures")


class DomainPlaneRetiredTests(unittest.TestCase):
    """N6 residue: the domain-state plane's contract and knob are gone."""

    def test_contract_is_removed(self):
        from market_service.runtime import contracts

        self.assertFalse(hasattr(contracts, "MarketStateEnvelope"))
        self.assertFalse(hasattr(contracts, "StateStatus"))
        self.assertNotIn("market_state", contracts.SCHEMA_VERSION_REGISTRY)

    def test_settings_knob_is_removed(self):
        from market_service.config import Settings

        self.assertNotIn("max_domain_state_age_seconds", Settings.__dataclass_fields__)


# ---------------------------------------------------------------------------
# Plane E — memory identity is derivable
# ---------------------------------------------------------------------------

class SegmentDerivationTests(unittest.TestCase):
    SCENARIO = {"target_price": "100", "horizon": "1h"}

    def test_reworded_task_and_other_target_share_a_segment(self):
        first = task_segment_id("SOLUSDT", "futures",
                                "Analyse SOL OFI depth.", self.SCENARIO)
        second = task_segment_id("solusdt", "futures",
                                 "analyse sol ofi depth", 
                                 {"target_price": "101", "horizon": "1h"})
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_identity_and_shape_partition_segments(self):
        base = task_segment_id("SOLUSDT", "futures", "OFI depth", self.SCENARIO)
        self.assertNotEqual(
            base, task_segment_id("BTCUSDT", "futures", "OFI depth", self.SCENARIO))
        self.assertNotEqual(
            base, task_segment_id("SOLUSDT", "spot", "OFI depth", self.SCENARIO))
        self.assertNotEqual(
            base, task_segment_id("SOLUSDT", "futures", "OFI depth",
                                  {"target_price": "100", "horizon": "4h"}))
        self.assertNotEqual(
            base, task_segment_id("SOLUSDT", "futures", "wall absorption", None))

    def test_segment_shape_is_readable(self):
        seg = task_segment_id("SOLUSDT", "futures", "OFI depth", self.SCENARIO)
        self.assertTrue(seg.startswith("task:SOLUSDT/futures:scenario:1h:"))

    def test_no_identity_means_no_segment(self):
        self.assertIsNone(task_segment_id(None, None, "OFI", None))
        self.assertIsNone(task_segment_id("SOLUSDT", None, "OFI", None))

    def test_session_id_is_derivable_and_matches_the_engine(self):
        from market_service.nooa_harness.engine.core.context import (
            _stable_session_id,
        )

        expected = str(uuid.uuid5(
            uuid.NAMESPACE_URL, "inference-engine://solusdt/futures"))
        self.assertEqual(session_id_for("SOLUSDT", "futures"), expected)
        self.assertEqual(_stable_session_id("SOLUSDT", "futures"), expected)
        self.assertNotEqual(session_id_for("SOLUSDT", "spot"), expected)


class PriorOutcomeTests(unittest.TestCase):
    def _record(self):
        state: dict = {}
        ledger_mod.record_entry(
            state, ledger_mod.LedgerKind.VERDICT,
            {"stage": "gate", "gate": "data", "status": "refused",
             "reasons": ["observations 4 < minimum 30"]},
            segment="task:SOLUSDT/futures:scenario:1h:aaaa1111",
        )
        ledger_mod.record_entry(
            state, ledger_mod.LedgerKind.VERDICT,
            {"stage": "gate", "gate": "plan", "status": "passed",
             "reasons": []},
            segment="task:SOLUSDT/futures:scenario:4h:bbbb2222",
        )
        return {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "terminal": "validation_failed",
            "run_record": {"deterministic_state": state},
        }

    def test_renders_only_the_requested_segment_and_is_non_citable(self):
        holder, _pg, _store_ = _prior_engine(record=self._record())
        block = asyncio.run(InferenceEngine._prior_outcomes.__get__(holder)(
            segment="task:SOLUSDT/futures:scenario:1h:aaaa1111"))

        self.assertIn("NOT evidence", block)
        self.assertIn("gate=data status=refused", block)
        self.assertIn("observations 4 < minimum 30", block)
        self.assertNotIn("gate=plan", block,
                         "another segment's verdict leaked into this one")
        self.assertIn("prior cycle terminal: validation_failed", block)

    def test_empty_without_a_prior_record(self):
        holder, _pg, _store_ = _prior_engine(record=None)
        block = asyncio.run(InferenceEngine._prior_outcomes.__get__(holder)(
            segment="task:SOLUSDT/futures:scenario:1h:aaaa1111"))
        self.assertEqual(block, "")

    def test_prior_outcomes_never_render_inside_the_ledger_block(self):
        """Prompt isolation stays intact: VERDICT rows do not become context."""
        prior_state = self._record()["run_record"]["deterministic_state"]
        rendered = ledger_mod.render_ledger(prior_state)
        self.assertNotIn("gate=data", rendered)


class PriorOutcomeWiringTests(unittest.TestCase):
    """The block is generated by the driver, bound at wake, rendered first."""

    def test_wake_binds_prior_outcomes_from_the_driver(self):
        import re

        src = re.sub(r"\s+", " ", (
            pathlib.Path("market_service/nooa_harness/engine/core/wake.py")
            .read_text()))
        self.assertIn(
            "st.prior_outcomes = await engine._prior_outcomes("
            "segment=ctx.task_id)", src)

    def test_narrate1_renders_outcomes_before_the_ledger_block(self):
        import re

        src = re.sub(r"\s+", " ", (
            pathlib.Path("market_service/nooa_harness/engine/kb.py")
            .read_text()))
        outcomes_at = src.index('context_block += f"{prior_outcomes}')
        ledger_at = src.index("RECALLED ENGINE LEDGER")
        self.assertLess(outcomes_at, ledger_at,
                        "prior outcomes must precede the evidence-adjacent "
                        "ledger block")


class TransportSegmentTests(unittest.TestCase):
    """The second dispatch seam carries the task segment (N9a)."""

    def test_execute_tool_passes_the_task_segment(self):
        captured: dict = {}

        async def _fake(store, name, args, **kwargs):
            captured.update(kwargs)
            return None, {}

        engine = types.SimpleNamespace(
            store=None, postgres=None, memory=None, settings=None,
            session_id="session-1", venue="futures",
        )
        ctx = types.SimpleNamespace(task_id="task:SOLUSDT/futures:general:-:x")
        transport = _EngineTransport(engine, ctx)

        original = context_mod.execute_tool
        context_mod.execute_tool = _fake
        try:
            asyncio.run(transport.execute_tool("memory.recall", {}))
        finally:
            context_mod.execute_tool = original

        self.assertEqual(captured["segment"], "task:SOLUSDT/futures:general:-:x")
        self.assertEqual(captured["venue"], "futures")


class OutputLoopWindowTests(unittest.TestCase):
    def test_output_window_names_the_memory_allowance(self):
        from market_service.nooa_harness.engine.kb import TOOL_WINDOWS

        window = TOOL_WINDOWS["output"]
        self.assertIn("memory.recall", window)
        self.assertIn("memory.remember", window)
        self.assertIn("dropped", window)
        self.assertNotIn("No tools this pass", window)


# ---------------------------------------------------------------------------
# Plane E — the operator can reproduce the agent's memory view
# ---------------------------------------------------------------------------

class MemoryCliTests(unittest.TestCase):
    def _invoke(self, args):
        from market_service.commands.nooa_cli_ext import memory_group

        return CliRunner().invoke(memory_group, args)

    def test_session_command_derives_the_agent_identity(self):
        args = ["session", "--symbol", "SOLUSDT", "--venue", "futures",
                "--task", "Analyse SOL OFI depth", "--target", "100",
                "--horizon", "1h"]
        result = self._invoke(args)
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["session_id"],
                         session_id_for("SOLUSDT", "futures"))
        self.assertEqual(
            payload["segment"],
            task_segment_id("SOLUSDT", "futures", "Analyse SOL OFI depth",
                            {"target_price": "100", "horizon": "1h"}),
        )

    def test_recall_exposes_segment_and_identity_defaults(self):
        from market_service.commands.nooa_cli_ext import memory_group

        names = {p.name for p in memory_group.commands["recall"].params}
        self.assertTrue({"session_id", "symbol", "venue", "segment"}
                        <= names)

    def test_writable_kinds_are_the_agent_kinds_plus_seed_fact(self):
        from market_service.commands.nooa_cli_ext import memory_group

        kind = next(p for p in memory_group.commands["remember"].params
                    if p.name == "kind")
        self.assertEqual(tuple(kind.type.choices),
                         ("observation", "hypothesis", "note", "fact"))

    def test_split_home_memory_cli_is_gone(self):
        import importlib.util

        self.assertIsNone(
            importlib.util.find_spec("market_service.commands.memory"),
            "a second memory CLI would drift from the canonical one",
        )


if __name__ == "__main__":
    unittest.main()
