"""Pydantic schemas for narration turns + the parse-failure exception.

Semantic authority: STRUCTURE-of-the-narration-contract. This module owns
the wire-format of one LLM turn (the shape every other module reads and
writes). It does NOT know how to extract one from raw text (that lives in
``narration.py``) or how to call the model (that lives in ``llm.py``).

The optional pydantic import is preserved: when the SDK is absent the
narrator falls back to raw JSON, and the schema names become ``None`` so
callers can branch on availability without try/except at every call site.
"""

from __future__ import annotations

from typing import Any

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
        phase: str = "P1"
        tool_calls: list[NarrationToolCall] = Field(default_factory=list)
        memory_proposals: list[dict[str, Any]] | None = None
else:
    NarrationToolCall = None  # type: ignore[assignment]
    NarrationTurn = None  # type: ignore[assignment]


class NarrationParseError(ValueError):
    """Narration output was not parseable into the required JSON contract."""


__all__ = [
    "NarrationToolCall",
    "NarrationTurn",
    "NarrationParseError",
]
