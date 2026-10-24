"""Interaction-plane segments — the one vocabulary for inner + outer models.

Inner engine (``inference/tooling/registry.py``) and outer CLIs
(``interaction_plane/cli.py``, ``commands/nooa_cli_ext.py``) import this
table. Adding a plane means adding a row here — never a new flag family.
"""

from __future__ import annotations

MODES: tuple[str, ...] = ("compact", "snapshot", "inventory", "full")

# segment → {planes, modes, tools, human_only}
# `human_only` on a mode means models get a parse warning with it,
# never a working view (see parsing.enforce_mode).
SEGMENTS: dict[str, dict] = {
    "warm": {
        "planes": ("redis",),
        "modes": ("compact", "full"),
        "tools": ("ns.read",),
        "description": "warm-plane worker projections per (substrate, symbol) + age_ms (via ns.read namespaces)",
    },
    "ledger": {
        "planes": ("redis",),
        "modes": ("snapshot", "inventory", "full"),
        "tools": ("ns.read",),
        "description": "live venue-scoped projections (microstructure + derivatives + substrate), Redis-only",
    },
    "micro": {
        "planes": ("redis",),
        "modes": ("compact", "full"),
        "tools": ("micro.status", "micro.events", "micro.ofi", "micro.evidence", "micro.fit_beta"),
        "description": "isolated microstructure capture health + tape (via ns.read namespaces)",
    },
    "inference": {
        "planes": ("redis", "postgres"),
        "modes": ("snapshot",),
        "tools": ("inference.trigger",),
        "description": "task-directed one-shot cycle — the ONLY write path",
    },
    "control": {
        "planes": ("redis",),
        "modes": ("compact",),
        "tools": ("poller-symbols", "poller-status"),
        "description": "poller symbol selection (Redis control keys, no compute)",
    },
}

# `full` is human-only everywhere it appears: models may receive it only
# with an explicit parse warning (see parsing.enforce_mode).
HUMAN_ONLY_MODES: frozenset[str] = frozenset({"full"})

# Outer CLI flag → segment. The CLI adapter (cli.py) routes purely on this.
FLAG_SEGMENTS: dict[str, str] = {
    "substrate_read": "warm",
    "read": "ledger",
    "microstructure_status": "micro",
    "inference": "inference",
    "poller": "control",
    "default": "warm",
}


def segment_names() -> list[str]:
    return sorted(SEGMENTS)


def segment_tools(segment: str) -> tuple[str, ...]:
    return tuple(SEGMENTS[segment]["tools"])


def all_tool_names() -> list[str]:
    seen: list[str] = []
    for name in sorted(SEGMENTS):
        for tool in SEGMENTS[name]["tools"]:
            if tool not in seen:
                seen.append(tool)
    return seen


__all__ = [
    "FLAG_SEGMENTS",
    "HUMAN_ONLY_MODES",
    "MODES",
    "SEGMENTS",
    "all_tool_names",
    "segment_names",
    "segment_tools",
]
