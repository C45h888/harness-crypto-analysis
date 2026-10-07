"""Canonical interaction plane — the sole gate for every model entry.

Package layout (no monolith):

* ``segments`` — the one vocabulary (inner + outer models)
* ``budgets`` — char roofs, list caps, truncation markers
* ``parsing`` — DETERMINISTIC PARSING LAYER (Python parses, model cites)
* ``prompts`` — MODEL CONTROL LAYER (designated prompt + stdout header)
* ``stores`` — store construction + venue (one opener)
* ``reads`` — async segment reads (warm / ledger / history / micro)
* ``control`` — poller control plane
* ``trigger`` — inference trigger (the ONLY write path)
* ``cli`` — thin argparse adapter (flags → package → header)
* ``manifest`` — describe() manifest (Hermes tool-manifest source)

Boundary: reads never compute, never invoke workers; the trigger never
reads raw state directly — it delegates to ``inference_runner``.
"""

from __future__ import annotations

from market_service.interaction_plane import (
    budgets,
    control,
    manifest,
    parsing,
    prompts,
    reads,
    segments,
    stores,
    trigger,
)
from market_service.interaction_plane.budgets import (
    INTERACTION_PROMPT_VERSION,
    INTERACTION_SCHEMA_VERSION,
)
from market_service.interaction_plane.cli import build_parser, main
from market_service.interaction_plane.manifest import describe
from market_service.interaction_plane.parsing import empty_read, wrap_result
from market_service.interaction_plane.prompts import (
    attach_header,
    build_interaction_prompt,
)

__all__ = [
    "INTERACTION_PROMPT_VERSION",
    "INTERACTION_SCHEMA_VERSION",
    "attach_header",
    "budgets",
    "build_interaction_prompt",
    "build_parser",
    "control",
    "describe",
    "empty_read",
    "main",
    "manifest",
    "parsing",
    "prompts",
    "reads",
    "segments",
    "stores",
    "trigger",
    "wrap_result",
]
