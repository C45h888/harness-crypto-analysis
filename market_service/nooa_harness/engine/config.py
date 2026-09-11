"""Engine configuration — single source of truth for budgets, KB paths, env.

Semantic authority: NOTHING in this module computes or narrates. It only
declares the constants every other engine module reads. When the budget
changes, this is the only file that needs to move.

The split:
  - AGENTIC_MAX_TOOL_ROUNDS / AGENTIC_MAX_LLM_TURNS / AGENTIC_PER_ROUND_CALL_CAP
    are the cycle's round budgets. The agent can re-dispatch tools within a
    round up to AGENTIC_PER_ROUND_CALL_CAP, can run AGENTIC_MAX_TOOL_ROUNDS
    rounds in total, and the LLM is allowed AGENTIC_MAX_LLM_TURNS calls
    (narrate#1 + tool follow-ups + repairs + forced-final).
  - SUMMARY_MIN_CHARS is the P4 explanation floor: a final turn whose
    summary is thinner is sent back for repair.
  - DEFAULT_NARRATION_MAX_TOKENS / NARRATION_MAX_TOKENS_ENV /
    DEFAULT_CONTEXT_WINDOW drive the per-call generation cap (25% of the
    context window, clamped).
  - MEMORY_RECALL_LIMIT / MEMORY_CONTEXT_BUDGET / MAX_MEMORY_PROPOSALS are
    the memory-plane budgets.
  - SESSION_TEMPLATE is the canonical session-id pattern (kept for callers
    that synthesise an id outside the engine).
  - KB paths are file-system anchors for the prompt-payload docs the
    narration layer reads at import time.

The structured-probe flag ``_STRUCTURED_OK`` lives here because it is
process-global state shared by every call site (LLM transport sets it once,
narration transport respects it thereafter).
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Session id pattern (legacy export; engines still use _stable_session_id) ---
SESSION_TEMPLATE = "inference-engine-{symbol}-{venue}"

# --- Narration token budget — agentic loop, not controlled generator. ---
# Muse Spark 1.2 contributor is mandatory-reasoning with 1_048_576 context;
# 25% of that is 262k. Default 50_000 lets the model run as an agent
# (tool calls + validation + memory cross-check + structured report) without
# starving the final JSON block. Operators override with NOOA_MODEL_MAX_TOKENS.
# Calculation modules + read tools are now the primary inference path — the
# predefined formula output is never the final word.
DEFAULT_NARRATION_MAX_TOKENS = 50_000
NARRATION_MAX_TOKENS_ENV = "NOOA_MODEL_MAX_TOKENS"
DEFAULT_CONTEXT_WINDOW = 1_048_576  # Muse Spark 1.2 contributor

# --- Cycle budgets (the agentic loop). ---
# live-proven: fallback-path models burn rounds on redundant re-invocation;
# budget must survive waste + repairs + final.
AGENTIC_MAX_TOOL_ROUNDS = 8
# narrate#1 + tool follow-ups + repairs + forced-final, with headroom for
# redundant rounds.
AGENTIC_MAX_LLM_TURNS = 12
# Max dispatches per tool round.
AGENTIC_PER_ROUND_CALL_CAP = 3
# P4 explanation floor — thinner finals are repaired.
SUMMARY_MIN_CHARS = 200

# --- Memory recall budgets. ---
# Expanded for paper KB cross-check.
MEMORY_RECALL_LIMIT = 12
MEMORY_CONTEXT_BUDGET = 8_000
MAX_MEMORY_PROPOSALS = 3

# --- Headline bound for the prior-artifact block in the prompt. ---
_PRIOR_HEADLINE_MAX_CHARS = 4_000

# --- Structured-output probe flag ---
# Module-level probe flag: once a backend rejects output_model, stop paying
# the failed structured attempt on every subsequent turn (same process).
_STRUCTURED_OK: bool | None = None

# --- Knowledge-base / prompt-payload paths. ---
# Resolved relative to this file so the package can be imported from any cwd.
# Note on depth: config.py lives at market_service/nooa_harness/engine/config.py,
# so parents[3] is the repo root (one level deeper than the legacy engine.py
# which used parents[2]).
_KB_BASE = Path(__file__).resolve().parents[3] / "docs" / "nooa-kb"
_TOOL_MANIFEST = _KB_BASE / "behavior" / "tool-manifest.md"
_MEMORY_PROTOCOL = _KB_BASE / "behavior" / "memory-protocol.md"
_PAPER_KB = _KB_BASE / "nooa-micro-structure-archieture.md"
_CALC_KB_PATHS = [
    _KB_BASE / "behavior" / "B01-agents-base.md",
]


def narration_max_tokens() -> int:
    """Resolve the narration generation budget — reasoning-aware, 25% window cap.

    Muse Spark contributor: 1_048_576 context → 25% = 262_144. Default 50_000
    is the floor for agentic execution; env override is clamped to
    [2_000, 262_144] so a misconfigured 500_000 does not OOM the gateway.
    """
    cap = int(DEFAULT_CONTEXT_WINDOW * 0.25)  # 262_144 for Muse Spark
    try:
        requested = int(os.getenv(NARRATION_MAX_TOKENS_ENV, str(DEFAULT_NARRATION_MAX_TOKENS)))
        return max(2_000, min(requested, cap))
    except (TypeError, ValueError):
        return DEFAULT_NARRATION_MAX_TOKENS


__all__ = [
    "SESSION_TEMPLATE",
    "DEFAULT_NARRATION_MAX_TOKENS",
    "NARRATION_MAX_TOKENS_ENV",
    "DEFAULT_CONTEXT_WINDOW",
    "AGENTIC_MAX_TOOL_ROUNDS",
    "AGENTIC_MAX_LLM_TURNS",
    "AGENTIC_PER_ROUND_CALL_CAP",
    "SUMMARY_MIN_CHARS",
    "MEMORY_RECALL_LIMIT",
    "MEMORY_CONTEXT_BUDGET",
    "MAX_MEMORY_PROPOSALS",
    "_PRIOR_HEADLINE_MAX_CHARS",
    "_STRUCTURED_OK",
    "_TOOL_MANIFEST",
    "_MEMORY_PROTOCOL",
    "_PAPER_KB",
    "_CALC_KB_PATHS",
    "narration_max_tokens",
]
