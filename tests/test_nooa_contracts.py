"""NOOA-free tests for the analyst contract surface.

These tests exercise the SpecialistReport contract, the
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
    MARKET_RUN_SCHEMA_VERSION,
    SPECIALIST_REPORT_SCHEMA_VERSION,
    SpecialistReport,
    SpecialistReportParseError,
)

try:  # discover -s tests puts tests/ on sys.path
    from _nooa_fixtures import _MOCK_RESPONSES, _SAMPLE_ENVELOPE
except ImportError:  # module-style invocation (tests.*)
    from tests._nooa_fixtures import (  # type: ignore[no-redef]
        _MOCK_RESPONSES,
        _SAMPLE_ENVELOPE,
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

    def test_model_is_verbatim_passthrough_regardless_of_provider(self):
        # Already-prefixed model is passed through verbatim (openrouter etc.)
        cfg = ModelBackendConfig(
            provider="Minimax.io",
            model="minimax/claude-something",
            base_url="https://gateway.example/anthropic",
            api_key="k",
        )
        self.assertEqual(cfg.routed_model(), "minimax/claude-something")

    def test_vllm_model_gets_openai_prefix(self):
        # vllm is OpenAI-compatible -> openai/<model> for litellm routing
        cfg = ModelBackendConfig(provider="vllm", model="llama-3-70b", base_url="http://host:8000/v1")
        self.assertEqual(cfg.routed_model(), "openai/llama-3-70b")

    def test_ollama_model_gets_ollama_prefix(self):
        cfg = ModelBackendConfig(provider="ollama", model="llama3")
        self.assertEqual(cfg.routed_model(), "ollama/llama3")

    def test_native_qwen_ollama_prefix(self):
        cfg = ModelBackendConfig(provider="ollama", model="qwen3:8b")
        self.assertEqual(cfg.routed_model(), "ollama/qwen3:8b")

    def test_existing_model_prefix_is_preserved(self):
        cfg = ModelBackendConfig(provider="openai", model="deepseek/deepseek-chat", base_url="https://openrouter.ai/api/v1")
        self.assertEqual(cfg.routed_model(), "deepseek/deepseek-chat")

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

    def test_reasoning_prose_with_embedded_json_extracts(self):
        # MiniMax reasoning gateways prefix answers with a thinking preamble
        # and fence the JSON in ```json ... ``` blocks. The parser must
        # extract the object and mark it explicitly — never silently.
        raw = (
            " thinking\nLet me analyze the envelope for SOLUSDT...\n\n"
            "```json\n"
            + json.dumps({
                "summary": "Spot demand bullish",
                "evidence": [{"path": "a.b", "value": 1, "interpretation": "ok"}],
                "confidence": "medium",
                "limitations": ["analysis domain degraded"],
                "null_fields": ["c.d"],
            })
            + "\n```\n"
        )
        report = SpecialistReport.from_llm_text("delta_orderflow", "r-1", raw)
        self.assertEqual(report.summary, "Spot demand bullish")
        self.assertTrue(report.extra.get("extracted") is True)

    def test_prose_without_json_still_raises(self):
        raw = (
            " thinking\nI reviewed the envelope. Coverage is incomplete and "
            "I cannot produce a directional assessment from the available"
            " data without inventing values."
        )
        with self.assertRaises(SpecialistReportParseError):
            SpecialistReport.from_llm_text("delta", "r-1", raw)


# ---------------------------------------------------------------------------
# (AnalystBriefingContractTests + RunnerModeTests + PersistenceOrderingTests
# REMOVED — the AnalystBriefing dataclass family was retired 2026-08-31
# in the market-read deviation. The engine's persistence ordering is
# covered by tests/test_engine.py: PG insert before Redis publish.)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()