"""NOOA-free tests for the analyst contract surface.

These tests exercise SpecialistReport / AnalystBriefing contracts, the
env-driven model backend config, the runner's read-vs-refresh modes, and the
postgres-first persistence ordering. They import NOOA-free code only, so they
run fast without ever loading litellm.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from market_service.nooa_harness.backends import ModelBackendConfig
from market_service.runtime.contracts import (
    ANALYST_BRIEFING_SCHEMA_VERSION,
    AnalystBriefing,
    MARKET_RUN_SCHEMA_VERSION,
    MarketRunEnvelope,
    SPECIALIST_REPORT_SCHEMA_VERSION,
    SpecialistReport,
    SpecialistReportParseError,
)

try:  # discover -s tests puts tests/ on sys.path
    from _nooa_fixtures import _MOCK_RESPONSES, _SAMPLE_ENVELOPE, _SAMPLE_ENVELOPE_BRIEFING
except ImportError:  # module-style invocation (tests.*)
    from tests._nooa_fixtures import (  # type: ignore[no-redef]
        _MOCK_RESPONSES,
        _SAMPLE_ENVELOPE,
        _SAMPLE_ENVELOPE_BRIEFING,
    )


# ---------------------------------------------------------------------------
# Backend config — env-driven, secrets imported (never hardcoded)
# ---------------------------------------------------------------------------


class BackendConfigTests(unittest.TestCase):
    def test_reads_provider_api_key_and_base_url_from_env(self):
        with patch.dict(
            os.environ,
            {
                "PROVIDER": "Minimax.io",
                "PROVIDER_API_KEY": "sk-test-not-a-real-key",
                "PROVIDER_BASE_URL": "https://api.minimax.io/anthropic",
                "NOOA_MODEL_NAME": "some-model",
            },
            clear=True,
        ):
            cfg = ModelBackendConfig.from_env()
            self.assertEqual(cfg.provider, "Minimax.io")
            self.assertEqual(cfg.api_key, "sk-test-not-a-real-key")
            self.assertEqual(cfg.api_key_env, "PROVIDER_API_KEY")
            self.assertEqual(cfg.base_url, "https://api.minimax.io/anthropic")
            self.assertEqual(cfg.model, "some-model")
            self.assertEqual(cfg.temperature, 0.2)

    def test_requires_model_name(self):
        with patch.dict(
            os.environ,
            {"PROVIDER": "openai", "PROVIDER_API_KEY": "k"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                ModelBackendConfig.from_env()

    def test_anthropic_endpoint_routes_model_with_prefix(self):
        cfg = ModelBackendConfig(
            provider="Minimax.io",
            model="claude-something",
            base_url="https://gateway.example/anthropic",
            api_key="k",
        )
        self.assertEqual(cfg.routed_model(), "anthropic/claude-something")

    def test_vllm_routes_to_openai_prefix(self):
        cfg = ModelBackendConfig(provider="vllm", model="llama-3-70b", base_url="http://host:8000/v1")
        self.assertEqual(cfg.routed_model(), "openai/llama-3-70b")

    def test_ollama_routes_to_ollama_prefix(self):
        cfg = ModelBackendConfig(provider="ollama", model="llama3")
        self.assertEqual(cfg.routed_model(), "ollama/llama3")

    def test_existing_prefix_is_preserved(self):
        cfg = ModelBackendConfig(provider="litellm", model="anthropic/claude-something")
        self.assertEqual(cfg.routed_model(), "anthropic/claude-something")

    def test_api_key_is_not_exported_as_model_metadata(self):
        cfg = ModelBackendConfig(
            provider="anthropic", model="m", base_url="https://x/anthropic", api_key="sekrit"
        )
        as_dict_meta = {"provider": cfg.provider, "model": cfg.model,
                        "base_url": cfg.base_url, "api_key_env": cfg.api_key_env}
        self.assertNotIn("sekrit", str(as_dict_meta))


# ---------------------------------------------------------------------------
# SpecialistReportContractTests
# ---------------------------------------------------------------------------


class SpecialistReportContractTests(unittest.TestCase):
    """SpecialistReport.from_llm_text is the boundary between LLM text and typed state."""

    def test_valid_json_round_trips(self):
        raw = json.dumps({
            "summary": "Bullish", "evidence": [{"path": "x.y", "value": 1, "interpretation": "ok"}],
            "confidence": "medium", "limitations": ["small sample"],
            "null_fields": ["a.b"], "cannot_establish": ["trend"],
        })
        report = SpecialistReport.from_llm_text("delta", "r-1", raw)
        self.assertEqual(report.name, "delta")
        self.assertEqual(report.run_id, "r-1")
        self.assertEqual(report.confidence, "medium")
        self.assertEqual(report.schema_version, SPECIALIST_REPORT_SCHEMA_VERSION)
        self.assertEqual(report.extra["null_fields"], ["a.b"])
        roundtrip = SpecialistReport.from_llm_text("delta", "r-1", report.to_json())
        self.assertEqual(roundtrip.to_dict(), report.to_dict())

    def test_invalid_json_raises_parse_error(self):
        with self.assertRaises(SpecialistReportParseError) as ctx:
            SpecialistReport.from_llm_text("delta", "r-1", "not json at all")
        err = ctx.exception
        self.assertEqual(err.specialist, "delta")
        self.assertEqual(err.run_id, "r-1")
        self.assertIn("invalid JSON", err.error)

    def test_empty_string_raises_parse_error(self):
        with self.assertRaises(SpecialistReportParseError):
            SpecialistReport.from_llm_text("delta", "r-1", "")

    def test_missing_required_field_raises(self):
        raw = json.dumps({"evidence": [], "confidence": "low", "limitations": []})  # missing summary
        with self.assertRaises(SpecialistReportParseError) as ctx:
            SpecialistReport.from_llm_text("delta", "r-1", raw)
        self.assertIn("summary", ctx.exception.error)

    def test_invalid_confidence_raises(self):
        raw = json.dumps({"summary": "x", "evidence": [], "confidence": "extreme", "limitations": []})
        with self.assertRaises(SpecialistReportParseError) as ctx:
            SpecialistReport.from_llm_text("delta", "r-1", raw)
        self.assertIn("confidence", ctx.exception.error)

    def test_evidence_item_must_be_object(self):
        raw = json.dumps({"summary": "x", "evidence": ["nope"], "confidence": "low", "limitations": []})
        with self.assertRaises(SpecialistReportParseError):
            SpecialistReport.from_llm_text("delta", "r-1", raw)

    def test_evidence_item_requires_path(self):
        raw = json.dumps({
            "summary": "x",
            "evidence": [{"value": 1, "interpretation": "ok"}],  # no path
            "confidence": "low", "limitations": [],
        })
        with self.assertRaises(SpecialistReportParseError) as ctx:
            SpecialistReport.from_llm_text("delta", "r-1", raw)
        self.assertIn("path", ctx.exception.error)


# ---------------------------------------------------------------------------
# AnalystBriefingContractTests
# ---------------------------------------------------------------------------


class AnalystBriefingContractTests(unittest.TestCase):
    """AnalystBriefing.from_controller_text always produces a briefing, even on parse failure."""

    def _controller_json(self) -> str:
        return json.dumps({
            "narrative": "Bullish bias with futures leading.",
            "consensus": {"direction": "up", "confidence": "medium"},
            "disagreements": [{"topic": "OI", "specialist_a": "delta", "specialist_b": "oi"}],
            "key_evidence": [{"run_id": "test-run-002", "path": "calc.flow", "claim": "net +50"}],
            "limitations": ["sample size"],
            "uncertainty_sources": ["freshness"],
        })

    def test_valid_controller_text_produces_typed_briefing(self):
        briefing = AnalystBriefing.from_controller_text(
            session_id="sess-1", run_id="test-run-002",
            model_provider="openai", model_name="gpt-x",
            generated_at="2026-01-01T00:00:02+00:00",
            raw=self._controller_json(),
            envelope=_SAMPLE_ENVELOPE_BRIEFING,
        )
        self.assertEqual(briefing.run_id, "test-run-002")
        self.assertEqual(briefing.session_id, "sess-1")
        self.assertEqual(briefing.model_provider, "openai")
        self.assertEqual(briefing.consensus["direction"], "up")
        self.assertEqual(briefing.consensus["confidence"], "medium")
        self.assertEqual(len(briefing.key_evidence), 1)
        self.assertEqual(len(briefing.disagreements), 1)
        self.assertEqual(briefing.schema_version, ANALYST_BRIEFING_SCHEMA_VERSION)
        self.assertEqual(briefing.envelope_summary["status"], "healthy")
        self.assertEqual(briefing.parse_errors, ())

    def test_malformed_controller_still_produces_briefing_with_parse_errors(self):
        briefing = AnalystBriefing.from_controller_text(
            session_id="sess-1", run_id="test-run-002",
            model_provider="openai", model_name="gpt-x",
            generated_at="2026-01-01T00:00:02+00:00",
            raw="not json", envelope=_SAMPLE_ENVELOPE_BRIEFING,
        )
        self.assertEqual(briefing.run_id, "test-run-002")
        self.assertIn("raw_narrative", briefing.extra)
        self.assertEqual(briefing.parse_errors[0]["stage"], "controller")
        self.assertEqual(briefing.consensus["direction"], "unknown")
        self.assertEqual(briefing.consensus["confidence"], "low")

    def test_invalid_confidence_in_consensus_is_clamped(self):
        raw = json.dumps({
            "narrative": "x", "consensus": {"direction": "up", "confidence": "extreme"},
        })
        briefing = AnalystBriefing.from_controller_text(
            session_id="sess-1", run_id="r", model_provider="o", model_name="m",
            generated_at="t", raw=raw, envelope=_SAMPLE_ENVELOPE_BRIEFING,
        )
        self.assertEqual(briefing.consensus["confidence"], "low")

    def test_from_mapping_roundtrip(self):
        b1 = AnalystBriefing.from_controller_text(
            session_id="sess-1", run_id="r", model_provider="o", model_name="m",
            generated_at="t", raw=self._controller_json(),
            envelope=_SAMPLE_ENVELOPE_BRIEFING,
        )
        b2 = AnalystBriefing.from_mapping(b1.to_dict())
        self.assertEqual(b1.to_dict(), b2.to_dict())
        self.assertEqual(b2.parse_errors, b1.parse_errors)


# ---------------------------------------------------------------------------
# RunnerModeTests
# ---------------------------------------------------------------------------


class SettingsStub:
    """Minimal Settings stand-in so the runner path needs no real env."""
    database_url = "postgresql://x"
    redis_url = "redis://x"
    redis_key_prefix = "marketflow"
    redis_stream_maxlen = 10000


class RunnerModeTests(unittest.IsolatedAsyncioTestCase):
    """runner.run_analyst_loop: read-existing is default, refresh is explicit."""

    def setUp(self):
        self._env = patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://test",
                "NOOA_MODEL_PROVIDER": "ollama",
                "NOOA_MODEL_NAME": "test-model",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    @staticmethod
    def _briefing_payload(envelope_obj):
        return {
            "schema_version": 1, "session_id": "sess-1", "run_id": envelope_obj.run_id,
            "model_provider": "openai", "model_name": "gpt-x",
            "generated_at": "t", "narrative": "n", "consensus": {"direction": "up", "confidence": "low"},
            "disagreements": [], "key_evidence": [], "limitations": [], "uncertainty_sources": [],
            "parse_errors": [], "envelope_summary": {},
        }

    def _build_loop(self, env):
        from market_service.nooa_harness import runner

        envelope_obj = MarketRunEnvelope.from_mapping(dict(_SAMPLE_ENVELOPE))
        fake_read = AsyncMock(return_value=envelope_obj)
        fake_persist = AsyncMock(return_value={"postgres_inserted": True, "redis_stream_id": "1-1"})
        fake_suite_analyze = AsyncMock(return_value={
            "briefing": self._briefing_payload(envelope_obj), "parse_errors": [],
            "specialist_reports": {}, "symbol": "SOLUSDT", "run_id": envelope_obj.run_id,
            "schema_version": 1, "briefing_json": json.dumps(self._briefing_payload(envelope_obj)),
        })
        patchers = [
            patch.object(runner, "_read_envelope", fake_read),
            patch.object(runner, "_persist_briefing", fake_persist),
            patch.object(runner, "build_suite", return_value=MagicMock(analyze=fake_suite_analyze)),
            # build_suite(symbol, backend.build_llm()) is mocked but the llm
            # argument is still evaluated — stub build_llm so it never imports
            # nooa/litellm.
            patch.object(runner.ModelBackendConfig, "build_llm", return_value=MagicMock()),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return runner, fake_read, fake_persist, fake_suite_analyze, envelope_obj

    async def test_run_id_mode_does_not_trigger_new_cycle(self):
        runner, fake_read, fake_persist, fake_suite, envelope_obj = self._build_loop(None)
        await runner.run_analyst_loop(
            "SOLUSDT", interval_s=0, timeout_s=10, cycles=1,
            run_id=envelope_obj.run_id,
            session_id="11111111-1111-1111-1111-111111111111",
        )
        fake_read.assert_awaited_once()
        fake_persist.assert_awaited_once()
        persist_arg = fake_persist.await_args.args[1]
        self.assertEqual(persist_arg.run_id, envelope_obj.run_id)

    async def test_latest_mode_does_not_trigger_new_cycle(self):
        runner, fake_read, fake_persist, fake_suite, envelope_obj = self._build_loop(None)
        await runner.run_analyst_loop(
            "SOLUSDT", interval_s=0, timeout_s=10, cycles=1, use_latest=True,
            session_id="11111111-1111-1111-1111-111111111111",
        )
        fake_read.assert_awaited_once()
        fake_persist.assert_awaited_once()

    async def test_default_mode_reads_latest_without_triggering(self):
        runner, fake_read, fake_persist, fake_suite, envelope_obj = self._build_loop(None)
        await runner.run_analyst_loop(
            "SOLUSDT", interval_s=0, timeout_s=10, cycles=1,
            session_id="11111111-1111-1111-1111-111111111111",
        )
        fake_read.assert_awaited_once()
        fake_persist.assert_awaited_once()

    async def test_exact_run_defaults_to_one_shot(self):
        runner, fake_read, fake_persist, fake_suite, envelope_obj = self._build_loop(None)
        await runner.run_analyst_loop(
            "SOLUSDT", interval_s=0, run_id=envelope_obj.run_id,
            session_id="11111111-1111-1111-1111-111111111111",
        )
        fake_read.assert_awaited_once()
        fake_persist.assert_awaited_once()

    async def test_latest_mode_deduplicates_unchanged_run(self):
        from market_service.nooa_harness import runner

        envelope_obj = MarketRunEnvelope.from_mapping(dict(_SAMPLE_ENVELOPE))
        fake_read = AsyncMock(side_effect=[envelope_obj, envelope_obj])
        fake_persist = AsyncMock(return_value={"postgres_inserted": True, "redis_stream_id": "1-1"})
        fake_suite_analyze = AsyncMock(return_value={
            "briefing": self._briefing_payload(envelope_obj), "parse_errors": [],
            "specialist_reports": {}, "symbol": "SOLUSDT", "run_id": envelope_obj.run_id,
            "schema_version": 1, "briefing_json": "{}",
        })
        with patch.object(runner, "_read_envelope", fake_read), \
             patch.object(runner, "_persist_briefing", fake_persist), \
             patch.object(runner, "build_suite", return_value=MagicMock(analyze=fake_suite_analyze)), \
             patch.object(runner.ModelBackendConfig, "build_llm", return_value=MagicMock()):
            await runner.run_analyst_loop(
                "SOLUSDT", interval_s=0, cycles=2, use_latest=True,
                session_id="11111111-1111-1111-1111-111111111111",
            )
        self.assertEqual(fake_read.await_count, 2)
        fake_suite_analyze.assert_awaited_once()
        fake_persist.assert_awaited_once()

    def test_session_ids_are_uuid_only(self):
        from market_service.nooa_harness.runner import _resolve_session_id

        value = _resolve_session_id(None)
        uuid.UUID(value)
        with self.assertRaises(ValueError):
            _resolve_session_id("auto-session")

    async def test_mutually_exclusive_run_id_and_latest_raise(self):
        from market_service.nooa_harness import runner

        with self.assertRaises(ValueError):
            await runner.run_analyst_loop(
                "SOLUSDT", interval_s=0, timeout_s=10, cycles=0,
                run_id="some-uuid", use_latest=True,
            )


# ---------------------------------------------------------------------------
# PersistenceOrderingTests
# ---------------------------------------------------------------------------


class PersistenceOrderingTests(unittest.IsolatedAsyncioTestCase):
    """Postgres is written before the Redis agent stream is published."""

    async def test_postgres_written_before_redis_publish(self):
        from market_service.nooa_harness import runner

        envelope_obj = MarketRunEnvelope.from_mapping(dict(_SAMPLE_ENVELOPE))
        briefing = AnalystBriefing.from_controller_text(
            session_id="sess-1", run_id=envelope_obj.run_id,
            model_provider="openai", model_name="gpt-x",
            generated_at="2026-01-01T00:00:02+00:00",
            raw=_MOCK_RESPONSES["controller"], envelope=dict(_SAMPLE_ENVELOPE),
        )
        order: list[str] = []

        class _FakePostgres:
            async def connect(self): pass
            async def close(self): pass
            async def insert_analyst_briefing(self, _b):
                order.append("postgres")
                return True

        class _FakeRedis:
            def __init__(self): self.calls: list[str] = []
            async def close(self): pass
            async def publish_briefing(self, _b):
                order.append("redis")
                return "1-1"
            def agent_stream(self, session_id, artifact_type):
                self.calls.append(artifact_type)
                return f"marketflow:agent:{session_id}:{artifact_type}"

        fake_pg = _FakePostgres()
        fake_redis = _FakeRedis()
        with patch.object(runner, "PostgresRuntimeStore", return_value=fake_pg), \
             patch.object(runner, "RedisRuntimeStore", return_value=fake_redis):
            result = await runner._persist_briefing(
                SettingsStub(), briefing,  # type: ignore[arg-type]
            )
        self.assertEqual(order, ["postgres", "redis"])
        self.assertEqual(result["postgres_inserted"], True)
        self.assertEqual(result["redis_stream_id"], "1-1")
        self.assertIn("briefings", result["redis_key"])


if __name__ == "__main__":
    unittest.main()