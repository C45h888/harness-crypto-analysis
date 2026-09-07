"""Inference-engine mechanics — decomposed package (Phase 0).

Formerly the 1488-line ``inference.py`` monolith, split along its four
semantic blocks (move-don't-rewrite; every body is verbatim):

    gate.py        HARD STATUS GATE — the deterministic trichotomy
                   (validated / provisional / insufficient) + null discipline
    capability.py  CAPABILITY REGISTRY — named, scope-validated dispatch
                   surfaces + audit entries (CAPABILITIES dict)
    dispatch.py    TOOL BASE — dispatch_* implementations, TOOL_NAMES /
                   TOOL_PHASE registries, execute_tool, name normalization
    wake.py        WAKE PLANE — trigger evaluation, dedupe, coalescing,
                   revalidation, default_wake_dispatcher

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
    TOOL_NAMES,
    TOOL_PHASE,
    _REQUIRED_PHASES,
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
    dispatch_market_group,
    dispatch_market_read,
    dispatch_memory_recall_paper,
    dispatch_read_capture_status,
    dispatch_read_derivatives,
    dispatch_read_evidence,
    dispatch_read_events,
    dispatch_read_intervals,
    dispatch_read_keystone_history,
    dispatch_read_wall_history,
    dispatch_replay,
    execute_tool,
)
from .gate import (
    GateInputs,
    _ESTABLISHED_CAPTURE_STATES,
    gate_interpretation,
    resolve_inference_status,
)
from .wake import (
    DEFAULT_CYCLE_COOLDOWN_S,
    DEFAULT_EVENT_DELTA_THRESHOLD,
    WAKE_SCHEMA_VERSION,
    CounterSnapshot,
    WakeConfig,
    build_wake_envelope,
    coalesce_wakes,
    default_wake_dispatcher,
    evaluate_triggers,
    publish_wake,
    read_pending_wakes,
    revalidate_wake,
    wake_dedupe_id,
)

__all__ = [
    "CAPABILITIES",
    "TOOL_NAMES",
    "TOOL_PHASE",
    "Capability",
    "CapabilityDenied",
    "CounterSnapshot",
    "WakeConfig",
    "build_wake_envelope",
    "capability_log_entry",
    "coalesce_wakes",
    "dispatch_assemble_evidence",
    "dispatch_read_capture_status",
    "dispatch_read_events",
    "dispatch_replay",
    "evaluate_triggers",
    "execute_tool",
    "gate_interpretation",
    "publish_wake",
    "read_pending_wakes",
    "resolve_inference_status",
    "revalidate_wake",
    "wake_dedupe_id",
]
