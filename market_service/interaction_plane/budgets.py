"""Interaction-plane budgets — the single source of byte discipline.

Every segment declares its char roof + list cap here. ``parsing.py``
enforces them; the CLI never invents its own cutoffs.
"""

from __future__ import annotations

import os

from market_service.runtime.contracts import SCHEMA_VERSION_REGISTRY

# Phase S-4: the interaction-plane contract version resolves to the tree
# registry (identity, not a local copy).
INTERACTION_SCHEMA_VERSION = SCHEMA_VERSION_REGISTRY["interaction_budget"]
INTERACTION_PROMPT_VERSION = "interaction-prompt-v1"


def _env_int(name: str, default: int, *, lo: int, hi: int) -> int:
    """Lean-budget override: env wins, clamped, never raises."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(value, hi))

MODE_CHAR_ROOF: dict[str, int] = {
    "compact": 8_000,
    # Lean budget: snapshot/inventory roofs trimmed (env restores); `full`
    # stays human-only unbounded. Truncation always carries a receipt (§parsing).
    "snapshot": _env_int("INTERACTION_SNAPSHOT_ROOF", 16_000, lo=4_000, hi=40_000),
    "inventory": _env_int("INTERACTION_INVENTORY_ROOF", 32_000, lo=8_000, hi=80_000),
    # `full` is human-only: unbounded raw payload for deep-dives.
    # Models receive it with a parse warning, never as a working view.
    "full": 180_000,
}

SEGMENT_LIST_CAP: dict[str, int] = {
    # Lean budget: list caps halved (env restores); cuts carry
    # `__truncated__` markers (§parsing) so the agent knows what it missed.
    "warm": _env_int("INTERACTION_CAP_WARM", 30, lo=5, hi=120),
    "ledger": _env_int("INTERACTION_CAP_LEDGER", 30, lo=5, hi=120),
    "micro": _env_int("INTERACTION_CAP_MICRO", 100, lo=20, hi=400),
    "history": _env_int("INTERACTION_CAP_HISTORY", 50, lo=10, hi=200),
    "control": _env_int("INTERACTION_CAP_CONTROL", 32, lo=8, hi=128),
    "inference": _env_int("INTERACTION_CAP_INFERENCE", 30, lo=5, hi=120),
}

DEFAULT_LIST_CAP = 60

# ToolResult payloads above this size collapse to preview + receipt
# instead of a silent prefix cut. Lean budget: 8k default (env restores).
TOOLRESULT_INLINE_LIMIT = _env_int("INTERACTION_TOOLRESULT_LIMIT", 8_000, lo=2_000, hi=40_000)


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
