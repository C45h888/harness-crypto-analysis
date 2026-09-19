"""Steady-track statistical chain — the agentic loop's fixed rail.

The canonical workflow the agent must walk, in order:

    Run A → Run B → Run Multivariate → Compare → Check Validation → Produce Common Output

Run A / Run B / Multivariate / Compare execute INSIDE ``calc.forward.forecast``
(the tool runs Route A → Route B → Route C → ``assemble_forecast_result`` and
leaves the ordered ``chain_trace`` receipt in the ForecastResult diagnostics).
The loop therefore commands one tool and VERIFIES the chain — it never
re-sequences the statistics itself.

This module owns the chain DEFINITION and the completion CHECK (pure reads
over the controller's classified outcomes). It never dispatches, never
prompts, never re-estimates. Enforcement rides the existing validation
repair path: required-but-never-attempted links steer a re-call; refused
links are findings (the chain continues, null discipline intact).

Link optionality (deterministic, task-derived):
- forecast   — always required (attempted; refusal is a finding).
- scenario   — required iff the task/scenario bears horizons or targets.
- hypothesis — required iff the task bears hypothesis language (H0/H1 need
               a post-fit test; the test itself still needs its
               pre-registered hypothesis_id from the agent).
- decay      — required iff the forecast evaluated (nothing else to trust).
- discipline — always required (validation home; attempt is the requirement).
"""

from __future__ import annotations

import json
from typing import Any

FORECAST_TOOL = "calc.forward.forecast"
FORWARD_SCENARIO_TOOL = "calc.forward.scenario"
HYPOTHESIS_TOOL = "calc.hypothesis.test"
DECAY_TOOL = "calc.decay.report"
DISCIPLINE_TOOL = "calc.discipline.audit"

# Diagnostic assembly substrates (pass-1 re-derivation surface).
OFI_TOOL = "calc.ofi.intervals"
AD_TOOL = "calc.depth.average"
JOIN_TOOL = "calc.forward.join"
FIT_TOOL = "calc.forward.fit"
DISTRIBUTION_TOOL = "calc.forward.distribution"

# Ordered links: (step, tool, meaning). Required-ness is computed per cycle
# by _link_required — the order here is the steady track itself.
CHAIN_LINKS: tuple[tuple[str, str, str], ...] = (
    ("run_forecast", FORECAST_TOOL,
     "canonical ForecastResult incl. internal A → B → Multivariate → Compare"),
    ("run_scenario", FORWARD_SCENARIO_TOOL,
     "horizon-native P(T)/P(S) when the task bears horizons/targets"),
    ("run_hypothesis", HYPOTHESIS_TOOL,
     "post-fit H0/H1 test when the task bears hypothesis language"),
    ("run_decay", DECAY_TOOL,
     "which horizons survive, once the forecast evaluated"),
    ("check_validation", DISCIPLINE_TOOL,
     "9-lock discipline audit (validation home)"),
)

_HORIZON_HINTS = ("horizon", "1s", "5s", "30s", "60s", "target",
                  "theta", "probab")
_HYPOTHESIS_HINTS = ("hypothes", "h0", "h1")


def _task_bears_horizons(task: Any, scenario: dict[str, Any] | None) -> bool:
    """Deterministic task-shape read (keyword + scenario presence)."""
    if scenario is not None:
        return True
    blob = task if isinstance(task, str) else (json.dumps(task) if task else "")
    return any(hint in blob.lower() for hint in _HORIZON_HINTS)


def _task_bears_hypotheses(task: Any, scenario: dict[str, Any] | None) -> bool:
    """Deterministic hypothesis-language read (keyword + scenario presence)."""
    if scenario is not None:
        return True
    blob = task if isinstance(task, str) else (json.dumps(task) if task else "")
    return any(hint in blob.lower() for hint in _HYPOTHESIS_HINTS)


def _link_required(
    tool: str, *, task: str | None, scenario: dict[str, Any] | None,
    forecast_evaluated: bool,
) -> bool:
    """Whether a chain link is required this cycle (deterministic)."""
    if tool == FORECAST_TOOL:
        return True
    if tool == FORWARD_SCENARIO_TOOL:
        return _task_bears_horizons(task, scenario)
    if tool == HYPOTHESIS_TOOL:
        return _task_bears_hypotheses(task, scenario)
    if tool == DECAY_TOOL:
        return forecast_evaluated
    if tool == DISCIPLINE_TOOL:
        return True
    return False


def chain_completion(
    controller: Any, *, task: str | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Completion of the steady track from classified outcomes (pure).

    Reads ``controller.tool_state`` / ``tool_refusal_reason`` (the same
    tri-state the scenario steer uses) so pre-LLM gather outcomes count:
    the pre-acquired forecast satisfies its link without an agent call.
    Returns links (ordered, with state/required/reason), missing[] (required
    + NOT_CALLED only — refusals are findings, never repair demands),
    refused[] (required + REFUSED, cited not re-called), and complete.
    """
    scenario = scenario if scenario is not None else getattr(
        controller, "scenario", None)
    states: dict[str, str] = {}
    for _, tool, _ in CHAIN_LINKS:
        try:
            states[tool] = controller.tool_state(tool).value
        except Exception:
            states[tool] = "not_called"
    forecast_evaluated = states.get(FORECAST_TOOL) == "evaluated"
    links: list[dict[str, Any]] = []
    missing: list[str] = []
    refused: list[dict[str, str]] = []
    for step, tool, meaning in CHAIN_LINKS:
        required = _link_required(
            tool, task=task, scenario=scenario,
            forecast_evaluated=forecast_evaluated)
        state = states.get(tool, "not_called")
        reason: str | None = None
        if state == "refused":
            try:
                reason = controller.tool_refusal_reason(tool)
            except Exception:
                reason = None
        links.append({"step": step, "tool": tool, "meaning": meaning,
                      "state": state, "required": required,
                      "reason": reason})
        if not required:
            continue
        if state == "not_called":
            missing.append(tool)
        elif state == "refused":
            refused.append({"tool": tool, "reason": reason or "deterministic refusal"})
    return {"links": links, "missing": missing, "refused": refused,
            "complete": not missing,
            "render": _render(links, missing, refused)}


def _render(
    links: list[dict[str, Any]], missing: list[str],
    refused: list[dict[str, str]],
) -> str:
    """One-line deterministic chain status for prompts and artifacts."""
    done = [lnk["tool"] for lnk in links
            if lnk["required"] and lnk["state"] == "evaluated"]
    return (
        "STATISTICAL CHAIN — done: "
        f"[{', '.join(done) or 'none'}]; "
        f"missing: [{', '.join(missing) or 'complete'}]; "
        f"refused-findings: [{', '.join(r['tool'] for r in refused) or 'none'}]."
    )


def chain_steer(completion: dict[str, Any]) -> str:
    """Repair steering from the completion — correct by construction.

    NOT_CALLED required links → CALL steer (named tools, in track order).
    REFUSED links → citation steer, never a re-call demand (refusal is
    terminal per tool/cycle; the controller already suppresses repeats).
    Empty when the track is complete.
    """
    parts: list[str] = []
    missing = completion.get("missing") or []
    if missing:
        parts.append(
            "STATISTICAL CHAIN INCOMPLETE — walk the steady track in order, "
            f"calling each missing link before finalizing: {', '.join(missing)}. "
            "Refusals are findings to record with reasons, never failures to repair."
        )
    for entry in completion.get("refused") or []:
        tool = entry.get("tool", "")
        reason = entry.get("reason") or "deterministic refusal"
        parts.append(
            f"{tool} already dispatched and refused ({reason}). Do NOT "
            f"re-call it — cite `{tool} → refusal` as a finding."
        )
    return "\n".join(parts)


def json_dumps_short(value: Any, cap: int = 2_000) -> str:
    """Bounded JSON rendering for chain payloads in prompts (never raises)."""
    try:
        return json.dumps(value, default=str)[:cap]
    except Exception:
        return str(value)[:cap]


# ---------------------------------------------------------------------------
# Reasoning positions — the hard-tracked 3-pass floor inside ANALYSIS
# ---------------------------------------------------------------------------
# Pass 1 ASSEMBLE re-derives the substrates (double replay accepted):
# OFI (Route A) → AD (Route B) → multivariate join. Pass 2 INTERPRET fits
# the equation and reads the distribution. Pass 3 HYPOTHESIZE forms the
# testable H0 (only here — never before the distribution exists) and tests
# it. Positions advance one per pass, each requiring ≥1 in-position
# dispatch; the loop cannot exit before all three are complete. All three
# run under the ANALYSIS observation (the iterating core), so no governance
# movement is involved — ordering is enforced by position predicates plus
# out-of-position denial in the dispatch path.

REASONING_MIN_POSITIONS = 3

POSITION_ORDER: tuple[str, ...] = ("assemble", "interpret", "hypothesize")

POSITION_TOOLS: dict[str, tuple[str, ...]] = {
    "assemble": (OFI_TOOL, AD_TOOL, JOIN_TOOL),
    "interpret": (FIT_TOOL, DISTRIBUTION_TOOL, DECAY_TOOL),
    "hypothesize": (HYPOTHESIS_TOOL,),
}

# Reverse index: tool → its position (tools absent here are unordered).
TOOL_POSITION: dict[str, str] = {
    tool: position
    for position, tools in POSITION_TOOLS.items()
    for tool in tools
}
TOOL_POSITION[FORWARD_SCENARIO_TOOL] = "hypothesize"


def _position_required(
    tool: str, position: str, *, task: str | None,
    scenario: dict[str, Any] | None,
) -> bool:
    """Whether a position tool is required this cycle (deterministic)."""
    if position == "hypothesize" and tool == FORWARD_SCENARIO_TOOL:
        return _task_bears_horizons(task, scenario)
    return True


def _addressed(outcomes: Any, tool: str) -> str | None:
    """Position reading over raw outcomes (denials never satisfy).

    Returns 'evaluated' / 'refused' / None. Unlike the controller tri-state
    (where denied/error classify as REFUSED), authorization denials —
    including out-of-position denials — must neither satisfy a position nor
    trigger a halt. Only real executions (result == 'ok') count.
    """
    for outcome in reversed(tuple(outcomes or ())):
        if getattr(outcome, "canonical", None) != tool:
            continue
        if getattr(outcome, "result", None) != "ok":
            continue
        if getattr(outcome, "refused", False):
            return "refused"
        return "evaluated"
    return None


def position_status(
    outcomes: Any, position: str, *, task: str | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Completion of one reasoning position from raw outcomes (pure).

    A position is done when nothing is missing — refusals ride forward as
    findings (assemble refusals halt separately via :func:`chain_halt_for`,
    which reads the ``refused`` list, not this flag).
    """
    tools = list(POSITION_TOOLS.get(position, ()))
    if position == "hypothesize" and _task_bears_horizons(task, scenario):
        tools = tools + [FORWARD_SCENARIO_TOOL]
    links: list[dict[str, str]] = []
    missing: list[str] = []
    refused: list[dict[str, str]] = []
    for tool in tools:
        if not _position_required(tool, position, task=task, scenario=scenario):
            continue
        state = _addressed(outcomes, tool)
        links.append({"tool": tool, "state": state or "not_called"})
        if state is None:
            missing.append(tool)
        elif state == "refused":
            reason = None
            for outcome in reversed(tuple(outcomes or ())):
                if (getattr(outcome, "canonical", None) == tool
                        and getattr(outcome, "refused", False)):
                    reason = getattr(outcome, "refusal_reason", None)
                    break
            refused.append({"tool": tool, "reason": reason or "deterministic refusal"})
    return {"position": position, "links": links, "missing": missing,
            "refused": refused, "complete": not missing}


def chain_halt_for(
    outcomes: Any, position: str, *, task: str | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Halt record when a required link refused in this position (pure).

    Halt applies to the ASSEMBLE position only: a refused foundation
    (OFI/AD/join) makes everything downstream meaningless, so the loop
    halts and the halt is handed to the FSM for the retry decision — the
    loop does not advance, re-call, or repair around it. Refusals in
    INTERPRET/HYPOTHESIZE are findings the track carries forward (the
    positions still advance once nothing is missing). Returns None when
    no halt is warranted.
    """
    if position != "assemble":
        return None
    status = position_status(outcomes, position, task=task, scenario=scenario)
    if not status["refused"]:
        return None
    first = status["refused"][0]
    return {"position": position, "tool": first["tool"],
            "reason": first["reason"]}


POSITION_BLURB: dict[str, str] = {
    "assemble": ("POSITION 1/3 ASSEMBLE — re-derive the substrates, no interpretation: "
                 "calc.ofi.intervals (Route A OFI first) → calc.depth.average (Route B AD) → "
                 "calc.forward.join (multivariate X_t joined to Y(h)). Later-position tools "
                 "called here are denied as out-of-position findings, never dispatched."),
    "interpret": ("POSITION 2/3 INTERPRET — fit the equation and read the distribution: "
                  "calc.forward.fit → calc.forward.distribution → calc.decay.report. "
                  "Form no hypothesis here; that belongs to position 3."),
    "hypothesize": ("POSITION 3/3 HYPOTHESIZE — think against the user prompt, form the "
                    "testable H0 (only now that the distribution exists) and run "
                    "calc.hypothesis.test with its hypothesis_id (+ calc.forward.scenario "
                    "when horizons/targets are present). Then close with tool_calls=[]."),
}


def position_steer(status: dict[str, Any]) -> str:
    """Steer for one reasoning position (deterministic, core-owned)."""
    position = status.get("position", "")
    parts = [POSITION_BLURB.get(position, "")]
    if status.get("missing"):
        parts.append(
            f"Still missing in {position}: {', '.join(status['missing'])} — "
            "call each missing link before anything else.")
    for entry in status.get("refused") or []:
        parts.append(
            f"{entry.get('tool')} refused ({entry.get('reason')}) — the track halts "
            "here; record the refusal as a finding and stop dispatching.")
    return "\n".join(p for p in parts if p)


def halt_steer(halt: dict[str, str]) -> str:
    """Reformulation steer for a halted track (deterministic, core-owned).

    The track is frozen at ``halt["position"]``: positions do not advance
    and the halt never self-clears. The ONLY way forward is re-calling the
    halted tool with materially different args (window, interval) —
    suppression lifts for new inputs only. A repeat with identical args is
    suppressed; anything else is out-of-position denied. Closing with
    ``tool_calls=[]`` exits to validation with the halt intact.
    """
    return (
        f"TRACK HALTED at {halt.get('position')} — {halt.get('tool')} refused "
        f"({halt.get('reason')}). Positions are frozen; nothing advances past "
        "this point on the current inputs. Either re-call "
        f"{halt.get('tool')} with materially different args (e.g. a wider "
        "window_minutes) to reformulate, or close with tool_calls=[] and "
        "carry the halt to validation for the FSM retry decision."
    )
