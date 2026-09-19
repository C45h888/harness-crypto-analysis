"""Tool registry — names, phases, homes, normalization (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry

TOOL_NAMES: dict[str, str] = {
    # T1 — microstructure (paper stack) — split AD/OFI per Pass C
    "micro.capture_status": "redis.read_capture_status",
    "micro.events": "redis.read_events",
    "micro.ofi_intervals": "redis.read_intervals",
    "micro.replay": "fitting.replay",
    "micro.fit_beta": "fitting.assemble_evidence",
    "micro.evidence": "redis.read_evidence",
    # T1 split: AD/OFI separate tools, final fit is hypothesis validation
    "calc.ofi.intervals": "calc.ofi_intervals",
    "calc.depth.average": "calc.ad_average",
    "calc.observation.build": "calc.observation_build",
    "calc.fit.price_impact": "calc.fit_price_impact",
    "calc.fit.depth_scaling": "calc.fit_depth_scaling",
    "calc.derived_diagnostic": "calc.derived_diagnostic",
    "calc.price.delta": "calc.derived_diagnostic",
    "calc.scenario.evaluate": "calc.scenario.evaluate",
    "calc.feature.build": "calc.feature.build",
    "calc.forward.join": "calc.forward.join",
    "calc.forward.fit": "calc.forward.fit",
    "calc.forward.distribution": "calc.forward.distribution",
    "calc.forward.scenario": "calc.forward.scenario",
    "calc.forward.forecast": "calc.forward.forecast",
    "calc.hypothesis.test": "calc.hypothesis.test",
    "calc.events.absorption": "calc.events.absorption",
    "calc.events.walls": "calc.events.walls",
    "calc.decay.report": "calc.decay.report",
    "calc.discipline.audit": "calc.discipline.audit",
    "memory.recall_paper": "memory.recall_paper",
    # T2 — market correlation (canonical pipeline seams)
    "market.read": "market.read",
    "market.derivatives": "market.read_derivatives",
    "market.keystone_history": "market.read_keystone_history",
    "market.wall_history": "market.read_wall_history",
    # T3 — substrate capability plane (tool-first invocation)
    "substrate.read": "substrate.read",
    "substrate.invoke": "substrate.invoke",
    "substrate.anchors": "substrate.anchors",
    "substrate.density": "substrate.density",
    "substrate.delta": "substrate.delta",
    "substrate.ladders": "substrate.ladders",
    "substrate.large_print": "substrate.large_print",
    "substrate.migration": "substrate.migration",
    "substrate.oi": "substrate.oi",
    "substrate.signals": "substrate.signals",
    "substrate.tape": "substrate.tape",
    "substrate.technicals": "substrate.technicals",
    "substrate.tiers": "substrate.tiers",
    "substrate.volume_profile": "substrate.volume_profile",
}


TOOL_PHASE: dict[str, str] = {
    # P1 — OFI / tape quality
    "micro.capture_status": "P1",
    "micro.events": "P1",
    "micro.ofi_intervals": "P1",
    "micro.replay": "P1",
    "calc.ofi.intervals": "P1",
    # P2 — AD / observations / fits
    "micro.fit_beta": "P2",
    "micro.evidence": "P2",
    "calc.depth.average": "P2",
    "calc.observation.build": "P2",
    "calc.fit.price_impact": "P2",
    "calc.fit.depth_scaling": "P2",
    # P3 — market correlation (Redis plane)
    "market.read": "P3",
    "market.derivatives": "P3",
    "market.keystone_history": "P3",
    "market.wall_history": "P3",
    # P3 — substrate capability plane (tool-first)
    "substrate.read": "P3",
    "substrate.invoke": "P3",
    "substrate.anchors": "P3",
    "substrate.density": "P3",
    "substrate.delta": "P3",
    "substrate.ladders": "P3",
    "substrate.large_print": "P3",
    "substrate.migration": "P3",
    "substrate.oi": "P3",
    "substrate.signals": "P3",
    "substrate.tape": "P3",
    "substrate.technicals": "P3",
    "substrate.tiers": "P3",
    "substrate.volume_profile": "P3",
    # P5 — paper grounding + derived ΔP + Track D forward stack
    "memory.recall_paper": "P5",
    "calc.derived_diagnostic": "P5",
    "calc.price.delta": "P5",
    "calc.scenario.evaluate": "P5",
    "calc.feature.build": "P5",
    "calc.forward.join": "P5",
    "calc.forward.fit": "P5",
    "calc.forward.distribution": "P5",
    "calc.forward.scenario": "P5",
    "calc.forward.forecast": "P5",
    "calc.hypothesis.test": "P5",
    "calc.events.absorption": "P3",
    "calc.events.walls": "P3",
    "calc.decay.report": "P5",
    "calc.discipline.audit": "P5",
}


TOOL_HOME_DEFAULT: tuple[str, str] = ("evidence", "acquisition")


TOOL_HOME_OVERRIDES: dict[str, tuple[str, str]] = {
    # Preferred homes.  P5 deterministic calculations may be requested on
    # the final evidence pass or during reasoning analysis, so their allowed
    # homes are declared separately below.
    "calc.hypothesis.test": ("reasoning", "analysis"),
    "calc.discipline.audit": ("validation", "gate"),
}


TOOL_HOME_OPTIONS_OVERRIDES: dict[str, tuple[tuple[str, str], ...]] = {
    "memory.recall_paper": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.ofi.intervals": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.depth.average": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.observation.build": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.fit.price_impact": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.fit.depth_scaling": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.derived_diagnostic": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.price.delta": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.scenario.evaluate": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.feature.build": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.forward.forecast": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.forward.join": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.forward.fit": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.forward.distribution": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.forward.scenario": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.decay.report": (("evidence", "acquisition"), ("reasoning", "analysis")),
    "calc.hypothesis.test": (("reasoning", "analysis"),),
    "calc.discipline.audit": (("validation", "gate"),),
}


TOOL_LOOP_DEFAULT = TOOL_HOME_DEFAULT[0]


TOOL_LOOP_OVERRIDES: dict[str, str] = {
    tool: home[0] for tool, home in TOOL_HOME_OVERRIDES.items()
}
TOOL_LOOP_OVERRIDES.update({
    "micro.capture_status": "evidence",
    "micro.fit_beta": "evidence",
})


def tool_homes(tool: str) -> tuple[tuple[str, str], ...]:
    """Return all FSM-authorized homes for a tool."""
    return TOOL_HOME_OPTIONS_OVERRIDES.get(
        tool, (TOOL_HOME_OVERRIDES.get(tool, TOOL_HOME_DEFAULT),)
    )


def tool_home(tool: str) -> tuple[str, str]:
    """Return the preferred nested-loop/sub-loop home for a tool."""
    return TOOL_HOME_OVERRIDES.get(tool, tool_homes(tool)[-1])


def tool_loop(tool: str) -> str:
    """The parent loop a tool call executes inside (compatibility view)."""
    return tool_home(tool)[0]


_PHASE_ORDER = ("P1", "P2", "P3", "P4", "P5")


_REQUIRED_PHASES = ("P1", "P2", "P3", "P5")


def _bounded(values: list[Any], cap: int) -> list[Any]:
    """Hard output bound for tool payloads (context-budget discipline)."""
    return values[:cap]


def _norm_tool_key(value: str) -> str:
    return value.replace("_", ".")


_TOOL_ALIASES: dict[str, str] = {
    _norm_tool_key(_alias_key): _alias_key for _alias_key in TOOL_NAMES
}
_TOOL_ALIASES.update({
    "micro.ofi": "micro.ofi_intervals",
    "micro.fit": "micro.fit_beta",
    "micro.status": "micro.capture_status",
    "micro.capture": "micro.capture_status",
    "calc.ofi": "calc.ofi.intervals",
    "calc.ad": "calc.depth.average",
    "calc.depth": "calc.depth.average",
    "calc.observations": "calc.observation.build",
    "calc.observation": "calc.observation.build",
    "calc.derived": "calc.derived_diagnostic",
    "market.history": "market.keystone_history",
})


def _normalize_tool_name(name: Any) -> str | None:
    """Resolve an LLM-supplied tool name to its canonical registry key.

    Accepts the exact key plus separator variants (``calc.ofi_intervals`` /
    ``calc.ofi.intervals``) and a small explicit alias map for truncated
    names. Returns None when nothing matches — the caller denies with the
    attempted payload attached for debuggability.
    """
    if not isinstance(name, str):
        return None
    cleaned = name.strip().lower()
    normalized = _norm_tool_key(cleaned)
    if normalized in _TOOL_ALIASES:
        return _TOOL_ALIASES[normalized]
    return None

