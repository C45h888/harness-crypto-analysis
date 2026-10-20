"""Canonical output plane — contract + storage layer (slice C1).

Pins the canonical output doctrine:

  - ``interpretation.hypothesis_probabilities`` is a REGISTERED field: its
    shape validates on write (strict, structured errors), reads are lenient
    (legacy rows without it parse fine);
  - ``CanonicalOutput`` is THE output contract: the frozen answer of record +
    its run record, with the artifact's null-discipline rules preserved;
  - ``run_record_of`` resolves either record shape to one view;
  - the storage namespace round-trips the contract (fake-pool level).
"""

from __future__ import annotations

import unittest

from market_service.runtime.contracts import (
    CANONICAL_OUTPUT_SCHEMA_VERSION,
    SCHEMA_VERSION_REGISTRY,
    CanonicalOutput,
    InferenceArtifact,
    normalize_adjudication,
    run_record_of,
    validate_adjudication,
)


def _adjudication(**over):
    base = {
        "H0": {"probability": "medium", "p": None,
               "basis_path": "calc.forward.fit → data.status"},
        "H1": {"probability": "low", "p": None,
               "basis_path": "calc.forward.fit → data.status"},
        "chosen": "H0", "tiebreak": None,
    }
    base.update(over)
    return base


def _interpretation(**over):
    base = {
        "summary": "x" * 120,
        "evidence": [{"path": "calc.ofi.intervals → ofi", "value": "12.5",
                      "interpretation": "sustained flow"}],
        "confidence": "medium",
        "hypothesis_probabilities": _adjudication(),
    }
    base.update(over)
    return base


def _output(**over):
    base = dict(
        symbol="BTCUSDT", venue="spot",
        generated_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:05+00:00",
        status="provisional", window_minutes=30, interval_seconds=10,
        input_hash="fixture", model_version="inference-engine-v1",
        answer_text="ANSWER\n--- summary ---\n" + "x" * 120,
        answer={"answer": {"summary": "x" * 120}},
        interpretation=_interpretation(),
        hypothesis={"H0": "h", "H1": "n"},
        hypothesis_verdict="inconclusive",
        conformance={"tier": "authored"},
        run_record={"deterministic_state": {"gate": {"status": "provisional"}},
                    "capability_log": [], "errors": []},
        task="Explain the evidence",
        terminal="settled",
    )
    base.update(over)
    return CanonicalOutput.create(**base)


class AdjudicationRegistrationTests(unittest.TestCase):
    def test_valid_adjudication_passes(self):
        validate_adjudication(_adjudication())
        _output()  # registered field accepted end-to-end

    def test_malformed_adjudication_fails_the_contract(self):
        for bad in (
            {"H0": {}, "H1": {}},                        # no chosen
            _adjudication(chosen="H2"),                  # bad choice
            _adjudication(H0="not-a-object"),            # bad side
            _adjudication(H0={"probability": "certain"}),  # bad band
            _adjudication(H0={"probability": "high", "basis_path": 7}),  # bad basis
        ):
            with self.assertRaises(ValueError):
                validate_adjudication(bad)
            with self.assertRaises(ValueError):
                _output(interpretation=_interpretation(
                    hypothesis_probabilities=bad))

    def test_normalize_is_lenient_on_read(self):
        self.assertIsNone(normalize_adjudication(None))
        self.assertIsNone(normalize_adjudication("garbage"))
        partial = normalize_adjudication({"chosen": "h0", "H0": {}})
        self.assertEqual(partial["chosen"], "H0")
        self.assertEqual(partial["H0"]["probability"], None)
        self.assertEqual(partial["H1"]["basis_path"], None)

    def test_legacy_interpretation_without_adjudication_still_validates(self):
        legacy = _interpretation()
        legacy.pop("hypothesis_probabilities")
        artifact = InferenceArtifact.create(
            symbol="BTCUSDT", venue="spot",
            generated_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:00+00:00",
            status="provisional", window_minutes=30, interval_seconds=10,
            deterministic_state={}, capability_log=[],
            input_hash="x", model_version="y",
            interpretation=legacy,
        )
        self.assertNotIn("hypothesis_probabilities", artifact.interpretation)


class CanonicalOutputContractTests(unittest.TestCase):
    def test_registry_identity(self):
        self.assertEqual(SCHEMA_VERSION_REGISTRY["canonical_output"], 1)
        self.assertEqual(CANONICAL_OUTPUT_SCHEMA_VERSION,
                         SCHEMA_VERSION_REGISTRY["canonical_output"])

    def test_round_trip(self):
        output = _output()
        again = CanonicalOutput.from_mapping(output.to_dict())
        self.assertEqual(again, output)
        self.assertEqual(output.answer_text[:6], "ANSWER")
        self.assertEqual(output.interpretation["hypothesis_probabilities"],
                         _adjudication())

    def test_insufficient_carries_null_answer_and_interpretation(self):
        with self.assertRaises(ValueError):
            _output(status="insufficient")
        ok = _output(status="insufficient", answer_text=None, answer=None,
                     interpretation=None)
        self.assertIsNone(ok.answer_text)
        self.assertIsNone(ok.interpretation)

    def test_answer_shapes_are_enforced(self):
        with self.assertRaises(ValueError):
            _output(answer_text=123)
        with self.assertRaises(ValueError):
            _output(answer=["not-an-object"])

    def test_combined_prediction_never_stored(self):
        with self.assertRaises(ValueError):
            _output(calculations={"combined_prediction": {"x": 1}})

    def test_bad_status_and_identity_rejected(self):
        with self.assertRaises(ValueError):
            _output(status="maybe")
        with self.assertRaises((KeyError, ValueError)):
            CanonicalOutput.from_mapping({"run_id": "only-an-id"})


class RunRecordViewTests(unittest.TestCase):
    def test_canonical_nesting_resolves(self):
        record = {"run_record": {"deterministic_state": {"gate": "g"},
                                 "capability_log": [1], "errors": [2]}}
        self.assertEqual(run_record_of(record)["deterministic_state"],
                         {"gate": "g"})

    def test_legacy_top_level_resolves(self):
        record = {"deterministic_state": {"gate": "legacy"},
                  "capability_log": [], "errors": []}
        self.assertEqual(run_record_of(record)["deterministic_state"],
                         {"gate": "legacy"})

    def test_empty_is_honest(self):
        self.assertEqual(run_record_of(None)["deterministic_state"], {})
        self.assertEqual(run_record_of({})["capability_log"], [])


class _FakePool:
    """Minimal asyncpg-pool double for the canonical SQL seam."""

    def __init__(self, row=None, rows=None, status="UPDATE 1"):
        self.row = row
        self.rows = rows or []
        self.status = status
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.row

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self.status


class CanonicalStoreSeamTests(unittest.TestCase):
    def _store(self, pool):
        from market_service.runtime.postgres_store import PostgresRuntimeStore

        store = PostgresRuntimeStore("postgresql://fixture")
        store.pool = pool
        return store

    def test_insert_round_trips_the_contract(self):
        import asyncio
        import json

        output = _output()
        pool = _FakePool(row={"run_id": output.run_id})
        store = self._store(pool)
        self.assertTrue(asyncio.run(store.insert_canonical_output(output)))
        kind, sql, args = pool.calls[0]
        self.assertEqual(kind, "fetchrow")
        self.assertIn("INSERT INTO canonical_output", sql)
        self.assertIn("ON CONFLICT (run_id) DO NOTHING", sql)
        self.assertEqual(len(args), 23)
        self.assertEqual(str(args[0]), output.run_id)
        self.assertEqual(args[14], output.answer_text)
        self.assertEqual(json.loads(args[16])["hypothesis_probabilities"]["H0"],
                         _adjudication()["H0"])  # interpretation param

    def test_parse_row_jsonb_and_datetimes(self):
        from datetime import UTC, datetime

        row = {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
            "completed_at": datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
            "answer": '{"answer": {"summary": "x"}}',
            "interpretation": None,
            "run_record": '{"deterministic_state": {"gate": "g"}}',
        }
        parsed = self._store(_FakePool())._parse_canonical_row(row)
        self.assertEqual(parsed["answer"], {"answer": {"summary": "x"}})
        self.assertEqual(parsed["run_record"]["deterministic_state"],
                         {"gate": "g"})
        self.assertEqual(parsed["generated_at"], "2026-01-01T00:00:00+00:00")

    def test_read_latest_and_exact(self):
        import asyncio

        row = {"run_id": "00000000-0000-0000-0000-000000000001"}
        pool = _FakePool(row=row)
        store = self._store(pool)
        self.assertIsNotNone(
            asyncio.run(store.read_canonical_output(symbol="BTCUSDT")))
        self.assertIsNotNone(asyncio.run(store.read_canonical_output(
            run_id="00000000-0000-0000-0000-000000000001")))
        self.assertIn("ORDER BY generated_at DESC LIMIT 1", pool.calls[0][1])
        self.assertIn("WHERE run_id = $1", pool.calls[1][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
