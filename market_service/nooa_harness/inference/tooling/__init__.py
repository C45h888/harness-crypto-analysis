"""Tooling plane — decomposed dispatch surfaces mounted on the fitting plane.

Planes (move-don't-rewrite from the ``dispatch.py`` monolith):

    registry.py         TOOL_NAMES / TOOL_PHASE / homes / loops / normalization
    tick_guard.py       frozen instrument tick resolution (structured refusal)
    replay_adapter.py   I/O-only window reads; math owned by fitting_route_c
    history_adapter.py  PG prior-block fits + legacy evidence assembly
    tools_market.py     T1/T2 reads — capture, market, substrate, memory
    tools_legacy.py     Route A/B — OFI/AD/OLS/derived/scenario.evaluate
    tools_forward.py    Route C — feature/join/fit/distribution/scenario/forecast
    tools_evidence.py   read-only evidence — hypothesis/events/decay/discipline
    executor.py         execute_tool router (single narration entry point)

Discipline: tooling never computes — scope-validate, read stores, call the
deterministic ``market_service.microstructure`` fitting plane, audit, project.
"""

from __future__ import annotations

from .executor import execute_tool
from .history_adapter import (
    _depth_fit_from_dict,
    _load_prior_block_fits,
    _price_fit_from_dict,
    _tool_fit_beta,
)
from .registry import (
    TOOL_HOME_DEFAULT,
    TOOL_HOME_OPTIONS_OVERRIDES,
    TOOL_HOME_OVERRIDES,
    TOOL_LOOP_DEFAULT,
    TOOL_LOOP_OVERRIDES,
    TOOL_NAMES,
    TOOL_PHASE,
    _PHASE_ORDER,
    _REQUIRED_PHASES,
    _bounded,
    _norm_tool_key,
    _normalize_tool_name,
    tool_home,
    tool_homes,
    tool_loop,
)
from .replay_adapter import (
    _forward_replay_inputs, _read_tape_payloads, _read_windowed_events,
    _spans_from_discontinuities, _spans_from_status_transitions,
    degraded_spans_in_window, forward_replay_inputs,
)
from .tick_guard import _frozen_tick, _legacy_tick, _resolved_tick_for_dispatch
from .tools_evidence import (
    dispatch_calc_decay_report,
    dispatch_calc_discipline_audit,
    dispatch_calc_events,
    dispatch_calc_hypothesis_test,
)
from .tools_forward import (
    _forward_core,
    dispatch_calc_feature_build,
    dispatch_calc_forward_distribution,
    dispatch_calc_forward_fit,
    dispatch_calc_forward_forecast,
    dispatch_calc_forward_join,
    dispatch_calc_forward_scenario,
)
from .tools_legacy import (
    dispatch_calc_ad_average,
    dispatch_calc_derived_diagnostic,
    dispatch_calc_fit_depth_scaling,
    dispatch_calc_fit_price_impact,
    dispatch_calc_observation_build,
    dispatch_calc_ofi_intervals,
    dispatch_calc_scenario_evaluate,
)
from .tools_market import (
    dispatch_assemble_evidence,
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
)

__all__ = [
    "TOOL_HOME_DEFAULT",
    "TOOL_HOME_OPTIONS_OVERRIDES",
    "TOOL_HOME_OVERRIDES",
    "TOOL_LOOP_DEFAULT",
    "TOOL_LOOP_OVERRIDES",
    "TOOL_NAMES",
    "TOOL_PHASE",
    "_PHASE_ORDER",
    "_REQUIRED_PHASES",
    "_bounded",
    "_forward_core",
    "_forward_replay_inputs",
    "_read_tape_payloads",
    "_spans_from_discontinuities",
    "_spans_from_status_transitions",
    "degraded_spans_in_window",
    "_frozen_tick",
    "_legacy_tick",
    "_load_prior_block_fits",
    "_norm_tool_key",
    "_normalize_tool_name",
    "_read_windowed_events",
    "_resolved_tick_for_dispatch",
    "_depth_fit_from_dict",
    "_price_fit_from_dict",
    "_tool_fit_beta",
    "dispatch_assemble_evidence",
    "dispatch_calc_ad_average",
    "dispatch_calc_decay_report",
    "dispatch_calc_derived_diagnostic",
    "dispatch_calc_discipline_audit",
    "dispatch_calc_events",
    "dispatch_calc_feature_build",
    "dispatch_calc_fit_depth_scaling",
    "dispatch_calc_fit_price_impact",
    "dispatch_calc_forward_distribution",
    "dispatch_calc_forward_fit",
    "dispatch_calc_forward_forecast",
    "dispatch_calc_forward_join",
    "dispatch_calc_forward_scenario",
    "dispatch_calc_hypothesis_test",
    "dispatch_calc_observation_build",
    "dispatch_calc_ofi_intervals",
    "dispatch_calc_scenario_evaluate",
    "dispatch_market_read",
    "dispatch_memory_recall_paper",
    "dispatch_read_capture_status",
    "dispatch_read_derivatives",
    "dispatch_read_events",
    "dispatch_read_evidence",
    "dispatch_read_intervals",
    "dispatch_read_keystone_history",
    "dispatch_read_wall_history",
    "dispatch_replay",
    "dispatch_substrate_invoke",
    "dispatch_substrate_read",
    "execute_tool",
    "forward_replay_inputs",
    "tool_home",
    "tool_homes",
    "tool_loop",
]
