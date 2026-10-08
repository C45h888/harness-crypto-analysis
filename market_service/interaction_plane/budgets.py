"""Interaction-plane budgets — the single source of byte discipline.

Every segment declares its char roof + list cap here. ``parsing.py``
enforces them; the CLI never invents its own cutoffs.
"""

from __future__ import annotations

from market_service.runtime.contracts import SCHEMA_VERSION_REGISTRY

# Phase S-4: the interaction-plane contract version resolves to the tree
# registry (identity, not a local copy).
INTERACTION_SCHEMA_VERSION = SCHEMA_VERSION_REGISTRY["interaction_budget"]
INTERACTION_PROMPT_VERSION = "interaction-prompt-v1"

MODE_CHAR_ROOF: dict[str, int] = {
    "compact": 8_000,
    "snapshot": 20_000,
    "inventory": 40_000,
    # `full` is human-only: unbounded raw payload for deep-dives.
    # Models receive it with a parse warning, never as a working view.
    "full": 180_000,
}

SEGMENT_LIST_CAP: dict[str, int] = {
    "warm": 60,
    "ledger": 60,
    "micro": 200,
    "history": 100,
    "memory": 16,
    "control": 64,
    "inference": 60,
}

DEFAULT_LIST_CAP = 60

# ToolResult payloads above this size collapse to preview + receipt
# instead of a silent prefix cut.
TOOLRESULT_INLINE_LIMIT = 40_000


def char_roof(mode: str) -> int:
    return MODE_CHAR_ROOF.get(mode, MODE_CHAR_ROOF["snapshot"])


def list_cap(segment: str) -> int:
    return SEGMENT_LIST_CAP.get(segment, DEFAULT_LIST_CAP)


__all__ = [
    "INTERACTION_PROMPT_VERSION",
    "INTERACTION_SCHEMA_VERSION",
    "MODE_CHAR_ROOF",
    "SEGMENT_LIST_CAP",
    "TOOLRESULT_INLINE_LIMIT",
    "char_roof",
    "list_cap",
]
