"""Loop shared context — state carriers, clock/session helpers, governed moves."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from market_service.nooa_harness.inference import execute_tool  # noqa: F401  (re-export: mock point)
from ..controller import CycleController
from ..fsm import GovernanceEvent, GovernanceEventKind, MembraneVerdict
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
) -> tuple[CycleController, MembraneVerdict]:
    """Authorize exactly one move through the membrane.

    Denials are deliberately side-effect free.  This helper never closes a
    sub-loop, retries a different event, or mutates the observation on behalf
    of the caller.  Callers that require progress use ``_must_govern`` and
    receive an explicit failure instead of silently drifting.
    """
    event = GovernanceEvent(kind, target)
    # Keep one transition seam: the immutable controller asks the FSM for
    # legality, applies the successor only when allowed, and records denials
    # without changing the observation.
    return controller.transition(event)


def _must_govern(
    controller: CycleController,
    kind: GovernanceEventKind,
    target: Any = None,
) -> tuple[CycleController, MembraneVerdict]:
    """Apply one transition or fail closed at the governance boundary."""
    successor, verdict = _govern(controller, kind, target)
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
    bootstrap: bool = False,
) -> MembraneVerdict:
    """Ask the FSM whether work may execute at the current observation."""
    return controller.authorize_work(
        nested_loop=nested_loop, sub_loop=sub_loop, bootstrap=bootstrap,
    )


def _must_authorize_work(
    controller: CycleController,
    *,
    nested_loop: NestedLoop,
    sub_loop: SubLoop | None = None,
    bootstrap: bool = False,
) -> MembraneVerdict:
    verdict = _authorize_work(
        controller, nested_loop=nested_loop, sub_loop=sub_loop,
        bootstrap=bootstrap,
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
    reasoned: _ReasonedCycle | None = None


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
    llm_calls: int = 0
    # Dispatch rounds remain separately visible from LLM/pass budgets.
    tool_rounds_used: int = 0
    repairs_sent: int = 0
    validation_retries_used: int = 0
    validation_entered: bool = False
    opened_subs: set[str] = field(default_factory=set)
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
        )


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
    }


# Failure-kind -> FSM event-kind mapping for terminal routing. Lifted out so
# the two terminal emitters stay aligned if a new failure category is added.
_FAILURE_TO_EVENT: dict[str, GovernanceEventKind] = {
    "narration_failed": GovernanceEventKind.NARRATION_FAILED,
    "parse_failed": GovernanceEventKind.PARSE_FAILED,
    "budget_exhausted": GovernanceEventKind.BUDGET_EXHAUSTED,
}


async def signal_and_route_failure(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple[Any, dict[str, Any]] | None:
    """Signal failure to the substrate; return continuation or None for terminal.

    This is the integration seam between the runtime and the failure substrate.
    The substrate classifies the failure, applies remediation (retry/fallback/
    degrade), and returns a ``FailureOutcome``. When the outcome says the
    failure was resolved, this function resets the failure state and returns
    None so the loop continues. When the outcome says terminate, this calls
    ``run_runtime_failure()`` and returns its result.

    Usage in the runtime:

        outcome = await signal_and_route_failure(engine, ctx, st)
        if outcome is not None:
            return outcome  # terminated via substrate
        # else: resolved — continue the loop
    """
    from ..failure_substrate import signal_failure

    kind = st.failure_kind or "infra_failed"
    detail = st.failure_detail

    outcome = await signal_failure(kind, detail)

    st.deterministic_state["failure_report"] = outcome.failure_report.to_dict()
    st.deterministic_state["failure"] = st.deterministic_state.get("failure", {}) | {
        "kind": kind, "detail": detail,
        "verdict": outcome.verdict.reason,
    }

    if not outcome.terminate_now:
        st.failure_kind = None
        st.failure_detail = None
        st.deterministic_state["failure"]["resolved"] = outcome.resolved
        st.deterministic_state["failure"]["verdict"] = outcome.verdict.reason
        return None

    return await run_runtime_failure(engine, ctx, st)


async def run_runtime_failure(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple[Any, dict[str, Any]]:
    """Route a transport/parse failure through the canonical FSM terminal."""
    kind = st.failure_kind or "infra_failed"
    event_kind = _FAILURE_TO_EVENT.get(kind, GovernanceEventKind.INFRA_FAILED)
    st.controller = st.controller.advance(GovernanceEvent(event_kind))
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
    if st.passes_per_loop.get("evidence", 0) > 0 and "evidence" not in _covered:
        st.controller = st.controller.record_loop_visit(
            "evidence", st.passes_per_loop["evidence"],
            ("sourcing", "acquisition", "verification"),
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
        GovernanceEvent(GovernanceEventKind.VALIDATION_FAILED)
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

