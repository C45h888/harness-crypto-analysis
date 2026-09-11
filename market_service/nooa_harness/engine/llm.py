"""LLM transport — calling the model and unwrapping what came back.

Semantic authority: HOW WE TALK TO THE LLM. This module owns the
transport-specific concerns: structured-output probing, the 4-tier
content extraction ladder, and the bounded envelope viewer used to
squeeze a possibly-large evidence payload into the model's param limit.

The split:

  - ``call_narration_llm(llm, system_prompt, user_prompt)`` — one narration
    call. Tries ``output_model=NarrationTurn`` first (schema rejects nameless
    tool_calls at the source); on backend refusal, sets the structured probe
    flag to ``False`` for the rest of the process and falls back to raw
    JSON. Returns the raw text regardless — ``narration.py`` owns parsing.
  - ``bounded_envelope_view(payload, roof, list_cap)`` — serialises a
    possibly-large dict into an LLM-bound projection, capping large lists
    with an explicit ``__truncated__`` marker and, as a last resort, the
    run-identity + coverage + errors block.
  - ``response_text(resp)`` — extracts the text content from a NOOA
    unified-LLM response (covers the four transport shapes: parsed
    content, ``.content`` string, ``.content`` list of parts, raw
    ``choices[0].message.content``).
  - Token-budget resolver lives in ``config.py`` (single source of truth
    for budgets).

The structured-probe flag is read+written here; ``config.py`` declares
it because every module that wants to consult it needs to import from
``config``, but the mutation lives at the only call site that knows
the backend behaviour.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import (
    AGENTIC_MAX_TOOL_ROUNDS,
    _STRUCTURED_OK,
    narration_max_tokens,
)
from .schemas import NarrationParseError, NarrationTurn

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover — narration falls back to raw JSON
    BaseModel = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


async def call_narration_llm(
    llm: Any,
    user_prompt: str,
    *,
    system_prompt: str | None = None,
) -> str:
    """One narration LLM call — structured probe + 4-tier extraction.

    The structured attempt is paid only while the process-level probe flag
    allows it; once a backend refuses ``output_model=NarrationTurn``, the
    flag flips to ``False`` and we never re-pay the failed attempt for the
    rest of the process.

    ``system_prompt`` is the caller's cycle-scoped rendered prompt (the
    engine builds it once per cycle via ``kb.SystemPromptCache``). When
    omitted it is rendered here, which keeps the direct-call shape working
    but re-reads the KB payload on every call — prefer passing it.

    Extraction ladder (in order):
      0. Structured-model surface — already validated, serialise to JSON.
      1. ``.content`` is a non-empty string → use it.
      2. ``.content`` is a list of ``{"text": ...}`` parts → concat.
      3. Reasoning-model fallback — ``.reasoning`` parks the final JSON
         when the generation budget ran out in the preamble.
      4. Legacy raw-transport extraction — direct litellm/OpenAI shapes.
    """
    if llm is None:
        raise NarrationParseError("no LLM client configured for narration")
    if system_prompt is None:
        # Direct-call fallback (tests, ad-hoc surfaces): render on demand.
        from .kb import build_system_prompt

        system_prompt = build_system_prompt("BTCUSDT", "spot",
                                            AGENTIC_MAX_TOOL_ROUNDS)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # Structured attempt first (schema rejects nameless tool_calls at the
    # source); plain-JSON fallback preserves current behavior on backends
    # that refuse response_format. The probe flag avoids paying the failed
    # attempt on every turn once a backend has refused.
    global _STRUCTURED_OK  # noqa: PLW0603 — process-global, set once per backend
    response = None
    if NarrationTurn is not None and _STRUCTURED_OK is not False:
        try:
            response = await llm.acall(
                messages=messages,
                output_model=NarrationTurn,
                max_tokens=narration_max_tokens(),
            )
            _STRUCTURED_OK = True
        except Exception:
            log.warning("structured narration call refused; falling back to raw JSON")
            _STRUCTURED_OK = False
            response = None
    if response is None:
        response = await llm.acall(
            messages=messages,
            max_tokens=narration_max_tokens(),
        )
    # 0) Structured-model surface: already validated, serialize to JSON.
    content = getattr(response, "content", None)
    if BaseModel is not None and isinstance(content, BaseModel):
        return json.dumps(content.model_dump(), default=str)
    # 1) The client's parsed text surface (nooa LLMResponse.content) is the
    #    authoritative transport-agnostic extraction.
    content = getattr(response, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        if any(p.strip() for p in parts):
            return "".join(parts)
    # 2) Reasoning-model fallback: with a reasoning backend, the final JSON
    #    may be parked in ``reasoning`` when the generation budget ran out
    #    in the reasoning preamble before a content block was emitted.
    #    Extract from reasoning rather than serializing the raw response.
    reasoning = getattr(response, "reasoning", None)
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    # 3) Legacy raw-transport extraction (direct litellm/OpenAI shapes).
    raw = getattr(response, "raw_response", response)
    choices = getattr(raw, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if parts:
                return "".join(parts)
    if isinstance(response, str):
        return response
    return str(raw)


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
    kept["_trimmed_keys"] = sorted(payload.get("canonical_state") or {})
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


__all__ = [
    "call_narration_llm",
    "bounded_envelope_view",
    "response_text",
]
