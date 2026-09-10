"""Inference-engine mechanics — decomposed package (Phase 0).

Formerly the 1488-line ``inference.py`` monolith, split along its four
semantic blocks (move-don't-rewrite; every body is verbatim):

    gate.py        HARD STATUS GATE — the deterministic trichotomy
                   (validated / provisional / insufficient) + null discipline
    capability.py  CAPABILITY REGISTRY — named, scope-validated dispatch
                   surfaces + audit entries (CAPABILITIES dict)
    dispatch.py    TOOL BASE — dispatch_* implementations, TOOL_NAMES /
                   TOOL_PHASE registries, execute_tool, name normalization

The autonomous wake plane (``wake.py``: trigger evaluation, dedupe,
coalescing, revalidation) was REMOVED — inference is invoked only with an
explicit task from the CLI surfaces (``harness --inference`` /
``nooa market inference run``). No trigger matrix, no loop, no worker.

All historical import paths keep working through this re-export shim:
``from market_service.nooa_harness.inference import X`` resolves exactly as
before. New code should import the block it belongs to.

Discipline preserved from the monolith:
- openai-free (the client is injected via ``backends.build_llm``)
- dispatchers never construct stores (dependency injection at every seam)
- CapabilityDenied returns structured denials, never raises to the caller
"""

from __future__ import annotations

from .capability import (
    CAPABILITIES,
    Capability,
    CapabilityDenied,
    capability_log_entry,
)
from .dispatch import (
    _REQUIRED_PHASES,
    TOOL_NAMES,
    TOOL_PHASE,
    _bounded,
    _depth_fit_from_dict,
    _load_prior_block_fits,
    _norm_tool_key,
    _normalize_tool_name,
    _price_fit_from_dict,
    _tool_fit_beta,
    dispatch_assemble_evidence,
    dispatch_calc_ad_average,
    dispatch_calc_derived_diagnostic,
    dispatch_calc_fit_depth_scaling,
    dispatch_calc_fit_price_impact,
    dispatch_calc_observation_build,
    dispatch_calc_ofi_intervals,
    dispatch_market_read,
    dispatch_memory_recall_paper,
    dispatch_read_capture_status,
    dispatch_read_derivatives,
    dispatch_read_events,
    dispatch_read_evidence,
    dispatch_read_intervals,
    dispatch_read_keystone_history,
    dispatch_read_wall_history,
    dispatch_replay,
    dispatch_substrate_invoke,
    dispatch_substrate_read,
    execute_tool,
)
from .gate import (
    _ESTABLISHED_CAPTURE_STATES,
    GateInputs,
    gate_interpretation,
    resolve_inference_status,
)

__all__ = [
    "CAPABILITIES",
    "TOOL_NAMES",
    "TOOL_PHASE",
    "Capability",
    "CapabilityDenied",
    "capability_log_entry",
    "dispatch_assemble_evidence",
    "dispatch_read_capture_status",
    "dispatch_read_events",
    "dispatch_replay",
    "dispatch_substrate_invoke",
    "dispatch_substrate_read",
    "execute_tool",
    "gate_interpretation",
    "resolve_inference_status",
]
