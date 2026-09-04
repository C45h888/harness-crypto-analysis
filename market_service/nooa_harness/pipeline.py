"""Canonical pipeline import path — re-export shim (semantic-debt pass).

The monolithic pipeline module was split into a two-plane layout:

    bedrock.py                  shared deterministic core (both planes build on it)
    pipeline_interpretation.py  INTERPRETATION PLANE — envelope + persistence +
                                derivative fetch + run_cycle / run_group_cycle
    pipeline_inference.py       INFERENCE PLANE — the OO agent's read seam into
                                the same math via an injected store

Names are re-exported here so historical import paths keep working. New code
should import the plane it belongs to: the outer CLI / ``nooa market`` read
commands from ``pipeline_interpretation``; the agent's tool base only ever
through ``pipeline_inference``. The inference plane importing this module
(past or directly) is the coupling the split removes.
"""

from __future__ import annotations  # noqa: I001

from .bedrock import (  # noqa: F401
    DERIV_FRESH_MS_DEFAULT,
    DERIV_TTL_S_DEFAULT,
    GROUP_MAP,
    WINDOW_MINUTES_MAP,
    _ANALYSIS_CALC_DEPS,
    _CALC_SECTION_DEPS,
    _accumulate_prior_walls,
    _adapt_oi,
    _adapt_wall_migration,
    _bound_arrays,
    _enrich_fut_keystone,
    _evidence_headlines,
    _is_deriv_fresh,
    _merge_derivatives,
    _resolve_scorecard_weights,
    _resolve_tier_config,
    _utc_iso,
    read_raw_window,
    resolve_analysis_sections,
    resolve_calc_sections,
    run_analysis,
    run_calculations,
    sections_for_groups,
)
from .pipeline_inference import run_inference_group  # noqa: F401
from .pipeline_interpretation import (  # noqa: F401
    MACRO_SYMBOLS,
    _keystone_snapshot_payload,
    _wall_snapshot_payload,
    assemble_envelope,
    fetch_derivative_evidence,
    persist_envelope,
    run_cycle,
    run_group_cycle,
)

__all__ = [
    "WINDOW_MINUTES_MAP",
    "GROUP_MAP",
    "read_raw_window",
    "run_calculations",
    "run_analysis",
    "assemble_envelope",
    "persist_envelope",
    "run_cycle",
    "run_group_cycle",
    "run_inference_group",
    "fetch_derivative_evidence",
    "MACRO_SYMBOLS",
    "DERIV_TTL_S_DEFAULT",
    "DERIV_FRESH_MS_DEFAULT",
]
