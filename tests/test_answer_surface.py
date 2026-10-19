"""C2 answer surface — cycle-close emission + the ``answer`` re-render.

Pins ``docs/CLI_ANSWER_SURFACE_PLAN.md`` §4:

  - the cycle closes with THE ANSWER rendered by the one composer from the
    run record that ships (``cycle_meta["answer"]``);
  - ``nooa market inference run`` prints the answer block at close (the
    operator sees it when the run settles);
  - ``nooa market inference answer <symbol>`` re-renders a past cycle's
    answer from its run record — EXACT parity with the close render, by
    construction (same composer, same record read);
  - degraded cycles (no authored ship) render no answer — the renderer
    never authors one.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from market_service.commands.nooa_cli_ext import command as market_group
from market_service.nooa_harness.engine.core.answer import answer_from_record
from tests.test_engine import _collapse_turn, _engine, _staged_narration, _wake
from tests.test_output_authoring import _THIN_DERIVE, _walk_tail
from tests.test_track_characterization import _evidence_prefix


async def _authored_walk(derive_turn: str):
    """One full cycle that SHIPS an authored output (collapse + derive)."""
    engine, _s, _p, _m = _engine(llm_responses=[
        *_evidence_prefix(), *_walk_tail(),
        _collapse_turn(),          # valid collapse
        derive_turn,               # the authored output
    ])
    return await engine.narrate_cycle(
        _wake(), {"decision": "fire"}, task="answer surface walk")


def _authored_run() -> dict:
    artifact, meta = asyncio.run(_authored_walk(_staged_narration("P6", final=True)))
    return {"artifact": artifact.to_dict(), "meta": meta}


def _flagged_run() -> dict:
    artifact, meta = asyncio.run(_authored_walk(_THIN_DERIVE))
    return {"artifact": artifact.to_dict(), "meta": meta}


class CycleCloseEmissionTests(unittest.TestCase):
    def test_cycle_close_carries_the_answer(self):
        run = _authored_run()
        answer = run["meta"]["answer"]
        self.assertIsNotNone(answer)
        self.assertTrue(answer.startswith("ANSWER"))
        self.assertIn("OUTPUT CONFORMANCE", answer)
        # the agent's narrative ships verbatim
        self.assertIn(run["artifact"]["interpretation"]["summary"], answer)

    def test_close_answer_equals_re_render_of_the_shipped_record(self):
        run = _authored_run()
        self.assertEqual(run["meta"]["answer"],
                         answer_from_record(run["artifact"]))

    def test_flagged_ship_answers_with_missing_blocks(self):
        run = _flagged_run()
        answer = run["meta"]["answer"]
        self.assertIsNotNone(answer)
        self.assertIn("missing (flagged)", answer)
        self.assertIn("terminal: flagged", answer)
        self.assertEqual(answer, answer_from_record(run["artifact"]))

    def test_degraded_cycle_renders_no_answer(self):
        from tests.test_cycle_characterization import capture

        run = asyncio.run(capture("transport"))
        self.assertIsNone(run["meta"].get("answer"))
        self.assertIsNone(answer_from_record(run["artifact"]))


class CliCloseSurfaceTests(unittest.TestCase):
    def test_run_prints_the_answer_at_close(self):
        from market_service.nooa_harness.engine.core.answer import compose_answer
        from tests.test_answer_composer import (
            _authored, _conformance, _gate, _meta)

        answer = compose_answer(_authored(), _gate(), _conformance(), _meta())
        fake = {
            "status": "provisional", "symbol": "BTCUSDT", "venue": "spot",
            "answer": answer,
            "artifact_id": "00000000-0000-0000-0000-000000000001",
            "artifact": {}, "cycle": {"terminal": "settled"},
        }
        with patch("market_service.nooa_harness.inference_runner.run_inference_once",
                   new=AsyncMock(return_value=fake)):
            result = CliRunner().invoke(
                market_group, ["inference", "run", "BTCUSDT", "--force"])
        self.assertEqual(result.exit_code, 0, result.output)
        # the answer is the close surface: it leads stdout...
        self.assertTrue(result.output.startswith(answer))
        # ...and the machine metadata stays beside it
        self.assertIn('"artifact_id"', result.output)

    def test_run_without_authored_answer_prints_metadata_only(self):
        fake = {"status": "provisional", "symbol": "BTCUSDT", "venue": "spot",
                "answer": None, "artifact": {}, "cycle": {}}
        with patch("market_service.nooa_harness.inference_runner.run_inference_once",
                   new=AsyncMock(return_value=fake)):
            result = CliRunner().invoke(
                market_group, ["inference", "run", "BTCUSDT", "--force"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("ANSWER", result.output)


class AnswerSubcommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._record = _authored_run()["artifact"]

    def _invoke(self, args, record, source="postgres"):
        async def _load(symbol, venue, artifact_id=None, redis_only=False):
            return source, record

        with patch("market_service.commands.nooa_cli_ext._load_run_record",
                   new=_load):
            return CliRunner().invoke(market_group, args)

    def test_re_render_parity_with_cycle_close(self):
        close_answer = answer_from_record(self._record)
        self.assertIsNotNone(close_answer)
        result = self._invoke(["inference", "answer", "BTCUSDT"], self._record)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.rstrip("\n"), close_answer)

    def test_redis_fallback_record_renders_identically(self):
        result = self._invoke(["inference", "answer", "BTCUSDT"], self._record,
                              source="redis")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.rstrip("\n"),
                         answer_from_record(self._record))

    def test_exact_run_record_selection(self):
        seen = {}

        async def _load(symbol, venue, artifact_id=None, redis_only=False):
            seen["artifact_id"] = artifact_id
            return "postgres", self._record

        with patch("market_service.commands.nooa_cli_ext._load_run_record",
                   new=_load):
            result = CliRunner().invoke(
                market_group,
                ["inference", "answer", "BTCUSDT",
                 "--artifact-id", "00000000-0000-0000-0000-000000000001"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen["artifact_id"],
                         "00000000-0000-0000-0000-000000000001")

    def test_missing_run_record_is_an_honest_error(self):
        result = self._invoke(["inference", "answer", "ETHUSDT"], None)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no run record", result.output)

    def test_degraded_run_record_never_authors_an_answer(self):
        record = json.loads(json.dumps(self._record))
        record["interpretation"] = None
        result = self._invoke(["inference", "answer", "BTCUSDT"], record)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no authored answer", result.output)


class RunRecordRoundTripTests(unittest.TestCase):
    def test_stores_round_trip_the_answer_exactly(self):
        """The record the close render used == the record the stores hold."""
        run = _authored_run()
        persisted = json.loads(json.dumps(run["artifact"], default=str))
        self.assertEqual(run["meta"]["answer"], answer_from_record(persisted))


if __name__ == "__main__":
    unittest.main(verbosity=2)
