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
- does NOT parse LLM text (engine/schemas.py keeps extract/coerce)
- never credits a refused call into phase coverage (refusal is a finding,
  not coverage — the final gate learns about refusals through the
  tri-state, not through coverage)
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

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
from market_service.nooa_harness.engine.scenario_contract import (
    INTENT_MODES,
    SCENARIO_HORIZONS,
    args_equivalent,
    canonical_number,
    numbers_equal,
    scenario_args_match,
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
    args: dict[str, Any] | None = None  # dispatch args (suppression is args-aware)


@dataclass(frozen=True)
class GovernanceRecord:
    """One FSM-authorized transition in the cycle trace."""

    kind: str
    target: str | None
    allowed: bool
    reason: str
    before_loop: str | None
    before_sub_loop: str | None
    after_loop: str | None
    after_sub_loop: str | None


@dataclass(frozen=True)
class LoopVisit:
    """One nested loop's traversal record (Track A congruence shape).

    Which loop ran, how many LLM passes it spent, which sub-loops the
    driver opened, and whether it completed (vs budget spent). The
    controller classifies the visit; the loop driver in core/driver.py reports it.
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
    # Scenario contract (ONE frozen authority; see scenario_contract.py).
    # compare=False keeps legacy value/hash semantics for controllers built
    # from a bare scenario dict (tests + direct construction).
    scenario_contract: Any | None = field(default=None, compare=False)
    scenario_intent: Mapping[str, Any] | None = field(default=None, compare=False)
    scenario_intent_rejects: tuple[dict[str, Any], ...] = field(
        default=(), compare=False)
    outcomes: tuple[ToolOutcome, ...] = ()
    loop_visits: tuple[LoopVisit, ...] = ()
    transitions: tuple[GovernanceRecord, ...] = ()
    phase_coverage: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({
            p: frozenset() for p in
            ("P1", "P2", "P3", "P4", "P5", "P6")
        })
    )
    # Governance coupling — defaulted OFF so existing behavior/tests are
    # untouched; ``compare/repr=False`` keeps value/hash semantics stable.
    membrane: AgenticLoopMembrane | None = field(
        default=None, repr=False, compare=False,
    )
    observation: LoopObservation | None = field(
        default=None, repr=False, compare=False,
    )
    # Phase R3 (cadence-into-plan): the controller ADOPTS the plan-bound
    # cadence at comprehension close (with_cadence). Selection is computed
    # by the pure ``engine.cadence.resolve_cadence`` and bound by the plan
    # factor; the controller is the sole semantic authority over it and
    # acquisition/dispatch read it from here. compare=False keeps value
    # semantics unchanged for legacy constructors (same treatment as
    # membrane/observation).
    cadence: Mapping[str, Any] | None = field(
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
        return replace(self, membrane=membrane)

    def with_observation(self, observation: LoopObservation) -> "CycleController":
        """A new controller carrying ``observation`` (immutable setter)."""
        return replace(self, observation=observation)

    def with_cadence(self, cadence: Mapping[str, Any] | None) -> "CycleController":
        """Adopt the plan-bound cadence (immutable setter).

        The mapping is frozen defensively (MappingProxyType) so an adopted
        cadence cannot be mutated in place by any consumer. ``None`` keeps
        the legacy no-cadence shape (legacy controllers/tests).
        """
        if cadence is None:
            return replace(self, cadence=None)
        return replace(self, cadence=MappingProxyType(dict(cadence)))

    def cadence_value(self, key: str) -> Any | None:
        """One resolved cadence value (interval_seconds/window_minutes/
        horizon_ms/regime/provenance); ``None`` when unadopted."""
        cad = self.cadence
        if not isinstance(cad, Mapping):
            return None
        return cad.get(key)

    def govern(self, event: GovernanceEvent) -> MembraneVerdict:
        """Consult the membrane: may this move happen? NEVER raises.

        Legacy mode (no membrane) is PERMISSIVE — exactly today's
        behaviour: the controller is the sole authority. With a membrane
        and an observation, the verdict is the membrane's. Without an
        observation the move is denied (nothing to govern).
        """
        if self.terminal is not None:
            return MembraneVerdict(
                allowed=False,
                reason=f"cycle already terminated at {self.terminal.value}",
            )
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
        before = self.observation
        after = verdict.next
        record = GovernanceRecord(
            kind=event.kind.value,
            target=(event.target.value if hasattr(event.target, "value") else None),
            allowed=True,
            reason=verdict.reason,
            before_loop=(before.nested_loop.value if before else None),
            before_sub_loop=(before.sub_loop.value if before and before.sub_loop else None),
            after_loop=(after.nested_loop.value if after else None),
            after_sub_loop=(after.sub_loop.value if after and after.sub_loop else None),
        )
        if verdict.terminal is not None:
            return replace(
                self,
                transitions=self.transitions + (record,),
                observation=None,
                terminal=verdict.terminal,
            )
        if verdict.next is None:
            return self
        return replace(
            self,
            transitions=self.transitions + (record,),
            observation=verdict.next,
        )

    def transition(self, event: GovernanceEvent) -> tuple["CycleController", MembraneVerdict]:
        """Authorize and apply exactly one transition.

        This is the only runtime transition seam.  A denied event returns the
        same observation with an appended denial record; it never closes or
        advances another state implicitly.
        """
        verdict = self.govern(event)
        if verdict.allowed:
            return self.advance(event), verdict
        return self.record_denial(event, verdict), verdict

    def record_denial(
        self, event: GovernanceEvent, verdict: MembraneVerdict,
    ) -> "CycleController":
        """Record a denied FSM request without changing runtime position."""
        before = self.observation
        record = GovernanceRecord(
            kind=event.kind.value,
            target=(event.target.value if hasattr(event.target, "value") else None),
            allowed=False,
            reason=verdict.reason,
            before_loop=(before.nested_loop.value if before else None),
            before_sub_loop=(before.sub_loop.value if before and before.sub_loop else None),
            after_loop=(before.nested_loop.value if before else None),
            after_sub_loop=(before.sub_loop.value if before and before.sub_loop else None),
        )
        return replace(self, transitions=self.transitions + (record,))

    def authorize_work(
        self,
        *,
        nested_loop: Any,
        sub_loop: Any | None = None,
    ) -> MembraneVerdict:
        """Ask the FSM whether work may execute at the current observation.

        Phase R4: the bootstrap flag is gone with the fetch semantic —
        there is no work that predates the FSM observation anymore.
        """
        if self.terminal is not None:
            return MembraneVerdict(False, reason=f"cycle already terminated at {self.terminal.value}")
        if self.membrane is None:
            return MembraneVerdict(True, reason="no membrane instantiated (legacy controller authority)")
        if self.observation is None:
            return MembraneVerdict(False, reason="controller has no live observation")
        return self.membrane.authorize_work(
            self.observation,
            nested_loop=nested_loop,
            sub_loop=sub_loop,
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
            payload=(result if outcome_result == "ok" and not refused else None),
            args=dict(args) if isinstance(args, dict) else None,
        )
        # Coverage credit is atomic with classification: refusal/denied/error
        # never credits (refusal is a finding, not coverage).
        coverage = dict(self.phase_coverage)
        if not refused and outcome_result == "ok":
            phase = TOOL_PHASE.get(canonical)
            if canonical == "ns.read":
                # One tool spans the read plane: credit the phase of the
                # namespace actually read, not the tool's static row.
                _ns = str((args or {}).get("namespace", ""))
                if _ns.startswith("micro.") and not _ns.endswith(".evidence"):
                    phase = "P1"
                elif _ns == "micro.evidence":
                    phase = "P2"
            if phase is not None:
                coverage[phase] = coverage.get(phase, frozenset()) | {canonical}
        return replace(
            self,
            outcomes=self.outcomes + (outcome,),
            phase_coverage=MappingProxyType(coverage),
        )

    def with_scenario_contract(self, contract: Any) -> "CycleController":
        """Bind the frozen scenario contract (immutable setter)."""
        return replace(self, scenario_contract=contract)

    def mark_declared(self, parsed: dict[str, Any]) -> "CycleController":
        """Credit P4/P6 + bind a declared scenario intent (declared, not run).

        The scenario declaration is authored by the model and DISPOSED by
        the engine (same doctrine as the task assessment): ``as_asked`` is
        always allowed; ``revision`` needs a permitting contract, a parsed
        target, a >=20-char reason, and a basis path; ``refuse`` records a
        deliberate unevaluable declaration. Rejects never bind and are
        named on ``scenario_intent_rejects`` for the repair surface.
        """
        ctrl = self
        if isinstance(parsed, dict) and parsed.get("scenario_intent") is not None:
            ctrl = ctrl.with_scenario_intent(parsed.get("scenario_intent"))
        declared = str(parsed.get("phase") or "").strip().upper()
        if declared not in ("P4", "P6"):
            return ctrl
        coverage = dict(ctrl.phase_coverage)
        coverage[declared] = coverage.get(declared, frozenset()) | {"declared"}
        return replace(ctrl, phase_coverage=MappingProxyType(coverage))

    def with_scenario_intent(self, raw: Any) -> "CycleController":
        """Dispose one model-declared scenario intent (pure, immutable)."""
        def _reject(reason: str) -> "CycleController":
            entry = {
                "field": "scenario_intent",
                "value": (str(raw.get("mode")) if isinstance(raw, Mapping)
                          else type(raw).__name__),
                "reason": reason,
            }
            return replace(
                self,
                scenario_intent_rejects=(self.scenario_intent_rejects + (entry,))[-8:],
            )

        if not isinstance(raw, Mapping):
            return _reject("scenario_intent must be a JSON object")
        mode_raw = str(raw.get("mode") or "").strip().lower()
        mode = {
            "": "as_asked", "as_asked": "as_asked", "asked": "as_asked",
            "same": "as_asked", "none": "as_asked",
            "revision": "revision", "revise": "revision",
            "changed": "revision", "different": "revision",
            "refuse": "refuse", "refusal": "refuse",
            "unevaluable": "refuse",
        }.get(mode_raw)
        if mode is None or mode not in INTENT_MODES:
            return _reject(
                f"mode {mode_raw!r} not in as_asked|revision|refuse")
        asked_t, asked_h = self._asked_scenario()
        target_raw = raw.get("target_price")
        horizon_raw = raw.get("horizon")
        target_c = (canonical_number(target_raw)
                    if target_raw not in (None, "") else None)
        horizon = (str(horizon_raw).strip()
                   if isinstance(horizon_raw, str) and horizon_raw.strip()
                   else None)
        reason = str(raw.get("reason") or "").strip()
        basis = str(raw.get("basis_path") or "").strip()
        if mode == "as_asked":
            return replace(self, scenario_intent={
                "mode": "as_asked",
                "target_price": asked_t,
                "horizon": horizon or asked_h,
                "reason": reason[:500] or None,
            })
        if mode == "refuse":
            return replace(self, scenario_intent={
                "mode": "refuse",
                "target_price": asked_t,
                "horizon": horizon or asked_h,
                "reason": reason[:500] or None,
            })
        # mode == revision
        problems: list[str] = []
        contract = self.scenario_contract
        if contract is None:
            problems.append("no scenario contract to revise")
        if target_c is None:
            problems.append("revision target_price unparseable")
        elif contract is not None and numbers_equal(target_c, contract.asked_target):
            problems.append("revision target equals asked target; use mode=as_asked")
        if (contract is not None and contract.divergence == "forbidden"
                and target_c is not None
                and target_c not in contract.allowed_targets):
            problems.append(
                "contract divergence is forbidden (prompt did not invite it); "
                f"allowed targets: {list(contract.allowed_targets)}")
        if horizon is not None and horizon not in SCENARIO_HORIZONS:
            problems.append(f"horizon {horizon!r} outside {'|'.join(SCENARIO_HORIZONS)}")
        if len(reason) < 20:
            problems.append("revision requires a reason (>=20 chars)")
        if not basis:
            problems.append("revision requires basis_path")
        if problems:
            return _reject("; ".join(problems))
        return replace(self, scenario_intent={
            "mode": "revision",
            "target_price": target_c,
            "horizon": horizon or asked_h,
            "reason": reason[:500],
            "basis_path": basis,
            "asked_target": asked_t,
            "asked_horizon": asked_h,
        })

    # ------------------------------------------------------------------
    # Loop-traversal ledger (Track A congruence shape: traversal is
    # classified here, driven in core.py — same split as phase coverage).
    # ------------------------------------------------------------------

    def record_loop_visit(
        self, loop: str, passes_spent: int,
        sub_loops: tuple[str, ...] = (), completed: bool = False,
    ) -> "CycleController":
        """Append one nested loop's traversal record (immutable)."""
        # A visit is a projection of the authorized transition trace.  Never
        # claim a sub-loop that the FSM did not actually open.
        opened = {
            record.after_sub_loop
            for record in self.transitions
            if record.kind == "open_subloop" and record.after_sub_loop
        }
        actual_sub_loops = tuple(name for name in sub_loops if name in opened)
        actual_completed = completed and set(actual_sub_loops) == set(sub_loops)
        return replace(
            self,
            loop_visits=self.loop_visits + (LoopVisit(
                loop=loop, passes_spent=passes_spent,
                sub_loops=actual_sub_loops, completed=actual_completed,
            ),),
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

    def transition_trace(self) -> list[dict[str, Any]]:
        """Serializable FSM-authorized transition trace for the artifact."""
        return [
            {
                "kind": record.kind,
                "target": record.target,
                "allowed": record.allowed,
                "reason": record.reason,
                "before": {"loop": record.before_loop, "sub_loop": record.before_sub_loop},
                "after": {"loop": record.after_loop, "sub_loop": record.after_sub_loop},
            }
            for record in self.transitions
        ]

    # ------------------------------------------------------------------
    # Scenario tri-state (one answer, all consumers)
    # ------------------------------------------------------------------

    def scenario_state(self) -> ScenarioEvalStatus:
        """Tri-state: NOT_CALLED / REFUSED / EVALUATED — sole source."""
        return self.tool_state(SCENARIO_TOOL)

    def tool_state(self, tool: str) -> ScenarioEvalStatus:
        """Generic tri-state for any deterministic scenario/test tool.

        Only genuine executions classify: REFUSED is an ok dispatch with a
        deterministic refusal; EVALUATED needs an ok non-refused payload.
        Authorization denials (wrong loop, out-of-position, suppression)
        and transport errors are SKIPPED — the tool was never attempted, so
        it stays NOT_CALLED (retryable) instead of masquerading as a
        refused finding. That distinction is what keeps the chain gate
        honest: a never-executed discipline audit can never read as
        "refused" and slip past the required-links check.
        """
        for outcome in reversed(self.outcomes):
            if outcome.canonical != tool:
                continue
            if outcome.result != "ok":
                continue
            if outcome.refused:
                return ScenarioEvalStatus.REFUSED
            # A dispatch that returned no payload is still a dispatch. It
            # must not fall back to NOT_CALLED, otherwise the repair loop
            # can ask the model to repeat an already exhausted tool.
            return (
                ScenarioEvalStatus.EVALUATED
                if outcome.payload is not None
                else ScenarioEvalStatus.REFUSED
            )
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

    def _asked_scenario(self) -> tuple[str | None, str | None]:
        """The contract's asked target/horizon (legacy dict fallback)."""
        contract = self.scenario_contract
        if contract is not None:
            return str(contract.asked_target), str(contract.asked_horizon)
        if isinstance(self.scenario, dict):
            target = self.scenario.get("target_price")
            horizon = self.scenario.get("horizon")
            return (
                str(target) if target is not None else None,
                str(horizon) if horizon is not None else None,
            )
        return None, None

    def effective_scenario(self) -> tuple[str | None, str | None]:
        """The target/horizon this cycle is bound to (declared revision wins)."""
        asked_t, asked_h = self._asked_scenario()
        intent = self.scenario_intent
        if isinstance(intent, Mapping) and intent.get("mode") == "revision":
            target = intent.get("target_price")
            horizon = intent.get("horizon")
            return (
                str(target) if target else asked_t,
                str(horizon) if horizon else asked_h,
            )
        return asked_t, asked_h

    def scenario_outcome(self) -> "ToolOutcome | None":
        """The last genuine scenario dispatch (success or refusal), else None.

        This is the OUTCOME validation must bind to — not any row in the
        capability log. ``None`` means NOT_CALLED (or only denied/errored
        dispatches, which never attempted the tool).
        """
        for outcome in reversed(self.outcomes):
            if outcome.canonical == SCENARIO_TOOL and outcome.result == "ok":
                return outcome
        return None

    def carry_scenario(
        self, canonical: str, args: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Bind the scenario contract into ABSENT dispatch args (pre-dispatch).

        Target and horizon are filled INDEPENDENTLY (a target-only call must
        not silently fall back to the tool's default horizon). A valid
        explicit target executes as given — validation compares it to the
        bound target. An INVALID explicit target (0, garbage) does not count
        as "explicit": the bound target is carried and the invalid raw value
        is named on the finding, so a refusal can never masquerade as the
        asked question being answered.

        Returns ``(args, finding)``. The finding rides the capability log
        (operators) AND the model-visible scenario surface (driver).
        """
        if canonical != SCENARIO_TOOL:
            return (dict(args) if isinstance(args, dict) else {}), None
        asked_t, asked_h = self._asked_scenario()
        effective_t, effective_h = self.effective_scenario()
        if effective_t is None and effective_h is None:
            return (dict(args) if isinstance(args, dict) else {}), None
        out = dict(args) if isinstance(args, dict) else {}
        changed = False
        ignored_invalid: str | None = None
        raw_target = out.get("target_price")
        if raw_target is None or (
                isinstance(raw_target, str) and not raw_target.strip()):
            if effective_t is not None:
                out["target_price"] = effective_t
                changed = True
        elif canonical_number(raw_target) is None:
            ignored_invalid = str(raw_target)
            if effective_t is not None:
                out["target_price"] = effective_t
                changed = True
        raw_horizon = out.get("horizon")
        if raw_horizon is None or (
                isinstance(raw_horizon, str) and not raw_horizon.strip()):
            if effective_h:
                out["horizon"] = effective_h
                changed = True
        if not changed:
            return out, None
        intent = (self.scenario_intent
                  if isinstance(self.scenario_intent, Mapping) else {})
        finding: dict[str, Any] = {
            "target_price": out.get("target_price"),
            "horizon": out.get("horizon"),
            "source": ("declared_revision" if intent.get("mode") == "revision"
                       else "directive"),
            "asked_target": asked_t,
            "asked_horizon": asked_h,
            "effective_target": effective_t,
            "effective_horizon": effective_h,
        }
        if self.scenario_contract is not None:
            finding["contract_id"] = self.scenario_contract.contract_id
        if ignored_invalid is not None:
            finding["ignored_invalid_explicit"] = ignored_invalid
        return out, finding

    def is_redundant(self, canonical: str, args: dict[str, Any] | None = None) -> bool:
        """True when this tool already refused this cycle with the same args.

        Suppression is args-aware and NUMERIC for scenario fields: a
        deterministic refusal is terminal only for equivalent inputs
        (``104.8`` == ``104.80``). A re-call with materially different args
        (window, interval, declared revision target) is new work the track
        may attempt — budgets and predicates still gate it. No-args callers
        match only no-args refusals, preserving the legacy call shape.
        """
        wanted = dict(args) if isinstance(args, dict) else {}
        return any(
            o.canonical == canonical and o.refused
            and args_equivalent(
                o.args if isinstance(o.args, Mapping) else {}, wanted)
            for o in self.outcomes
        )

    # ------------------------------------------------------------------
    # Steer synthesis (the repair prompt's scenario guidance)
    # ------------------------------------------------------------------

    def scenario_steer(self) -> str:
        """Repair steering bound to the EFFECTIVE contract target.

        NOT_CALLED → call steer with the asked tuple. REFUSED → if the
        refusal was of the asked/declared target, the refusal is terminal
        and the steer is citation-only; if it refused a DIFFERENT target,
        the steer names the mismatch and demands the bound target (or a
        declared revision when the prompt permits one). EVALUATED → a
        different target is named as a contract violation, never left for
        the final to discover.
        """
        asked_t, asked_h = self._asked_scenario()
        effective_t, effective_h = self.effective_scenario()
        if asked_t is None and self.scenario_contract is None:
            return ""
        state = self.scenario_state()
        outcome = self.scenario_outcome()
        if state is ScenarioEvalStatus.NOT_CALLED:
            return (
                "SCENARIO UNEVALUATED: call calc.scenario.evaluate with the "
                f"contract target/horizon ({effective_t} @ {effective_h}) "
                "before finalizing — the final is rejected without its "
                "→ … evidence root.\n"
            )
        recorded_args = (isinstance(getattr(outcome, "args", None), Mapping)
                         and bool(outcome.args))
        if state is ScenarioEvalStatus.REFUSED:
            if outcome is not None and recorded_args and not scenario_args_match(
                    outcome.args, effective_t, effective_h):
                return (
                    "SCENARIO REFUSED A DIFFERENT TARGET/HORIZON: last "
                    f"dispatch args {getattr(outcome, 'args', None)!r} do not "
                    f"match the bound target {effective_t} @ {effective_h}. "
                    "A refusal only answers the asked question when it is a "
                    "refusal OF that target — dispatch the bound target "
                    "exactly (a declared revision is required to change it "
                    "when the contract permits one).\n"
                )
            reason = self.scenario_refusal_reason() or "deterministic refusal"
            return (
                "SCENARIO ALREADY DISPATCHED and refused deterministically "
                f"(reason: {reason}). Do NOT re-call it — the refusal is "
                "terminal for this cycle. Cite the refusal as "
                "`calc.scenario.evaluate → refusal`, set "
                "scenario.verdict=unevaluable, and name the refusal reason "
                "in the scenario rationale.\n"
            )
        if outcome is not None and recorded_args and not scenario_args_match(
                outcome.args, effective_t, effective_h):
            return (
                "SCENARIO CONTRACT VIOLATION: the evaluated dispatch args "
                f"{getattr(outcome, 'args', None)!r} are not the bound target "
                f"{effective_t} @ {effective_h}. Evaluate the bound target; "
                "if the question must change, declare scenario_intent mode="
                "revision with reason + basis_path (only when the contract "
                "permits divergence).\n"
            )
        return ""

    def _scenario_json(self) -> str:
        import json
        return json.dumps(self.scenario)

    # ------------------------------------------------------------------
    # Loop-exit decision (semantics only — the FSM owns transition legality)
    # ------------------------------------------------------------------

    def scenario_dispatch_due(self) -> bool:
        """True when a contract scenario exists but was never dispatched.

        Pure systemic guard: a price-target question the cycle never
        attempted (tri-state ``NOT_CALLED``) cannot be answered — any
        verdict without a dispatch is fabrication, and the validation home
        cannot dispatch it (loop-work boundary). The reasoning exit uses
        this to force one bounded dispatch pass instead of closing blind.
        """
        if self.scenario_contract is None and not isinstance(
                self.scenario, dict):
            return False
        try:
            return self.scenario_state() is ScenarioEvalStatus.NOT_CALLED
        except Exception:
            return False

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

    # ------------------------------------------------------------------
    # Turn-shape authority (Signal B load doctrine: slim/standard/full)
    # ------------------------------------------------------------------

    def turn_shape(self, loop: str, passes_spent: int = 0) -> str:
        """Required turn shape for the next narration turn (pure).

        The per-pass load contract: evidence passes carry position +
        tool strategy only (slim); reasoning passes add citations and a
        hypothesis draft (standard); validation recovery, output, and
        finalize turns carry the full object (full). Static base map from
        loop position plus ONE stagnation overlay: an evidence loop that
        has spent 2+ passes without advancing escalates to standard and
        must cite instead of dispatching blindly again. Unknown loops
        resolve to full (never under-ask where position is unclear).
        Shape changes are recorded by callers on the capability log /
        ledger, so every escalation is auditable without touching the
        frozen membrane: the observation (governed) feeds this derivation
        (semantic), the composer renders it. Returns
        one of ``slim`` | ``standard`` | ``full``.
        """
        name = str(loop or "").strip().lower()
        try:
            spent = int(passes_spent)
        except (TypeError, ValueError):
            spent = 0
        if name in ("validation", "output"):
            return "full"
        if name == "reasoning":
            return "standard"
        if name in ("evidence", "context"):
            if spent >= 2:
                return "standard"
            return "slim"
        return "full"


__all__ = [
    "SCENARIO_TOOL",
    "FORWARD_SCENARIO_TOOL",
    "HYPOTHESIS_TOOL",
    "CycleController",
    "GovernanceRecord",
    "LoopVisit",
    "ScenarioEvalStatus",
    "next_uncovered_phase",
    "scenario_verdict",
    "ToolOutcome",
]


# ---------------------------------------------------------------------------
# Coverage order + scenario tri-state reads — owned HERE (classification
# authority), moved from engine/narration.py (Pass-C redistribution).
# narration.py re-exports these names until its deletion pass.
# ---------------------------------------------------------------------------


def next_uncovered_phase(coverage: dict[str, set[str]]) -> str:
    """First required phase family with no executed tool yet; then P6."""
    from market_service.nooa_harness.inference import _REQUIRED_PHASES

    for phase in _REQUIRED_PHASES:
        if not coverage.get(phase):
            return phase
    if not coverage.get("P6"):
        return "P6"
    return "P5"



def scenario_verdict(
    scenario_result: dict[str, Any] | None,
    capability_log: list[dict[str, Any]],
    gate_status: str,
    gate_reasons: list[str],
) -> tuple[str, str]:
    """Deterministic reachability verdict from the scenario tool payload.

    Decided from the accumulated ``calc.scenario.evaluate`` result — never
    from LLM text. Band-aware cut points: max exceedance across the band
    == 0 → invalidated (unreachable even on the friendly edge); min
    exceedance ≥ 0.5 → validated (preponderance through the full band);
    anything between → inconclusive. The provisional gate always ceilings
    at inconclusive (reason carries the directional read); refusals and
    missing evaluations are inconclusive with the cause named.
    """
    entry: dict[str, Any] | None = None
    for row in reversed(capability_log):
        if isinstance(row, dict) and row.get("capability") == "calc.scenario.evaluate":
            entry = row
            break
    if not isinstance(scenario_result, dict):
        detail = (entry or {}).get("detail") or {}
        cause = detail.get("reason", "scenario tool never called") \
            if isinstance(detail, dict) else "scenario tool never called"
        return ("inconclusive",
                f"scenario unevaluated ({cause}); reachability undecided")
    direction = str(scenario_result.get("direction") or "?")
    target = str(scenario_result.get("target_price") or "?")
    windows = scenario_result.get("n_windows_usable")
    horizon = str(scenario_result.get("horizon") or "?")
    context = (f"{direction} {target} at {horizon} over {windows} usable windows")
    if gate_status == "provisional":
        exc = str(scenario_result.get("exceedance") or "?")
        return ("inconclusive",
                f"Gate provisional ({';'.join(gate_reasons)}); scenario reads "
                f"{context} at {exc} exceedance but held inconclusive — "
                f"promotion waits for a validated fit")
    if gate_status != "validated":
        return ("inconclusive", "; ".join(gate_reasons) or "gate not validated")
    rng = scenario_result.get("exceedance_range")
    try:
        edges = [Decimal(str(v)) for v in rng] if isinstance(rng, list) and len(rng) == 2 \
            else [Decimal(str(scenario_result.get("exceedance")))]
    except (InvalidOperation, ValueError, TypeError):
        return ("inconclusive", f"scenario exceedance unparseable ({context})")
    if max(edges) == 0:
        return ("invalidated",
                f"H0 holds: {context} at 0 exceedance across the full band — "
                f"required flow outside the observed regime")
    if min(edges) >= Decimal("0.5"):
        return ("validated",
                f"H1 holds: {context} at ≥0.5 exceedance through the full band")
    return ("inconclusive",
            f"flow regime straddles the requirement ({context}); "
            f"band-range exceedance undecided")


# Per-phase steering fragments — appended to follow-up prompts so each
# turn knows what the next uncovered phase demands. Window freedom is
# stated once here: the agent may vary interval_seconds (10/15/30) and
# window_minutes (15/30/60) in calc/fit tool args; deterministic code
# executes, the agent never recomputes.
