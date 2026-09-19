"""TOOL BASE — thin re-export shim over the decomposed tooling plane.

The 1909-line monolith now lives in ``tooling/`` (registry / tick_guard /
replay_adapter / history_adapter / tools_market / tools_legacy /
tools_forward / tools_evidence / executor). This module re-exports its
public surface so every historical import path keeps working. New code
should import from ``tooling`` directly.
"""
from __future__ import annotations

from .tooling import (
    _REQUIRED_PHASES,
    TOOL_NAMES,
    TOOL_PHASE,
    tool_home,
    tool_homes,
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
    dispatch_calc_forward_forecast,
    dispatch_calc_observation_build,
    dispatch_calc_ofi_intervals,
    dispatch_calc_scenario_evaluate,
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

__all__ = [
    "_REQUIRED_PHASES",
    "TOOL_NAMES",
    "TOOL_PHASE",
    "tool_home",
    "tool_homes",
    "_bounded",
    "_depth_fit_from_dict",
    "_load_prior_block_fits",
    "_norm_tool_key",
    "_normalize_tool_name",
    "_price_fit_from_dict",
    "_tool_fit_beta",
    "dispatch_assemble_evidence",
    "dispatch_calc_ad_average",
    "dispatch_calc_derived_diagnostic",
    "dispatch_calc_fit_depth_scaling",
    "dispatch_calc_fit_price_impact",
    "dispatch_calc_forward_forecast",
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

]
