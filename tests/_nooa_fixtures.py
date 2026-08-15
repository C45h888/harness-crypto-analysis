"""Shared fixtures for the NOOA harness tests.

This module deliberately imports NOOA-free code only. The heavy ``nooa`` /
litellm import is isolated so the fast contract/runner/persistence tests can
run without ever loading litellm.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Sample envelope (nooa-free)
# ---------------------------------------------------------------------------

_SAMPLE_ENVELOPE = {
    "schema_version": 1,
    "run_id": "test-run-001",
    "symbol": "SOLUSDT",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "completed_at": "2026-01-01T00:00:01+00:00",
    "status": "healthy",
    "data_source": "canonical",
    "coverage": {"domain_status": {"data-access": "ok", "calculations": "ok", "analysis": "ok"}},
    "canonical_state": {
        "data-access": {
            "evidence": {
                "spot": {
                    "order_book": {
                        "bids": [["100.0", "10.0"], ["99.0", "5.0"]],
                        "asks": [["101.0", "8.0"], ["102.0", "12.0"]],
                    },
                    "trades_raw": [
                        {"price": "100.5", "qty": "1.0", "side": "buy", "time": 1700000000000},
                    ],
                },
                "futures": {
                    "order_book": {
                        "bids": [["100.1", "20.0"], ["99.1", "15.0"]],
                        "asks": [["100.9", "10.0"], ["101.9", "18.0"]],
                    },
                    "funding": {"fundingRate": "0.0001", "markPrice": "100.5", "nextFundingTime": 1700000064000},
                    "open_interest": {"openInterest": "500000.0", "timestamp": 1700000000000},
                    "ticker_24h": {"lastPrice": "100.5", "volume": "1000000.0", "highPrice": "102.0", "lowPrice": "98.0"},
                },
            }
        },
        "calculations": {
            "calculations": {
                "flow": {"spot": {"buy_volume": 100.0, "sell_volume": 80.0}, "futures": {"buy_volume": 200.0, "sell_volume": 150.0}},
                "orderbook": {
                    "fut_keystone": {"bid": 100.1, "ask": 100.9},
                    "fut_microprice_skew_bps": 5.0,
                },
            }
        },
        "analysis": {
            "analysis": {
                "wall_migration": {"fuel_ratio": 0.6, "densest_clusters": [], "wall_delta": {}, "trap_assessment": {}},
                "demand": {"decomposition": {"spot": {"obi": 0.5}, "futures": {"obi": 0.7}, "cvd": {}, "buy_share": 0.55}},
            }
        },
    },
    "domain_outputs": {},
    "errors": [],
    "source_metadata": {"domain_run_ids": {"data-access": "test-run-001", "calculations": "test-run-001", "analysis": "test-run-001"}},
}

_SAMPLE_ENVELOPE_BRIEFING = {
    "schema_version": 1,
    "run_id": "test-run-002",
    "symbol": "SOLUSDT",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "completed_at": "2026-01-01T00:00:01+00:00",
    "status": "healthy",
    "data_source": "domain_pipeline",
    "coverage": {"domain_status": {"data-access": "ok", "calculations": "ok", "analysis": "ok"}},
    "canonical_state": {},
    "domain_outputs": {},
    "errors": [],
    "source_metadata": {"domain_run_ids": {"data-access": "test-run-002"}},
}

# ---------------------------------------------------------------------------
# Mock LLM responses (nooa-free — plain JSON strings per agent)
# ---------------------------------------------------------------------------

_MOCK_RESPONSES = {
    "delta_orderflow": json.dumps({
        "summary": "Futures delta is stronger than spot with net buying pressure.",
        "evidence": [{"path": "calculations.flow.futures", "value": {"buy_volume": 200.0, "sell_volume": 150.0}, "interpretation": "Net +50 futures buying"}],
        "confidence": "medium",
        "limitations": ["Spot trade sample is small"],
        "null_fields": [],
    }),
    "macro": json.dumps({
        "summary": "Positive funding rate suggests bullish sentiment.",
        "evidence": [{"path": "data-access.evidence.futures.funding", "value": {"fundingRate": "0.0001"}, "interpretation": "Slightly positive funding"}],
        "confidence": "low",
        "limitations": ["Single data point, no historical context"],
    }),
    "open_interest": json.dumps({
        "summary": "OI at 500K suggests moderate participation.",
        "evidence": [{"path": "data-access.evidence.futures.open_interest", "value": {"openInterest": "500000.0"}, "interpretation": "Moderate OI"}],
        "confidence": "low",
        "limitations": ["No OI change history available"],
        "cannot_establish": ["OI trend direction without historical data"],
    }),
    "liquidations": json.dumps({
        "summary": "No liquidation feed available; book pressure suggests moderate support.",
        "evidence": [{"path": "calculations.orderbook.fut_keystone", "value": {"bid": 100.1, "ask": 100.9}, "interpretation": "Narrow spread"}],
        "confidence": "low",
        "limitations": ["Liquidation data not available from exchange"],
        "missing_data": ["Liquidation feed not available"],
    }),
    "controller": json.dumps({
        "narrative": "SOLUSDT shows moderate bullish pressure with futures leading spot. Funding is positive but OI trend is unknown. Confidence is low due to missing historical context.",
        "consensus": {"direction": "moderately bullish", "confidence": "low"},
        "disagreements": [],
        "key_evidence": [{"run_id": "test-run-001", "path": "calculations.flow.futures", "claim": "Net +50 futures buying"}],
        "limitations": ["No historical OI trend", "Limited spot trade sample", "No liquidation data"],
        "uncertainty_sources": ["Missing OI history", "Single-point funding reading"],
    }),
}


def _make_mock_llm():
    """Create a mock LLM that returns plausible agent responses (nooa-free)."""
    mock = MagicMock()
    mock.complete = AsyncMock()
    mock.complete.side_effect = _mock_complete
    mock.generate = AsyncMock()
    mock.generate.side_effect = _mock_complete
    return mock


async def _mock_complete(prompt=None, messages=None, **kwargs):
    """Route mock responses based on agent type in the prompt."""
    prompt_str = str(prompt or messages or "")
    if "delta/orderflow" in prompt_str or "DeltaOrderflow" in prompt_str:
        return _MOCK_RESPONSES["delta_orderflow"]
    if "macro/context" in prompt_str or "MacroAgent" in prompt_str:
        return _MOCK_RESPONSES["macro"]
    if "open-interest" in prompt_str or "OpenInterest" in prompt_str:
        return _MOCK_RESPONSES["open_interest"]
    if "liquidation" in prompt_str or "Liquidation" in prompt_str:
        return _MOCK_RESPONSES["liquidations"]
    if "controller" in prompt_str or "synthesize" in prompt_str:
        return _MOCK_RESPONSES["controller"]
    return "{}"