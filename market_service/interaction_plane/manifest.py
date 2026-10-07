"""Interaction manifest — the Hermes tool-manifest source."""

from __future__ import annotations

from typing import Any

from market_service.interaction_plane import segments as _segments
from market_service.interaction_plane.budgets import (
    INTERACTION_PROMPT_VERSION,
    INTERACTION_SCHEMA_VERSION,
    MODE_CHAR_ROOF,
    SEGMENT_LIST_CAP,
)
from market_service.interaction_plane.prompts import PROMPT_VERSION


def describe() -> dict[str, Any]:
    """Segment/mode/budget/prompt manifest (no I/O)."""
    from market_service.runtime import read_paths

    return {
        "interaction_version": INTERACTION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "segments": dict(_segments.SEGMENTS),
        "modes": list(_segments.MODES),
        "human_only_modes": sorted(_segments.HUMAN_ONLY_MODES),
        "char_roofs": dict(MODE_CHAR_ROOF),
        "list_caps": dict(SEGMENT_LIST_CAP),
        "read_tools": dict(read_paths.READ_SURFACE_TOOLS),
        "envelope": "{tool, status, reason, data, null_fields, budget_receipt}",
        "prompt_versions": {
            "interaction": INTERACTION_PROMPT_VERSION,
        },
    }


__all__ = ["describe"]
