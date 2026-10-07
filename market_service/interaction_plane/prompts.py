"""Model control layer — the designated system prompt for every CLI-loaded model.

Inner engine keeps its 34KB ``kb.py`` prompt. Outer models (Hermes / pi /
claude code via any CLI) get this short frozen prompt, delivered as the
``interaction`` stdout header on every ``--json`` response (see
``attach_header``). Same null / citation / refusal rules both sides.
"""

from __future__ import annotations

from typing import Any

from market_service.interaction_plane.budgets import INTERACTION_PROMPT_VERSION
from market_service.interaction_plane.segments import SEGMENTS

__all__ = [
    "PROMPT_VERSION",
    "REFUSAL_RULE",
    "WORKFLOW_RULE",
    "attach_header",
    "build_interaction_prompt",
]

PROMPT_VERSION = INTERACTION_PROMPT_VERSION

_ROLE = (
    "You are a read-only interpreter over ToolResult envelopes. "
    "You never recompute values, never open exchanges, never write state."
)

_NULL_RULE = (
    "NULL DISCIPLINE: null means not-provided — never substitute zero, "
    "never average over it, never drop the row silently. `null_fields` "
    "names what was not computed; cite it explicitly."
)

_CITATION_RULE = (
    "CITATION: every numeric claim cites `<tool> → data.<field>`. "
    "A claim without a path is a guess — refuse it."
)

REFUSAL_RULE = (
    "REFUSAL: unsupported horizons, missing planes (`available:false`), "
    "and refused tools are FINDINGS. Report verdict=unevaluable + reason, "
    "never invent numbers, never retry the same call."
)

_BUDGET_RULE = (
    "BUDGETS: you received a bounded view. `budget_receipt.truncated=true` "
    "means ask for inventory/compact, never `full`. `full` is human-only."
)

WORKFLOW_RULE = (
    "WORKFLOW: snapshot/compact first → inventory for coverage → one "
    "targeted read. No full dumps, no model-run parsing scripts — "
    "Python already parsed; you cite."
)


def build_interaction_prompt(segment: str, mode: str) -> str:
    """Frozen designated prompt for (segment, mode). Versioned, no I/O."""
    tools = ", ".join(SEGMENTS.get(segment, {}).get("tools", ()))
    lines = [
        f"INTERACTION PROMPT {PROMPT_VERSION} — segment={segment} mode={mode}.",
        _ROLE,
        _NULL_RULE,
        _CITATION_RULE,
        REFUSAL_RULE,
        _BUDGET_RULE,
        WORKFLOW_RULE,
        f"TOOLS IN THIS SEGMENT: {tools or '(none)'}." if tools else None,
        "Snapshot first, then correlate — a second call without a new question is thrash.",
    ]
    return "\n".join(line for line in lines if line)


def attach_header(
    result: dict[str, Any],
    *,
    segment: str,
    mode: str,
    source: str | None = None,
) -> dict[str, Any]:
    """Attach the ``interaction`` stdout header (prompt + routing receipt).

    Mutates nothing: returns a new dict with the header prepended.
    Every ``--json`` CLI response passes through here exactly once.
    """
    from market_service.interaction_plane.budgets import INTERACTION_SCHEMA_VERSION

    header: dict[str, Any] = {
        "version": INTERACTION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "segment": segment,
        "mode": mode,
        "source": source,
        "prompt": build_interaction_prompt(segment, mode),
    }
    out: dict[str, Any] = {"interaction": header}
    if isinstance(result, dict):
        out.update(result)
    else:
        out["result"] = result
    return out
