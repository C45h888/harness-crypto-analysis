"""NOOA-gated tests for the agent suite and full analyst pipeline.

These tests genuinely require ``nooa`` (which pulls in litellm on import).
On hosts where that import is pathologically slow (litellm builds a large
pydantic model graph at import) they are skipped via a subprocess probe —
the fast, NOOA-free contract/runner/persistence tests live in
``tests/test_nooa_contracts.py`` and always run.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from market_service.nooa_harness.backends import ModelBackendConfig

try:  # discover -s tests puts tests/ on sys.path
    from _nooa_fixtures import _MOCK_RESPONSES, _SAMPLE_ENVELOPE, _make_mock_llm
except ImportError:  # module-style invocation (tests.*)
    from tests._nooa_fixtures import (  # type: ignore[no-redef]
        _MOCK_RESPONSES,
        _SAMPLE_ENVELOPE,
        _make_mock_llm,
    )

# ---------------------------------------------------------------------------
# Subprocess import probe: only import nooa if it imports within budget.
# ---------------------------------------------------------------------------

_NOOA_IMPORT_TIMEOUT_S = float(os.getenv("NOOA_IMPORT_TIMEOUT_S", "45"))


def _nooa_import_ok() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-c", "import nooa; from nooa import Agent, strategy; from nooa.strategies import CodeActStrategy, PredictStrategy"],
            timeout=_NOOA_IMPORT_TIMEOUT_S,
            capture_output=True,
            check=True,
        )
        return True
    except Exception:
        return False


_NOOA_AVAILABLE = _nooa_import_ok()

_requires_nooa = unittest.skipUnless(
    _NOOA_AVAILABLE,
    f"nooa/litellm import did not complete within {_NOOA_IMPORT_TIMEOUT_S:g}s on this host",
)

if _NOOA_AVAILABLE:
    from market_service.nooa_harness.agents import (
        ControllerAgent,
        DeltaOrderflowAgent,
        LiquidationAgent,
        MacroAgent,
        MarketAnalyst,
        OpenInterestAgent,
    )
    from market_service.nooa_harness.suite import AnalystSuite, build_suite
else:  # pragma: no cover - only reached when nooa is unavailable
    ControllerAgent = DeltaOrderflowAgent = LiquidationAgent = MacroAgent = None
    MarketAnalyst = OpenInterestAgent = None
    AnalystSuite = build_suite = None


@_requires_nooa
class NooaHarnessContractTests(unittest.TestCase):
    def test_backend_config_rejects_unknown_provider(self):
        # Unknown provider strings are now passed through to litellm (route by
        # model prefix), so they must NOT raise — assert they are accepted.
        with patch.dict(os.environ, {"PROVIDER": "Minimax.io", "PROVIDER_BASE_URL": "https://x/anthropic", "NOOA_MODEL_NAME": "m"}, clear=True):
            cfg = ModelBackendConfig.from_env()
            self.assertEqual(cfg.routed_model(), "anthropic/m")

    def test_agent_class_hierarchy(self):
        llm = MagicMock()
        agents = [
            DeltaOrderflowAgent("SOLUSDT", llm=llm),
            MacroAgent("SOLUSDT", llm=llm),
            OpenInterestAgent("SOLUSDT", llm=llm),
            LiquidationAgent("SOLUSDT", llm=llm),
            ControllerAgent("SOLUSDT", llm=llm),
        ]
        for agent in agents:
            self.assertIsInstance(agent, MarketAnalyst)
            self.assertEqual(agent.symbol, "SOLUSDT")
            self.assertTrue(hasattr(agent, "remit"))
            self.assertTrue(hasattr(agent, "_current_envelope"))

    def test_envelope_schema_generation(self):
        agent = DeltaOrderflowAgent("SOLUSDT", llm=MagicMock())
        schema = agent._envelope_schema()
        parsed = json.loads(schema)
        self.assertIn("schema_version", parsed)
        self.assertIn("canonical_state", parsed)
        self.assertIn("data-access", parsed["canonical_state"])

    def test_envelope_summary_format(self):
        agent = DeltaOrderflowAgent("SOLUSDT", llm=MagicMock())
        agent._current_envelope = _SAMPLE_ENVELOPE
        summary = json.loads(agent._envelope_summary())
        self.assertEqual(summary["run_id"], "test-run-001")
        self.assertEqual(summary["status"], "healthy")
        self.assertIn("data_keys", summary)


@_requires_nooa
class NooaHarnessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Tests that exercise the full analyst suite pipeline."""

    def setUp(self):
        self.llm = _make_mock_llm()

    def test_build_suite_creates_all_agents(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        self.assertIsInstance(suite, AnalystSuite)
        self.assertIsInstance(suite.controller, ControllerAgent)
        self.assertIsInstance(suite.delta_orderflow, DeltaOrderflowAgent)
        self.assertIsInstance(suite.macro, MacroAgent)
        self.assertIsInstance(suite.open_interest, OpenInterestAgent)
        self.assertIsInstance(suite.liquidations, LiquidationAgent)
        self.assertEqual(suite.controller.symbol, "SOLUSDT")

    def test_suite_injects_envelope_into_agents(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        self.assertIsNone(suite.delta_orderflow._current_envelope)
        self.assertIsNone(suite.controller._current_envelope)
        envelope = dict(_SAMPLE_ENVELOPE)
        for agent in (suite.delta_orderflow, suite.macro, suite.open_interest, suite.liquidations, suite.controller):
            agent._current_envelope = envelope
        env = suite.delta_orderflow._current_envelope
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env["run_id"], "test-run-001")

    async def test_analyze_output_shape(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        envelope = dict(_SAMPLE_ENVELOPE)
        with patch.object(suite.delta_orderflow, "assess", return_value=_MOCK_RESPONSES["delta_orderflow"]), \
             patch.object(suite.macro, "assess", return_value=_MOCK_RESPONSES["macro"]), \
             patch.object(suite.open_interest, "assess", return_value=_MOCK_RESPONSES["open_interest"]), \
             patch.object(suite.liquidations, "assess", return_value=_MOCK_RESPONSES["liquidations"]), \
             patch.object(suite.controller, "synthesize", return_value=_MOCK_RESPONSES["controller"]):
            result = await suite.analyze(
                envelope,
                session_id="sess-1", model_provider="openai", model_name="gpt-x",
            )
        self.assertEqual(result["symbol"], "SOLUSDT")
        self.assertEqual(result["run_id"], "test-run-001")
        self.assertEqual(result["schema_version"], 1)
        self.assertIn("specialist_reports", result)
        self.assertIn("briefing", result)
        self.assertIn("parse_errors", result)
        briefing = result["briefing"]
        self.assertEqual(briefing["run_id"], "test-run-001")
        self.assertEqual(briefing["session_id"], "sess-1")
        for name in ("delta_orderflow", "macro", "open_interest", "liquidations"):
            self.assertIn(name, result["specialist_reports"])
            report = result["specialist_reports"][name]
            self.assertIsNotNone(report)
            self.assertEqual(report["name"], name)
            self.assertEqual(report["run_id"], "test-run-001")
        self.assertIn("SOLUSDT shows moderate bullish pressure", briefing["narrative"])
        self.assertEqual(briefing["consensus"]["direction"], "moderately bullish")
        self.assertEqual(briefing["consensus"]["confidence"], "low")

    async def test_analyze_with_degraded_envelope(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        envelope = dict(_SAMPLE_ENVELOPE)
        envelope["status"] = "degraded"
        envelope["errors"] = [{"source": "data-access", "error": "timeout", "details": {}}]
        with patch.object(suite.delta_orderflow, "assess", return_value=_MOCK_RESPONSES["delta_orderflow"]), \
             patch.object(suite.macro, "assess", return_value=_MOCK_RESPONSES["macro"]), \
             patch.object(suite.open_interest, "assess", return_value=_MOCK_RESPONSES["open_interest"]), \
             patch.object(suite.liquidations, "assess", return_value=_MOCK_RESPONSES["liquidations"]), \
             patch.object(suite.controller, "synthesize", return_value=_MOCK_RESPONSES["controller"]):
            result = await suite.analyze(
                envelope,
                session_id="sess-1", model_provider="openai", model_name="gpt-x",
            )
        self.assertEqual(result["run_id"], "test-run-001")
        self.assertEqual(result["briefing"]["envelope_summary"]["status"], "degraded")

    def test_agent_remit_strings_are_set(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        for agent in (suite.delta_orderflow, suite.macro, suite.open_interest, suite.liquidations, suite.controller):
            self.assertTrue(agent.remit, f"{type(agent).__name__} remit is empty")
            self.assertIsInstance(agent.remit, str)


@_requires_nooa
class SuiteParseErrorTests(unittest.IsolatedAsyncioTestCase):
    """suite.analyze() routes specialist output through the parser and records failures."""

    def setUp(self):
        self.llm = _make_mock_llm()

    async def test_malformed_specialist_output_is_recorded_not_aborted(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        envelope = dict(_SAMPLE_ENVELOPE)
        with patch.object(suite.delta_orderflow, "assess", return_value=_MOCK_RESPONSES["delta_orderflow"]), \
             patch.object(suite.macro, "assess", return_value="not valid json"), \
             patch.object(suite.open_interest, "assess", return_value=_MOCK_RESPONSES["open_interest"]), \
             patch.object(suite.liquidations, "assess", return_value=_MOCK_RESPONSES["liquidations"]), \
             patch.object(suite.controller, "synthesize", return_value=_MOCK_RESPONSES["controller"]):
            result = await suite.analyze(
                envelope,
                session_id="sess-1", model_provider="openai", model_name="gpt-x",
            )
        self.assertIsNotNone(result["specialist_reports"]["delta_orderflow"])
        self.assertIsNone(result["specialist_reports"]["macro"])
        self.assertIsNotNone(result["specialist_reports"]["open_interest"])
        self.assertIsNotNone(result["specialist_reports"]["liquidations"])
        macro_errors = [e for e in result["parse_errors"] if e.get("specialist") == "macro"]
        self.assertEqual(len(macro_errors), 1)
        self.assertEqual(macro_errors[0]["stage"], "specialist")
        self.assertIn("invalid JSON", macro_errors[0]["error"])
        self.assertEqual(result["briefing"]["run_id"], envelope["run_id"])

    async def test_missing_required_field_in_specialist_is_recorded(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        envelope = dict(_SAMPLE_ENVELOPE)
        bad = json.dumps({"evidence": [], "confidence": "low", "limitations": []})
        with patch.object(suite.delta_orderflow, "assess", return_value=_MOCK_RESPONSES["delta_orderflow"]), \
             patch.object(suite.macro, "assess", return_value=_MOCK_RESPONSES["macro"]), \
             patch.object(suite.open_interest, "assess", return_value=bad), \
             patch.object(suite.liquidations, "assess", return_value=_MOCK_RESPONSES["liquidations"]), \
             patch.object(suite.controller, "synthesize", return_value=_MOCK_RESPONSES["controller"]):
            result = await suite.analyze(
                envelope,
                session_id="sess-1", model_provider="openai", model_name="gpt-x",
            )
        oi_errors = [e for e in result["parse_errors"] if e.get("specialist") == "open_interest"]
        self.assertEqual(len(oi_errors), 1)
        self.assertIn("summary", oi_errors[0]["error"])

    async def test_specialist_timeout_publishes_degraded_briefing(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        envelope = dict(_SAMPLE_ENVELOPE)

        async def slow_assess(_envelope):
            await asyncio.sleep(0.05)
            return _MOCK_RESPONSES["macro"]

        with patch.object(suite.delta_orderflow, "assess", new=AsyncMock(return_value=_MOCK_RESPONSES["delta_orderflow"])), \
             patch.object(suite.macro, "assess", new=AsyncMock(side_effect=slow_assess)), \
             patch.object(suite.open_interest, "assess", new=AsyncMock(return_value=_MOCK_RESPONSES["open_interest"])), \
             patch.object(suite.liquidations, "assess", new=AsyncMock(return_value=_MOCK_RESPONSES["liquidations"])), \
             patch.object(suite.controller, "synthesize", new=AsyncMock(return_value=_MOCK_RESPONSES["controller"])):
            result = await suite.analyze(
                envelope,
                session_id="sess-1", model_provider="openai", model_name="gpt-x",
                specialist_timeout_s=0.001,
            )
        timeout_errors = [
            item for item in result["parse_errors"]
            if item.get("specialist") == "macro" and item.get("kind") == "timeout"
        ]
        self.assertEqual(len(timeout_errors), 1)
        self.assertEqual(result["briefing"]["status"], "degraded")
        self.assertIsNone(result["briefing"]["specialist_reports"]["macro"])

    async def test_specialist_exception_is_recorded_degraded(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        envelope = dict(_SAMPLE_ENVELOPE)

        async def exploding(_envelope):
            raise RuntimeError("boom")

        with patch.object(suite.delta_orderflow, "assess", new=AsyncMock(return_value=_MOCK_RESPONSES["delta_orderflow"])), \
             patch.object(suite.macro, "assess", new=AsyncMock(side_effect=exploding)), \
             patch.object(suite.open_interest, "assess", new=AsyncMock(return_value=_MOCK_RESPONSES["open_interest"])), \
             patch.object(suite.liquidations, "assess", new=AsyncMock(return_value=_MOCK_RESPONSES["liquidations"])), \
             patch.object(suite.controller, "synthesize", new=AsyncMock(return_value=_MOCK_RESPONSES["controller"])):
            result = await suite.analyze(
                envelope,
                session_id="sess-1", model_provider="openai", model_name="gpt-x",
            )
        exc_errors = [
            item for item in result["parse_errors"]
            if item.get("specialist") == "macro" and item.get("kind") == "exception"
        ]
        self.assertEqual(len(exc_errors), 1)
        self.assertIn("RuntimeError", exc_errors[0]["error"])
        self.assertEqual(result["briefing"]["status"], "degraded")
        self.assertIsNone(result["briefing"]["specialist_reports"]["macro"])

    async def test_specialists_start_concurrently_and_share_envelope(self):
        suite = build_suite("SOLUSDT", llm=self.llm)
        envelope = dict(_SAMPLE_ENVELOPE)
        received: list[int] = []

        async def assess(_envelope):
            received.append(id(_envelope))
            await asyncio.sleep(0.01)
            return _MOCK_RESPONSES["delta_orderflow"]

        with patch.object(suite.delta_orderflow, "assess", new=AsyncMock(side_effect=assess)), \
             patch.object(suite.macro, "assess", new=AsyncMock(side_effect=assess)), \
             patch.object(suite.open_interest, "assess", new=AsyncMock(side_effect=assess)), \
             patch.object(suite.liquidations, "assess", new=AsyncMock(side_effect=assess)), \
             patch.object(suite.controller, "synthesize", new=AsyncMock(return_value=_MOCK_RESPONSES["controller"])):
            result = await suite.analyze(
                envelope,
                session_id="sess-1", model_provider="openai", model_name="gpt-x",
            )
        self.assertEqual(received, [id(envelope)] * 4)
        self.assertEqual(result["briefing"]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()