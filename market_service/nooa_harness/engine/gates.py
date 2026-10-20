"""Deterministic gate substrate — the ONE home for every guard and close.

Gates are GUARDS on the loop, not workflow positions and not sub-loops. A
gate consumes a frozen, deterministic input snapshot and emits a frozen
``GateVerdict``. It performs no I/O, spends no LLM budget, and never mutates
state — so the same inputs always produce the same verdict, and the
controller/membrane routes a failure from a *data contract* instead of
inline code "doing as it pleases".

A loop close is a gate too (``GateKind.CLOSE``, ``close_loop``): the former
receipt type no longer exists. The verdict row on the engine ledger IS the
canonical record of what ran and how it closed.

Authority split:

  - THIS module owns the gate VOCABULARY, the pure verdict functions, the
    worker registry (``GATE_WORKERS`` / ``evaluate``), the single refusal
    router (``route_refusal``), and the verdict recorder (``record_verdict``).
  - ``engine/fsm.py`` owns where a refused gate TERMINATES (``GATE_REFUSED``
    and any future gate-specific terminals).
  - ``engine/controller.py`` owns classification/credit/exit: it consumes the
    verdict, records it, and asks the membrane for the terminal.
  - ``core/`` owns WHEN a gate runs in the loop body (the ``GATE_PLAN``
    step executes post-framing in ``run_comprehension``).

Workers are pure: snapshot in, verdict out. They never import ``core``.
A worker fault fails CLOSED (a refusal with a ``gate_worker_error`` reason),
never crashes a cycle.

Phase R7 (fetch semantic removed): the DATA gate is RETIRED. It was the
judgement layer for the agent-fetch semantic — it verdicted model-chosen
cadence data against the tape-quality trichotomy, and its zero-LLM
insufficient-tape refusal was the fetch turn's only reason to exist.
With comprehension as the first agentic step and plan-bound acquisition,
per-tool deterministic refusals plus the validation gate carry the same
guarantees without a pre-receipt judgement layer. The PLAN gate is
forward-only — a refused plan routes to a terminal, never to a re-plan
edge. The gate vocabulary is deliberately pure data so it may be replayed
and unit-tested with no stores.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .config import MAX_MERGED_EVIDENCE, SUMMARY_MIN_CHARS
from .ledger import LedgerKind, record_entry
from .scenario_contract import (
    numbers_equal,
    render_status_block,
    scenario_args_match,
)

log = logging.getLogger(__name__)

__all__ = [
    "GateKind",
    "GateStatus",
    "GateVerdict",
    "GateRefused",
    "evaluate_plan_gate",
    "evaluate_budget_gate",
    "require_gate",
]

# Workflow kinds a plan may declare (mirror of task_directive.KINDS; duplicated
# here as a frozen gate contract so the gate does not import the directive
# module at load time).
_SUPPORTED_PLAN_KINDS = frozenset({"price_target", "hypothesis", "general"})

# Horizon regimes a plan may declare (task_directive vocabulary).
_SUPPORTED_HORIZON_REGIMES = frozenset({"native", "long", "unsupported", "none"})

# Regimes that REQUIRE a bound forecast horizon for acquisition to proceed.
_HORIZON_BEARING_REGIMES = frozenset({"native", "long"})


class GateKind(str, Enum):
    """Which guard a verdict belongs to."""

    FRAMING = "framing"  # understanding completeness (pre-forest)
    PLAN = "plan"        # post-framing plan adequacy (forward-only)
    DATA = "data"        # data-sufficiency adjudication
    CHAIN = "chain"      # steady-track completeness
    BUDGET = "budget"    # LLM-call/tool ceiling adjudication (G4)
    CONTENT = "content"  # final-turn content contract
    CLOSE = "close"      # loop/sub-loop closure (the former receipt)


class GateStatus(str, Enum):
    """The discrete outcome of one gate."""

    PASSED = "passed"
    REFUSED = "refused"


class GateDisposition(str, Enum):
    """How a loop/sub-loop actually closed (the former receipt disposition).

    ``NOT_APPLICABLE`` and ``SKIPPED`` exist so an inapplicable or unentered
    step is disposed *explicitly* rather than fabricated (NESTED_LOOP_SPEC
    §8.4: no invented work, no no-op event parade).
    """

    COMPLETED = "completed"
    NOT_APPLICABLE = "not_applicable"
    REFUSED = "refused"
    DENIED = "denied"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class GateVerdict:
    """A frozen, deterministic gate outcome.

    ``reasons`` is the ordered tuple of failures/warnings that drove the
    decision; ``facts`` is the ordered input snapshot the verdict was
    computed from (so a refused cycle can cite exactly what it refused on).
    The verdict is intentionally immutable and JSON-renderable via
    ``to_dict``.
    """

    gate: GateKind
    status: GateStatus
    reasons: tuple[str, ...] = ()
    facts: tuple[tuple[str, Any], ...] = field(default=())
    disposition: GateDisposition = GateDisposition.COMPLETED
    steps: tuple[Any, ...] = ()
    refs: tuple[str, ...] = ()
    segment: str | None = None

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASSED

    @property
    def refused(self) -> bool:
        return self.status is GateStatus.REFUSED

    def facts_dict(self) -> dict[str, Any]:
        return dict(self.facts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "status": self.status.value,
            "passed": self.passed,
            "disposition": self.disposition.value,
            "reasons": list(self.reasons),
            "facts": dict(self.facts),
            "steps": [getattr(step, "value", step) for step in self.steps],
            "refs": list(self.refs),
            "segment": self.segment,
        }


class GateRefused(Exception):
    """A refused gate, carrying its full deterministic verdict.

    The controller catches this (or calls ``require_gate``) and routes the
    verdict to the membrane terminal — it never re-plans from a refusal.
    """

    def __init__(self, verdict: GateVerdict):
        self.verdict = verdict
        super().__init__(
            f"gate {verdict.gate.value} refused: "
            + ("; ".join(verdict.reasons) or "no reason recorded")
        )


def require_gate(verdict: GateVerdict) -> GateVerdict:
    """Return ``verdict`` when passed, else raise ``GateRefused`` (fail closed)."""
    if not verdict.passed:
        raise GateRefused(verdict)
    return verdict


# ---------------------------------------------------------------------------
# PLAN gate — post-framing adequacy (forward-only)
# ---------------------------------------------------------------------------


def evaluate_plan_gate(
    plan: Mapping[str, Any] | None,
) -> GateVerdict:
    """Deterministically validate the disposed plan's structural contract.

    Forward-only: a refused plan routes to a terminal (the caller decides
    which), never to a re-plan edge. The gate checks the frozen plan
    vocabulary the acquisition loop depends on:

      - the plan is a mapping;
      - ``kind`` is a supported workflow kind;
      - ``horizon_regime`` is a known regime;
      - a horizon-bearing regime (native/long) binds a positive integer
        ``forecast_horizon_ms`` — acquisition cannot be horizon-bound
        without it.

    Recorded ``directive_refusals`` are carried as facts (findings), not
    automatic refusals: a refusal is a recorded answer, not an invalid plan.
    """
    reasons: list[str] = []
    facts: list[tuple[str, Any]] = []
    if not isinstance(plan, Mapping):
        return GateVerdict(
            gate=GateKind.PLAN,
            status=GateStatus.REFUSED,
            reasons=("plan is not a mapping",),
            facts=(("plan_type", type(plan).__name__),),
        )

    kind = plan.get("kind")
    facts.append(("kind", kind))
    if not isinstance(kind, str) or kind not in _SUPPORTED_PLAN_KINDS:
        reasons.append(f"plan.kind {kind!r} is not a supported workflow kind")

    regime = plan.get("horizon_regime")
    facts.append(("horizon_regime", regime))
    if regime not in _SUPPORTED_HORIZON_REGIMES:
        reasons.append(f"plan.horizon_regime {regime!r} is not a known regime")

    horizon = plan.get("forecast_horizon_ms")
    facts.append(("forecast_horizon_ms", horizon))
    if regime in _HORIZON_BEARING_REGIMES:
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
            reasons.append(
                f"plan.forecast_horizon_ms {horizon!r} must be a positive integer "
                f"for a {regime} regime"
            )

    refusals = plan.get("directive_refusals")
    facts.append(("directive_refusals", list(refusals) if isinstance(refusals, list) else []))

    return GateVerdict(
        gate=GateKind.PLAN,
        status=GateStatus.REFUSED if reasons else GateStatus.PASSED,
        reasons=tuple(reasons),
        facts=tuple(facts),
    )


# ---------------------------------------------------------------------------
# BUDGET gate — ceiling adjudication before each dispatch round (G4)
# ---------------------------------------------------------------------------


def evaluate_budget_gate(
    *,
    tool_rounds_used: int,
    dispatched_in_pass: int,
    max_tool_rounds: int,
    max_dispatches_per_pass: int,
) -> GateVerdict:
    """Deterministically adjudicate the LLM-call/tool budgets before a round.

    The NUMBERS are config-owned (engine.config ceilings); the VERDICT is
    governance-owned (this module, same shape as the plan gate). The driver
    consults this verdict before every round — it never decides budget
    inline. Overflow is a finding that names what will not run (the caller
    moves the names to ``unexecuted``); it is a refusal of the WORK, not a
    terminal — the loop continues its closing semantics.

    Rules (evaluated in order):
    - rounds exhausted (``tool_rounds_used >= max_tool_rounds``): nothing
      further executes this cycle;
    - pass ceiling reached (``dispatched_in_pass >= max_dispatches_per_pass``):
      nothing further executes in THIS pass (a later pass may still run).

    Identical inputs always yield identical verdicts (pure).
    """
    reasons: list[str] = []
    facts: list[tuple[str, Any]] = [
        ("tool_rounds_used", tool_rounds_used),
        ("dispatched_in_pass", dispatched_in_pass),
        ("max_tool_rounds", max_tool_rounds),
        ("max_dispatches_per_pass", max_dispatches_per_pass),
    ]
    if tool_rounds_used >= max_tool_rounds:
        reasons.append(
            f"cycle tool-round budget exhausted "
            f"({tool_rounds_used}/{max_tool_rounds})"
        )
    if dispatched_in_pass >= max_dispatches_per_pass:
        reasons.append(
            f"pass dispatch ceiling reached "
            f"({dispatched_in_pass}/{max_dispatches_per_pass})"
        )
    return GateVerdict(
        gate=GateKind.BUDGET,
        status=GateStatus.REFUSED if reasons else GateStatus.PASSED,
        reasons=tuple(reasons),
        facts=tuple(facts),
    )


def merge_interpretation(turns: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Merged interpretation view over a cycle's parsed turns (deterministic).

    The staged loop legitimately leaves interpretation fields null while
    tools are pending (the contract says so) and the last turn of a cycle is
    often a bare phase declaration. Judging only that turn zeroes out content
    the model already produced earlier in the SAME cycle. This view merges
    what was actually said — and a revision supersedes: every singleton
    field resolves to the LAST non-null statement, so an agent that refines
    its hypothesis mid-cycle is judged on the refined one, never the stale
    first draft:

      summary       — longest non-empty across turns (P4 explanation)
      hypothesis    — LAST non-null dict (revision wins)
      evidence      — union, order-preserving, deduped BY PATH: a later
                      non-null value supersedes an earlier null (a repair
                      can finally fix an entry); distinct non-null values
                      stay visible as contradictions. Bounded by
                      MAX_MERGED_EVIDENCE (a per-turn cap cannot bound a
                      union across turns)
      confidence    — LAST non-null (revision wins)
      limitations   — union of strings
      model_separation — LAST non-null (revision wins)
      scenario / forward_scenario / hypothesis_evidence /
                      hypothesis_probabilities — LAST non-null
                      (most recent statement wins)

    Pure function: no state, no I/O. ``validate_final_turn`` receives the
    merged view; per-turn shape violations are still repaired per turn.
    """
    merged: dict[str, Any] = {
        "summary": "",
        "evidence": [],
        "confidence": None,
        "limitations": [],
        "model_separation": None,
        "hypothesis": None,
        "scenario": None,
        "forward_scenario": None,
        "hypothesis_evidence": None,
        "hypothesis_probabilities": None,
    }
    seen_evidence: dict[str, int] = {}
    seen_limits: set[str] = set()
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        summary = turn.get("summary")
        if isinstance(summary, str) and len(summary.strip()) > len(merged["summary"]):
            merged["summary"] = summary
        hypothesis = turn.get("hypothesis")
        if isinstance(hypothesis, dict):
            merged["hypothesis"] = hypothesis
        evidence = turn.get("evidence")
        if isinstance(evidence, list):
            for entry in evidence:
                if not isinstance(entry, dict):
                    continue
                path_key = str(entry.get("path") or "").strip()
                if not path_key:
                    # Pathless entries are gate material (they fail shape);
                    # never let them collide with one another.
                    merged["evidence"].append(entry)
                    continue
                idx = seen_evidence.get(path_key)
                if idx is None:
                    seen_evidence[path_key] = len(merged["evidence"])
                    merged["evidence"].append(entry)
                    continue
                prev = merged["evidence"][idx]
                prev_value, new_value = prev.get("value"), entry.get("value")
                if prev_value is None and new_value is not None:
                    # null -> value: a later turn REFINED the citation.
                    # Replace in place so a repair can actually fix an entry.
                    # The old (path, value) key appended a corrected twin and
                    # left the broken one unremovable, so the gate re-refused
                    # the same index on every pass (live 2026-10-20: 2 repairs
                    # could not clear 4 null-valued citations).
                    merged["evidence"][idx] = entry
                elif str(prev_value) == str(new_value):
                    # Same claim: a later reading wins when it has one.
                    if str(entry.get("interpretation") or "").strip():
                        merged["evidence"][idx] = entry
                else:
                    # Two DIFFERENT non-null values for one path is a real
                    # contradiction: keep both so the echo gate still sees it.
                    merged["evidence"].append(entry)
        if turn.get("confidence") is not None:
            merged["confidence"] = turn.get("confidence")
        limitations = turn.get("limitations")
        if isinstance(limitations, list):
            for item in limitations:
                text = str(item)
                if text not in seen_limits:
                    seen_limits.add(text)
                    merged["limitations"].append(text)
        if turn.get("model_separation"):
            merged["model_separation"] = turn.get("model_separation")
        for key in ("scenario", "forward_scenario", "hypothesis_evidence",
                    "hypothesis_probabilities"):
            if turn.get(key) is not None:
                merged[key] = turn[key]
    if len(merged["evidence"]) > MAX_MERGED_EVIDENCE:
        log.warning("capping merged evidence %d → %d entries",
                    len(merged["evidence"]), MAX_MERGED_EVIDENCE)
        merged["evidence"] = merged["evidence"][:MAX_MERGED_EVIDENCE]
    return merged


def _bound_from_controller(controller: Any, ctx: Any) -> tuple[Any, Any]:
    """Effective (target, horizon) with legacy fallbacks (pure)."""
    effective = getattr(controller, "effective_scenario", None)
    if callable(effective):
        try:
            return effective()
        except Exception:
            pass
    if isinstance(ctx.scenario, dict):
        return ctx.scenario.get("target_price"), ctx.scenario.get("horizon")
    return None, None


def _derive_verdict(
    payload: Any, st: CycleRuntimeState, det_verdict: Any,
) -> tuple[str, str]:
    gate = st.deterministic_state.get("gate") or {}
    return det_verdict(
        payload, st.capability_log,
        str(gate.get("status") or "provisional"),
        list(gate.get("reasons") or []),
    )


def check_scenario_equality(
    judged: dict[str, Any], st: CycleRuntimeState, ctx: Any,
) -> list[str]:
    """Scenario contract equality (validation-owned, deterministic).

    Two planes, ONE oracle:

    * Contract plane (a ScenarioContract and/or a real dispatch outcome
      exists): the LAST genuine dispatch must carry the EFFECTIVE bound
      target/horizon. A refusal only answers the asked question when it
      refuses THAT target — an explicit wrong-target refusal is a finding,
      not an unevaluable escape. The final block must echo the bound
      target/horizon; a declared revision must surface its divergence.
      Verdict derivation reads the SAME outcome payload that was bound.
    * Legacy plane (no contract/outcome — direct construction/tests): the
      pre-contract behavior (accumulated payload + log) is preserved so
      older callers keep working.

    Returns missing[]; output keeps the agent's block verbatim.
    """
    try:
        from .controller import ScenarioEvalStatus as _SES
        from .controller import scenario_verdict as _det_verdict
    except Exception:
        return []
    try:
        if ctx.scenario is None:
            return []
        controller = st.controller
        contract = getattr(controller, "scenario_contract", None)
        outcome = None
        outcome_read = getattr(controller, "scenario_outcome", None)
        if callable(outcome_read):
            try:
                outcome = outcome_read()
            except Exception:
                outcome = None
        state = controller.scenario_state()
        scen = judged.get("scenario")

        if contract is not None or outcome is not None:
            eff_t, eff_h = _bound_from_controller(controller, ctx)
            recorded_args = (outcome is not None
                             and isinstance(getattr(outcome, "args", None), Mapping)
                             and bool(outcome.args))
            if state is _SES.REFUSED:
                if (recorded_args
                        and not scenario_args_match(outcome.args, eff_t, eff_h)):
                    return [
                        "scenario refused "
                        f"{getattr(outcome, 'args', None)!r} != bound target "
                        f"{eff_t!r} @ {eff_h!r} — a refusal answers the asked "
                        "question only when it refuses the bound target "
                        "(dispatch the contract target exactly; a declared "
                        "revision is required to change it)"
                    ]
                return []
            if not isinstance(scen, dict):
                return []  # the structural check names the missing block
            if state is _SES.NOT_CALLED:
                # No dispatch, no data: a verdict (reachable, not_reachable,
                # OR unevaluable) is fabrication. The structural gate names
                # the missing block; this names the missing DISPATCH.
                return [
                    "scenario never dispatched: call calc.scenario.evaluate "
                    f"with the bound target {eff_t!r} @ {eff_h!r} before "
                    "declaring a scenario verdict"
                ]
            if (recorded_args
                    and not scenario_args_match(outcome.args, eff_t, eff_h)):
                return [
                    "scenario evaluated "
                    f"{getattr(outcome, 'args', None)!r} != bound target "
                    f"{eff_t!r} @ {eff_h!r} — evaluate the bound target "
                    "(or declare a permitted revision; answering a different "
                    "question is not a silent option)"
                ]
            if (scen.get("target_price") not in (None, "")
                    and not numbers_equal(scen.get("target_price"), eff_t)):
                return [
                    f"scenario.target_price {scen.get('target_price')!r} != "
                    f"bound target {eff_t!r} — echo the evaluated contract target"
                ]
            if (scen.get("horizon") not in (None, "")
                    and str(scen.get("horizon")).strip() != str(eff_h)):
                return [
                    f"scenario.horizon {scen.get('horizon')!r} != bound "
                    f"horizon {eff_h!r} — echo the contract horizon"
                ]
            intent = getattr(controller, "scenario_intent", None)
            if isinstance(intent, Mapping) and intent.get("mode") == "revision":
                divergence = scen.get("divergence")
                if (not isinstance(divergence, dict)
                        or not str(divergence.get("reason") or "").strip()):
                    return [
                        "scenario.divergence {asked_target, reason} required: "
                        "a declared revision must surface the asked target "
                        "and why the question changed"
                    ]
            verdict = str(scen.get("verdict") or "")
            if verdict == "unevaluable":
                return []  # refused-shape verdict with evaluated tooling is
                # caught by the structural unevaluable rules, not here
            payload = (outcome.payload if outcome is not None else
                       (st.accumulated_tool_results or {}).get(
                           "calc.scenario.evaluate"))
            derived, reason = _derive_verdict(payload, st, _det_verdict)
            want = {"invalidated": "not_reachable",
                    "validated": "reachable"}.get(derived)
            if want is None:
                if verdict not in ("reachable", "not_reachable"):
                    return [
                        "scenario verdict must be reachable|not_reachable: the "
                        f"tool evaluated ({reason}) — unevaluable is forbidden "
                        "with tool data"
                    ]
                return []
            if verdict != want:
                return [
                    f"scenario verdict must be {want!r}: deterministic "
                    f"derivation says {derived} ({reason}) — the agent's "
                    "block keeps its words, but the verdict obeys the derivation"
                ]
            return []

        # --- Legacy plane (no contract, no classified outcome) -------------
        if state is _SES.REFUSED:
            return []
        if not isinstance(scen, dict):
            return []
        verdict = str(scen.get("verdict") or "")
        if verdict == "unevaluable":
            return []
        derived, reason = _derive_verdict(
            (st.accumulated_tool_results or {}).get("calc.scenario.evaluate"),
            st, _det_verdict,
        )
        want = {"invalidated": "not_reachable",
                "validated": "reachable"}.get(derived)
        if want is None:
            if verdict not in ("reachable", "not_reachable"):
                return [
                    "scenario verdict must be reachable|not_reachable: the tool "
                    f"evaluated ({reason}) — unevaluable is forbidden with tool data"
                ]
            return []
        if verdict != want:
            return [
                f"scenario verdict must be {want!r}: deterministic derivation says "
                f"{derived} ({reason}) — the agent's block keeps its words, "
                "but the verdict obeys the derivation"
            ]
        return []
    except Exception:
        log.exception("scenario equality check failed open")
        return []


def has_directive_verdict(parsed: dict[str, Any] | None) -> bool:
    """Task-conformance read (pure): did the final surface the verdict?

    True when the final's evidence cites the task directive or a scenario
    verdict path (forward scenario / legacy exceedance) — the deterministic
    directive/scenario surface the plan bound. Used by the validation gate
    for target-bearing directives; the repair steer names the citation.
    """
    evidence = (parsed or {}).get("evidence") or []
    for entry in evidence if isinstance(evidence, list) else []:
        path = (str((entry or {}).get("path", ""))
                if isinstance(entry, dict) else str(entry or ""))
        if ("task_directive" in path or "forward_scenario" in path
                or "calc.forward.scenario" in path
                or "calc.scenario.evaluate" in path):
            return True
    return False


def validate_final_turn(
    parsed: dict[str, Any],
    coverage: dict[str, set[str]],
    scenario: dict[str, Any] | None = None,
    *,
    scenario_status: "ScenarioEvalStatus | None" = None,
    scenario_refusal_reason: str | None = None,
    accumulated: dict[str, Any] | None = None,
    findings: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Phase-aware final gate: depth is structural, not advisory.

    A FINAL turn passes only when every required phase family has at least
    one executed tool (P1 OFI, P2 AD/fits, P3 market correlation, P5
    derived ΔP), the hypothesis carries H0, the P4 explanation meets the
    length floor, confidence is the contract enum, every evidence entry
    carries a non-empty interpretation, and evidence cites at least two
    distinct roots with at least one fresh tool result (not just
    deterministic_state). Returns (passed, missing[]) — missing drives
    the repair prompt.

    Scenario semantics come from the CONTROLLER's tri-state
    (``ScenarioEvalStatus``), never re-derived here:
      - EVALUATED (or legacy None → treated as evaluated/unknown): the
        evidence must cite a calc.scenario.evaluate → … data path.
      - REFUSED: the tool refused deterministically — the citation
        requirement is satisfied by a refusal FINDING (path
        ``calc.scenario.evaluate → refusal``), scenario.verdict must be
        ``unevaluable`` (reachable/not_reachable are forbidden without
        tool data), and the numeric echo requirements are waived (nothing
        to echo). The LLM cannot override null discipline.
      - NOT_CALLED: the citation demand stands unchanged; the repair
        steer (controller-owned) tells the model to call the tool.
    """
    from market_service.nooa_harness.inference import _REQUIRED_PHASES
    from market_service.runtime.contracts import normalize_confidence

    missing: list[str] = []
    for phase in _REQUIRED_PHASES:
        if not coverage.get(phase):
            missing.append(
                f"phase {phase} uncovered: execute its tools before finalizing "
                f"(covered so far: {sorted(coverage.get(phase) or [])})"
            )
    if not coverage.get("P6"):
        missing.append(
            "P6 output-generation turn required: synthesize the primary inference "
            "output from this run's reasoning (declare phase P6, no tools)"
        )
    hypothesis = parsed.get("hypothesis")
    if not isinstance(hypothesis, dict) or not str(hypothesis.get("H0") or "").strip():
        missing.append(
            "hypothesis.H0 required: frame H0/H1 in line with the hypothesis frame"
        )
    summary = parsed.get("summary") or ""
    if not isinstance(summary, str) or len(summary.strip()) < SUMMARY_MIN_CHARS:
        # F9 structural/relaxed: thin prose is a finding, never a gate. A
        # presentational repair re-rolls every number and manufactures
        # recompute opportunities; only citation shape + echo truth block.
        _note = (
            f"P4 explanation thin ({len(summary.strip())}/{SUMMARY_MIN_CHARS} chars): "
            "say what the fits show AND why it is happening now"
        )
        if findings is not None:
            findings.append(_note)
        else:
            missing.append(_note)
    confidence = parsed.get("confidence")
    if confidence is not None and normalize_confidence(confidence) is None:
        missing.append(
            f"confidence must be one of low|medium|high (got {confidence!r}); "
            "hyphenated blends coerce conservatively (low-medium → low)"
        )
    roots: set[str] = set()
    evidence = parsed.get("evidence")
    # ONE shape authority, shared with the shipping filter
    # (output._assemble_authored → synthesize.classify_evidence). The gate
    # judges shape only (no frame); backing is assembly's fact-check.
    from .core.synthesize import (
        EVIDENCE_NO_PATH,
        EVIDENCE_NO_READING,
        EVIDENCE_NULL_VALUE,
        classify_evidence,
    )
    if isinstance(evidence, list):
        for idx, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                missing.append(f"evidence[{idx}] must be an object with path + interpretation")
                continue
            # F9 structural parse gate (deterministic, pre-resolver): an
            # unattributed number can never reach the artifact. Missing path
            # or null value is a gate here — except deterministic refusal
            # findings (`→ refusal`), which carry no numeric value by null
            # discipline. The echo check below judges path-correct +
            # value-wrong. Interpretation stays required (the reading is
            # what keeps prose grounded to the cited field).
            _shape = classify_evidence(entry)
            if _shape in (EVIDENCE_NO_PATH, EVIDENCE_NULL_VALUE):
                missing.append(
                    f"evidence[{idx}] needs path+value — cite the tool+field "
                    "you read (unattributed numbers never reach the artifact)"
                )
                continue
            if _shape == EVIDENCE_NO_READING:
                missing.append(
                    f"evidence[{idx}] missing interpretation: say what "
                    f"{entry.get('path')!r} shows (paths+values cited, readings empty is a violation)"
                )
            path = str(entry.get("path") or "")
            head = path.split("→")[0].strip()
            # Root identity: the TOOL head (calc.ofi.intervals, market.read, …)
            # is the root — splitting on "." collapsed every calc.* tool into
            # the single root "calc", so a perfectly-cited cycle could never
            # show "≥2 distinct roots". deterministic_state paths collapse to
            # their namespace (they are not fresh tool results).
            if head.startswith("deterministic_state"):
                root = "deterministic_state"
            else:
                root = head
            if root:
                roots.add(root)
    if len(roots) < 2 or not (roots - {"deterministic_state"}):
        missing.append(
            "evidence must cite ≥2 distinct roots including ≥1 fresh tool result "
            f"(got roots: {sorted(roots) or 'none'}" + ")"
        )
    delta_paths = [
        str(entry.get("path") or "")
        for entry in (evidence if isinstance(evidence, list) else [])
        if isinstance(entry, dict)
    ]
    if not any(
        head in ("calc.price.delta", "calc.derived_diagnostic")
        for path in delta_paths
        for head in [path.split("→")[0].strip()]
    ):
        missing.append(
            "evidence must cite the derived ΔP via a calc.price.delta → … path"
        )
    if scenario is not None:
        from .controller import ScenarioEvalStatus  # local import: avoids cycle
        scen = parsed.get("scenario")
        if scenario_status is ScenarioEvalStatus.REFUSED:
            # Controller says the tool refused deterministically. The
            # citation requirement is satisfied by a refusal FINDING;
            # verdict must be unevaluable; numeric echoes are waived.
            scen_ok = isinstance(scen, dict)
            if not scen_ok:
                missing.append(
                    "scenario block required: the scenario tool refused — "
                    "return the scenario object with verdict=unevaluable "
                    "and the refusal cited"
                )
            else:
                verdict = str(scen.get("verdict") or "")
                if verdict not in ("unevaluable",):
                    missing.append(
                        "scenario.verdict must be 'unevaluable': the tool "
                        f"refused deterministically ({scenario_refusal_reason or 'refusal'}) "
                        "— reachable/not_reachable are forbidden without tool data"
                    )
                rationale = scen.get("rationale")
                reason_stub = str(scenario_refusal_reason or "refused")[:20].lower()
                names_cause = (
                    isinstance(rationale, str)
                    and (reason_stub in rationale.lower()
                         or "refus" in rationale.lower())
                )
                if (not isinstance(rationale, str)
                        or len(rationale.strip()) < 80
                        or not names_cause):
                    missing.append(
                        "scenario.rationale required (≥80 chars) naming the "
                        "refusal reason — a bare verdict without the "
                        "deterministic cause is rejected"
                    )
            if not any(
                head == "calc.scenario.evaluate"
                and "refusal" in path
                for path in delta_paths
                for head in [path.split("→")[0].strip()]
            ):
                missing.append(
                    "evidence must cite the refusal as "
                    "`calc.scenario.evaluate → refusal` (a finding, not a "
                    "re-call demand — the tool will refuse again)"
                )
        else:
            if not isinstance(scen, dict):
                missing.append(
                    "scenario block required: a scenario was given — return the "
                    "scenario{target_price,horizon,direction,required_ofi,exceedance,"
                    "probability,verdict,rationale} object evaluated via calc.scenario.evaluate"
                )
            else:
                if str(scen.get("verdict") or "") not in (
                        "reachable", "not_reachable", "unevaluable"):
                    missing.append(
                        "scenario.verdict required: reachable|not_reachable|unevaluable"
                    )
                if (scenario_status is ScenarioEvalStatus.NOT_CALLED
                        and str(scen.get("verdict") or "") == "unevaluable"):
                    missing.append(
                        "scenario.verdict 'unevaluable' is forbidden without a "
                        "deterministic refusal — call calc.scenario.evaluate with "
                        "the contract target/horizon; a refusal is the ONLY "
                        "unevaluable path")
                if normalize_confidence(scen.get("probability")) is None:
                    missing.append(
                        "scenario.probability required: low|medium|high "
                        "(qualitative read of the exceedance + band + tape quality)"
                    )
                for field in ("target_price", "horizon", "required_ofi",
                              "exceedance"):
                    if not str(scen.get(field) or "").strip():
                        missing.append(
                            f"scenario.{field} required: echo the calc.scenario.evaluate "
                            f"→ … value, never compute it yourself"
                        )
                # Thin-tape context travels WITH the verdict: a 0% exceedance
                # without fit_status + window count reads as "impossible" when
                # it means "the tape couldn't speak". Both are deterministic
                # echoes, so the check is structural, never semantic.
                for field in ("fit_status", "n_windows_usable"):
                    if not str(scen.get(field) or "").strip():
                        missing.append(
                            f"scenario.{field} required: echo the calc.scenario.evaluate "
                            f"→ … value so the verdict carries its tape context"
                        )
                rationale = scen.get("rationale")
                if not isinstance(rationale, str) or len(rationale.strip()) < 80:
                    missing.append(
                        "scenario.rationale required (≥80 chars): name fit_status + "
                        "usable windows + r2 beside the verdict — a bare verdict "
                        "without its tape context is rejected"
                    )
            if not any(
                head == "calc.scenario.evaluate"
                for path in delta_paths
                for head in [path.split("→")[0].strip()]
            ):
                missing.append(
                    "evidence must cite the scenario evaluation via a "
                    "calc.scenario.evaluate → … path"
                )
        hypothesis_sc = parsed.get("hypothesis")
        if (not isinstance(hypothesis_sc, dict)
                or not str(hypothesis_sc.get("H0") or "").strip()
                or not str(hypothesis_sc.get("H1") or "").strip()):
            missing.append(
                "scenario cycles frame TWO hypotheses: H0 (target NOT reachable) "
                "and H1 (target reachable), both non-empty"
            )
    # Slice E — value-echo verify (path-correct + value-wrong = missing).
    # Resolves through chain.resolve_basis_path (the shared citation resolver):
    # field-segmented, comparator-disjoint, skip-when-unresolvable. See that
    # function for the run-6 lesson.
    try:
        from .core.chain import resolve_basis_path as _resolve

        def _norm(_v: Any) -> str | None:
            if _v is None:
                return None
            try:
                from decimal import Decimal
                return str(Decimal(str(_v)))
            except Exception:
                return str(_v).strip() or None
        _ev = parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else []
        for _entry in _ev:
            if not isinstance(_entry, dict):
                continue
            _path = str(_entry.get("path") or "")
            _val = _entry.get("value")
            if _val is None or not _path:
                continue
            _head, _fname, _truth = _resolve(
                _path, accumulated if isinstance(accumulated, dict) else {})
            if _fname is None or _truth is None:
                continue
            if _norm(_val) != _norm(_truth):
                missing.append(
                    f"evidence value mismatch on {_path!r}: cited {_val!r} "
                    f"!= {_head} field {_fname!r} payload {_truth!r} — cite the exact "
                    "tool+field you read (comparator.* is the univariate baseline, "
                    "top-level is the multivariate fit; never swap them)"
                )
    except Exception:
        pass
    # Adjudication coherence (spec §6): the agent weighs each live hypothesis
    # probabilistically and chooses. The checker verifies COHERENCE — choice
    # follows probabilities follows tools — never truth. A wrong-but-coherent
    # vector passes by explicit scope decision. Unevaluable (refused) paths
    # bypass weighing entirely.
    try:
        from .controller import ScenarioEvalStatus as _SES
        _refused_scen = (scenario is not None
                         and scenario_status is _SES.REFUSED)
    except Exception:
        _refused_scen = False
    # Unevaluable bypass: a refused scenario tool leaves no distribution to
    # weigh — the verdict carries the refusal finding instead of probabilities.
    _weigh_exempt = bool(_refused_scen)
    try:
        _probs = parsed.get("hypothesis_probabilities")
        _hyp = parsed.get("hypothesis")
        _needs_weigh = ((isinstance(_hyp, dict) and bool(str(_hyp.get("H0") or "").strip()))
                        and not _weigh_exempt)
        if _needs_weigh and not isinstance(_probs, dict):
            missing.append(
                "hypothesis_probabilities required: weigh H0/H1 "
                "(low|medium|high + exact basis_path each, chosen = argmax) — "
                "probabilities inform, never execute"
            )
        elif isinstance(_probs, dict) and _needs_weigh:
            from .core.chain import resolve_basis_path as _resolve_basis
            _RANK = {"low": 0, "medium": 1, "high": 2}

            def _acc_data(_v: Any) -> Any:
                if isinstance(_v, dict) and "tool" in _v and "data" in _v:
                    return _v.get("data")
                return _v
            _a = accumulated if isinstance(accumulated, dict) else {}
            _fitA = _acc_data(_a.get("calc.forward.fit"))
            _fitA = _fitA if isinstance(_fitA, dict) else {}
            _distA = _acc_data(_a.get("calc.forward.distribution"))
            _distA = _distA if isinstance(_distA, dict) else {}

            def _basis_truth(_path: str) -> tuple[str | None, Any]:
                # Shared citation resolver (chain-owned): adjudication can
                # never disagree with echo on what a path means.
                _head, _field, _truth = _resolve_basis(_path, _a)
                if _field is None:
                    return None, None
                return _field, _truth

            def _num(_v: Any) -> float | None:
                try:
                    return float(str(_v))
                except Exception:
                    return None
            _keys = ("H0", "H1")
            _score: dict[str, tuple[int, float]] = {}
            for _hk in _keys:
                _entry = _probs.get(_hk)
                if not isinstance(_entry, dict):
                    missing.append(
                        f"hypothesis_probabilities.{_hk} required: object with "
                        "probability low|medium|high + exact basis_path"
                    )
                    continue
                _penum = str(_entry.get("probability") or "").strip().lower()
                if _penum not in _RANK:
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.probability must be "
                        f"low|medium|high (got {_entry.get('probability')!r})"
                    )
                    continue
                _bpath = str(_entry.get("basis_path") or "")
                # F9 structural parse gate: unattributed adjudication numbers
                # never reach the artifact; resolver judges wrong paths below.
                if not _bpath.strip():
                    missing.append(
                        f"hypothesis_probabilities.{_hk} needs basis_path — cite "
                        "the accumulated tool field the weighing stands on"
                    )
                    continue
                _bval = _entry.get("basis_value")
                _fk, _ft = _basis_truth(_bpath)
                if _fk is None:
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.basis_path must resolve "
                        "to an accumulated tool field (head + field segments; "
                        "comparator.* is the baseline, top-level the multivariate)"
                    )
                    continue
                if _bval is not None and _norm(_bval) != _norm(_ft):
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.basis_value mismatch: "
                        f"cited {_bval!r} != {_fk!r} payload {_ft!r}"
                    )
                _pnum = _num(_entry.get("p"))
                if _entry.get("p") is not None and _pnum is None:
                    missing.append(
                        f"hypothesis_probabilities.{_hk}.p must be numeric or null"
                    )
                _score[_hk] = (_RANK[_penum], _pnum if _pnum is not None else 0.5)
            _ps = [_probs.get(_hk, {}).get("p") if isinstance(_probs.get(_hk), dict) else None
                   for _hk in _keys]
            _pnums = [_num(_v) for _v in _ps]
            if all(_v is not None for _v in _pnums):
                if abs(sum(_pnums) - 1.0) > 0.15:  # type: ignore[arg-type]
                    missing.append(
                        "hypothesis_probabilities numeric P must sum to ≈1 "
                        f"(got {sum(_pnums):.3f}) — or leave p null and weigh by enum"
                    )
            # Bucket coherence (coarse — rejects gross incoherence only).
            _fit_status = _fitA.get("status")
            _has_dist = any(_distA.get(_k) is not None
                            for _k in ("expected_delta", "p_ge_zero", "p_ge_theta"))
            _fore_ok = isinstance(_acc_data(_a.get("calc.forward.forecast")), dict)
            if _fit_status in ("insufficient", "refused") or not (_has_dist or _fore_ok):
                for _hk in _keys:
                    _en = _probs.get(_hk) if isinstance(_probs.get(_hk), dict) else {}
                    if str((_en or {}).get("probability") or "").lower() == "high":
                        missing.append(
                            f"hypothesis_probabilities.{_hk}=high with no usable "
                            "distribution/forecast truth — nothing is proven; "
                            "weigh low|medium until 2b truth exists"
                        )
                        break
            else:
                _pnode = _distA.get("p_ge_theta", _distA.get("p_ge_zero"))
                _pv = _num(_pnode)
                if _pv is not None:
                    _h1en = str((_probs.get("H1") or {}).get("probability") or "").lower() \
                        if isinstance(_probs.get("H1"), dict) else ""
                    if _h1en == "high" and _pv < 0.55:
                        missing.append(
                            "hypothesis_probabilities.H1=high beside "
                            f"distribution P={_pv:.3f} — high needs P≥0.55 or a "
                            "named refused-tool finding carrying the choice"
                        )
            # Choice rule: chosen == argmax(rank, p); ties need tiebreak;
            # deviation needs a cited refusal finding on the ledger.
            _chosen = str(_probs.get("chosen") or "").strip()
            if _chosen not in ("H0", "H1"):
                missing.append(
                    "hypothesis_probabilities.chosen must be H0|H1"
                )
            elif len(_score) == 2:
                _best = max(_score.items(), key=lambda _kv: (_kv[1][0], _kv[1][1]))
                _runner = min(_score.items(), key=lambda _kv: (_kv[1][0], _kv[1][1]))
                _tied = _best[1] == _runner[1]
                if _tied and not str(_probs.get("tiebreak") or "").strip():
                    missing.append(
                        "hypothesis_probabilities tied — tiebreak citing evidence required"
                    )
                if _chosen != _best[0]:
                    _ev_paths = [str((_e or {}).get("path") or "")
                                 for _e in (parsed.get("evidence") or [])
                                 if isinstance(_e, dict)]
                    if not any("refusal" in _p for _p in _ev_paths):
                        missing.append(
                            f"hypothesis_probabilities.chosen must be the argmax "
                            f"({_best[0]} outranks {_chosen}) unless a cited "
                            "refusal finding carries the deviation"
                        )
    except Exception:
        log.exception("adjudication check failed open; continuing without it")
    return (not missing), missing


# ===========================================================================
# Worker substrate — the single home for every deterministic guard and close.
#
# Workers receive a FROZEN snapshot and return a GateVerdict. They perform no
# I/O, spend no LLM budget, and never mutate state; the substrate below is the
# only writer (ledger VERDICT row). A loop close is a gate too (GateKind.CLOSE,
# `close_loop`), so the former receipt type no longer exists.
# ===========================================================================

#: Which terminal a refused guard routes to. BUDGET/CLOSE are deliberately
#: absent: budget refusal withholds WORK (caller moves pending to unexecuted)
#: and a close verdict only records disposition.
REFUSAL_TERMINALS: dict[GateKind, str] = {
    GateKind.FRAMING: "gate_refused",
    GateKind.PLAN: "gate_refused",
    GateKind.DATA: "gate_refused",
    GateKind.CONTENT: "validation_failed",
    GateKind.CHAIN: "validation_failed",
}


class GateWorker:
    """One deterministic guard (or close) over a frozen snapshot."""

    kind: GateKind

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:  # pragma: no cover
        raise NotImplementedError


class _PlanWorker(GateWorker):
    kind = GateKind.PLAN

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:
        return evaluate_plan_gate(snapshot)


class _BudgetWorker(GateWorker):
    kind = GateKind.BUDGET

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:
        return evaluate_budget_gate(**dict(snapshot))


class _FramingWorker(GateWorker):
    kind = GateKind.FRAMING

    REQUIRED = (
        "seed_plan", "task_workflow", "seeded_plan_state",
        "task_workflow_state", "comprehension", "understanding",
    )

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:
        missing = [key for key in self.REQUIRED if not snapshot.get(key)]
        reasons = tuple(f"framing incomplete: {key} missing" for key in missing)
        facts: list[tuple[str, Any]] = [(key, bool(snapshot.get(key)))
                                        for key in self.REQUIRED]
        facts.append(("understanding", snapshot.get("understanding")))
        return GateVerdict(
            gate=self.kind,
            status=GateStatus.REFUSED if missing else GateStatus.PASSED,
            reasons=reasons,
            facts=tuple(facts),
            disposition=(GateDisposition.REFUSED if missing
                         else GateDisposition.COMPLETED),
            refs=tuple(str(r) for r in (snapshot.get("refs") or ())),
        )


class _DataWorker(GateWorker):
    """Data-sufficiency policy (the retired trichotomy's remaining contract).

    The agentic plane adjudicates data sufficiency through per-tool
    deterministic refusals plus the CONTENT gate. The policy is frozen here
    as data (`provisional`) so no second gate module can drift from it.
    """

    kind = GateKind.DATA

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:
        status = str(snapshot.get("status") or "provisional")
        return GateVerdict(
            gate=self.kind,
            status=(GateStatus.REFUSED if status == "insufficient"
                    else GateStatus.PASSED),
            reasons=(() if status != "insufficient"
                     else ("data insufficient",)),
            facts=(("status", status),),
            disposition=(GateDisposition.REFUSED if status == "insufficient"
                         else GateDisposition.COMPLETED),
        )


class _ChainWorker(GateWorker):
    kind = GateKind.CHAIN

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:
        missing = [str(m) for m in (snapshot.get("missing") or ())]
        gaps = [str(g) for g in (snapshot.get("gaps") or ())]
        halt = snapshot.get("halted")
        reasons = [f"chain missing: {m}" for m in missing]
        if halt:
            reasons.append(f"chain halted: {halt}")
        return GateVerdict(
            gate=self.kind,
            status=GateStatus.REFUSED if reasons else GateStatus.PASSED,
            reasons=tuple(reasons),
            facts=(("missing", missing), ("gaps", gaps), ("halted", bool(halt))),
            disposition=(GateDisposition.REFUSED if reasons
                         else GateDisposition.COMPLETED),
        )


class _ContentWorker(GateWorker):
    kind = GateKind.CONTENT

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:
        passed, missing = validate_final_turn(
            snapshot.get("judged") or {},
            snapshot.get("coverage") or {},
            scenario=snapshot.get("scenario"),
            scenario_status=snapshot.get("scenario_status"),
            scenario_refusal_reason=snapshot.get("scenario_refusal_reason"),
            accumulated=snapshot.get("accumulated"),
            findings=snapshot.get("findings"),
        )
        extra = [str(m) for m in (snapshot.get("extra_misses") or ())]
        missing = list(missing) + extra
        return GateVerdict(
            gate=self.kind,
            status=GateStatus.PASSED if passed and not extra
            else GateStatus.REFUSED,
            reasons=tuple(missing),
            facts=(("missing", list(missing)),),
            disposition=(GateDisposition.COMPLETED
                         if passed and not extra else GateDisposition.REFUSED),
        )


class _CloseWorker(GateWorker):
    """A loop/sub-loop close is a verdict, never a separate receipt type."""

    kind = GateKind.CLOSE

    def evaluate(self, snapshot: Mapping[str, Any]) -> GateVerdict:
        disposition = snapshot.get("disposition") or GateDisposition.COMPLETED
        if isinstance(disposition, str):
            disposition = GateDisposition(disposition)
        facts: list[tuple[str, Any]] = [
            ("loop", getattr(snapshot.get("loop"), "value", snapshot.get("loop"))),
            ("sub_loop", getattr(snapshot.get("sub_loop"), "value",
                                snapshot.get("sub_loop"))),
        ]
        payload = snapshot.get("payload") or {}
        if isinstance(payload, Mapping):
            facts.extend((str(k), v) for k, v in payload.items())
        return GateVerdict(
            gate=self.kind,
            status=(GateStatus.REFUSED
                    if disposition is GateDisposition.REFUSED
                    else GateStatus.PASSED),
            disposition=disposition,
            steps=tuple(snapshot.get("steps") or ()),
            refs=tuple(str(r) for r in (snapshot.get("refs") or ())),
            segment=snapshot.get("segment"),
            facts=tuple(facts),
        )


GATE_WORKERS: dict[GateKind, GateWorker] = {
    worker.kind: worker for worker in (
        _FramingWorker(), _PlanWorker(), _DataWorker(), _ChainWorker(),
        _BudgetWorker(), _ContentWorker(), _CloseWorker(),
    )
}


def evaluate(kind: GateKind, snapshot: Mapping[str, Any]) -> GateVerdict:
    """Evaluate one gate over a frozen snapshot (fail closed, never raise)."""
    worker = GATE_WORKERS.get(kind)
    if worker is None:
        return GateVerdict(
            gate=kind, status=GateStatus.REFUSED,
            reasons=(f"no worker registered for gate {getattr(kind, 'value', kind)}",),
            disposition=GateDisposition.REFUSED,
        )
    try:
        return worker.evaluate(snapshot)
    except Exception as exc:  # worker faults are refusals, never crashes
        log.exception("gate worker %s failed; refusing closed", kind.value)
        return GateVerdict(
            gate=kind, status=GateStatus.REFUSED,
            reasons=(f"gate_worker_error:{type(exc).__name__}",),
            disposition=GateDisposition.REFUSED,
        )


def require(kind: GateKind, snapshot: Mapping[str, Any]) -> GateVerdict:
    """Evaluate and raise ``GateRefused`` on refusal (fail closed)."""
    verdict = evaluate(kind, snapshot)
    if verdict.refused:
        raise GateRefused(verdict)
    return verdict


def route_refusal(verdict: GateVerdict) -> str | None:
    """The ONLY refusal→terminal mapping. None means non-terminal."""
    if not verdict.refused:
        return None
    return REFUSAL_TERMINALS.get(verdict.gate)


def _state_dict(st: Any) -> dict[str, Any]:
    """The deterministic_state dict for a carrier or a bare dict."""
    if isinstance(st, dict):
        return st
    return getattr(st, "deterministic_state", st)


def record_verdict(
    verdict: GateVerdict, st: Any, *, segment: str | None = None,
) -> Any:
    """Append the verdict as a ledger VERDICT row (best-effort)."""
    ds = _state_dict(st)
    payload = {"stage": "gate", **verdict.to_dict()}
    try:
        entry = record_entry(
            ds, LedgerKind.VERDICT, payload,
            evidence_refs=verdict.refs,
            segment=segment or verdict.segment,
        )
    except Exception:
        log.exception("verdict record failed; continuing")
        return None
    try:
        st.ledger_stage_ids.append(f"verdict:{verdict.gate.value}#{entry.seq}")
    except Exception:
        pass
    return entry


def close_loop(
    st: Any, *,
    loop: Any,
    sub_loop: Any,
    disposition: Any = GateDisposition.COMPLETED,
    steps: Any = (),
    payload: Mapping[str, Any] | None = None,
    refs: Any = (),
    segment: str | None = None,
) -> GateVerdict:
    """Close a loop/sub-loop: evaluate CLOSE and record the verdict.

    This is the former ``receipts.emit_receipt`` — a close is a gate verdict.
    """
    verdict = evaluate(GateKind.CLOSE, {
        "loop": loop, "sub_loop": sub_loop, "disposition": disposition,
        "steps": steps, "payload": dict(payload or {}), "refs": refs,
        "segment": segment,
    })
    record_verdict(verdict, st, segment=segment)
    return verdict


__all__ += [
    "GateDisposition", "GateWorker", "GATE_WORKERS", "REFUSAL_TERMINALS",
    "evaluate", "require", "route_refusal", "record_verdict", "close_loop",
    "merge_interpretation", "check_scenario_equality",
    "has_directive_verdict", "validate_final_turn",
]


def project_closes(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive the legacy ``loop_receipts`` view from CLOSE verdict rows.

    The receipt vector is no longer state: it is a permanent artifact
    projection over the ledger's VERDICT rows.
    """
    rows = state.get("ledger") if isinstance(state, Mapping) else None
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if row.get("kind") != LedgerKind.VERDICT.value:
            continue
        payload = row.get("payload") or {}
        if payload.get("gate") != GateKind.CLOSE.value:
            continue
        facts = payload.get("facts") or {}
        body = {key: value for key, value in facts.items()
                if key not in ("loop", "sub_loop")}
        out.append({
            "loop": facts.get("loop"),
            "sub_loop": facts.get("sub_loop"),
            "disposition": payload.get("disposition"),
            "steps": list(payload.get("steps") or ()),
            "payload": body,
            "evidence_refs": list(payload.get("refs") or ()),
        })
    return out
