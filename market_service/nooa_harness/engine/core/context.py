"""Loop shared context — state carriers, clock/session helpers, governed moves."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

from market_service.nooa_harness.inference import execute_tool  # noqa: F401  (re-export: mock point)
from ..controller import CycleController
from ..fsm import GovernanceEvent, GovernanceEventKind, MembraneVerdict
from ..llm import last_tier as _last_tier
from ..loop_states import NestedLoop, SubLoop, TaskIntent
from .chain import (
    POSITION_ORDER,
    chain_completion,
    position_status,
)

log = logging.getLogger(__name__)

# Phase-name -> intent for the GOVERNED OBSERVATION only. Phase coverage
# credit stays exactly as-is (TOOL_PHASE, untouched); this binding merely
# moves the membrane's observation intent to follow the phase the agent is
# serving, so the governance surface sees intent, not P-ordinals.
_PHASE_INTENT: dict[str, TaskIntent] = {
    "P1": TaskIntent.INFER_ORDER_FLOW,
    "P2": TaskIntent.INFER_DEPTH,
    "P3": TaskIntent.CORRELATE_EVIDENCE,
    "P4": TaskIntent.EXPLAIN_FINDINGS,
    "P5": TaskIntent.DERIVE_HYPOTHESIS,
    "P6": TaskIntent.SYNTHESIZE_OUTPUT,
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stable_session_id(symbol: str, venue: str) -> str:
    """Deterministic per-(symbol, venue) UUID session id.

    The durable stores (agent_memory, inference_artifact) cast session_id to
    a UUID column, so the engine must hand them a real UUID. Deriving it from
    symbol+venue keeps memory coherent across cycles for one scope — a random
    id per cycle would fragment the memory plane.
    """
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"inference-engine://{symbol.lower()}/{venue}",
    ))


class GovernanceDenied(RuntimeError):
    """A required runtime transition was denied by the canonical FSM.

    ``controller`` carries the immutable successor containing the denial
    record, so the failure path cannot erase the attempted illegal move.
    """

    def __init__(self, message: str, controller: CycleController | None = None):
        super().__init__(message)
        self.controller = controller



def _govern(
    controller: CycleController,
    kind: GovernanceEventKind,
    target: Any = None,
    cadence_verdict: Mapping[str, Any] | None = None,
) -> tuple[CycleController, MembraneVerdict]:
    """Authorize exactly one move through the membrane.

    Denials are deliberately side-effect free.  This helper never closes a
    sub-loop, retries a different event, or mutates the observation on behalf
    of the caller.  Callers that require progress use ``_must_govern`` and
    receive an explicit failure instead of silently drifting.

    ``cadence_verdict`` carries the ADOPT_CADENCE payload (Phase G2); it is
    ignored for every other event kind.
    """
    event = GovernanceEvent(kind, target, cadence_verdict=cadence_verdict)
    # Keep one transition seam: the immutable controller asks the FSM for
    # legality, applies the successor only when allowed, and records denials
    # without changing the observation.
    return controller.transition(event)


def _must_govern(
    controller: CycleController,
    kind: GovernanceEventKind,
    target: Any = None,
    cadence_verdict: Mapping[str, Any] | None = None,
) -> tuple[CycleController, MembraneVerdict]:
    """Apply one transition or fail closed at the governance boundary."""
    successor, verdict = _govern(
        controller, kind, target, cadence_verdict=cadence_verdict)
    if not verdict.allowed:
        raise GovernanceDenied(
            f"{kind.value} denied"
            + (f" target={getattr(target, 'value', target)!r}" if target is not None else "")
            + f": {verdict.reason}",
            controller=successor,
        )
    return successor, verdict


def _authorize_work(
    controller: CycleController,
    *,
    nested_loop: NestedLoop,
    sub_loop: SubLoop | None = None,
) -> MembraneVerdict:
    """Ask the FSM whether work may execute at the current observation.

    Phase R4: the bootstrap flag is gone with the fetch semantic.
    """
    return controller.authorize_work(
        nested_loop=nested_loop, sub_loop=sub_loop,
    )


def _must_authorize_work(
    controller: CycleController,
    *,
    nested_loop: NestedLoop,
    sub_loop: SubLoop | None = None,
) -> MembraneVerdict:
    verdict = _authorize_work(
        controller, nested_loop=nested_loop, sub_loop=sub_loop,
    )
    if not verdict.allowed:
        raise GovernanceDenied(f"work denied: {verdict.reason}")
    return verdict




@dataclass(frozen=True)
class _GatheredEvidence:
    evidence: dict[str, Any] | None
    gate_status: str
    gate_reasons: list[str]
    deterministic_state: dict[str, Any]
    accumulated_tool_results: dict[str, Any]
    tool_results: dict[str, Any]


@dataclass(frozen=True)
class _ReasonedCycle:
    parsed_first: dict[str, Any]
    parsed_final: dict[str, Any]
    llm_calls: int
    tool_rounds_used: int
    repairs_sent: int
    finalize_now: bool
    final_validation: dict[str, Any]
    unexecuted: list[str]
    tool_results: dict[str, Any]
    comprehension: dict[str, Any] | None = None
    passes_per_loop: dict[str, int] | None = None
    # Per-turn extraction-tier history (llm.py ladder observability): one
    # entry per narration call, in order. The process-global LAST_TIER only
    # keeps the latest — this preserves the whole cycle for the artifact.
    llm_tiers: list[str] = field(default_factory=list)


@dataclass
class _CycleContext:
    """Per-invocation handoffs, never an alternative governance authority."""

    wake: WakeEnvelope
    task: str | None
    scenario: dict[str, Any] | None
    generated_at: str
    controller: CycleController
    capability_log: list[dict[str, Any]]
    gathered: _GatheredEvidence | None = None
    # Agent fetch receipt (Option-2 bootstrap): the agent's own gate-read
    # pull plus the deterministic floor fill, produced by run_agent_fetch
    # before the DATA gate runs. Comprehension seeds its carrier from this.
    fetched: Any | None = None
    reasoned: _ReasonedCycle | None = None
    # Canonical prompt variable (owner: core.wake.PromptVariable). Normalized
    # once in run_wake; loops read fields off it instead of re-slicing raw
    # task. Compat passthroughs task/scenario retained until Phase-3 cleanup.
    prompt: Any | None = None
    # Task-scoped segment (state redistribution): a deterministic id derived
    # from the normalized prompt, threaded to every CENTRAL-LEDGER entry AND to
    # the agent's own memory writes/reads so neither bleeds across tasks.
    task_id: str | None = None


# --------------------------------------------------------------------------
# Reason-stage runtime carrier + helpers
# --------------------------------------------------------------------------
# CycleRuntimeState is the mutable carrier threading one reason-stage run
# through comprehension/evidence/reasoning/validation. It owns its own
# counters, the live controller reference, and the FSM-side mutations the
# pass-loop helpers perform. The carrier is bound here (rather than in a
# per-loop module) because every loop reads and writes the same fields
# through the same helpers — colocating it with the FSM helpers keeps the
# runtime-plumbing layer in one place.


# ---------------------------------------------------------------------------
# Phase S-3 (docs/STATE_CHARTER_SPEC.md) — the deterministic_state keyspace
# namespace: every TOP-LEVEL root the engine plane writes or reads. The
# charter test (test_state_charter.TestDeterministicRoots) AST-scans every
# ``deterministic_state[...]`` write site + the gather init dict across
# nooa_harness/engine and pins THIS set — a new root is added by editing
# the writer AND this registry ("context and gather move together"), and an
# undeclared write fails the charter at commit time.
# ---------------------------------------------------------------------------
DETERMINISTIC_STATE_ROOTS: frozenset[str] = frozenset({
    # task-directive roots (gather init + inference_runner persist)
    "task", "scenario", "task_directive", "task_plan", "task_workflow",
    # wake + acquisition surface roots (Phase R7: data_gate /
    # bootstrap_basis / fetch_note retired with the fetch semantic)
    "wake", "capture_status", "microstructure_evidence",
    "forecast_result", "forward_scenario", "forward_scenario_note",
    "forward_gate", "gate",
    # pass/loop-carried verdict roots
    "assessment_note", "chain_gaps", "chain_halt", "comprehension",
    "congruence", "failure", "failure_report", "terminal",
    "loop_traversal", "validation_issue", "understanding_receipt",
    "statistical_chain", "governance_trace",
    # canonical loop-receipt vector (engine/receipts.py; the canonical half of
    # the receipt/note partition — see docs/NESTED_LOOP_SPEC §8)
    "loop_receipts",
    # central engine ledger (engine/ledger.py) — the ONE deterministic home
    # for engine-authored memory, sectioned by LedgerKind
    "ledger",
    # re-ordered cycle roots (comprehension/acquisition plane)
    "data_quality", "seeded_plan", "plan_gate", "persistence",
    "narration_tiers", "base_summary", "assembly_dropped",
    # agentic synthesis (core/synthesize.py): FSM-visible failure note for
    # the semantics-review surface; the cycle degrades, never dies on it
    "synthesis_note",
})


@dataclass
class CycleRuntimeState:
    """Mutable carrier threading one reason-stage run through its loops."""

    controller: Any
    capability_log: list[dict[str, Any]]
    deterministic_state: dict[str, Any]
    accumulated_tool_results: dict[str, Any]
    tool_results: dict[str, Any]
    parsed_current: dict[str, Any]
    parsed_1: dict[str, Any] | None = None
    # Every parsed narration turn of the cycle, in order. The merged view
    # (narration.merge_interpretation) is what final validation judges.
    turn_log: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: int = 0
    # Dispatch rounds remain separately visible from LLM/pass budgets.
    tool_rounds_used: int = 0
    repairs_sent: int = 0
    validation_retries_used: int = 0
    validation_entered: bool = False
    opened_subs: set[str] = field(default_factory=set)
    # Loop receipts (canonical, engine-authored): one close record per
    # loop/sub-loop, mirrored into the chartered
    # ``deterministic_state["loop_receipts"]`` root by
    # ``engine.receipts.emit_receipt``. Receipts are authoritative structural
    # facts and carry no model prose (the agent's memory node is separate).
    loop_receipts: list[Any] = field(default_factory=list)
    passes_per_loop: dict[str, int] = field(default_factory=lambda: {
        "comprehension": 0, "evidence": 0, "reasoning": 0,
        "validation": 0, "output": 0,
    })
    dispatched_in_pass: int = 0
    loop_tag: str = "evidence"
    unexecuted: list[str] = field(default_factory=list)
    task_reminder: str = ""
    scenario_reminder: str = ""
    phase_guidance: dict[str, str] = field(default_factory=dict)
    # Prompt-scope blocks (chartered carrier fields — never ad-hoc attrs).
    # Normalized once in run_wake / run_comprehension; loops read these.
    task_block: str = ""
    scenario_block: str = ""
    wake_block: str = ""
    workflow_block: str = ""
    ledger_block: str = ""
    prior_note: str = ""
    # Reasoning-entry partition context (first semantic of REASONING).
    # Recalled at REASONING entry from the task-segment partition writes
    # (understanding + plan + handoff) so the first passes reason over
    # what previous sub-loops/nested loops concluded, never from scratch.
    reasoning_ledger_block: str = ""
    reasoning_ledger_ids: list[str] = field(default_factory=list)
    # Phase 0 (staged ANALYSIS): frozen 2a exit payload + inter-stage ledger
    # carry. base_summary is frozen at assemble close from
    # accumulated_tool_results (chain.base_summary); ledger_stage_ids
    # cites the base/refined LEDGER observations threaded through the
    # task segment so 2b reasons over 2a conclusions, never from scratch.
    base_summary: dict[str, Any] = field(default_factory=dict)
    ledger_stage_ids: list[str] = field(default_factory=list)
    # Digest-fallback bound (failure substrate): the plan-bound forecast
    # digest is revealed to the agent at most ONCE per cycle, and only
    # after an agent forecast call failed in 2b. Guards double-see.
    digest_revealed: bool = False
    # Adjudication recall (validation entry): the verdict-basis ledger rows
    # (reasoning-close frame + truth) rendered for the repair/forced-final
    # turns, so the agent weighs against recalled conclusions, never fresh.
    adjudication_ledger_block: str = ""
    comprehension: dict[str, Any] | None = None
    seed_plan: dict[str, Any] | None = None
    task_workflow: dict[str, Any] | None = None
    # Task directive (plan factor): Phase-A parse + Phase-B disposal result.
    # Phase B refines it at the primitive intake stage; the engine disposes.
    task_directive: dict[str, Any] | None = None
    task_plan: dict[str, Any] | None = None
    finalize_now: bool = False
    final_validation: dict[str, Any] = field(default_factory=lambda: {
        "passed": False, "missing": ["loop_not_run"],
    })
    failure_kind: str | None = None
    failure_detail: str | None = None
    # Hard-track reasoning positions (assemble → interpret → hypothesize).
    # The loop cannot exit before all three are complete: each needs ≥1
    # in-position dispatch and its predicate met, advancing at most one
    # position per pass. A refused required link halts the track into
    # chain_halt for the FSM retry decision (see core/chain.py).
    reason_position: int = 0
    position_hits: dict[str, int] = field(default_factory=dict)
    chain_halt: dict[str, str] | None = None
    # The FORCED FINAL turn (kb.compose_forced_final_prompt) is spent at most
    # once per cycle: after the repair budget is exhausted and before the
    # validation-failed terminal.
    forced_final_sent: bool = False
    # Per-turn extraction-tier history, frozen into _ReasonedCycle at exit.
    llm_tiers: list[str] = field(default_factory=list)

    def to_reasoned(self) -> _ReasonedCycle:
        """Freeze the handoff the OUTPUT loop consumes."""
        return _ReasonedCycle(
            parsed_first=self.parsed_1 or {},
            parsed_final=self.parsed_current,
            llm_calls=self.llm_calls,
            tool_rounds_used=self.tool_rounds_used,
            repairs_sent=self.repairs_sent,
            finalize_now=self.finalize_now,
            final_validation=self.final_validation,
            unexecuted=list(self.unexecuted),
            tool_results=self.tool_results,
            comprehension=self.comprehension,
            passes_per_loop=dict(self.passes_per_loop),
            llm_tiers=list(self.llm_tiers),
        )


def record_tier(st: CycleRuntimeState) -> None:
    """Append the last narration extraction tier to the cycle history.

    Call immediately after every ``_call_llm`` so the artifact carries the
    full tier sequence (structured_model vs content_str vs reasoning vs
    fallbacks) instead of only the process-global latest. Never raises.
    """
    try:
        tier = _last_tier()
    except Exception:
        tier = None
    st.llm_tiers.append(tier or "unknown")


def _mark_declared(
    ctrl: CycleController, parsed: dict[str, Any],
) -> CycleController:
    """PURE helper — returns the successor controller; the caller rebinds."""
    return ctrl.mark_declared(parsed)


def _open_once(st: CycleRuntimeState, sub: SubLoop) -> None:
    """Open a sub-loop unless already opened (membrane advances siblings)."""
    if sub.value in st.opened_subs:
        return
    st.controller, _ = _must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, sub,
    )
    st.opened_subs.add(sub.value)


def freeze_chain(
    st: CycleRuntimeState, task: str | None, scenario: dict[str, Any] | None,
) -> None:
    """Snapshot the statistical chain into deterministic_state.

    Single shape at every freeze site (followup, reasoning exit/halt,
    validation): whole-track links plus per-position readings, the active
    position index, and the halt record. Readers never need to know which
    site froze it.
    """
    snap = chain_completion(st.controller, task=task, scenario=scenario)
    st.deterministic_state["statistical_chain"] = {
        "links": snap["links"],
        "missing": snap["missing"],
        "refused": snap["refused"],
        "complete": snap["complete"],
        "positions": {
            position: position_status(
                st.controller.outcomes, position,
                task=task, scenario=scenario)
            for position in POSITION_ORDER
        },
        "reason_position": st.reason_position,
        "chain_halt": st.chain_halt,
        # Slice D: base pointer — the halt/retry reader gets base + position
        # in one snapshot instead of joining two roots.
        "base_summary": dict(getattr(st, "base_summary", {}) or {}),
        "stage_ids": list(getattr(st, "ledger_stage_ids", []) or []),
    }


def failure_event(kind: str | None) -> GovernanceEvent:
    """The main FSM's ONE failure edge: the CYCLE_FAILED intake.

    The failure KIND rides on the event; the terminal is delegated to the
    primary failure node. This is the only place the runtime builds a
    failure event — every failure site routes through here.
    """
    return GovernanceEvent(GovernanceEventKind.CYCLE_FAILED, failure_kind=kind)


def failure_substrate_for(st: CycleRuntimeState) -> Any:
    """The per-cycle failure substrate (its retry ledger is cycle-scoped)."""
    sub = getattr(st, "_failure_substrate", None)
    if sub is None:
        from ..failure_substrate import FailureSubstrate

        sub = FailureSubstrate()
        st._failure_substrate = sub  # type: ignore[attr-defined]
    return sub


class _EngineTransport:
    """The capability handle a worker OWNS while it handles a task.

    Deliberately thin: it exposes ONLY the acts a bounded worker may take — run
    tool calls through the AUTHORIZED dispatch path, run one tool directly, or
    make one narration LLM call. It never exposes the FSM, the controller, or
    the engine class itself.
    """

    __slots__ = ("_engine", "_ctx", "_st")

    def __init__(self, engine: Any, ctx: Any = None, st: Any = None) -> None:
        self._engine = engine
        self._ctx = ctx
        self._st = st

    async def dispatch_tool(self, calls: list[dict[str, Any]], **kwargs: Any) -> set[str]:
        """Re-dispatch through the AUTHORIZED path (the mirror's seam).

        Re-enters the engine's own per-call gate chain by presenting the calls as
        the turn's ``tool_calls``, so budget / cadence / position / FSM work
        authorization / redundancy suppression / ``record_outcome`` all apply
        exactly once — the FSM stays the law. Returns the names actually
        dispatched (empty when a gate refused).
        """
        st = self._st
        if st is None or getattr(st, "_mirroring", False):
            return set()                       # no recursion into the mirror
        prev = getattr(st, "parsed_current", None)
        st._mirroring = True                    # type: ignore[attr-defined]
        try:
            st.parsed_current = {"tool_calls": list(calls)}
            await self._engine._dispatch_turn(self._ctx, st)
        finally:
            st._mirroring = False               # type: ignore[attr-defined]
            st.parsed_current = prev
        results = getattr(st, "_round_results", {}) or {}
        return {str(c.get("name")) for c in calls} & set(results)

    async def execute_tool(
        self, name: str, args: dict[str, Any], **kwargs: Any,
    ) -> tuple[Any, dict[str, Any]]:
        return await execute_tool(
            self._engine.store, name, args,
            postgres=self._engine.postgres,
            memory=self._engine.memory,
            settings=self._engine.settings,
            session_id=self._engine.session_id,
        )

    async def call_llm(self, prompt: str) -> str:
        return await self._engine._call_llm(prompt)


async def dispatch_failure(
    engine: Any, ctx: Any, st: CycleRuntimeState,
    *, kind: str | None = None, intent: dict[str, Any] | None = None,
) -> Any:
    """Classify the signalled failure and DISPATCH it through the failure node.

    The substrate ASSESSES and DISPATCHES; the worker owns the transport and
    returns a status the substrate consumes. This records the verdict + worker
    status on the state and returns the ``FailureOutcome`` — it does NOT write a
    terminal (that is the caller's decision, exactly once, via
    ``signal_and_route_failure`` or the caller's own terminal route).
    """
    from ..failure_fsm import FAILURE_FSM
    from ..failure_substrate import FailureTask

    kind = kind or st.failure_kind or "infra_failed"
    detail = st.failure_detail
    verdict = FAILURE_FSM.classify(kind)
    task = FailureTask.from_verdict(
        verdict, detail=detail, loop_tag=st.loop_tag,
        intent=intent if intent is not None else getattr(st, "tool_intent", None),
    )
    transport = _EngineTransport(engine, ctx, st)
    outcome = await failure_substrate_for(st).dispatch(
        verdict, task=task, transport=transport,
    )
    st.deterministic_state["failure_report"] = outcome.failure_report.to_dict()
    st.deterministic_state["failure"] = st.deterministic_state.get("failure", {}) | {
        "kind": kind,
        "detail": detail,
        "class": verdict.failure_class.value,
        "severity": verdict.severity.value,
        "remediation": outcome.remediation.value,
        "worker": outcome.worker_name,
        "terminal": verdict.terminal.value,
        "continuing": outcome.continue_cycle,
    }
    status = outcome.status
    if status is not None:
        st.deterministic_state["failure"]["worker_outcome"] = status.outcome
        if status.mirrored:
            st.deterministic_state["failure"]["mirrored"] = list(status.mirrored)
        if status.findings:
            st.deterministic_state["failure"]["findings"] = list(status.findings)
    return outcome


async def signal_and_route_failure(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple[Any, dict[str, Any]] | None:
    """Dispatch the signalled failure; on escalation write the terminal once.

    Returns the terminal ``(artifact, meta)`` when the failure escalates, or
    ``None`` when the substrate says the cycle continues (resolved / retrying).

    Usage in the runtime:

        result = await signal_and_route_failure(engine, ctx, st)
        if result is not None:
            return result          # escalated → terminal
        # else: the failure is handled — resume the loop
    """
    outcome = await dispatch_failure(engine, ctx, st)
    if outcome.continue_cycle:
        st.failure_kind = None
        st.failure_detail = None
        st.deterministic_state["failure"]["resolved"] = outcome.resolved
        return None
    return await run_runtime_failure(engine, ctx, st)


async def recover_narration_call(
    engine: Any, ctx: Any, st: CycleRuntimeState, prompt: str,
) -> str | None:
    """Bounded re-attempt of a failed LLM narration call (the RETRY remediation).

    While the failure substrate says CONTINUE, dispatch the failure and
    re-attempt the narration (bounded by the worker's retry budget). Returns the
    raw narration on success with the failure state cleared, or ``None``.
    On escalation the failure kind is LEFT SET so the caller's existing
    ``if st.failure_kind: return run_runtime_failure(...)`` writes the terminal
    exactly once — no double advance.
    """
    while st.failure_kind:
        outcome = await dispatch_failure(engine, ctx, st)
        if outcome.terminate_now:
            return None                     # caller routes to the terminal
        st.failure_kind = None
        st.failure_detail = None
        if not outcome.continue_cycle:
            return None
        try:
            return await engine._call_llm(prompt)
        except Exception as exc:
            st.failure_kind = "narration_failed"
            st.failure_detail = f"{type(exc).__name__}: {exc}"
            continue
    return None


async def recover_tool_failure(
    engine: Any, ctx: Any, st: CycleRuntimeState, *,
    kind: str = "tool_parse_failed",
    intent: dict[str, Any] | None = None,
) -> Any:
    """Dispatch a TOOL failure through the failure node (the mirror's caller).

    The substrate dispatches the TOOL worker (the deterministic output mirror),
    which re-dispatches the salvaged calls through the authorized path — so the
    mirrored results land in the cycle's outcome ledger exactly once. Returns
    ``None`` when the failure is resolved (the caller resumes the loop with the
    mirrored results present), or the escalated ``FailureOutcome`` when the
    failure terminates (the caller leaves ``st.failure_kind`` set and writes the
    terminal once).
    """
    intent = intent if intent is not None else getattr(st, "tool_intent", None)
    if not intent or not intent.get("tool_calls"):
        # Nothing salvageable — fall back to the canonical terminal route.
        return await run_runtime_failure(engine, ctx, st)
    outcome = await dispatch_failure(
        engine, ctx, st, kind=kind, intent=intent,
    )
    if outcome.continue_cycle:
        st.failure_kind = None
        st.failure_detail = None
        st.tool_intent = None                  # type: ignore[attr-defined]
        st.deterministic_state["failure"]["resolved"] = outcome.resolved
        return None
    return outcome


async def run_runtime_failure(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple[Any, dict[str, Any]]:
    """Route a transport/parse failure through the failure node's terminal.

    The terminal is decided by the Failure FSM and delegated through the
    main FSM's ``CYCLE_FAILED`` intake edge — the main membrane holds no
    failure semantics of its own.
    """
    kind = st.failure_kind or "infra_failed"
    st.controller = st.controller.advance(failure_event(kind))
    terminal = st.controller.terminal.value if st.controller.terminal else kind
    st.deterministic_state["terminal"] = terminal
    st.deterministic_state["failure"] = {
        "kind": kind, "detail": st.failure_detail,
    }
    st.deterministic_state["loop_traversal"] = st.controller.loop_coverage()
    st.deterministic_state["governance_trace"] = st.controller.transition_trace()
    return await engine._degraded_artifact(
        st.deterministic_state, st.capability_log,
        f"{kind}: {st.failure_detail or 'runtime failure'}",
    ), {
        "llm_calls": st.llm_calls,
        "terminal": terminal,
        "failure": st.deterministic_state["failure"],
        "final_validation": st.final_validation,
        "repairs": st.repairs_sent,
        "tool_round": bool(st.tool_results),
        "tool_rounds": st.tool_rounds_used,
        "phase_coverage": {
            phase: sorted(tools)
            for phase, tools in st.controller.phase_coverage.items()
        },
    }


async def run_validation_terminal(
    engine: Any, ctx: Any, st: CycleRuntimeState, missing: list[str],
) -> tuple[Any, dict[str, Any]]:
    """Shared terminal path: structured issue → FSM → degraded artifact."""
    validation_issue = {
        "loop": "validation",
        "missing": list(missing),
        "congruence": st.controller.congruence(),
        "loop_traversal": st.controller.loop_coverage(),
        "passes_per_loop": dict(st.passes_per_loop),
    }
    _covered = set(st.controller.loop_coverage())
    if (st.passes_per_loop.get("evidence", 0) > 0
            and "context" not in _covered):
        st.controller = st.controller.record_loop_visit(
            "context",
            st.passes_per_loop.get("comprehension", 0)
            + st.passes_per_loop.get("evidence", 0),
            ("intake", "interpretation", "framing", "sourcing",
             "acquisition", "verification"),
            completed=False,
        )
    if st.passes_per_loop.get("reasoning", 0) > 0 and "reasoning" not in _covered:
        st.controller = st.controller.record_loop_visit(
            "reasoning", st.passes_per_loop["reasoning"],
            ("hypothesis", "analysis", "synthesis"),
            completed=False,
        )
    st.controller = st.controller.record_loop_visit(
        "validation", st.passes_per_loop.get("validation", 0),
        ("gate", "recovery"), completed=False,
    )
    st.controller = st.controller.advance(
        failure_event("validation_failed")
    )
    terminal = (
        st.controller.terminal.value if st.controller.terminal
        else "validation_failed"
    )
    ctx.controller = st.controller
    st.deterministic_state["terminal"] = terminal
    st.deterministic_state["validation_issue"] = validation_issue
    st.deterministic_state["loop_traversal"] = st.controller.loop_coverage()
    st.deterministic_state["congruence"] = st.controller.congruence()
    return await engine._degraded_artifact(
        st.deterministic_state, st.capability_log,
        f"validation_failed: {'; '.join(missing)[:500]}",
    ), {
        "llm_calls": st.llm_calls,
        "terminal": terminal,
        "validation_issue": validation_issue,
        "final_validation": {"passed": False, "missing": list(missing)},
        "repairs": st.repairs_sent,
        "tool_round": bool(st.tool_results),
        "tool_rounds": st.tool_rounds_used,
        "phase_coverage": {
            phase: sorted(tools)
            for phase, tools in st.controller.phase_coverage.items()
        },
    }

