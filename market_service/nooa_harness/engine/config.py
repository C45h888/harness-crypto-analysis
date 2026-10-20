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
  - MEMORY_RECALL_LIMIT / MEMORY_CONTEXT_BUDGET are the memory-plane budgets.
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
# Scope: max_tokens INCLUDES reasoning preamble + final JSON (one shared
# generation budget — see llm.py reasoning fallback). Give it room, but give
# it walls: reasoning_reserve() names the portion reserved inside the cap so
# a long preamble cannot silently starve the JSON block.
DEFAULT_NARRATION_MAX_TOKENS = 50_000
NARRATION_MAX_TOKENS_ENV = "NOOA_MODEL_MAX_TOKENS"
DEFAULT_CONTEXT_WINDOW = 1_048_576  # Muse Spark 1.2 contributor
#: Reasoning reserve inside max_tokens (env NOOA_REASONING_RESERVE, default
#: 8k for timing runs; production higher but explicit, never extra).
REASONING_RESERVE_ENV = "NOOA_REASONING_RESERVE"
DEFAULT_REASONING_RESERVE = 8_000

# --- Cycle budgets (the agentic loop). ---
# live-proven: fallback-path models burn rounds on redundant re-invocation;
# budget must survive waste + repairs + final.
# Proof-pass doctrine (2b forecast proof): the agent earns the multivariate
# truth with its own interpret-position call — one required dispatch more
# than the digest era, so the ceiling moves 10 → 11. The reformulation walk
# (refusal + reform + re-walk + proof) is what spends it.
AGENTIC_MAX_TOOL_ROUNDS = 11
# narrate#1 + tool follow-ups + repairs + forced-final, with headroom for
# redundant rounds. Raised 16 → 30 for the quality-assessment window: the
# proof-pass doctrine (forecast proof + frame + stage stores) spends more
# turns per cycle, and the ceiling must never be what stops an assessment
# run. Bounded per-loop pass budgets + the round ceiling still bound cost;
# this backstop is unbreakable headroom, not a spending target.
AGENTIC_MAX_LLM_TURNS = 30
# Max dispatches per tool round.
AGENTIC_PER_ROUND_CALL_CAP = 3
# Per-loop-state pass allotment (Track A congruence shape, loop-surface spec v1).
# The budget counts LLM passes per nested loop, never tools: within a pass
# the agent packs whatever calls the work needs, bounded only by the
# anti-runaway dispatch ceiling below.
# Phase R7 (fetch semantic removed): the fetch pass budget is gone with
# the fetch turn. Comprehension is the first agentic pass (its bounded
# assessment turn rides its budget); acquisition is plan-bound (0 LLM);
# evidence/reasoning/validation/output accounting.
# 1 + 3 + 6 + 1(+2 retries) + 1 + 1 = 13 planned, inside the backstop.
# Headroom (16): +1 comprehension assessment + reflection/refusal reserves.
# Synthesis doctrine: output spends COLLAPSE + DERIVE + the answer
# verbalization turn (+ the one correction turn when the hard gate misses)
# — bounded, tool-free, inside the backstop.
LOOP_PASS_BUDGET: dict[str, int] = {
    "comprehension": 1,
    "evidence": 3,
    # R7 + floor-steer + proof-pass doctrine: three forced positions (2b
    # interpret spends TWO passes: fit/dist/decay, then the forecast proof —
    # the pass ceiling fits three calls) + the empty-close floor steer +
    # halt-reform headroom + one slack pass. The hard-track floor is what
    # the steady statistical track spends, not a suggestion.
    "reasoning": 8,
    "validation": 1,
    # The fourth output pass is the canonical answer verbalization: the
    # agent speaks finally FROM the gated foundation (verdict + assembled
    # values + memory), after the one correction turn has had its chance.
    "output": 4,
}
# Bounded internal retry inside VALIDATION (diagnose -> steered repair ->
# revalidate). Persistent failure emits a structured issue to the FSM
# terminal; terminal stewardship is a later FSM pass.
# 2026-09-27: 1 -> 2. One repair could not carry four missing final items
# (hypothesis, P4 explanation, evidence roots, delta path) in a single turn.
VALIDATION_RETRY_PASSES = 2
# One tool-call batch is bounded by the public per-round contract.  The
# runtime and prompt use the same number; excess calls are named on the
# cycle's unexecuted list rather than silently dropped.
MAX_DISPATCHES_PER_PASS = AGENTIC_PER_ROUND_CALL_CAP
# P4 explanation floor — thinner finals are repaired.
SUMMARY_MIN_CHARS = 200

# --- Memory recall budgets. ---
# Lean budget: recall trimmed (env restores); same mechanism, fewer rows.
# No state change — kind/query/limit contract untouched, bounds only.
def _recall_int(name: str, default: int, *, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(os.getenv(name, str(default))), hi))
    except (TypeError, ValueError):
        return default


MEMORY_RECALL_LIMIT = _recall_int("NOOA_MEMORY_RECALL_LIMIT", 8, lo=2, hi=32)
MEMORY_CONTEXT_BUDGET = _recall_int("NOOA_MEMORY_CONTEXT_BUDGET", 4_000, lo=1_000, hi=16_000)

# --- Evidence bounds (gate/assembly authority split) ---
# Per-TURN cap lives in schemas.coerce_turn (temporal primacy, lead first).
# A turn cap cannot bound the JUDGED set: merge_interpretation unions across
# every turn of a cycle (16 x ~15 turns). This is the cycle-level bound the
# gate actually judges; excess is truncated with a log, never silently.
MAX_MERGED_EVIDENCE = _recall_int("NOOA_MAX_MERGED_EVIDENCE", 24, lo=8, hi=64)
# Removal-only salvage at the validation terminal: when the repair budget is
# spent and EVERY remaining miss is an entry-shape miss (null value on a
# non-refusal path), the engine may drop those entries deterministically and
# re-judge once. Bounded so a junk cycle (half the entries unbackable) still
# terminals instead of being laundered into a settled artifact.
MAX_SALVAGE_ENTRIES = _recall_int("NOOA_MAX_SALVAGE_ENTRIES", 8, lo=1, hi=32)

# --- Headline bound for the prior-artifact block in the prompt. ---
_PRIOR_HEADLINE_MAX_CHARS = _recall_int("NOOA_PRIOR_HEADLINE_CHARS", 2_000, lo=500, hi=8_000)

# --- Structured-output probe flag ---
# Module-level probe flag: once a backend rejects output_model, stop paying
# the failed structured attempt on every subsequent turn (same process).
_STRUCTURED_OK: bool | None = None
# Structured-output probing (output_model=NarrationTurn) is OPT-IN.
# Live 2026-09-28 (4/4 correlation across full cycles): when the probe SUCCEEDS
# on openrouter/qwen/qwen3.7-flash the model emits tool_calls-only turns and
# the judged interpretation is EMPTY (summary 0, evidence 0, hypothesis null
# in every turn); when the probe is REFUSED and the raw text-JSON path runs,
# the same model returns full summary/evidence/hypothesis. The engine's real
# contract is TEXT JSON parsed by extract_json_object — so the probe stays OFF
# unless NOOA_STRUCTURED_OUTPUT=1.
STRUCTURED_OUTPUT_ENV = "NOOA_STRUCTURED_OUTPUT"


def structured_output_enabled() -> bool:
    """Whether the structured (output_model) probe may be attempted at all."""
    return os.getenv(STRUCTURED_OUTPUT_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")

# --- Knowledge-base / prompt-payload paths. ---
# Resolved relative to this file so the package can be imported from any cwd.
# Note on depth: config.py lives at market_service/nooa_harness/engine/config.py,
# so parents[3] is the repo root (one level deeper than the legacy engine.py
# which used parents[2]).
_KB_BASE = Path(__file__).resolve().parents[3] / "docs" / "nooa-kb"
_TOOL_MANIFEST = _KB_BASE / "behavior" / "tool-manifest.md"
_MEMORY_PROTOCOL = _KB_BASE / "behavior" / "memory-protocol.md"


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


def reasoning_reserve() -> int:
    """Reasoning portion INSIDE max_tokens (never extra, never separate)."""
    try:
        requested = int(os.getenv(REASONING_RESERVE_ENV, str(DEFAULT_REASONING_RESERVE)))
        cap = narration_max_tokens() - 2_000  # always leave a final-JSON floor
        return max(2_000, min(requested, cap))
    except (TypeError, ValueError):
        return DEFAULT_REASONING_RESERVE


def token_budget_breakdown() -> dict[str, int]:
    """Walled budget view: one shared cap split into reserve + floor."""
    total = narration_max_tokens()
    reserve = reasoning_reserve()
    return {
        "max_tokens": total,
        "reasoning_reserve": reserve,
        "final_floor": max(0, total - reserve),
        "context_window": int(DEFAULT_CONTEXT_WINDOW),
        "cap": int(DEFAULT_CONTEXT_WINDOW * 0.25),
    }


#: Bounded narration transport (F10): a dropped stream must fail fast into
#: the retry plane instead of stalling a cycle for hours. Env override in
#: seconds, clamped so a misconfiguration can neither spin (too low) nor
#: reintroduce the unbounded stall (too high).
NARRATION_TIMEOUT_ENV = "NOOA_NARRATION_TIMEOUT_S"
DEFAULT_NARRATION_TIMEOUT_S = 150


def narration_timeout_s() -> int:
    """Per-attempt transport timeout for narration LLM calls."""
    try:
        return max(30, min(int(os.getenv(NARRATION_TIMEOUT_ENV, str(DEFAULT_NARRATION_TIMEOUT_S))), 600))
    except (TypeError, ValueError):
        return DEFAULT_NARRATION_TIMEOUT_S


__all__ = [
    "SESSION_TEMPLATE",
    "DEFAULT_NARRATION_MAX_TOKENS",
    "NARRATION_MAX_TOKENS_ENV",
    "DEFAULT_CONTEXT_WINDOW",
    "REASONING_RESERVE_ENV",
    "DEFAULT_REASONING_RESERVE",
    "NARRATION_TIMEOUT_ENV",
    "DEFAULT_NARRATION_TIMEOUT_S",
    "AGENTIC_MAX_TOOL_ROUNDS",
    "AGENTIC_MAX_LLM_TURNS",
    "AGENTIC_PER_ROUND_CALL_CAP",
    "LOOP_PASS_BUDGET",
    "VALIDATION_RETRY_PASSES",
    "MAX_DISPATCHES_PER_PASS",
    "SUMMARY_MIN_CHARS",
    "MEMORY_RECALL_LIMIT",
    "MEMORY_CONTEXT_BUDGET",
    "MAX_MERGED_EVIDENCE",
    "MAX_SALVAGE_ENTRIES",
    "_PRIOR_HEADLINE_MAX_CHARS",
    "_STRUCTURED_OK",
    "_TOOL_MANIFEST",
    "_MEMORY_PROTOCOL",
    "narration_max_tokens",
    "reasoning_reserve",
    "token_budget_breakdown",
    "narration_timeout_s",
]
