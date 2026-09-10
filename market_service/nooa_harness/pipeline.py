"""Canonical pipeline import path — re-export shim (semantic-debt pass).

The monolithic pipeline module was split into a two-plane layout:

    calculations/composition.py shared deterministic core (both planes build on it)
    pipeline_interpretation.py  INTERPRETATION PLANE — envelope + persistence +
                                derivative fetch (cycles removed: workers are
                                invoked as tools via substrate_worker.tools)

Names are re-exported here so historical import paths keep working. New code
should import the plane it belongs to: the outer CLI / ``nooa market`` read
commands from ``pipeline_interpretation``; the agent's tool base only ever
through ``substrate_worker.tools``.
"""

from __future__ import annotations  # noqa: I001

from market_service.calculations.composition import (  # noqa: F401
    GROUP_MAP,
    WINDOW_MINUTES_MAP,
    _ANALYSIS_CALC_DEPS,
    _CALC_SECTION_DEPS,
    _accumulate_prior_walls,
    _adapt_oi,
    _adapt_wall_migration,
    _enrich_fut_keystone,
    _resolve_scorecard_weights,
    _resolve_tier_config,
    _utc_iso,
    resolve_analysis_sections,
    resolve_calc_sections,
    run_analysis,
    run_calculations,
    sections_for_groups,
)
from market_service.runtime.bounds import (  # noqa: F401
    _bound_arrays,
    _evidence_headlines,
)
from market_service.runtime.derivatives import (  # noqa: F401
    DERIV_FRESH_MS_DEFAULT,
    DERIV_TTL_S_DEFAULT,
    _is_deriv_fresh,
    _merge_derivatives,
)
from market_service.runtime.raw_window import (
    build_raw_window as read_raw_window,
)
from .pipeline_interpretation import (  # noqa: F401
    MACRO_SYMBOLS,
    _keystone_snapshot_payload,
    _wall_snapshot_payload,
    assemble_envelope,
    fetch_derivative_evidence,
)

__all__ = [
    "DERIV_FRESH_MS_DEFAULT",
    "DERIV_TTL_S_DEFAULT",
    "GROUP_MAP",
    "MACRO_SYMBOLS",
    "WINDOW_MINUTES_MAP",
    "assemble_envelope",
    "fetch_derivative_evidence",
    "read_raw_window",
    "run_analysis",
    "run_calculations",
]
