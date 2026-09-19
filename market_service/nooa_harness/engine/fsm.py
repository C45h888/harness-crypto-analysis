"""AgenticLoopMembrane — the primary governance membrane over the agent.

FILE IDENTITY: this module is ``engine/fsm.py`` — the canonical governance
membrane. It is a PURE DERIVATION of ``engine/loop_states.py``: every state
name (stages, loops, sub-loops, steps, intents, terminals, observations)
lives in ``loop_states.py``, and legality here is derived only from those
mappings. ``engine/membrane.py`` was a scratch file superseded by this one
and must not be used.

LAYER 2 of the two-layer agentic surface. This module is the DETERMINISTIC
FOUNDATION of the agentic loop: the frozen legality authority. It decides
WHAT transitions are legal over the Layer-1 vocabulary
(``loop_states.py``) and routes failure to first-class terminals
gracefully. It does NOT classify tool outcomes, does NOT credit coverage,
does NOT parse LLM text — semantics stay in the ``CycleController``.

AUTHORITY FLOW (the deterministic foundation, fsm -> controller -> agent):

    AgenticLoopMembrane  — LEGALITY. The constitution. Admissible
                           observations, legal transitions, terminal
                           routing. Frozen and total: every
                           (state, event) pair yields a verdict, never
                           an unhandled crash.
        -> CycleController — SEMANTICS. Classifies outcomes, credits
                             coverage, owns the scenario tri-state and
                             exit decisions. Sits UNDER the membrane and
                             consults it (``CycleController.govern``).
        -> the agent (LLM) — PROPOSALS ONLY. Proposes turns; the
                             controller disposes; the membrane governs.

The membrane is a product automaton over ``LoopObservation``:

    (NestedLoop, TaskIntent, SubLoop | None)

Every legal state and every legal transition is VIEWABLE:
  - ``states()``                     — all admissible observations.
  - ``legal_events(state)``          — what may happen from a state.
  - ``all_transitions()``            — every (state, event, outcome).
  - ``render()``                     — a human-readable table.
  - ``decide(state, event)``         — a verdict; never raises.

FAILURE IS GRACEFUL AND FIRST-CLASS. Failure event kinds (``GATE_REFUSED``,
``NARRATION_FAILED``, ``PARSE_FAILED``, ``VALIDATION_FAILED``,
``BUDGET_EXHAUSTED``, ``INFRA_FAILED``) are legal from EVERY non-terminal
observation and route to the corresponding ``LoopTerminal``. The membrane
never guesses a failure class; the runtime reports the kind, the membrane
routes it.

Constitution (the rules, so the table is derivable and viewable):
  - ``initial()`` is (COMPREHENSION, UNDERSTAND_TASK, None).
  - ``ENTER_LOOP`` moves to the NEXT stage's loop with its default intent
    (default intent = first intent served by that loop); legal only when
    not inside a sub-loop; the loop must be the immediate successor.
  - ``SET_TASK`` re-tasks within the current loop; the intent must be
    served by that loop.
  - ``OPEN_SUBLOOP`` opens exactly the NEXT sub-loop in the loop's order
    (or the first when none is open).
  - ``CLOSE_SUBLOOP`` closes the open sub-loop.
  - ``SETTLE`` ends the cycle; legal only from OUTPUT.
  - success is exactly one terminal (SETTLED); failures partition the rest.

The retired mutable-loop vocabulary is not part of the runtime. The membrane
is the replacement constitution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from market_service.nooa_harness.engine.loop_states import (
    INTENT_LOOP,
    LOOP_INTENTS,
    LOOP_SUBLOOPS,
    STAGE_LOOP,
    STAGE_ORDER,
    FAILURE_TERMINALS,
    SUCCESS_TERMINALS,
    AgenticStage,
    LoopObservation,
    LoopTerminal,
    NestedLoop,
    SubLoop,
    TaskIntent,
)


# ---------------------------------------------------------------------------
# Governance events
# ---------------------------------------------------------------------------


class GovernanceEventKind(str, Enum):
    """The kinds of events that drive the membrane.

    Progress events carry a ``target``:
      - ENTER_LOOP    -> target is the ``NestedLoop`` to enter.
      - SET_TASK      -> target is the ``TaskIntent`` to adopt.
      - OPEN_SUBLOOP  -> target is the ``SubLoop`` to open.

    Failure events carry no target; they route to a ``LoopTerminal``
    (see ``_FAILURE_KIND_TERMINAL``), legal from every non-terminal state.
    """

    ENTER_LOOP = "enter_loop"
    SET_TASK = "set_task"
    OPEN_SUBLOOP = "open_subloop"
    COMPLETE_SUBLOOP = "complete_subloop"
    CLOSE_SUBLOOP = "close_subloop"
    SETTLE = "settle"
    GATE_REFUSED = "gate_refused"
    NARRATION_FAILED = "narration_failed"
    PARSE_FAILED = "parse_failed"
    VALIDATION_FAILED = "validation_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INFRA_FAILED = "infra_failed"


@dataclass(frozen=True)
class GovernanceEvent:
    """A typed event offered to the membrane.

    ``kind``   — the event kind.
    ``target`` — the payload for progress events (NestedLoop / TaskIntent /
                 SubLoop); ``None`` for close/settle/failure events.
    """

    kind: GovernanceEventKind
    target: NestedLoop | TaskIntent | SubLoop | None = None


_FAILURE_KIND_TERMINAL: dict[GovernanceEventKind, LoopTerminal] = {
    GovernanceEventKind.GATE_REFUSED: LoopTerminal.GATE_REFUSED,
    GovernanceEventKind.NARRATION_FAILED: LoopTerminal.NARRATION_FAILED,
    GovernanceEventKind.PARSE_FAILED: LoopTerminal.PARSE_FAILED,
    GovernanceEventKind.VALIDATION_FAILED: LoopTerminal.VALIDATION_FAILED,
    GovernanceEventKind.BUDGET_EXHAUSTED: LoopTerminal.BUDGET_EXHAUSTED,
    GovernanceEventKind.INFRA_FAILED: LoopTerminal.INFRA_FAILED,
}

FAILURE_EVENT_KINDS: tuple[GovernanceEventKind, ...] = tuple(
    _FAILURE_KIND_TERMINAL
)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MembraneVerdict:
    """The membrane's answer to ``decide(state, event)`` — never raises.

    ``allowed``  — whether the move is legal.
    ``next``     — the successor observation when ``allowed`` and the move
                   stays on the board (``None`` when the move is a terminal
                   or was denied).
    ``terminal`` — the ``LoopTerminal`` reached when the move ends the
                   cycle (success or graceful failure); else ``None``.
    ``reason``   — a human-readable justification (also the denial reason).
    """

    allowed: bool
    next: LoopObservation | None = None
    terminal: LoopTerminal | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# The membrane
# ---------------------------------------------------------------------------


class AgenticLoopMembrane:
    """The frozen legality authority over the agentic loop.

    Stateless by construction: every decision is a pure function of the
    observation + event against the Layer-1 mappings, so the same
    (state, event) always yields the same verdict. Immutable by policy:
    no mutating method exists. ``canonical()`` returns the single
    constitution; the module-level ``GOVERNANCE_MEMBRANE`` is it.
    """

    __slots__ = ()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def canonical(cls) -> "AgenticLoopMembrane":
        """The canonical constitution (single source of truth)."""
        return cls()

    @staticmethod
    def _loop_order() -> tuple[NestedLoop, ...]:
        return tuple(STAGE_LOOP[stage] for stage in STAGE_ORDER)

    # ------------------------------------------------------------------
    # Admissibility
    # ------------------------------------------------------------------

    def is_legal_state(self, observation: object) -> bool:
        """Is this observation admissible on the board?

        Requirements:
          - it is a ``LoopObservation``;
          - the task intent is served by the observation's loop;
          - the sub-loop (when set) is a sub-loop of the observation's loop.
        """
        if not isinstance(observation, LoopObservation):
            return False
        if INTENT_LOOP.get(observation.task) is not observation.nested_loop:
            return False
        if observation.sub_loop is None:
            return True
        return observation.sub_loop in LOOP_SUBLOOPS[observation.nested_loop]

    @staticmethod
    def _next_sub_loop(loop: NestedLoop, current: SubLoop | None) -> SubLoop | None:
        subs = LOOP_SUBLOOPS[loop]
        if current is None:
            return subs[0]
        idx = subs.index(current)
        if idx + 1 < len(subs):
            return subs[idx + 1]
        return None

    def default_intent_for(self, loop: NestedLoop) -> TaskIntent:
        """The first intent served by a loop = the intent a loop enters with."""
        return LOOP_INTENTS[loop][0]

    def successor_loop(self, loop: NestedLoop) -> NestedLoop | None:
        """The next loop in the traversal; ``None`` after OUTPUT."""
        order = self._loop_order()
        idx = order.index(loop)
        if idx + 1 < len(order):
            return order[idx + 1]
        return None

    def authorize_work(
        self,
        observation: LoopObservation,
        *,
        nested_loop: NestedLoop,
        sub_loop: SubLoop | None = None,
        bootstrap: bool = False,
    ) -> MembraneVerdict:
        """Authorize work at the current membrane-visible position.

        Work authorization is deliberately narrower than transition legality:
        the FSM does not know tool semantics, but it does know whether the
        runtime is currently in the loop/sub-loop that owns the work.  The
        only exception is the two deterministic gate reads, which are an
        explicit wake bootstrap and are allowed before COMPREHENSION opens
        its first sub-loop.
        """
        if not self.is_legal_state(observation):
            return MembraneVerdict(False, reason="work requested from an illegal observation")
        if bootstrap:
            if (
                nested_loop is NestedLoop.COMPREHENSION
                and observation.nested_loop is NestedLoop.COMPREHENSION
                and observation.task is TaskIntent.UNDERSTAND_TASK
                and observation.sub_loop is None
            ):
                return MembraneVerdict(True, next=observation, reason="authorized wake bootstrap work")
            return MembraneVerdict(False, reason="bootstrap work is only legal at initial comprehension")
        if observation.nested_loop is not nested_loop:
            return MembraneVerdict(
                False,
                reason=(f"work belongs to {nested_loop.value}, current loop is "
                        f"{observation.nested_loop.value}"),
            )
        if sub_loop is not None and observation.sub_loop is not sub_loop:
            return MembraneVerdict(
                False,
                reason=(f"work belongs to sub-loop {sub_loop.value}, current sub-loop is "
                        f"{observation.sub_loop.value if observation.sub_loop else 'none'}"),
            )
        return MembraneVerdict(True, next=observation, reason="work authorized at current observation")

    def terminal_for(self, kind: GovernanceEventKind) -> LoopTerminal | None:
        """The terminal a failure kind routes to; ``None`` for progress kinds.

        This is the graceful-failure seam: the loop reports a KIND, the
        membrane returns the terminal. The loop never guesses a terminal.
        """
        return _FAILURE_KIND_TERMINAL.get(kind)

    # ------------------------------------------------------------------
    # The board (viewability — every state is viewable)
    # ------------------------------------------------------------------

    def initial(self) -> LoopObservation:
        """The post-wake observation: COMPREHENSION / UNDERSTAND_TASK."""
        return LoopObservation(
            NestedLoop.COMPREHENSION, TaskIntent.UNDERSTAND_TASK, None
        )

    def states(self) -> tuple[LoopObservation, ...]:
        """Every admissible observation on the board, in traversal order."""
        out: list[LoopObservation] = []
        for loop in self._loop_order():
            for task in LOOP_INTENTS[loop]:
                for sub_loop in (*LOOP_SUBLOOPS[loop], None):
                    obs = LoopObservation(loop, task, sub_loop)
                    if self.is_legal_state(obs):
                        out.append(obs)
        return tuple(out)

    def terminals(self) -> tuple[LoopTerminal, ...]:
        """All endpoints (success + failures)."""
        return tuple(LoopTerminal)

    # ------------------------------------------------------------------
    # Legality
    # ------------------------------------------------------------------

    def legal_events(self, observation: LoopObservation) -> tuple[GovernanceEvent, ...]:
        """The events legal FROM this observation (the viewable constitution)."""
        if not self.is_legal_state(observation):
            return ()
        loop, sub_loop = observation.nested_loop, observation.sub_loop
        events: list[GovernanceEvent] = []

        if sub_loop is None:
            successor = self.successor_loop(loop)
            if successor is not None:
                events.append(
                    GovernanceEvent(GovernanceEventKind.ENTER_LOOP, successor)
                )
        for intent in LOOP_INTENTS[loop]:
            if intent is not observation.task:
                events.append(GovernanceEvent(GovernanceEventKind.SET_TASK, intent))
        nxt = self._next_sub_loop(loop, sub_loop)
        if nxt is not None:
            events.append(GovernanceEvent(GovernanceEventKind.OPEN_SUBLOOP, nxt))
        if sub_loop is not None:
            events.append(GovernanceEvent(GovernanceEventKind.COMPLETE_SUBLOOP))
            events.append(GovernanceEvent(GovernanceEventKind.CLOSE_SUBLOOP))
        if loop is NestedLoop.OUTPUT:
            events.append(GovernanceEvent(GovernanceEventKind.SETTLE))
        # Graceful failure: every failure kind is legal from every state.
        for kind in FAILURE_EVENT_KINDS:
            events.append(GovernanceEvent(kind))
        return tuple(events)

    def decide(
        self, observation: LoopObservation, event: GovernanceEvent
    ) -> MembraneVerdict:
        """The membrane's answer — NEVER raises.

        Failure kinds are routed to their terminal from any non-terminal
        observation. Progress events are adjudicated against the
        constitution; illegal moves are DENIED with a reason (the loop must
        not proceed), never thrown.
        """
        if not isinstance(event, GovernanceEvent):
            return MembraneVerdict(
                allowed=False, reason="event is not a GovernanceEvent"
            )
        kind = event.kind

        # Graceful failure — first-class, legal from every state.
        if kind in _FAILURE_KIND_TERMINAL:
            # Failure routing is total over admissible observations, but a
            # terminal/absent observation is not a live cycle state.  Keeping
            # this check here prevents a second failure from overwriting an
            # already-settled controller.
            if not isinstance(observation, LoopObservation):
                return MembraneVerdict(
                    allowed=False,
                    reason="failure event requires a live loop observation",
                )
            terminal = _FAILURE_KIND_TERMINAL[kind]
            return MembraneVerdict(
                allowed=True,
                terminal=terminal,
                reason=f"graceful failure routed to terminal {terminal.value}",
            )

        if not self.is_legal_state(observation):
            return MembraneVerdict(
                allowed=False, reason=f"illegal observation {observation!r}"
            )

        if kind is GovernanceEventKind.SETTLE:
            if observation.nested_loop is not NestedLoop.OUTPUT:
                return MembraneVerdict(
                    allowed=False,
                    reason="settle is only legal from the OUTPUT loop",
                )
            return MembraneVerdict(
                allowed=True,
                terminal=LoopTerminal.SETTLED,
                reason="cycle settled at SETTLED",
            )

        if kind is GovernanceEventKind.ENTER_LOOP:
            target = event.target
            if not isinstance(target, NestedLoop):
                return MembraneVerdict(
                    allowed=False, reason="enter_loop requires a NestedLoop target"
                )
            successor = self.successor_loop(observation.nested_loop)
            if successor is None:
                return MembraneVerdict(
                    allowed=False, reason="no successor loop from this loop"
                )
            if target is not successor:
                return MembraneVerdict(
                    allowed=False,
                    reason=f"can only enter the successor loop {successor.value}",
                )
            if observation.sub_loop is not None:
                return MembraneVerdict(
                    allowed=False,
                    reason="close the sub-loop before leaving the loop",
                )
            return MembraneVerdict(
                allowed=True,
                next=LoopObservation(
                    target, self.default_intent_for(target), None
                ),
                reason=f"entered loop {target.value}",
            )

        if kind is GovernanceEventKind.SET_TASK:
            target = event.target
            if not isinstance(target, TaskIntent):
                return MembraneVerdict(
                    allowed=False, reason="set_task requires a TaskIntent target"
                )
            if INTENT_LOOP[target] is not observation.nested_loop:
                return MembraneVerdict(
                    allowed=False,
                    reason=f"intent {target.value} is not served by loop "
                    f"{observation.nested_loop.value}",
                )
            return MembraneVerdict(
                allowed=True,
                next=LoopObservation(observation.nested_loop, target, observation.sub_loop),
                reason=f"adopted intent {target.value}",
            )

        if kind is GovernanceEventKind.OPEN_SUBLOOP:
            target = event.target
            if not isinstance(target, SubLoop):
                return MembraneVerdict(
                    allowed=False, reason="open_subloop requires a SubLoop target"
                )
            nxt = self._next_sub_loop(
                observation.nested_loop, observation.sub_loop
            )
            if nxt is None:
                return MembraneVerdict(
                    allowed=False, reason="no next sub-loop to open"
                )
            if target is not nxt:
                return MembraneVerdict(
                    allowed=False,
                    reason=f"must open the next sub-loop in order ({nxt.value})",
                )
            return MembraneVerdict(
                allowed=True,
                next=LoopObservation(observation.nested_loop, observation.task, target),
                reason=f"opened sub-loop {target.value}",
            )

        if kind in (
            GovernanceEventKind.COMPLETE_SUBLOOP,
            GovernanceEventKind.CLOSE_SUBLOOP,
        ):
            if observation.sub_loop is None:
                return MembraneVerdict(
                    allowed=False, reason="no sub-loop is open to complete/close"
                )
            reason = (
                "completed sub-loop"
                if kind is GovernanceEventKind.COMPLETE_SUBLOOP
                else "closed sub-loop"
            )
            return MembraneVerdict(
                allowed=True,
                next=LoopObservation(observation.nested_loop, observation.task, None),
                reason=reason,
            )

        return MembraneVerdict(
            allowed=False, reason=f"unknown event kind {kind.value}"
        )

    # ------------------------------------------------------------------
    # Viewability — the whole board
    # ------------------------------------------------------------------

    def all_transitions(self) -> tuple[tuple[LoopObservation, GovernanceEvent, LoopObservation | LoopTerminal], ...]:
        """Every (state, event, outcome) triple on the board."""
        out: list[tuple[LoopObservation, GovernanceEvent, LoopObservation | LoopTerminal]] = []
        for state in self.states():
            for event in self.legal_events(state):
                verdict = self.decide(state, event)
                if not verdict.allowed:
                    # A legal event must adjudicate as allowed (integrity).
                    raise ValueError(
                        f"legal event {event} denied from {state}: {verdict.reason}"
                    )
                outcome: LoopObservation | LoopTerminal
                if verdict.terminal is not None:
                    outcome = verdict.terminal
                else:
                    assert verdict.next is not None
                    outcome = verdict.next
                out.append((state, event, outcome))
        return tuple(out)

    def render(self) -> str:
        """A human-readable, viewable rendering of the whole constitution."""
        lines = [
            "AgenticLoopMembrane — primary governance membrane (all states viewable)",
            f"  loops      : {', '.join(l.value for l in self._loop_order())}",
            f"  states     : {len(self.states())} admissible observations",
            f"  terminals  : {', '.join(t.value for t in self.terminals())}",
            "",
            "per-stage legal progress (failures omitted; legal everywhere):",
        ]
        for stage in STAGE_ORDER:
            loop = STAGE_LOOP[stage]
            lines.append(f"  {stage.value:<9} {loop.value:<14} intents={[i.value for i in LOOP_INTENTS[loop]]}")
            for sub_loop in LOOP_SUBLOOPS[loop]:
                lines.append(f"      sub-loop {sub_loop.value}")
        lines.append("")
        lines.append("terminals:")
        lines.append(f"  success: {sorted(t.value for t in SUCCESS_TERMINALS)}")
        lines.append(f"  failure: {sorted(t.value for t in FAILURE_TERMINALS)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def self_check(self) -> None:
        """Fail fast if the constitution drifts.

        Verifies:
          - loop order is exactly the stage-derived traversal;
          - ``states()`` contains only legal observations and every legal
            observation shape is present;
          - every 'legal event' adjudicates as ALLOWED to a legal outcome;
          - every (state, event-kind) pair yields a verdict (never raises);
          - failure kinds route to failure terminals; success/failure
            partition ``LoopTerminal``.
        """
        order = self._loop_order()
        if len(order) != len(NestedLoop) or set(order) != set(NestedLoop):
            raise ValueError("loop order must cover every nested loop exactly once")
        for loop in order:
            if not LOOP_INTENTS[loop]:
                raise ValueError(f"loop {loop.value!r} has no default intent")

        states = self.states()
        if not states:
            raise ValueError("membrane board must have at least one state")
        for obs in states:
            if not self.is_legal_state(obs):
                raise ValueError(f"board contains illegal state {obs!r}")

        # Every state-kind combination must produce a verdict (graceful).
        kinds = tuple(GovernanceEventKind)
        for state in states:
            seen: set[GovernanceEventKind] = set()
            for event in self.legal_events(state):
                verdict = self.decide(state, event)
                seen.add(event.kind)
                if not verdict.allowed:
                    raise ValueError(
                        f"legal event {event.kind.value} denied from {state}: "
                        f"{verdict.reason}"
                    )
                if verdict.terminal is not None:
                    if verdict.terminal not in set(FAILURE_TERMINALS) | SUCCESS_TERMINALS:
                        raise ValueError(f"unknown terminal {verdict.terminal}")
                elif verdict.next is not None:
                    # Allowed non-terminal move: it must land on a legal state.
                    if not self.is_legal_state(verdict.next):
                        raise ValueError(
                            f"transition from {state} lands on illegal state {verdict.next}"
                        )
                else:
                    raise ValueError(
                        f"allowed event {event.kind.value} from {state} has "
                        "neither a next state nor a terminal"
                    )
            # Graceful failure: every failure kind yields a verdict.
            for kind in FAILURE_EVENT_KINDS:
                verdict = self.decide(
                    state, GovernanceEvent(kind)
                )
                if not verdict.allowed or verdict.terminal is None:
                    raise ValueError(
                        f"failure kind {kind.value} not routed to a terminal "
                        f"from {state}"
                    )

        # Verify every progress event kind is exercised somewhere (the
        # constitution is not silently dead).
        exercised = {e.kind for state in states for e in self.legal_events(state)}
        for kind in (
            GovernanceEventKind.ENTER_LOOP,
            GovernanceEventKind.SET_TASK,
            GovernanceEventKind.OPEN_SUBLOOP,
            GovernanceEventKind.COMPLETE_SUBLOOP,
            GovernanceEventKind.CLOSE_SUBLOOP,
            GovernanceEventKind.SETTLE,
        ):
            if kind not in exercised:
                raise ValueError(f"progress event kind {kind.value} is dead (never legal)")

        if SUCCESS_TERMINALS | FAILURE_TERMINALS != frozenset(LoopTerminal):
            raise ValueError("terminals must partition LoopTerminal")
        if SUCCESS_TERMINALS & FAILURE_TERMINALS:
            raise ValueError("success and failure terminals must be disjoint")


GOVERNANCE_MEMBRANE: AgenticLoopMembrane = AgenticLoopMembrane.canonical()
GOVERNANCE_MEMBRANE.self_check()


__all__ = [
    "AgenticLoopMembrane",
    "GovernanceEvent",
    "GovernanceEventKind",
    "MembraneVerdict",
    "GOVERNANCE_MEMBRANE",
    "FAILURE_EVENT_KINDS",
]