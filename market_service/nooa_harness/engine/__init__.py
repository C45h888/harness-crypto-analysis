"""Inference engine package — the statistical inference agent.

Decomposition pass (move-don't-rewrite, body-verbatim): the 1759-line
``engine.py`` monolith is now a package of single-purpose modules.
Every public + test-pinned name continues to be importable from
``market_service.nooa_harness.engine`` — both the old module form and
this package form share the same surface, so callers do not need to
change.

Semantic authorities:

  - ``config``       — budgets, KB paths, env resolution, session template
  - ``schemas``      — NarrationTurn / NarrationToolCall / NarrationParseError
  - ``kb``           — KB loaders + system-prompt + output-format templates
  - ``llm``          — LLM transport (call_narration_llm), bounded envelope,
                       response text extraction
  - ``narration``    — turn parsing, final-turn validator, scenario verdict,
                       phase guidance + next-phase picker (pure functions)
  - ``core``         — InferenceEngine: the cycle (gather → gate → narrate →
                       persist → memory proposals)
  - ``interpreter``  — MicrostructureInterpretationAgent: the read-only
                       one-shot interpreter of one persisted evidence

The ``engine_compat.py`` legacy file at this directory's sibling path
is the old monolith, kept as a no-content-change reference during the
validation window. It is scheduled for deletion after the assessment
verifies zero migration debt.

Boundary rule preserved from the monolith:
- nooa-free at import (only pydantic, stdlib, sibling modules, and the
  nooa_harness.inference package are loaded at module-init time).
- The LLM client is INJECTED via ``backends.build_llm``; no OpenAI SDK
  import at module load.
- Tool dispatchers never construct their own stores; the engine threads
  injected connections + settings to every dispatch.
"""

from __future__ import annotations

# --- The two top-level classes (InferenceEngine + interpreter) ---
from .core import InferenceEngine
from .interpreter import MicrostructureInterpretationAgent

# --- LLM transport + view helpers (used by both engine + interpreter) ---
from .llm import bounded_envelope_view, call_narration_llm, response_text

# --- Public schema + exception ---
from .schemas import NarrationParseError, NarrationToolCall, NarrationTurn

# --- Public config constants ---
from .config import (
    AGENTIC_MAX_LLM_TURNS,
    AGENTIC_MAX_TOOL_ROUNDS,
    AGENTIC_PER_ROUND_CALL_CAP,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_NARRATION_MAX_TOKENS,
    MAX_MEMORY_PROPOSALS,
    MEMORY_CONTEXT_BUDGET,
    MEMORY_RECALL_LIMIT,
    NARRATION_MAX_TOKENS_ENV,
    SESSION_TEMPLATE,
    SUMMARY_MIN_CHARS,
    narration_max_tokens,
)

# --- KB loaders + prompt templates (public names; the templates are
#     test-pinned strings rented from this path) ---
from .kb import (
    SYSTEM_PROMPT_TEMPLATE,
    SystemPromptCache,
    TURN_CONTRACT_LINE,
    build_output_format,
    build_system_prompt,
    load_kb,
    load_paper_kb,
)

# --- Narration primitives ---
from .narration import (
    PHASE_GUIDANCE,
    coerce_turn,
    extract_json_object,
    next_uncovered_phase,
    scenario_verdict,
    validate_final_turn,
)


__all__ = [
    # top-level classes
    "InferenceEngine",
    "MicrostructureInterpretationAgent",
    # exception
    "NarrationParseError",
    # schemas
    "NarrationToolCall",
    "NarrationTurn",
    # config
    "AGENTIC_MAX_LLM_TURNS",
    "AGENTIC_MAX_TOOL_ROUNDS",
    "AGENTIC_PER_ROUND_CALL_CAP",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_NARRATION_MAX_TOKENS",
    "MAX_MEMORY_PROPOSALS",
    "MEMORY_CONTEXT_BUDGET",
    "MEMORY_RECALL_LIMIT",
    "NARRATION_MAX_TOKENS_ENV",
    "SESSION_TEMPLATE",
    "SUMMARY_MIN_CHARS",
    "narration_max_tokens",
    # kb / prompt
    "SYSTEM_PROMPT_TEMPLATE",
    "SystemPromptCache",
    "TURN_CONTRACT_LINE",
    "build_output_format",
    "build_system_prompt",
    "load_kb",
    "load_paper_kb",
    # llm
    "bounded_envelope_view",
    "call_narration_llm",
    "response_text",
    # narration
    "PHASE_GUIDANCE",
    "coerce_turn",
    "extract_json_object",
    "next_uncovered_phase",
    "scenario_verdict",
    "validate_final_turn",
]


# ---------------------------------------------------------------------------
# Private-name compat shims for tests + internal callers that historically
# imported the underscored names from ``market_service.nooa_harness.engine``.
# These are NOT in __all__ (deliberately) and live behind a one-line proxy so
# the seam stays visible in the diff. New code should call the unprefixed
# public names; old code keeps working unchanged.
# ---------------------------------------------------------------------------

# `from .engine import _extract_json_object` -> narration
_extract_json_object = extract_json_object
# `from .engine import _coerce_turn` -> narration
_coerce_turn = coerce_turn
# `from .engine import _validate_final_turn` -> narration
_validate_final_turn = validate_final_turn
# `from .engine import _scenario_verdict` -> narration
_scenario_verdict = scenario_verdict
# `from .engine import _PHASE_GUIDANCE` -> narration (already a dict, identity re-export)
_PHASE_GUIDANCE = PHASE_GUIDANCE
# `from .engine import _SYSTEM_PROMPT_TEMPLATE` -> kb
_SYSTEM_PROMPT_TEMPLATE = SYSTEM_PROMPT_TEMPLATE
# `from .engine import _narration_max_tokens` -> config
_narration_max_tokens = narration_max_tokens
# `from .engine import _next_uncovered_phase` -> narration
_next_uncovered_phase = next_uncovered_phase
# `from .engine import _load_kb` -> kb
_load_kb = load_kb
# `from .engine import _load_paper_kb` -> kb
_load_paper_kb = load_paper_kb
# `from .engine import _PRIOR_HEADLINE_MAX_CHARS` -> config (int constant)
from .config import _PRIOR_HEADLINE_MAX_CHARS
