"""NOOA analyst agents — the model interface loaded inside the harness.

These classes are the first agentic layer above the canonical harness. They
do not pull exchange data, calculate indicators, write Redis, or place
trades. The existing harness supplies one complete canonical envelope to the
suite; NOOA supplies the reasoning over that envelope.

> ***Note (repurposing):*** these agent classes are being repurposed into
> deterministic calculation/analysis objects that pull from the poller-fed
> Redis stream. They remain python objects inside ``market_service/nooa_harness/``
> and are never the CLI mount surface — terminal-based agents (pi, hermes,
> claude code) reach the system through ``harness.py``, never these classes
> directly.

Specialists build their own prompt and call the model exactly once
(``self._llm.acall``) — no framework-level validation-retry loop. The raw
text is returned and parsed by ``SpecialistReport.from_llm_text``, which
tolerates the ``thinking`` preamble and ```json``` fences used by
reasoning-model gateways. The controller uses the same non-code-executing
path (``_call_model_once`` + strict JSON-only contract) so the model never
emits executable Python inside the harness container.

The envelope handed to the model is always the bounded LLM view
(``bounded_envelope_view``); the complete canonical envelope stays immutable
inside the runtime stores.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from nooa import Agent, spec
from nooa.agentdoc import hidden

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
# Bounded LLM view + response text helpers (shared by agents and the CLI)
# ---------------------------------------------------------------------------


def bounded_envelope_view(
    payload: dict[str, Any] | None,
    roof: int = 180_000,
    list_cap: int = 60,
) -> str:
    """Serialized envelope sized for the LLM param limit (never silent).

    Returns the full envelope when it already fits. Otherwise large lists
    are capped with an explicit ``__truncated__`` marker that records the
    original count, and only as a last resort is the payload reduced to run
    identity + coverage + errors with the trimmed top-level keys listed. The
    complete canonical envelope is never mutated — this is only the LLM-bound
    projection.
    """
    if not payload:
        return "{}"
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

    rendered = json.dumps(_cap(payload, list_cap), default=str)
    if len(rendered) <= roof:
        return rendered

    kept = {
        k: payload.get(k)
        for k in ("run_id", "symbol", "status", "schema_version",
                  "generated_at", "completed_at", "data_source")
    }
    kept["coverage"] = payload.get("coverage")
    kept["errors"] = payload.get("errors")
    kept["_trimmed_keys"] = sorted(payload.get("canonical_state") or {})
    return json.dumps(kept, default=str)


def response_text(resp: Any) -> str:
    """Extract the text content from a NOOA unified-LLM response."""
    raw = getattr(resp, "raw_response", resp)
    choices = getattr(raw, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if parts:
                return "".join(parts)
    if isinstance(resp, str):
        return resp
    return str(raw)


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

    # Internal state — hidden from the LLM
    _current_envelope: Annotated[dict[str, Any] | None, hidden] = None
    _specialist_reports: Annotated[dict[str, str] | None, hidden] = None

    # Bounded LLM view limits. NOOA's PredictStrategy caps each strategy
    # parameter at ``max_param_chars=200_000``; we keep the envelope roof just
    # under that but materially higher than the old 60-item / 180k-chars view so
    # the analyst can see a richer, deduped window of the deeper order book and
    # trades and still leave headroom for the JSON report. Large arrays are still
    # explicitly marked ``__truncated__`` (never silently dropped).
    _MAX_LLM_ENVELOPE_CHARS = 190_000
    _LIST_CAP = 200
    # Generation budget: reasoning-model gateways consume tokens on a
    # `thinking` preface, then emit the JSON report — give it headroom so the
    # report is not truncated mid-evidence.
    _MAX_TOKENS = 4000

    def __init__(self, symbol: str, *, llm: Any):
        super().__init__(llm=llm)
        self.symbol = symbol.upper()

    def _bounded_envelope(self, max_chars: int | None = None) -> str:
        """Deprecated-style convenience: bounded LLM view of the current envelope."""
        if self._current_envelope is None:
            return "{}"
        return bounded_envelope_view(
            self._current_envelope,
            roof=max_chars or self._MAX_LLM_ENVELOPE_CHARS,
            list_cap=self._LIST_CAP,
        )

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

    async def _call_model_once(
        self,
        envelope_payload: dict[str, Any] | None,
        *,
        max_tokens: int | None = None,
        extra_context: str | None = None,
    ) -> str:
        """One deterministic LLM call (no validation-retry loop).

        Builds the system (remit + task) and user (format + optional extra
        context + schema + bounded envelope) prompts and calls the configured
        model client once. The raw text is returned as-is; parsing happens in
        ``SpecialistReport.from_llm_text`` which extracts JSON from prose.
        """
        llm = getattr(self, "_llm", None)
        if llm is None:
            raise RuntimeError(f"{type(self).__name__}: no NOOA model client configured")
        payload = envelope_payload if envelope_payload is not None else self._current_envelope
        extra = f"{extra_context}\n\n" if extra_context else ""
        user = (
            f"{self.output_format_prompt}\n\n"
            f"{extra}"
            "Envelope schema (shape reference):\n"
            f"{self._envelope_schema()}\n\n"
            "Canonical envelope for this run (raw arrays bounded):\n"
            f"{bounded_envelope_view(payload, self._MAX_LLM_ENVELOPE_CHARS, self._LIST_CAP)}"
            "\n\nIMPORTANT: Output ONLY one valid JSON object matching the format. "
            "Allowed a brief reasoning line, but the JSON object must be present "
            "and complete. No markdown fences around it. Do not end early."
        )
        system = f"{self.remit}\n\n{self.task_prompt}"
        resp = await llm.acall(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens or self._MAX_TOKENS,
        )
        return response_text(resp)


# ---------------------------------------------------------------------------
# Specialist agents — one-shot raw-text assessment, parsed by the suite
# ---------------------------------------------------------------------------


class DeltaOrderflowAgent(MarketAnalyst):
    """Reason about delta, trades, CVD, OBI, VWAP, and book pressure."""

    remit = "Interpret orderflow and delta evidence without inventing missing values."
    task_prompt = (
        "You are a delta/orderflow analyst. Extract directional evidence "
        "from the canonical envelope. Never invent values. State 'null' "
        "when data is unavailable. Cite exact envelope paths for every claim."
    )
    output_format_prompt = (
        "Return a JSON object with:\n"
        "  summary: 2-3 sentence directional assessment\n"
        "  evidence: [{\"path\": \"envelope path\", \"value\": ..., \"interpretation\": \"what this means\", \"metric_name\": \"label\"}]\n"
        "  confidence: low|medium|high\n"
        "  limitations: [\"...\"]\n"
        "  null_fields: [\"envelope paths that were null\"]"
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
        """Assess directional orderflow, delta, and order-book evidence.

        Returns raw model text; the suite parses it via
        ``SpecialistReport.from_llm_text``.
        """
        return await self._call_model_once(envelope)


class MacroAgent(MarketAnalyst):
    """Reason about funding, broader market context, and macro conditions."""

    remit = "Connect market context to the observed symbol while separating evidence from inference."
    task_prompt = (
        "You are a macro/context analyst. Assess funding conditions, "
        "broader market context, and regime signals. Separate evidence "
        "from inference. Cite exact envelope paths."
    )
    output_format_prompt = (
        "Return a JSON object with:\n"
        "  summary: 2-3 sentence macro assessment\n"
        "  evidence: [{\"path\": \"...\", \"value\": ..., \"interpretation\": \"what this means\", \"metric_name\": \"label\"}]\n"
        "  confidence: low|medium|high\n"
        "  limitations: [\"...\"]"
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
        return await self._call_model_once(envelope)


class OpenInterestAgent(MarketAnalyst):
    """Reason about open interest, positioning, and changes in participation."""

    remit = "Interpret open-interest evidence and state what it cannot establish."
    task_prompt = (
        "You are an open-interest analyst. Assess OI state, change, and "
        "positioning evidence. State what you cannot establish from the "
        "available data. Cite exact envelope paths."
    )
    output_format_prompt = (
        "Return a JSON object with:\n"
        "  summary: 2-3 sentence OI assessment\n"
        "  evidence: [{\"path\": \"...\", \"value\": ..., \"interpretation\": \"what this means\", \"metric_name\": \"label\"}]\n"
        "  confidence: low|medium|high\n"
        "  limitations: [\"...\"]\n"
        "  cannot_establish: [\"what the data cannot tell us\"]"
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
                    "  canonical_state.analysis.analysis.demand.decomposition"                    "  canonical_state.analysis.analysis.demand.decomposition"
                )
            ),
        ],
    ) -> str:
        """Assess open-interest state and positioning evidence in the envelope."""
        return await self._call_model_once(envelope)


class LiquidationAgent(MarketAnalyst):
    """Reason about liquidation pressure and forced-flow evidence."""

    remit = "Interpret liquidation evidence and preserve explicit data limitations."
    task_prompt = (
        "You are a liquidation analyst. Assess liquidation-pressure "
        "evidence from the available data. Note that liquidation data "
        "is often incomplete — state what is missing clearly. "
        "Cite exact envelope paths."
    )
    output_format_prompt = (
        "Return a JSON object with:\n"
        "  summary: 2-3 sentence liquidation assessment\n"
        "  evidence: [{\"path\": \"...\", \"value\": ..., \"interpretation\": \"what this means\", \"metric_name\": \"label\"}]\n"
        "  confidence: low|medium|high\n"
        "  limitations: [\"...\"]\n"
        "  missing_data: [\"what liquidation data is absent\"]"
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
        return await self._call_model_once(envelope)


# ---------------------------------------------------------------------------
# Pass-3 microstructure interpretation agent (read-only over fitted evidence)
# ---------------------------------------------------------------------------

_MICROSTRUCTURE_EVIDENCE_SCHEMA = json.dumps(
    {
        "schema_version": 1,
        "symbol": "BTCUSDT",
        "venue": "spot",
        "evidence_id": "ev-<hash-prefix>",
        "input_hash": "<sha256>",
        "model_version": "ofi-depth-v1",
        "interval_seconds": 10,
        "window_start_ms": "<int>",
        "window_end_ms": "<int>",
        "tick_size": "<decimal>",
        "depth_estimator": "event_mean_best_bid_ask_v1",
        "price_impact_fit": {
            "fit_id": "beta-<hash>",
            "alpha": "<decimal>",
            "beta": "<decimal>",
            "stderr_beta": "<decimal|null>",
            "r2": "<decimal|null>",
            "n_observations": "<int>",
            "excluded_observations": "<int>",
            "heteroskedasticity_flag": "<bool>",
            "mean_ad": "<decimal|null>",
            "price_unit": "ticks",
            "status": "validated|provisional|insufficient",
        },
        "sensitivity_fit": "same shape as price_impact_fit, OFI recomputed without price-changing events, or null",
        "depth_scaling_fit": {
            "fit_id": "depth-<hash>",
            "c": "<decimal|null>",
            "lambda": "<decimal|null>",
            "stderr_lambda": "<decimal|null>",
            "n_blocks": "<int>",
            "r2": "<decimal|null>",
            "fit_ids": ["beta-<hash>", "..."],
            "status": "validated|provisional|insufficient",
        },
        "coverage": "object: events/intervals captured, gaps, reconnects",
        "status": "validated|provisional|insufficient",
    },
    indent=2,
)


class MicrostructureInterpretationAgent(MarketAnalyst):
    """Read-only interpreter of immutable MicrostructureEvidence.

    Receives BOTH fitted models — the empirical price-impact fit
    (ΔP_k = α + β·OFI_k) and the depth-scaling fit (β = c·AD^-λ) — each with
    independent diagnostics, and explains fit quality, sign, magnitude and
    limitations. The two models are NEVER merged into a single point
    prediction: the substituted combined expression
    ΔP = α + c·OFI/AD^λ + (ν·OFI + ε) carries a heteroskedastic ν·OFI term,
    so it is a derived diagnostic at most.

    Authority: this agent never recomputes OFI, never refits β/c/λ, never
    opens Binance, never reconstructs the book, and never overrides a
    deterministic status. It cites evidence paths and states what the data
    cannot establish.
    """

    remit = (
        "Interpret fitted microstructure evidence without recomputing any "
        "value. The deterministic fitter is the only producer of coefficients."
    )
    task_prompt = (
        "You are a microstructure analyst. You receive one immutable "
        "MicrostructureEvidence object containing two SEPARATE fitted models: "
        "(1) price_impact_fit, an OLS of ΔP_k = alpha + beta*OFI_k over one "
        "estimation block, and (2) depth_scaling_fit, a log-log fit of "
        "beta_i = c * AD_i^-lambda across blocks. Interpret each model's sign, "
        "magnitude, fit quality (r2, stderr, n_observations) and status "
        "(validated/provisional/insufficient) independently. NEVER combine them "
        "into a single price prediction — the combined expression carries a "
        "heteroskedastic nu*OFI interaction term and is at most a derived "
        "diagnostic. When sensitivity_fit is present, compare it to the primary "
        "fit (it excludes price-changing events; divergence flags the paper's "
        "tautology caveat). Respect the deterministic status: never treat a "
        "provisional or insufficient fit as a trading signal. Never recompute "
        "OFI, beta, c, or lambda. Cite exact evidence paths for every claim."
    )
    output_format_prompt = (
        "Return a JSON object with:\n"
        "  summary: 2-3 sentence interpretation of the fitted models\n"
        "  evidence: [{\"path\": \"evidence path\", \"value\": ..., "
        "\"interpretation\": \"what this means\", \"metric_name\": \"label\"}]\n"
        "  confidence: low|medium|high\n"
        "  limitations: [\"...\"]\n"
        "  model_separation: one sentence on why the two fits are read separately"
    )

    def _envelope_schema(self) -> str:
        """Shape reference for MicrostructureEvidence (not the market envelope)."""
        return _MICROSTRUCTURE_EVIDENCE_SCHEMA

    @staticmethod
    def _evidence_from_envelope(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
        """Locate microstructure evidence inside a canonical envelope view.

        Pass-3 routes evidence directly today; once the envelope carries a
        versioned ``microstructure`` domain this resolver finds it there.
        """
        if not isinstance(envelope, dict):
            return None
        for key in ("microstructure", "microstructure_evidence"):
            candidate = envelope.get(key)
            if isinstance(candidate, dict):
                return candidate
        canonical = envelope.get("canonical_state")
        if isinstance(canonical, dict):
            for key in ("microstructure", "microstructure_evidence"):
                candidate = canonical.get(key)
                if isinstance(candidate, dict):
                    return candidate
        return None

    async def assess(
        self,
        evidence: Annotated[
            dict[str, Any],
            spec(
                description=(
                    "One immutable MicrostructureEvidence object (or a canonical "
                    "envelope containing one under a microstructure key). Paths: "
                    "price_impact_fit.{beta,alpha,r2,status}, "
                    "sensitivity_fit.*, depth_scaling_fit.{c,lambda,status}, "
                    "coverage.*, status"
                )
            ),
        ],
    ) -> str:
        """Interpret one evidence object; deterministic unavailable when absent.

        When no evidence is present this returns a parseable unavailable report
        WITHOUT a model call (null discipline — no LLM call to state absence).
        """
        payload = evidence if isinstance(evidence, dict) else None
        resolved = payload
        if resolved is None or "evidence_id" not in resolved:
            resolved = self._evidence_from_envelope(payload)
        if resolved is None:
            return json.dumps({
                "summary": "Microstructure evidence unavailable for this run.",
                "evidence": [],
                "confidence": "low",
                "limitations": ["no MicrostructureEvidence persisted or present in the envelope"],
                "null_fields": ["microstructure"],
            })
        return await self._call_model_once(
            resolved,
            extra_context=(
                "The payload below is ONE MicrostructureEvidence object, not the "
                "market envelope. Both fitted models carry deterministic status "
                "fields that you must respect."
            ),
        )


# ---------------------------------------------------------------------------
# Controller agent
# ---------------------------------------------------------------------------


class ControllerAgent(MarketAnalyst):
    """Synthesize specialist reasoning into one analyst narrative."""

    remit = "Reconcile specialist views against the canonical envelope and state uncertainty clearly."

    # SECURITY (resolved): the controller previously used CodeActStrategy,
    # which let the model emit Python that executed inside the harness
    # container — an arbitrary code-execution surface inside a boundary that
    # holds credentials and Postgres/Redis network access. It now follows the
    # same non-code-executing, JSON-only path as the specialists: one
    # deterministic LLM call returning raw JSON text, parsed by
    # AnalystBriefing.from_controller_text. No model-supplied code is ever
    # executed. Local behavior is unchanged — same inputs (bounded envelope +
    # specialist reports) and the same JSON output contract.
    task_prompt = (
        "You are the controller analyst. You receive 4 specialist reports "
        "(delta_orderflow, macro, open_interest, liquidations) and the "
        "canonical envelope. Your job:\n"
        "1. Cross-reference each specialist claim against the envelope\n"
        "2. Identify contradictions between specialists\n"
        "3. Identify areas of consensus\n"
        "4. Produce a narrative briefing that separates evidence from inference\n"
        "5. State uncertainty explicitly — never pretend confidence"
    )
    output_format_prompt = (
        "Return a JSON object with:\n"
        "  narrative: string (2-3 paragraphs synthesizing all views)\n"
        "  consensus: {\n"
        "    direction: string (e.g. bullish, bearish, neutral)\n"
        "    confidence: low|medium|high\n"
        "    confidence_score: number 0.0-1.0 (your numeric confidence)\n"
        "    timeframe: string (e.g. intraday, session, swing)\n"
        "    magnitude: string (e.g. marginal, moderate, strong)\n"
        "  }\n"
        "  disagreements: [{topic, specialist_a, specialist_b, resolution}]\n"
        "  key_evidence: [{path, claim, value, specialist}]\n"
        "  limitations: [string]\n"
        "  uncertainty_sources: [string]\n"
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
        """Produce a data-driven narrative from the envelope and specialist reports.

        Non-code-executing path (SECURITY): one deterministic LLM call with a
        strict JSON-only response contract, exactly like the specialists.
        Returns raw JSON text; the suite parses it via
        ``AnalystBriefing.from_controller_text``.
        """
        reports_block = "Specialist reports:\n" + json.dumps(
            specialist_reports or {}, default=str, indent=2,
        )
        return await self._call_model_once(
            envelope,
            extra_context=reports_block,
        )


__all__ = [
    "ControllerAgent",
    "DeltaOrderflowAgent",
    "LiquidationAgent",
    "MacroAgent",
    "MarketAnalyst",
    "OpenInterestAgent",
]
