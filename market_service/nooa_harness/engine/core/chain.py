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


def _task_bears_horizons(task: str | None, scenario: dict[str, Any] | None) -> bool:
    """Deterministic task-shape read (keyword + scenario presence)."""
    if scenario is not None:
        return True
    blob = (task or "").lower()
    return any(hint in blob for hint in _HORIZON_HINTS)


def _task_bears_hypotheses(task: str | None, scenario: dict[str, Any] | None) -> bool:
    """Deterministic hypothesis-language read (keyword + scenario presence)."""
    if scenario is not None:
        return True
    blob = (task or "").lower()
    return any(hint in blob for hint in _HYPOTHESIS_HINTS)


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
