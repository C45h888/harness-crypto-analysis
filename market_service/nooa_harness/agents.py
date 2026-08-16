"""NOOA analyst agents with LLM-generation strategies.

These classes are the first agentic layer above the canonical harness.  They
do not pull exchange data, calculate indicators, write Redis, or place trades.
The existing harness supplies one complete canonical envelope to the suite;
NOOA supplies the reasoning over that envelope.

Each specialist's ``assess()`` method uses ``@strategy(PredictStrategy())`` so
the LLM generates the assessment text directly from the envelope.  The
controller's ``synthesize()`` uses ``CodeActStrategy`` so it can
programmatically reconcile specialist reports.

The ``...`` (ellipsis) body is the NOOA contract — the framework detects it
via ``has_ellipsis_body()`` and wraps the method into an LLM generation call.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from nooa import Agent, Context, spec, strategy
from nooa.agentdoc import hidden
from nooa.context_blocks import DynamicContext
from nooa.strategies import CodeActStrategy, PredictStrategy

# ---------------------------------------------------------------------------
# Envelope schema — injected into agent context so the LLM knows the shape
# ---------------------------------------------------------------------------

_ENVELOPE_SCHEMA = json.dumps(
    {
        "schema_version": 1,
        "run_id": "<uuid>",
        "symbol": "SOLUSDT",
        "status": "healthy|degraded|invalid",
        "canonical_state": {
            "data-access": {
                "evidence": {
                    "spot": {
                        "order_book": {"bids": [["<price>", "<qty>"]], "asks": [["<price>", "<qty>"]]},
                        "trades_raw": [{"price": "<float>", "qty": "<float>", "side": "buy|sell", "time": "<int>"}],
                        "klines": [{"open": "<float>", "high": "<float>", "low": "<float>", "close": "<float>", "volume": "<float>"}],
                        "ticker_24h": {"lastPrice": "<float>", "volume": "<float>", "highPrice": "<float>", "lowPrice": "<float>"},
                    },
                    "futures": {
                        "order_book": {"bids": [["<price>", "<qty>"]], "asks": [["<price>", "<qty>"]]},
                        "trades_raw": [{"price": "<float>", "qty": "<float>", "side": "buy|sell", "time": "<int>"}],
                        "klines": "array of kline objects",
                        "ticker_24h": "ticker object",
                        "funding": {"fundingRate": "<float>", "markPrice": "<float>", "nextFundingTime": "<int>"},
                        "open_interest": {"openInterest": "<float>", "timestamp": "<int>"},
                    },
                }
            },
            "calculations": {
                "calculations": {
                    "flow": {"spot": "object", "futures": "object", "net": "object"},
                    "bucketed_cvd": "object",
                    "correlation": "object",
                    "signal_inputs": "object",
                    "volume_profile": "object",
                    "technical": "object",
                    "turnover": "object",
                    "orderbook": {
                        "fut_keystone": "object",
                        "fut_top_density_bids": "array of [price, qty]",
                        "fut_top_density_asks": "array of [price, qty]",
                        "fut_significant_levels": "array of level objects",
                        "fut_absorption_ladder": "array of level objects",
                        "fut_microprice_skew_bps": "<float or null>",
                    },
                }
            },
            "analysis": {
                "analysis": {
                    "wall_migration": {"fuel_ratio": "<float>", "densest_clusters": "array", "wall_delta": "object", "trap_assessment": "object"},
                    "path_absorption": {"fuel_ratio": "<float>", "simulated_ascent": "object", "simulated_descent": "object"},
                    "auction": {"microprice": {"depth": "object"}},
                    "demand": {"decomposition": {"spot": "object", "futures": "object", "cvd": "object", "buy_share": "<float>"}},
                    "regime": "object",
                    "stage": "object",
                }
            },
        },
        "coverage": {"domain_status": "object", "freshness": "object", "completeness": "object"},
        "errors": [{"source": "<string>", "error": "<string>", "details": "object"}],
        "source_metadata": {"domain_run_ids": "object"},
    },
    indent=2,
)


# ---------------------------------------------------------------------------
# Base analyst agent
# ---------------------------------------------------------------------------


class MarketAnalyst(Agent):
    """Base class shared by the controller and market specialists.

    Each agent receives the canonical MarketRunEnvelope and reasons over it.
    The ``remit`` class attribute becomes part of the system prompt.
    """

    symbol: Annotated[str, spec(description="Trading pair, e.g. SOLUSDT")] = "SOLUSDT"
    remit: Annotated[
        str,
        spec(description="What this agent is responsible for analyzing"),
    ] = "Reason over the complete canonical market envelope."

    # Internal state — hidden from the LLM, set before each generation call
    _current_envelope: Annotated[dict[str, Any] | None, hidden] = None
    _specialist_reports: Annotated[dict[str, str] | None, hidden] = None

    # NOOA's PredictStrategy enforces max_param_chars (default 200k) on every
    # strategy parameter. The live envelope carries raw evidence arrays that
    # routinely exceed that, so the LLM-bound view is trimmed deterministically
    # with explicit markers — the internal `_current_envelope` stays complete.
    _MAX_LLM_ENVELOPE_CHARS = 180_000
    _LIST_CAP = 60

    def __init__(self, symbol: str, *, llm: Any):
        super().__init__(llm=llm)
        self.symbol = symbol.upper()

    def _bounded_envelope(self, max_chars: int | None = None) -> str:
        """Serialized envelope sized for the LLM param limit (never silent).

        Returns the full envelope when it already fits. Otherwise large lists
        are capped with an explicit ``__truncated__`` marker that records the
        original count, and only as a last resort is the payload reduced to
        run identity + coverage + errors with the trimmed top-level keys
        listed. The agent's internal ``_current_envelope`` is never mutated.
        """
        if self._current_envelope is None:
            return "{}"
        roof = max_chars or self._MAX_LLM_ENVELOPE_CHARS
        payload = self._current_envelope
        rendered = json.dumps(payload, default=str)
        if len(rendered) <= roof:
            return rendered

        def _cap(value: Any, max_items: int) -> Any:
            if isinstance(value, dict):
                return {k: _cap(v, max_items) for k, v in value.items()}
            if isinstance(value, list):
                if len(value) > max_items:
                    return {
                        "__truncated__": True,
                        "count": len(value),
                        "items": [_cap(item, max_items) for item in value[:max_items]],
                    }
                return [_cap(item, max_items) for item in value]
            return value

        rendered = json.dumps(_cap(payload, self._LIST_CAP), default=str)
        if len(rendered) <= roof:
            return rendered

        kept = {
            k: payload.get(k)
            for k in ("run_id", "symbol", "status", "schema_version",
                      "generated_at", "completed_at", "data_source")
        }
        kept["coverage"] = payload.get("coverage")
        kept["errors"] = payload.get("errors")
        kept["_trimmed_keys"] = sorted((payload.get("canonical_state") or {}))
        return json.dumps(kept, default=str)

    def _envelope_schema(self) -> str:
        """Return the envelope schema as a context block for the LLM."""
        return _ENVELOPE_SCHEMA

    def _envelope_summary(self) -> str:
        """Return a compact summary of the current envelope for the LLM."""
        if self._current_envelope is None:
            return "{}"
        envelope = self._current_envelope
        return json.dumps(
            {
                "run_id": envelope.get("run_id"),
                "symbol": envelope.get("symbol"),
                "status": envelope.get("status"),
                "coverage": envelope.get("coverage", {}),
                "errors": envelope.get("errors", []),
                "data_keys": sorted(
                    envelope.get("canonical_state", {}).get("data-access", {}).get("evidence", {}).get("spot", {}).keys()
                ) if envelope.get("canonical_state", {}).get("data-access", {}).get("evidence", {}).get("spot") else [],
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Specialist agents
# ---------------------------------------------------------------------------


class DeltaOrderflowAgent(MarketAnalyst):
    """Reason about delta, trades, CVD, OBI, VWAP, and book pressure."""

    remit = "Interpret orderflow and delta evidence without inventing missing values."

    @strategy(
        PredictStrategy(),
        context={
            "task": (
                "You are a delta/orderflow analyst. Extract directional evidence "
                "from the canonical envelope. Never invent values. State 'null' "
                "when data is unavailable. Cite exact envelope paths for every claim."
            ),
            "output_format": (
                "Return a JSON object with:\n"
                "  summary: 2-3 sentence directional assessment\n"
                "  evidence: [{\"path\": \"envelope path\", \"value\": ..., \"interpretation\": \"...\"}]\n"
                "  confidence: low|medium|high\n"
                "  limitations: [\"...\"]\n"
                "  null_fields: [\"envelope paths that were null\"]"
            ),
            "envelope_schema": DynamicContext("self._envelope_schema()"),
            "envelope": DynamicContext("self._bounded_envelope()"),
        },
    )
    async def assess(
        self,
        envelope: Annotated[
            dict[str, Any],
            spec(
                description=(
                    "Complete MarketRunEnvelope. Key paths for orderflow:\n"
                    "  canonical_state.data-access.evidence.spot.order_book\n"
                    "  canonical_state.data-access.evidence.futures.order_book\n"
                    "  canonical_state.data-access.evidence.spot.trades_raw\n"
                    "  canonical_state.data-access.evidence.futures.trades_raw\n"
                    "  canonical_state.calculations.calculations.flow\n"
                    "  canonical_state.calculations.calculations.bucketed_cvd\n"
                    "  canonical_state.calculations.calculations.orderbook\n"
                    "  canonical_state.analysis.analysis.demand.decomposition"
                )
            ),
        ],
    ) -> str:
        """Assess directional orderflow, delta, and order-book evidence in the envelope."""
        ...


class MacroAgent(MarketAnalyst):
    """Reason about funding, broader market context, and macro conditions."""

    remit = "Connect market context to the observed symbol while separating evidence from inference."

    @strategy(
        PredictStrategy(),
        context={
            "task": (
                "You are a macro/context analyst. Assess funding conditions, "
                "broader market context, and regime signals. Separate evidence "
                "from inference. Cite exact envelope paths."
            ),
            "output_format": (
                "Return a JSON object with:\n"
                "  summary: 2-3 sentence macro assessment\n"
                "  evidence: [{\"path\": \"...\", \"value\": ..., \"interpretation\": \"...\"}]\n"
                "  confidence: low|medium|high\n"
                "  limitations: [\"...\"]"
            ),
            "envelope_schema": DynamicContext("self._envelope_schema()"),
            "envelope": DynamicContext("self._bounded_envelope()"),
        },
    )
    async def assess(
        self,
        envelope: Annotated[
            dict[str, Any],
            spec(
                description=(
                    "Complete MarketRunEnvelope. Key paths for macro:\n"
                    "  canonical_state.data-access.evidence.futures.funding\n"
                    "  canonical_state.data-access.evidence.futures.ticker_24h\n"
                    "  canonical_state.data-access.evidence.spot.klines\n"
                    "  canonical_state.calculations.calculations.technical\n"
                    "  canonical_state.analysis.analysis.regime\n"
                    "  canonical_state.analysis.analysis.stage"
                )
            ),
        ],
    ) -> str:
        """Assess funding, context, and macro evidence present in the envelope."""
        ...


class OpenInterestAgent(MarketAnalyst):
    """Reason about open interest, positioning, and changes in participation."""

    remit = "Interpret open-interest evidence and state what it cannot establish."

    @strategy(
        PredictStrategy(),
        context={
            "task": (
                "You are an open-interest analyst. Assess OI state, change, and "
                "positioning evidence. State what you cannot establish from the "
                "available data. Cite exact envelope paths."
            ),
            "output_format": (
                "Return a JSON object with:\n"
                "  summary: 2-3 sentence OI assessment\n"
                "  evidence: [{\"path\": \"...\", \"value\": ..., \"interpretation\": \"...\"}]\n"
                "  confidence: low|medium|high\n"
                "  limitations: [\"...\"]\n"
                "  cannot_establish: [\"what the data cannot tell us\"]"
            ),
            "envelope_schema": DynamicContext("self._envelope_schema()"),
            "envelope": DynamicContext("self._bounded_envelope()"),
        },
    )
    async def assess(
        self,
        envelope: Annotated[
            dict[str, Any],
            spec(
                description=(
                    "Complete MarketRunEnvelope. Key paths for OI:\n"
                    "  canonical_state.data-access.evidence.futures.open_interest\n"
                    "  canonical_state.data-access.evidence.futures.ticker_24h\n"
                    "  canonical_state.calculations.calculations.flow\n"
                    "  canonical_state.analysis.analysis.demand.decomposition"
                )
            ),
        ],
    ) -> str:
        """Assess open-interest state and positioning evidence in the envelope."""
        ...


class LiquidationAgent(MarketAnalyst):
    """Reason about liquidation pressure and forced-flow evidence."""

    remit = "Interpret liquidation evidence and preserve explicit data limitations."

    @strategy(
        PredictStrategy(),
        context={
            "task": (
                "You are a liquidation analyst. Assess liquidation-pressure "
                "evidence from the available data. Note that liquidation data "
                "is often incomplete — state what is missing clearly. "
                "Cite exact envelope paths."
            ),
            "output_format": (
                "Return a JSON object with:\n"
                "  summary: 2-3 sentence liquidation assessment\n"
                "  evidence: [{\"path\": \"...\", \"value\": ..., \"interpretation\": \"...\"}]\n"
                "  confidence: low|medium|high\n"
                "  limitations: [\"...\"]\n"
                "  missing_data: [\"what liquidation data is absent\"]"
            ),
            "envelope_schema": DynamicContext("self._envelope_schema()"),
            "envelope": DynamicContext("self._bounded_envelope()"),
        },
    )
    async def assess(
        self,
        envelope: Annotated[
            dict[str, Any],
            spec(
                description=(
                    "Complete MarketRunEnvelope. Key paths for liquidations:\n"
                    "  canonical_state.data-access.evidence.futures.order_book\n"
                    "  canonical_state.data-access.evidence.futures.trades_raw\n"
                    "  canonical_state.calculations.calculations.orderbook\n"
                    "  canonical_state.analysis.analysis.wall_migration\n"
                    "  canonical_state.analysis.analysis.path_absorption\n"
                    "  Note: exchange liquidation feed is not available; infer from book pressure"
                )
            ),
        ],
    ) -> str:
        """Assess liquidation pressure and related evidence in the envelope."""
        ...


# ---------------------------------------------------------------------------
# Controller agent
# ---------------------------------------------------------------------------


class ControllerAgent(MarketAnalyst):
    """Synthesize specialist reasoning into one analyst narrative."""

    remit = "Reconcile specialist views against the canonical envelope and state uncertainty clearly."

    # TODO(nooa-security): CodeActStrategy lets the model emit Python that
    # executes inside the harness container. This is acceptable while the
    # only envelope-side capability is the read-only MarketRunEnvelope,
    # but the doctrine explicitly flags the container boundary as
    # security-critical before live credentials or broader capabilities
    # are exposed. Replace with a non-code-executing strategy (e.g.
    # PredictStrategy + strict JSON-only response contract) or sandbox
    # generated code before any capability expansion lands.

    @strategy(
        CodeActStrategy(),
        context={
            "task": (
                "You are the controller analyst. You receive 4 specialist reports "
                "(delta_orderflow, macro, open_interest, liquidations) and the "
                "canonical envelope. Your job:\n"
                "1. Cross-reference each specialist claim against the envelope\n"
                "2. Identify contradictions between specialists\n"
                "3. Identify areas of consensus\n"
                "4. Produce a narrative briefing that separates evidence from inference\n"
                "5. State uncertainty explicitly — never pretend confidence"
            ),
            "output_format": (
                "Return a JSON object with:\n"
                "  narrative: string (2-3 paragraphs synthesizing all views)\n"
                "  consensus: {direction: string, confidence: low|medium|high}\n"
                "  disagreements: [{topic, specialist_a, specialist_b, resolution}]\n"
                "  key_evidence: [{run_id, path, claim}]\n"
                "  limitations: [string]\n"
                "  uncertainty_sources: [string]\n"
                "The code you generate should read the envelope and specialist "
                "reports, then produce the JSON above."
            ),
            "envelope_schema": DynamicContext("self._envelope_schema()"),
            "specialist_reports": DynamicContext("json.dumps(self._specialist_reports, indent=2)"),
            "envelope_summary": DynamicContext("self._envelope_summary()"),
        },
    )
    async def synthesize(
        self,
        envelope: Annotated[
            dict[str, Any],
            spec(
                description=(
                    "Complete MarketRunEnvelope. The controller cross-references "
                    "specialist claims against this envelope."
                )
            ),
        ],
        specialist_reports: Annotated[
            dict[str, str],
            spec(
                description=(
                    "Specialist assessment reports keyed by specialist name: "
                    "delta_orderflow, macro, open_interest, liquidations"
                )
            ),
        ],
    ) -> str:
        """Produce a data-driven narrative from the envelope and specialist reports."""
        ...


__all__ = [
    "ControllerAgent",
    "DeltaOrderflowAgent",
    "LiquidationAgent",
    "MacroAgent",
    "MarketAnalyst",
    "OpenInterestAgent",
]