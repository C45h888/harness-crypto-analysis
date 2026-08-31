"""NOOA narration support — bounded views and the microstructure reader.

HISTORICAL NOTE (inference-engine pass): the old interpretation plane —
``MarketAnalyst`` specialists (delta/macro/OI/liquidations),
``ControllerAgent``, and the ``AnalystSuite`` that ran them — was DELETED.
The primary interpretation plane is now the statistical inference engine
(``market_service/nooa_harness/engine.InferenceEngine``), which narrates its
own deterministic state via one bounded LLM call through the runner.

Kept here because the engine's tool stack and the Pass-3 microstructure
reader still use them:

- ``bounded_envelope_view`` / ``response_text`` — LLM-view helpers shared by
  engine code and the CLI.
- ``MicrostructureInterpretationAgent`` — the Pass-3 read-only interpreter
  for one persisted MicrostructureEvidence (used by
  ``nooa market microstructure interpret``). Slated for absorption into the
  engine's narration path in a follow-up cleanup.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from nooa import Agent, spec
from nooa.agentdoc import hidden


# ---------------------------------------------------------------------------
# Bounded LLM view + response text helpers (shared by engine and the CLI)
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
    from market_service.runtime.read_paths import json_safe_dumps

    if not payload:
        return "{}"
    rendered = json_safe_dumps(payload)
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

    rendered = json_safe_dumps(_cap(payload, list_cap))
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
    return json_safe_dumps(kept)


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


class MicrostructureInterpretationAgent(Agent):
    """Read-only interpreter of one persisted MicrostructureEvidence.

    RECEIVES BOTH fitted models — the empirical price-impact fit
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

    NOTE (inference-engine pass): the primary interpretation plane is now
    ``InferenceEngine``; this agent remains the dedicated reader for the
    ``nooa market microstructure interpret`` command.
    """

    symbol: Annotated[str, spec(description="Trading pair, e.g. BTCUSDT")] = "BTCUSDT"
    remit: Annotated[
        str,
        spec(description="What this agent is responsible for analyzing"),
    ] = (
        "Interpret fitted microstructure evidence without recomputing any "
        "value. The deterministic fitter is the only producer of coefficients."
    )

    # Internal state — hidden from the LLM
    _current_envelope: Annotated[dict[str, Any] | None, hidden] = None

    _MAX_LLM_ENVELOPE_CHARS = 190_000
    _LIST_CAP = 200
    _MAX_TOKENS = 4000

    def __init__(self, symbol: str, *, llm: Any):
        super().__init__(llm=llm)
        self.symbol = symbol.upper()

    def _envelope_schema(self) -> str:
        """Shape reference for MicrostructureEvidence (not the market envelope)."""
        return _MICROSTRUCTURE_EVIDENCE_SCHEMA

    @staticmethod
    def _evidence_from_envelope(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
        """Locate microstructure evidence inside a canonical envelope view."""
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
        return await self._call_model_once(resolved)

    async def _call_model_once(
        self,
        payload: dict[str, Any] | None,
        *,
        max_tokens: int | None = None,
        extra_context: str | None = None,
    ) -> str:
        """One deterministic LLM call over the evidence payload."""
        llm = getattr(self, "_llm", None)
        if llm is None:
            raise RuntimeError(
                f"{type(self).__name__}: no NOOA model client configured"
            )
        extra = f"{extra_context}\n\n" if extra_context else ""
        user = (
            "Return ONLY one valid JSON object: {summary, evidence: "
            "[{path, value, interpretation, metric_name}], confidence, "
            "limitations, model_separation}.\n\n"
            f"{extra}"
            "Evidence schema (shape reference):\n"
            f"{self._envelope_schema()}\n\n"
            "MicrostructureEvidence for this run:\n"
            f"{bounded_envelope_view(payload, self._MAX_LLM_ENVELOPE_CHARS, self._LIST_CAP)}"
            "\n\nIMPORTANT: Output ONLY one valid JSON object. No markdown fences."
        )
        system = self.remit
        resp = await llm.acall(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens or self._MAX_TOKENS,
        )
        return response_text(resp)


__all__ = [
    "MicrostructureInterpretationAgent",
    "bounded_envelope_view",
    "response_text",
]
