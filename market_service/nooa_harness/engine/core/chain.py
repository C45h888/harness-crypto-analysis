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

# ---------------------------------------------------------------------------
# Staged ANALYSIS (Phases 0-2): 2a BASE-INTERPRET → 2b REFINED-ARCHAIC
# ---------------------------------------------------------------------------
# ANALYSIS.2a (assemble) re-derives the substrates and EXITS through
# ``calc.forward.join``: join.n_pairs > 0 is the exit gate — nothing in 2b
# is callable until the join proves pairs exist. The assemble close freezes
# ``base_summary()`` + writes a base observation to the task-segment memory.
# ANALYSIS.2b (interpret) runs the refined/archaic equation stack
# (fit → distribution/delta → decay → conditional scenario/events) with
# DATA-READY preconditions (TOOL_PRECONDITIONS): each refined tool names
# the accumulated payload fields it reads, so premature calls become
# ``tool.premature`` findings pre-dispatch — never executed, never burning
# a tool round. All positions still run under the ANALYSIS observation;
# ordering is enforced by position predicates + precondition gate +
# out-of-position denial in the dispatch path.

POSITION_ORDER: tuple[str, ...] = ("assemble", "interpret", "hypothesize")

POSITION_TOOLS: dict[str, tuple[str, ...]] = {
    "assemble": (OFI_TOOL, AD_TOOL, JOIN_TOOL),
    # 2b refined-archaic: forecast is the PRIMARY statistical truth
    # (multivariate A→B→C + distribution) — agent-pulled here only, never
    # in CONTEXT (the plan-bound digest is context, this call is proof).
    "interpret": (FIT_TOOL, DISTRIBUTION_TOOL, DECAY_TOOL, FORECAST_TOOL),
    "hypothesize": (HYPOTHESIS_TOOL,),
}

# Reverse index: tool → its position (tools absent here are unordered).
# FORECAST_TOOL is pinned to interpret (2b): agent calls outside 2b are
# out-of-position denied — the multivariate truth is earned in the archaic
# pass, never pulled early. The deterministic plan-bound digest in CONTEXT
# is a different authority (receipt, not agent proof) and is unaffected.
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
                    "testable H0 (only now that the distribution + forecast primary truth "
                    "exists) IN LINE WITH the HYPOTHESIS FRAME rendered below, and run "
                    "calc.hypothesis.test with its hypothesis_id (+ calc.forward.scenario "
                    "when horizons/targets are present). The frame crosses the prompt "
                    "variable with actual data; empty prompt → data builds H0/H1. "
                    "Then close with tool_calls=[]."),
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


# ---------------------------------------------------------------------------
# Phase 0 — base_summary: frozen 2a exit payload (pure, never raises)
# ---------------------------------------------------------------------------
def _unwrap_data(value: Any) -> Any:
    """Unwrap a ToolResult envelope to its ``data`` payload (pure)."""
    if isinstance(value, dict) and "tool" in value and "data" in value:
        return value.get("data")
    return value


def _mean_decimal_str(values: list[Any]) -> str | None:
    """Mean of decimal-string values as string (pure, None when empty)."""
    try:
        from decimal import Decimal
        decs: list[Any] = []
        for v in values:
            if v is None:
                continue
            try:
                decs.append(Decimal(str(v)))
            except Exception:
                continue
        if not decs:
            return None
        return str(sum(decs) / len(decs))
    except Exception:
        return None


def base_summary(accumulated: dict[str, Any]) -> dict[str, Any]:
    """Frozen 2a exit payload from accumulated tool results (pure).

    Reads the base tool payloads the assemble position produced and
    reduces them to the scalars the refined stage gates on:
    ``mean_ofi`` (from calc.ofi.intervals), ``mean_ad`` (from
    calc.depth.average), ``n_pairs`` (from calc.forward.join), plus
    quality / counts for the DATA CHECKS block. Missing tools yield
    None fields — never raises, never invents values.
    """
    try:
        ofi_data = _unwrap_data(accumulated.get(OFI_TOOL))
        ad_data = _unwrap_data(accumulated.get(AD_TOOL))
        join_data = _unwrap_data(accumulated.get(JOIN_TOOL))
        ofi_vals: list[Any] = []
        qualities: set[str] = set()
        n_intervals: int | None = None
        if isinstance(ofi_data, list):
            n_intervals = len(ofi_data)
            for row in ofi_data:
                if not isinstance(row, dict):
                    continue
                if row.get("ofi") is not None:
                    ofi_vals.append(row.get("ofi"))
                if row.get("quality"):
                    qualities.add(str(row.get("quality")))
        mean_ofi = _mean_decimal_str(ofi_vals)
        mean_ad: str | None = None
        n_ad: int | None = None
        if isinstance(ad_data, dict):
            raw_mean = ad_data.get("mean_ad")
            mean_ad = str(raw_mean) if raw_mean is not None else None
            try:
                n_ad = int(ad_data.get("n_intervals")) \
                    if ad_data.get("n_intervals") is not None else None
            except Exception:
                n_ad = None
        n_pairs: int | None = None
        exclusions = 0
        if isinstance(join_data, dict):
            try:
                n_pairs = int(join_data.get("n_pairs")) \
                    if join_data.get("n_pairs") is not None else None
            except Exception:
                n_pairs = None
            try:
                log = join_data.get("exclusion_log") or {}
                if isinstance(log, dict):
                    exclusions = int(log.get("excluded", 0) or 0)
                elif isinstance(log, list):
                    exclusions = len(log)
            except Exception:
                exclusions = 0
        return {
            "mean_ofi": mean_ofi,
            "mean_ad": mean_ad,
            "n_pairs": n_pairs,
            "n_ofi_intervals": n_intervals,
            "n_ad_intervals": n_ad,
            "qualities": sorted(qualities),
            "exclusions": exclusions,
            "base_complete": (
                mean_ofi is not None
                and mean_ad is not None
                and n_pairs is not None
                and n_pairs > 0
            ),
        }
    except Exception:
        return {
            "mean_ofi": None, "mean_ad": None, "n_pairs": None,
            "n_ofi_intervals": None, "n_ad_intervals": None,
            "qualities": [], "exclusions": 0, "base_complete": False,
        }


# ---------------------------------------------------------------------------
# Phase 1 — TOOL_PRECONDITIONS: data-ready gates for the refined stack (pure)
# ---------------------------------------------------------------------------
# Each refined tool names the accumulated-payload reads it requires.
# Checked PRE-DISPATCH in driver._dispatch_turn: a violation becomes a
# ``tool.premature`` finding (named needs, never executed, never burning a
# tool round, controller stays NOT_CALLED). Refusals/denials upstream also
# block: a refused fit never unlocks distribution.
DELTA_TOOL = "calc.price.delta"
DERIVED_TOOL = "calc.derived_diagnostic"

TOOL_PRECONDITIONS: dict[str, tuple[str, ...]] = {
    # 2a bridge: join needs at least one base substrate to join against.
    JOIN_TOOL: ("base:ofi_or_ad_present",),
    # 2b refined: fit needs pairs; distribution/delta need a usable fit;
    # decay needs at least one fit; scenario needs a distribution (or the
    # context-seeded forecast); hypothesis needs a fit + pre-registered id
    # (id itself is engine-supplied at dispatch, so only the fit gates here).
    FIT_TOOL: ("join.n_pairs>0",),
    # Forecast primary truth: earned in 2b after the base exit gate. The
    # digest in CONTEXT does not satisfy this — the agent call does.
    FORECAST_TOOL: ("join.n_pairs>0",),
    DISTRIBUTION_TOOL: ("fit.status usable",),
    DELTA_TOOL: ("fit.status usable",),
    DERIVED_TOOL: ("fit.status usable",),
    DECAY_TOOL: ("fit.present",),
    FORWARD_SCENARIO_TOOL: ("distribution.present or forecast.evaluated",),
    HYPOTHESIS_TOOL: ("fit.present",),
}


def _fit_status(accumulated: dict[str, Any]) -> str | None:
    """Usable-fit read over accumulated (unwrapped, pure)."""
    fit = _unwrap_data(accumulated.get(FIT_TOOL))
    if isinstance(fit, dict):
        status = fit.get("status")
        return str(status) if status is not None else None
    return None


def _has_tool_data(accumulated: dict[str, Any], tool: str) -> bool:
    """Whether a tool has non-null accumulated data (pure)."""
    data = _unwrap_data(accumulated.get(tool))
    if data is None:
        return False
    if isinstance(data, dict) and not data:
        return False
    if isinstance(data, list) and not data:
        return False
    return True


def check_tool_preconditions(
    canonical: str, accumulated: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Data-ready gate for one canonical tool (pure).

    Returns ``(ok, needs)`` — ``needs`` names the unmet reads so the
    dispatch path can render a ``tool.premature`` finding. Tools absent
    from TOOL_PRECONDITIONS are unconditionally ready (base tools +
    validation-home discipline + reads). Never raises.
    """
    try:
        if canonical not in TOOL_PRECONDITIONS:
            return True, []
        needs: list[str] = []
        if canonical == JOIN_TOOL:
            if not (_has_tool_data(accumulated, OFI_TOOL)
                    or _has_tool_data(accumulated, AD_TOOL)):
                needs.append("base substrate first: call calc.ofi.intervals "
                             "and calc.depth.average before calc.forward.join")
        elif canonical == FIT_TOOL:
            join = _unwrap_data(accumulated.get(JOIN_TOOL))
            n_pairs: int | None = None
            if isinstance(join, dict):
                try:
                    n_pairs = int(join.get("n_pairs")) \
                        if join.get("n_pairs") is not None else None
                except Exception:
                    n_pairs = None
            if n_pairs is None or n_pairs <= 0:
                needs.append("join.n_pairs>0: call calc.forward.join and "
                             "confirm pairs exist before calc.forward.fit")
        elif canonical == FORECAST_TOOL:
            join = _unwrap_data(accumulated.get(JOIN_TOOL))
            n_pairs = None
            if isinstance(join, dict):
                try:
                    n_pairs = int(join.get("n_pairs")) \
                        if join.get("n_pairs") is not None else None
                except Exception:
                    n_pairs = None
            if n_pairs is None or n_pairs <= 0:
                needs.append("join.n_pairs>0: call calc.forward.join and "
                             "confirm pairs exist before calc.forward.forecast "
                             "(primary truth is earned in 2b, never pulled early)")
        elif canonical in (DISTRIBUTION_TOOL, DELTA_TOOL, DERIVED_TOOL):
            status = _fit_status(accumulated)
            if status not in ("validated", "provisional", "calibrated"):
                needs.append(
                    f"usable fit first (fit.status={status!r}): call "
                    "calc.forward.fit and confirm status validated/provisional "
                    f"before {canonical}; insufficient fits reform the window, "
                    "never advance to distribution")
        elif canonical == DECAY_TOOL:
            if not _has_tool_data(accumulated, FIT_TOOL):
                needs.append("forward fit first: call calc.forward.fit "
                             "before calc.decay.report")
        elif canonical == FORWARD_SCENARIO_TOOL:
            has_dist = _has_tool_data(accumulated, DISTRIBUTION_TOOL)
            has_forecast = _has_tool_data(accumulated, FORECAST_TOOL)
            if not (has_dist or has_forecast):
                needs.append("distribution or forecast first: call "
                             "calc.forward.distribution (or rely on the "
                             "context-seeded calc.forward.forecast) before "
                             "calc.forward.scenario")
        elif canonical == HYPOTHESIS_TOOL:
            if not _has_tool_data(accumulated, FIT_TOOL):
                needs.append("forward fit first: call calc.forward.fit "
                             "before calc.hypothesis.test (hypothesis_id is "
                             "engine-supplied; the fit is what you must earn)")
        return (not needs), needs
    except Exception:
        return True, []


# ---------------------------------------------------------------------------
# Hypothesis builder (Slice B): prompt variable + actual data → H0/H1 frame
# ---------------------------------------------------------------------------
# The LLM's H0/H1 must be IN LINE WITH the distribution it received and
# must prove/disprove what the PROMPT asks. This builder is the
# deterministic frame the hypothesize steer renders: prompt grounding
# (directive kind/targets/seed or task text) crossed with the primary
# statistical truth (forecast multivariate + distribution + fit).
# Empty/irrelevant prompt → data builds the hypothesis (autonomous mode).
# Pure: returns the frame dict, never calls tools or LLM.
def build_hypothesis_frame(
    task: str | None,
    task_directive: dict[str, Any] | None,
    accumulated: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic H0/H1 frame from prompt + data (pure, never raises)."""
    try:
        directive = task_directive if isinstance(task_directive, dict) else {}
        kind = str(directive.get("kind") or "general")
        targets = list(directive.get("targets") or [])
        invalidations = list(directive.get("invalidations") or [])
        seed = directive.get("hypothesis_seed") if isinstance(
            directive.get("hypothesis_seed"), dict) else {}
        seed_h0 = str((seed or {}).get("H0") or "").strip() or None
        seed_h1 = str((seed or {}).get("H1") or "").strip() or None
        forecast = _unwrap_data(accumulated.get(FORECAST_TOOL))
        dist = _unwrap_data(accumulated.get(DISTRIBUTION_TOOL))
        fit = _unwrap_data(accumulated.get(FIT_TOOL))
        base = base_summary(accumulated)
        # Primary truth reads (forecast multivariate + distribution).
        y_pred: Any = None
        regime: Any = None
        if isinstance(forecast, dict):
            ft = forecast.get("forward_target") or {}
            y_pred = ft.get("y_pred")
            regime = forecast.get("regime") or forecast.get("horizon_regime")
        exp_delta: Any = None
        pi_lo: Any = None
        pi_hi: Any = None
        p_ge_zero: Any = None
        if isinstance(dist, dict):
            exp_delta = dist.get("expected_delta")
            pi_lo = dist.get("pi_95_lo")
            pi_hi = dist.get("pi_95_hi")
            p_ge_zero = dist.get("p_ge_zero")
        fit_status = fit.get("status") if isinstance(fit, dict) else None
        prompt_text = (task or "").strip()
        prompt_bearing = bool(targets or invalidations or seed_h0 or prompt_text)
        # H0/H1 construction: prompt-anchored when bearing, data-driven
        # when empty/irrelevant.
        if targets or invalidations:
            t_vals = [str(t.get("value")) for t in targets if isinstance(t, dict)]
            s_vals = [str(s.get("value")) for s in invalidations if isinstance(s, dict)]
            h0 = (f"Target {', '.join(t_vals)} NOT reachable at the asked horizon "
                  f"(invalidation {', '.join(s_vals)})" if s_vals else
                  f"Target {', '.join(t_vals)} NOT reachable at the asked horizon")
            h1 = (f"Target {', '.join(t_vals)} reachable at the asked horizon" \
                  if t_vals else "Price reaches the asked level")
            basis = "prompt_targets"
        elif seed_h0:
            h0 = seed_h0
            h1 = str((seed or {}).get("H1") or "NOT H0").strip() or "NOT H0"
            basis = "directive_seed"
        elif prompt_text:
            h0 = f"No exploitable move in the direction {prompt_text[:120]!r} implies"
            h1 = f"{prompt_text[:120]} — data supports a directional read"
            basis = "prompt_text"
        else:
            # Autonomous: data builds the hypothesis.
            direction = "flat"
            try:
                ref = y_pred if y_pred is not None else exp_delta
                direction = ("up" if ref is not None and str(ref).startswith("-") is False
                             and float(str(ref)) > 0 else
                             "down" if ref is not None and float(str(ref)) < 0 else "flat")
            except Exception:
                direction = "flat"
            h0 = (f"No directional edge (flat): distribution E[Y]={exp_delta}, "
                  f"forecast Y={y_pred}, fit={fit_status}")
            h1 = f"Directional edge {direction}: test effect>0 against the distribution"
            basis = "data_autonomous"
        return {
            "H0": h0, "H1": h1, "basis": basis,
            "prompt_bearing": prompt_bearing,
            "directive_kind": kind,
            "truth": {
                "forecast_y_pred": y_pred, "forecast_regime": regime,
                "expected_delta": exp_delta, "pi_95": [pi_lo, pi_hi],
                "p_ge_zero": p_ge_zero, "fit_status": fit_status,
                "mean_ofi": base.get("mean_ofi"),
                "mean_ad": base.get("mean_ad"),
                "n_pairs": base.get("n_pairs"),
            },
        }
    except Exception:
        return {"H0": "No directional edge", "H1": "Directional edge",
                "basis": "fallback", "prompt_bearing": False,
                "directive_kind": "general", "truth": {}}


def hypothesis_steer(frame: dict[str, Any]) -> str:
    """Render the hypothesis frame as hypothesize steer (pure)."""
    try:
        truth = frame.get("truth") or {}
        return (
            "HYPOTHESIS FRAME (prove/disprove the prompt against the data — "
            "your H0/H1 must stay in line with this distribution):\n"
            f"  H0: {frame.get('H0')}\n"
            f"  H1: {frame.get('H1')}\n"
            f"  basis: {frame.get('basis')} (prompt_bearing={frame.get('prompt_bearing')})\n"
            f"  primary truth — forecast Y={truth.get('forecast_y_pred')} "
            f"regime={truth.get('forecast_regime')} | distribution "
            f"E[Y]={truth.get('expected_delta')} PI95={truth.get('pi_95')} "
            f"P(≥0)={truth.get('p_ge_zero')} | fit={truth.get('fit_status')} | "
            f"base OFI={truth.get('mean_ofi')} AD={truth.get('mean_ad')} "
            f"n_pairs={truth.get('n_pairs')}\n"
            "Form the testable H0 now (only here — never before the "
            "distribution/forecast existed) and run calc.hypothesis.test. "
            "Empty/irrelevant prompt → the data frame above IS the hypothesis."
        )
    except Exception:
        return "Form H0/H1 in line with the distribution and test it."
