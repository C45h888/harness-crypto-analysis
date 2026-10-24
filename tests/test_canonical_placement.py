"""Canonical-output PLACEMENT — deliver + confirm, settle on delivery (C2).

Pins the canonical-path semantics of ``docs/CLI_ANSWER_SURFACE_PLAN.md`` §3
as implemented:

  - PLACEMENT = deliver the answer to its sink + place the canonical row;
    the frozen ``answer_text`` is the answer OF RECORD;
  - a canonical-write failure is RECORDED as ``run_record_errors`` and the
    cycle SETTLES (the run record is bookkeeping);
  - a delivery failure (composer render or sink) is terminal and honest:
    ``delivery_failed``, truthful ``final_validation``, the row still lands
    best-effort;
  - store-less deployments settle with the answer on the surface;
  - the engine's own readers (prior ledger recall, wake high-water) read the
    canonical rows.
"""

from __future__ import annotations

import unittest

from market_service.nooa_harness.engine.core.answer import answer_from_record
from market_service.nooa_harness.engine.failure_fsm import FAILURE_FSM, FailureKind
from market_service.nooa_harness.engine.loop_states import LoopTerminal
from tests.test_engine import _engine, _wake


class PlacementSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def _authored_engine(self):
        from tests.test_engine import _collapse_turn, _staged_narration as _sn
        from tests.test_engine import (
            _TRACK_ASSEMBLE, _TRACK_DISCIPLINE, _TRACK_FORECAST,
            _TRACK_HYPOTHESIZE, _TRACK_INTERPRET, _track_tools,
        )

        turns = [
            _sn("P1", tools=[{"name": "ns.read", "args": {"namespace": "micro.status", "symbol": "BTCUSDT", "venue": "spot"}},
                             {"name": "micro.fit_beta"},
                             {"name": "ns.read", "args": {"namespace": "derivatives.latest", "symbol": "BTCUSDT", "venue": "spot"}}]),
            _sn("P2", tools=[{"name": "calc.depth.average"},
                             {"name": "ns.read", "args": {"namespace": "derivatives.latest", "symbol": "BTCUSDT", "venue": "spot"}}]),
            _sn("P5", tools=[{"name": "calc.price.delta"}]),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_FORECAST),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _sn("P6", final=True),
            _track_tools(*_TRACK_DISCIPLINE),
            _sn("P6", final=True),
            _collapse_turn(),
            _sn("P6", final=True),
        ]
        return _engine(llm_responses=turns)

    async def test_delivery_places_the_canonical_row(self):
        engine, _store, postgres, _m = self._authored_engine()
        output, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="canonical placement walk")
        self.assertEqual(meta["terminal"], "settled")
        self.assertEqual(meta["delivery"], {"delivered": True, "error": None})
        self.assertEqual(meta["run_record_errors"], [])
        # ONE canonical row: the frozen answer of record + the run record
        self.assertEqual(len(postgres.inserted), 1)
        row = postgres.inserted[0]
        self.assertEqual(row.run_id, output.run_id)
        self.assertEqual(row.answer_text, meta["answer"])
        self.assertEqual(row.answer_text, answer_from_record(row.to_dict()))
        self.assertIsNotNone(row.answer)
        # insert lands the record (pending); the refresh closes the terminal
        self.assertEqual(row.terminal, "pending_settlement")
        self.assertEqual(postgres.updated[-1].terminal, "settled")
        self.assertIn("ledger", row.run_record["deterministic_state"])

    async def test_write_failure_settles_with_run_record_errors(self):
        engine, _store, postgres, _m = self._authored_engine()

        async def _boom(output):
            raise RuntimeError("durable ledger down")

        postgres.insert_canonical_output = _boom
        output, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="write-failure walk")
        # the run record is bookkeeping: failure is RECORDED, never blocking
        self.assertEqual(meta["terminal"], "settled")
        self.assertTrue(meta["delivery"]["delivered"])
        self.assertTrue(any("durable ledger down" in e["error"]
                            for e in meta["run_record_errors"]))
        self.assertEqual(meta["answer"], output.answer_text)

    async def test_delivery_failure_is_honest_and_terminal(self):
        engine, _store, postgres, _m = self._authored_engine()

        def _sink(answer_text):
            raise RuntimeError("surface unreachable")

        engine.answer_sink = _sink
        output, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="delivery-failure walk")
        self.assertEqual(meta["terminal"], "delivery_failed")
        self.assertFalse(meta["delivery"]["delivered"])
        self.assertIn("surface unreachable", meta["delivery"]["error"])
        # honest final_validation: the verdict reports the failed delivery
        self.assertFalse(meta["final_validation"]["delivered"])
        self.assertIn("delivery_error", meta["final_validation"])
        # the answer OF RECORD still lands (best-effort) for the audit
        self.assertEqual(len(postgres.inserted), 1)
        self.assertIsNotNone(postgres.inserted[0].answer_text)

    async def test_store_less_deployment_settles(self):
        engine, store, _postgres, _m = self._authored_engine()
        engine.postgres = None

        async def _boom(output):
            raise RuntimeError("no redis")

        store.publish_canonical_output = _boom
        _output, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="store-less walk")
        self.assertEqual(meta["terminal"], "settled")
        self.assertTrue(meta["delivery"]["delivered"])
        self.assertTrue(meta["run_record_errors"])
        self.assertIsNotNone(meta["answer"])

    async def test_frozen_text_is_the_answer_of_record(self):
        engine, store, postgres, _m = self._authored_engine()
        output, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, task="frozen parity walk")
        self.assertEqual(output.answer_text, meta["answer"])
        # the projection carries the same bytes the CLI surface returns
        self.assertEqual(store.published[-1].answer_text, meta["answer"])


class FailureVocabularyTests(unittest.TestCase):
    def test_delivery_failed_routes_to_its_own_terminal(self):
        verdict = FAILURE_FSM.classify(FailureKind.DELIVERY_FAILED.value)
        self.assertIs(verdict.terminal, LoopTerminal.DELIVERY_FAILED)
        self.assertTrue(verdict.terminates)

    def test_delivery_failed_is_distinct_from_infra(self):
        self.assertIsNot(LoopTerminal.DELIVERY_FAILED, LoopTerminal.INFRA_FAILED)
        self.assertIn(LoopTerminal.DELIVERY_FAILED,
                      set(LoopTerminal) - {LoopTerminal.SETTLED})


class ReaderMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prior_ledger_recall_reads_canonical_rows(self):
        from market_service.nooa_harness.engine.ledger import (
            LedgerKind, record_entry,
        )

        state: dict = {}
        record_entry(state, LedgerKind.OBSERVATION,
                     {"stage": "verdict-chosen", "chosen": "H0"})
        record = {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "status": "provisional",
            "run_record": {"deterministic_state": state},
        }
        engine, _store, postgres, _m = _engine(llm_responses=[])
        postgres.read_canonical_output = _async_returns(record)
        rows, block = await engine._recall_ledger()
        self.assertTrue(rows)
        self.assertIn("verdict-chosen", block)

    async def test_wake_high_water_reads_canonical_rows(self):
        record = {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "status": "provisional",
            "completed_at": "2026-01-01T00:00:00+00:00",
            "run_record": {"deterministic_state": {
                "coverage": {"events_total": 5000},
                "price_impact_fit": {"beta": "0.001", "status": "validated"},
            }},
        }
        engine, _store, postgres, _m = _engine(llm_responses=[])
        postgres.read_canonical_output = _async_returns(record)
        headline = await engine.collect_snapshot()
        self.assertEqual(headline["last_artifact_events_total"], 5000)
        self.assertEqual(headline["last_artifact_capture_state"], None)


def _async_returns(value):
    async def _call(*args, **kwargs):
        return value

    return _call


if __name__ == "__main__":
    unittest.main(verbosity=2)
