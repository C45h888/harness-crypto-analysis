"""Deterministic principles — the advisory guard beside the gates.

LAYER: base abstraction, next to ``engine/gates.py``. Gates return
PASS/REFUSED (binary, terminal-capable). Principles return FINDINGS
(advisory only): evaluated per turn against the classified controller
outcomes, the frozen task plan, and the parsed turn, they steer the agent
and land on the artifact's finding records — they never deny a dispatch
and never route a terminal. That is what keeps them a guard, not a gate.

Seed set (Option-2 + planning-layer pass):

  - ``fetch_window``      — the fetch turn establishes tape quality and
    snapshot context only; window membership is evaluated HERE (single
    vocabulary shared with the fetch dispatcher), never as an inline
    prefix check that can drift.
  - ``hypothesis_timing`` — H0 content before the hypothesize position
    is drift (the hard track already denies the tool; the principle
    names the finding so the agent stops forming early).
  - ``scenario_coverage`` — a target-bearing plan with no scenario-tool
    attempt yet is a finding from mid-loop onward (blocking stays at
    the validation gate, where it always was).

Pure: no state, no I/O, no imports from the runtime (the controller is
read through duck-typed ``tool_state`` guarded by try/except).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# The fetch window (single vocabulary — the dispatcher reads this too)
# ---------------------------------------------------------------------------

#: Tool-name prefixes the fetch turn may command (tape-quality surface).
FETCH_PREFIXES: tuple[str, ...] = ("micro.",)

#: Exact tool names admitted besides the prefixes (read-only snapshot
#: context — invokes stay in evidence/reasoning where intent exists).
FETCH_TOOLS: tuple[str, ...] = ("market.read", "substrate.read")


def in_fetch_window(canonical: str) -> bool:
    """Whether a canonical tool name may be commanded on the fetch turn."""
    name = (canonical or "").strip()
    if not name:
        return False
    if name in FETCH_TOOLS:
        return True
    return any(name.startswith(prefix) for prefix in FETCH_PREFIXES)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrincipleFinding:
    """One advisory finding: principle name + deterministic detail.

    ``severity`` is always ``\"advisory\"`` today — the field exists so a
    future pass can tier findings without changing the shape.
    """

    principle: str
    detail: str
    severity: str = "advisory"


def _window_violations(turn: Any) -> list[PrincipleFinding]:
    calls = (turn or {}).get("tool_calls") if isinstance(turn, dict) else None
    if not isinstance(calls, list):
        return []
    findings: list[PrincipleFinding] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or call.get("tool") or "").strip()
        if name and not in_fetch_window(name):
            findings.append(PrincipleFinding(
                principle="fetch_window",
                detail=f"{name} is outside the fetch window "
                       f"(prefixes {list(FETCH_PREFIXES)}, tools {list(FETCH_TOOLS)}) — "
                       "fetch establishes tape quality + snapshot context only",
            ))
    return findings


def _hypothesis_timing(
    turn: Any, reason_position: int,
) -> list[PrincipleFinding]:
    hypothesis = (turn or {}).get("hypothesis") if isinstance(turn, dict) else None
    h0 = str((hypothesis or {}).get("H0") or "").strip() if isinstance(
        hypothesis, dict) else ""
    if h0 and reason_position < 2:
        return [PrincipleFinding(
            principle="hypothesis_timing",
            detail="H0 formed before the hypothesize position — hold "
                   "hypothesis formation until the distribution exists "
                   "(position 3); earlier H0 is drift, not grounding",
        )]
    return []


def _tool_attempted(controller: Any, tool: str) -> bool:
    """True when the controller has any terminal reading on the tool."""
    try:
        state = controller.tool_state(tool)
    except Exception:
        return False
    return getattr(state, "value", state) in ("evaluated", "refused")


def _scenario_coverage(
    controller: Any, plan: Any,
) -> list[PrincipleFinding]:
    targets = (plan or {}).get("targets") or [] if isinstance(plan, dict) else []
    invalidations = (plan or {}).get("invalidations") or [] if isinstance(
        plan, dict) else []
    if not (targets or invalidations):
        return []
    if controller is None:
        return []
    if _tool_attempted(controller, "calc.forward.scenario"):
        return []
    if _tool_attempted(controller, "calc.scenario.evaluate"):
        return []
    return [PrincipleFinding(
        principle="scenario_coverage",
        detail="target-bearing plan with no scenario-tool attempt yet "
               "(calc.forward.scenario / calc.scenario.evaluate) — the "
               "verdict the plan demands has no tool behind it",
    )]


def evaluate_principles(
    *, controller: Any, plan: Any, turn: Any, reason_position: int = 0,
    fetch_turn: bool = False,
) -> list[PrincipleFinding]:
    """Evaluate the seed principles over one turn (pure).

    ``plan`` is the frozen task plan (or None pre-receipt — plan-derived
    principles skip). ``reason_position`` is the hard-track index (0-2).
    ``fetch_turn`` scopes the window principle to the fetch turn only —
    later loops command the full registry by design. Returns advisory
    findings only; an empty list means conformant.
    """
    findings: list[PrincipleFinding] = []
    if fetch_turn:
        findings.extend(_window_violations(turn))
    findings.extend(_hypothesis_timing(turn, reason_position))
    findings.extend(_scenario_coverage(controller, plan))
    return findings


def render_findings(findings: list[PrincipleFinding]) -> str:
    """Render findings as a prompt steer block (pure, empty string when none)."""
    if not findings:
        return ""
    lines = [
        "PRINCIPLES (deterministic advisory guard — findings to record, "
        "never denials, never re-calls):",
    ]
    lines.extend(f"- [{f.principle}] {f.detail}" for f in findings)
    return "\n".join(lines) + "\n"


__all__ = [
    "FETCH_PREFIXES",
    "FETCH_TOOLS",
    "PrincipleFinding",
    "evaluate_principles",
    "in_fetch_window",
    "render_findings",
]
