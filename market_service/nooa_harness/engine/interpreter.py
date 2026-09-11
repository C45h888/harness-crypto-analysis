"""MicrostructureInterpretationAgent — read-only interpreter of one persisted evidence.

Semantic authority: ONE-SHOT READER OVER A PERSISTED EVIDENCE. This is the
Pass-3 read-only reader used by the ``nooa market microstructure interpret``
CLI surface. Distinct from ``InferenceEngine`` in every way that matters:

  - No staged loop, no wake, no memory proposals, no phase coverage.
  - One deterministic LLM call over the bounded evidence payload.
  - Lives in the inference plane (engine package) because the
    decomposition pass absorbed it from the retired ``agents`` module;
    it does NOT share state with ``InferenceEngine``.

The agent RECEIVES BOTH fitted models — the empirical price-impact fit
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

The LLM client is INJECTED exactly like ``InferenceEngine`` — no NOOA
``Agent`` base, no framework import at module load.
"""

from __future__ import annotations

import json
from typing import Any

from .llm import bounded_envelope_view, response_text


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


class MicrostructureInterpretationAgent:
    """Read-only interpreter of one persisted MicrostructureEvidence."""

    remit: str = (
        "Interpret fitted microstructure evidence without recomputing any "
        "value. The deterministic fitter is the only producer of coefficients."
    )

    _MAX_LLM_ENVELOPE_CHARS = 190_000
    _LIST_CAP = 200
    _MAX_TOKENS = 4000

    def __init__(self, symbol: str, *, llm: Any):
        self.llm = llm
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

    async def assess(self, evidence: dict[str, Any]) -> str:
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
        if self.llm is None:
            raise RuntimeError(
                f"{type(self).__name__}: no NOOA model client configured"
            )
        extra = f"{extra_context}\n\n" if extra_context else ""
        user = (
            "Return ONLY one valid JSON object with fields: "
            "{summary, evidence: "
            "[{path, value, interpretation, metric_name}], confidence, "
            "limitations, model_separation}.\n"
            "confidence MUST be EXACTLY one of: \"low\", \"medium\", \"high\" "
            "(NOT the evidence status like provisional/insufficient).\n\n"
            f"{extra}"
            "Evidence schema (shape reference):\n"
            f"{self._envelope_schema()}\n\n"
            "MicrostructureEvidence for this run:\n"
            f"{bounded_envelope_view(payload, self._MAX_LLM_ENVELOPE_CHARS, self._LIST_CAP)}"
            "\n\nIMPORTANT: Output ONLY one valid JSON object. No markdown fences."
        )
        system = self.remit
        resp = await self.llm.acall(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens or self._MAX_TOKENS,
        )
        return response_text(resp)


__all__ = ["MicrostructureInterpretationAgent"]
