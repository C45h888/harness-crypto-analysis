"""CycleController — the agentic loop's immutable semantic authority.

Semantic authority: CLASSIFICATION, CREDIT, and EXIT of one inference
cycle. Every tool dispatch the LLM requests flows through this controller,
which is the ONLY component that interprets the dispatch outcome shape
(``result == "ok"`` + ``detail.status == "refused"`` = null-discipline
refusal). Phase-coverage credit, the scenario-evaluation tri-state, and
the loop's exit decision (dispatch / validate) are owned HERE — never
re-derived inline by the loop, the validator, or the prompt.

IMMUTABILITY — the controller is a VALUE, not a stateful object:

  Every transition (``record_outcome``, ``mark_declared``) returns a NEW
  CycleController; the instance is never mutated. Given the same starting
  controller and the same tool outcome, ``record_outcome`` always produces
  the same successor. This makes the controller a pure, replayable ledger:
  it is always internally consistent, can be unit-tested without I/O, and
  is trivially safe to share across the loop's phases.

SEPARATION OF POWERS (this module vs engine/fsm.py):

  - engine/fsm.py owns the LOOP-STATE TRANSITION TABLE — what transitions
    are legal (the governance membrane). It decides whether a transition
    may happen.
  - THIS controller owns SEMANTICS — what a tool outcome means, what
    coverage it earns, what the scenario tri-state is. It classifies the
    outcome the transition records; it never owns the transition table.

  The controller sits UNDER the FSM membrane: the FSM is the constitution,
  the controller is the executive.

Boundary rules:
- pure over injected state: no I/O, no LLM client, no store access
- does NOT parse LLM text (engine/narration.py keeps extract/coerce)
- never credits a refused call into phase coverage (refusal is a finding,
  not coverage — the final gate learns about refusals through the
  tri-state, not through coverage)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from market_service.nooa_harness.inference import (
    TOOL_PHASE,
    capability_log_entry,
)
from market_service.nooa_harness.engine.fsm import (
    AgenticLoopMembrane,
    GovernanceEvent,
    MembraneVerdict,
)
from market_service.nooa_harness.engine.loop_states import (
    LoopObservation,
    LoopTerminal,
)

log = logging.getLogger(__name__)

SCENARIO_TOOL = "calc.scenario.evaluate"
FORWARD_SCENARIO_TOOL = "calc.forward.scenario"
HYPOTHESIS_TOOL = "calc.hypothesis.test"


class ScenarioEvalStatus(Enum):
    """Tri-state answer to 'was the scenario evaluated this cycle?'.

    NOT_CALLED — no dispatch of calc.scenario.evaluate this cycle.
    REFUSED    — dispatched; deterministic refusal (terminal finding).
    EVALUATED  — dispatched; numeric payload present.
    """

    NOT_CALLED = "not_called"
    REFUSED = "refused"
    EVALUATED = "evaluated"


@dataclass(frozen=True)
class ToolOutcome:
    """Classified result of one dispatch — the only interpretation shape."""

    canonical: str
    result: str                 # capability_log result: ok | denied | error
    refused: bool               # detail.status == 'refused' (null discipline)
    refusal_reason: str | None
    payload: Any                # None when refused/denied/error


@dataclass(frozen=True)
class LoopVisit:
    """One nested loop's traversal record (Track A congruence shape).

    Which loop ran, how many LLM passes it spent, which sub-loops the
    driver opened, and whether it completed (vs budget spent). The
    controller classifies the visit; the loop driver in core.py reports it.
    """

    loop: str
    passes_spent: int
    sub_loops: tuple[str, ...] = ()
    completed: bool = False


@dataclass(frozen=True)
class CycleController:
    """Immutable, cycle-scoped semantic authority over the agentic loop.

    Fields:
      scenario       — the cycle's scenario dict, or None.
      outcomes       — append-only ledger of classified ToolOutcome rows.
      phase_coverage — canonical tool-name → the phase family it credits.
      membrane       — the governance membrane (Layer 2) this controller
                       sits UNDER. None = legacy mode: the controller is
                       the sole authority (govern() is permissive).
      observation    — the current LoopObservation the membrane governs.
      terminal       — the LoopTerminal once the cycle has settled
                       (recorded by advance()), else None.

    Every method that would mutate a mutable object instead returns a NEW
    CycleController with the successor state. Accessors are pure reads.

    AUTHORITY: the controller SITS UNDER the membrane. When a membrane is
    instantiated, ``govern``/``advance`` route every loop move through its
    verdicts; the controller never guesses legality, it consults.
    """

    scenario: dict[str, Any] | None = None
    outcomes: tuple[ToolOutcome, ...] = ()
    loop_visits: tuple[LoopVisit, ...] = ()
    phase_coverage: dict[str, frozenset[str]] = field(
        default_factory=lambda: {
            p: frozenset() for p in
            ("P1", "P2", "P3", "P4", "P5", "P6")
        }
    )
    # Governance coupling — defaulted OFF so existing behavior/tests are
    # untouched; ``compare/repr=False`` keeps value/hash semantics stable.
    membrane: AgenticLoopMembrane | None = field(
        default=None, repr=False, compare=False,
    )
    observation: LoopObservation | None = field(
        default=None, repr=False, compare=False,
    )
    terminal: LoopTerminal | None = field(
        default=None, repr=False, compare=False,
    )

    # ------------------------------------------------------------------
    # Governance (sit under the membrane; pure, immutable)
    # ------------------------------------------------------------------

    def with_membrane(self, membrane: AgenticLoopMembrane) -> "CycleController":
        """A new controller governing under ``membrane`` (immutable setter)."""
        return CycleController(
            scenario=self.scenario,
            outcomes=self.outcomes,
            loop_visits=self.loop_visits,
            phase_coverage=self.phase_coverage,
            membrane=membrane,
            observation=self.observation,
            terminal=self.terminal,
        )

    def with_observation(self, observation: LoopObservation) -> "CycleController":
        """A new controller carrying ``observation`` (immutable setter)."""
        return CycleController(
            scenario=self.scenario,
            outcomes=self.outcomes,
            loop_visits=self.loop_visits,
            phase_coverage=self.phase_coverage,
            membrane=self.membrane,
            observation=observation,
            terminal=self.terminal,
        )

    def govern(self, event: GovernanceEvent) -> MembraneVerdict:
        """Consult the membrane: may this move happen? NEVER raises.

        Legacy mode (no membrane) is PERMISSIVE — exactly today's
        behaviour: the controller is the sole authority. With a membrane
        and an observation, the verdict is the membrane's. Without an
        observation the move is denied (nothing to govern).
        """
        if self.membrane is None:
            return MembraneVerdict(
                allowed=True,
                reason="no membrane instantiated (legacy controller authority)",
            )
        if self.observation is None:
            return MembraneVerdict(
                allowed=False,
                reason="controller has no observation to govern",
            )
        return self.membrane.decide(self.observation, event)

    def advance(self, event: GovernanceEvent) -> "CycleController":
        """Apply a governed move; returns a NEW controller (immutable).

        - denied        -> self (nothing happened).
        - allowed+next  -> observation becomes the successor.
        - allowed+term  -> terminal recorded, observation cleared.
        """
        verdict = self.govern(event)
        if not verdict.allowed:
            return self
        if verdict.terminal is not None:
            return CycleController(
                scenario=self.scenario,
                outcomes=self.outcomes,
                loop_visits=self.loop_visits,
                phase_coverage=self.phase_coverage,
                membrane=self.membrane,
                observation=None,
                terminal=verdict.terminal,
            )
        if verdict.next is None:
            return self
        return CycleController(
            scenario=self.scenario,
            outcomes=self.outcomes,
            loop_visits=self.loop_visits,
            phase_coverage=self.phase_coverage,
            membrane=self.membrane,
            observation=verdict.next,
            terminal=self.terminal,
        )

    # ------------------------------------------------------------------
    # Transitions (each returns a NEW immutable controller)
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        canonical: str,
        tool_log: dict[str, Any],
        result: Any,
        *,
        raw_name: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> "CycleController":
        """Classify one dispatch and return the successor controller.

        ``tool_log`` is the capability_log entry the dispatch produced;
        ``result`` is the payload (None on refusal/denial/error). The
        classification rules are the ONLY place the
        ok-with-refused-detail null-discipline shape is interpreted.
        Credit (phase coverage) is applied atomically: a data outcome
        credits its phase family; a refusal/denial/error never does.
        """
        outcome_result = str(tool_log.get("result") or "ok")
        detail = tool_log.get("detail")
        refused = (outcome_result == "ok" and isinstance(detail, dict)
                   and detail.get("status") == "refused")
        refusal_reason = (str(detail.get("reason"))
                          if refused and isinstance(detail, dict)
                          and detail.get("reason") is not None else None)
        outcome = ToolOutcome(
            canonical=canonical,
            result=outcome_result,
            refused=refused,
            refusal_reason=refusal_reason,
            payload=result,
        )
        # Coverage credit is atomic with classification: refusal/denied/error
        # never credits (refusal is a finding, not coverage).
        coverage = dict(self.phase_coverage)
        if not refused and outcome_result == "ok":
            phase = TOOL_PHASE.get(canonical)
            if phase is not None:
                coverage[phase] = coverage.get(phase, frozenset()) | {canonical}
        return CycleController(
            scenario=self.scenario,
            outcomes=self.outcomes + (outcome,),
            loop_visits=self.loop_visits,
            phase_coverage=coverage,
            # Governance coupling — an immutable controller must carry its
            # membrane/observation/terminal through every successor.
            membrane=self.membrane,
            observation=self.observation,
            terminal=self.terminal,
        )

    def mark_declared(self, parsed: dict[str, Any]) -> "CycleController":
        """Credit P4/P6 declaration from a parsed turn (declared, not executed)."""
        declared = str(parsed.get("phase") or "").strip().upper()
        if declared not in ("P4", "P6"):
            return self
        coverage = dict(self.phase_coverage)
        coverage[declared] = coverage.get(declared, frozenset()) | {"declared"}
        return CycleController(
            scenario=self.scenario,
            outcomes=self.outcomes,
            loop_visits=self.loop_visits,
            phase_coverage=coverage,
            membrane=self.membrane,
            observation=self.observation,
            terminal=self.terminal,
        )

    # ------------------------------------------------------------------
    # Loop-traversal ledger (Track A congruence shape: traversal is
    # classified here, driven in core.py — same split as phase coverage).
    # ------------------------------------------------------------------

    def record_loop_visit(
        self, loop: str, passes_spent: int,
        sub_loops: tuple[str, ...] = (), completed: bool = False,
    ) -> "CycleController":
        """Append one nested loop's traversal record (immutable)."""
        return CycleController(
            scenario=self.scenario,
            outcomes=self.outcomes,
            loop_visits=self.loop_visits + (LoopVisit(
                loop=loop, passes_spent=passes_spent,
                sub_loops=tuple(sub_loops), completed=completed,
            ),),
            phase_coverage=self.phase_coverage,
            membrane=self.membrane,
            observation=self.observation,
            terminal=self.terminal,
        )

    def loop_coverage(self) -> dict[str, dict[str, Any]]:
        """Traversal summary per loop: passes spent + completed or not."""
        return {
            visit.loop: {
                "passes_spent": visit.passes_spent,
                "sub_loops": list(visit.sub_loops),
                "completed": visit.completed,
            }
            for visit in self.loop_visits
        }

    # ------------------------------------------------------------------
    # Scenario tri-state (one answer, all consumers)
    # ------------------------------------------------------------------

    def scenario_state(self) -> ScenarioEvalStatus:
        """Tri-state: NOT_CALLED / REFUSED / EVALUATED — sole source."""
        return self.tool_state(SCENARIO_TOOL)

    def tool_state(self, tool: str) -> ScenarioEvalStatus:
        """Generic tri-state for any deterministic scenario/test tool.

        Same semantics as scenario_state: REFUSED covers policy refusals
        and non-ok dispatches; EVALUATED needs an ok non-refused payload.
        """
        for outcome in reversed(self.outcomes):
            if outcome.canonical != tool:
                continue
            if outcome.refused:
                return ScenarioEvalStatus.REFUSED
            if outcome.result == "ok" and outcome.payload is not None:
                return ScenarioEvalStatus.EVALUATED
            # ok with null payload but not policy-refused (e.g. tool.error
            # path): treat as refusal-shaped for steer purposes.
            if outcome.result != "ok":
                return ScenarioEvalStatus.REFUSED
        return ScenarioEvalStatus.NOT_CALLED

    def forward_scenario_state(self) -> ScenarioEvalStatus:
        """Tri-state for the horizon-native forward scenario tool."""
        return self.tool_state(FORWARD_SCENARIO_TOOL)

    def hypothesis_state(self) -> ScenarioEvalStatus:
        """Tri-state for the independent hypothesis-test tool."""
        return self.tool_state(HYPOTHESIS_TOOL)

    def scenario_refusal_reason(self) -> str | None:
        """The refusal reason when state is REFUSED, else None."""
        return self.tool_refusal_reason(SCENARIO_TOOL)

    def tool_refusal_reason(self, tool: str) -> str | None:
        """The refusal reason for any tracked tool when REFUSED, else None."""
        for outcome in reversed(self.outcomes):
            if outcome.canonical == tool and outcome.refused:
                return outcome.refusal_reason
        return None

    def forward_scenario_refusal_reason(self) -> str | None:
        """The forward-scenario refusal reason when REFUSED, else None."""
        return self.tool_refusal_reason(FORWARD_SCENARIO_TOOL)

    def scenario_payload(self) -> Any:
        """The scenario tool's numeric payload when EVALUATED, else None."""
        return self.tool_payload(SCENARIO_TOOL)

    def tool_payload(self, tool: str) -> Any:
        """A tracked tool's numeric payload when EVALUATED, else None."""
        for outcome in reversed(self.outcomes):
            if (outcome.canonical == tool
                    and outcome.result == "ok"
                    and not outcome.refused
                    and isinstance(outcome.payload, dict)):
                return outcome.payload
        return None

    def forward_scenario_payload(self) -> Any:
        """The forward-scenario tool's payload when EVALUATED, else None."""
        return self.tool_payload(FORWARD_SCENARIO_TOOL)

    def congruence(self) -> dict[str, Any]:
        """Presence-level legacy-vs-forward verdict (Track A congruence shape).

        Reports WHAT happened (which paths evaluated/refused), never what
        it means — numeric agreement lives in deterministic
        ``compare_scenario_paths``, which the prompts pass cites. This is
        the loop-side substrate of "legacy upgraded by forward": both
        paths evaluated means comparable; a refusal on either side names
        its reason so the repair steer can cite it.
        """
        legacy = self.tool_state(SCENARIO_TOOL)
        forward = self.tool_state(FORWARD_SCENARIO_TOOL)
        evaluated = ScenarioEvalStatus.EVALUATED
        refused = ScenarioEvalStatus.REFUSED
        if legacy is evaluated and forward is evaluated:
            verdict = "both_evaluated"
        elif refused in (legacy, forward):
            verdict = "refused_present"
        elif legacy is evaluated or forward is evaluated:
            verdict = "partial"
        else:
            verdict = "none"
        return {
            "legacy": legacy.value,
            "forward": forward.value,
            "hypothesis": self.tool_state(HYPOTHESIS_TOOL).value,
            "verdict": verdict,
            "refusal_reasons": {
                "legacy": self.tool_refusal_reason(SCENARIO_TOOL),
                "forward": self.tool_refusal_reason(FORWARD_SCENARIO_TOOL),
                "hypothesis": self.tool_refusal_reason(HYPOTHESIS_TOOL),
            },
            "note": ("numeric agreement via deterministic "
                       "compare_scenario_paths; neither path is ground truth"),
        }

    # ------------------------------------------------------------------
    # Redundant-dispatch suppression (refusal is terminal per tool/cycle)
    # ------------------------------------------------------------------

    def is_redundant(self, canonical: str) -> bool:
        """True when this tool already refused this cycle (terminal).

        A refused deterministic tool cannot change its answer within a
        cycle: re-dispatching is a contract violation (manifest
        anti-pattern 5) the controller now enforces structurally.
        """
        return any(
            o.canonical == canonical and o.refused for o in self.outcomes
        )

    # ------------------------------------------------------------------
    # Steer synthesis (the repair prompt's scenario guidance)
    # ------------------------------------------------------------------

    def scenario_steer(self) -> str:
        """Repair steering from the tri-state — correct by construction.

        NOT_CALLED → CALL steer (the old bug: key-presence-with-None-value
        silenced this). REFUSED → citation steer, never a re-call demand.
        EVALUATED → echo demand (current behavior).
        """
        state = self.scenario_state()
        if not self.scenario:
            return ""
        if state is ScenarioEvalStatus.NOT_CALLED:
            return (
                "SCENARIO UNEVALUATED: call calc.scenario.evaluate with the "
                f"SCENARIO target_price/horizon ({self._scenario_json()}) "
                "before finalizing — the final is rejected without its "
                "→ … evidence root.\n"
            )
        if state is ScenarioEvalStatus.REFUSED:
            reason = self.scenario_refusal_reason() or "deterministic refusal"
            return (
                "SCENARIO ALREADY DISPATCHED and refused deterministically "
                f"(reason: {reason}). Do NOT re-call it — the refusal is "
                "terminal for this cycle. Cite the refusal as "
                "`calc.scenario.evaluate → refusal`, set "
                "scenario.verdict=unevaluable, and name the refusal reason "
                "in the scenario rationale.\n"
            )
        return ""

    def _scenario_json(self) -> str:
        import json
        return json.dumps(self.scenario)

    # ------------------------------------------------------------------
    # Loop-exit decision (semantics only — the FSM owns transition legality)
    # ------------------------------------------------------------------

    def next_action(self, parsed_turn: dict[str, Any]) -> str:
        """Classify the loop's next action from the current parsed turn.

        Returns one of: 'dispatch' (turn carries executable tool_calls),
        'validate' (empty/absent tool_calls → the loop validates the
        final). Budget/round accounting stays in the loop; the controller
        owns SEMANTICS, not the while-loop counters.
        """
        tool_calls = parsed_turn.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return "dispatch"
        return "validate"


__all__ = [
    "SCENARIO_TOOL",
    "FORWARD_SCENARIO_TOOL",
    "HYPOTHESIS_TOOL",
    "CycleController",
    "LoopVisit",
    "ScenarioEvalStatus",
    "ToolOutcome",
]
