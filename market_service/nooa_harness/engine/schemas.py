"""Pydantic schemas for narration turns + the parse-failure exception.

Semantic authority: STRUCTURE-of-the-narration-contract. This module owns
the wire-format of one LLM turn (the shape every other module reads and
writes). It knows how to extract one from raw text (``extract_json_object``) and
normalize it (``coerce_turn``), and how the contract reads — but never
how to call the model (that lives in ``llm.py``).

The optional pydantic import is preserved: when the SDK is absent the
narrator falls back to raw JSON, and the schema names become ``None`` so
callers can branch on availability without try/except at every call site.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover — narration falls back to raw JSON
    BaseModel = None  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]


if BaseModel is not None:
    class NarrationToolCall(BaseModel):
        """One validated tool dispatch request from a narration turn."""
        name: str
        args: dict[str, Any] = Field(default_factory=dict)

    class NarrationTurn(BaseModel):
        """Structured narration turn — enforced via output_model when supported.

        Interpretation fields are optional so a tool-request turn parses;
        tool_calls entries REQUIRE a non-empty name (nameless calls fail
        validation here instead of dying later as tool.unknown).
        """
        summary: str | None = None
        evidence: list[dict[str, Any]] | None = None
        confidence: str | None = None
        limitations: list[str] | None = None
        model_separation: str | None = None
        hypothesis: dict[str, Any] | None = None
        # Contract parity with kb.build_output_format: the structured probe
        # must be able to carry EVERY key the turn contract demands, or a
        # successful probe silently drops the scenario / P(T)P(S) / H0-test
        # payloads the validator reads (drift pinned by test).
        scenario: dict[str, Any] | None = None
        forward_scenario: dict[str, Any] | None = None
        hypothesis_evidence: dict[str, Any] | None = None
        phase: str = "P1"
        tool_calls: list[NarrationToolCall] = Field(default_factory=list)
        memory_proposals: list[dict[str, Any]] | None = None
else:
    NarrationToolCall = None  # type: ignore[assignment]
    NarrationTurn = None  # type: ignore[assignment]


class NarrationParseError(ValueError):
    """Narration output was not parseable into the required JSON contract."""


__all__ = [
    "coerce_turn",
    "extract_json_object",
    "NarrationToolCall",
    "NarrationTurn",
    "NarrationParseError",
]


# ---------------------------------------------------------------------------
# Wire-format unpacking — raw LLM text into the contract above.
# Moved from engine/narration.py (Pass-C redistribution); narration.py
# re-exports these names until its deletion pass.
# ---------------------------------------------------------------------------


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from LLM text (fenced or embedded)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # fenced block first
    if "```" in raw:
        for chunk in raw.split("```"):
            candidate = chunk.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
    # first brace-balanced object
    start = raw.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(raw)):
            if raw[idx] == "{":
                depth += 1
            elif raw[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start:idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
                    break
        start = raw.find("{", start + 1)
    return None



def coerce_turn(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize one narration turn: drop undispatchable tool_calls entries.

    Entries without a non-empty string ``name`` can never dispatch — drop
    them here (logged) instead of burning a tool round on tool.unknown.
    Raw-JSON fallbacks (structured output refused) frequently emit the key
    ``tool`` instead of ``name`` (proven live: a full 8-turn cycle with
    every call dropped); accept it as an alias, ``name`` winning on
    conflict. Missing/invalid ``args`` become {}. A non-dict hypothesis
    is wrapped.
    """
    if not isinstance(parsed, dict):
        return None
    calls = parsed.get("tool_calls")
    if calls is None:
        return parsed
    if not isinstance(calls, list):
        parsed["tool_calls"] = []
        return parsed
    kept: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if (not isinstance(name, str) or not name.strip()) and isinstance(
            call.get("tool"), str
        ) and call.get("tool").strip():
            name = call.get("tool")
            log.warning("coercing tool_call key 'tool' → 'name': %.120r", call)
        if not isinstance(name, str) or not name.strip():
            log.warning("dropping tool_call without a registry name: %.120r", call)
            continue
        args = call.get("args")
        kept.append({"name": name.strip(),
                     "args": dict(args) if isinstance(args, dict) else {}})
    parsed["tool_calls"] = kept
    hypothesis = parsed.get("hypothesis")
    if hypothesis is not None and not isinstance(hypothesis, dict):
        parsed["hypothesis"] = {"raw": hypothesis}
    return parsed


