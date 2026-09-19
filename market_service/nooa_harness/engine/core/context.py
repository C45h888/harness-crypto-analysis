"""Loop shared context — state carriers, clock/session helpers, governed moves."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from market_service.nooa_harness.inference import execute_tool  # noqa: F401  (re-export: mock point)
from ..controller import CycleController
from ..fsm import GovernanceEvent, GovernanceEventKind, MembraneVerdict
from ..loop_states import NestedLoop, SubLoop, TaskIntent

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

