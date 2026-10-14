"""Failure contracts — the value types and the capability surface.

This is the leaf module that carries the SEMANTIC BOUNDARY between the three
planes. It defines:

  · FailureTask   — the retryable unit the substrate dispatches to a worker.
  · WorkerStatus  — what a bounded worker reports back (the substrate is the
                    SOLE consumer).
  · FailureTransport — the capability surface a worker OWNS while it handles a
                    task. The worker uses it to ACT (run a tool, call the LLM);
                    the substrate injects it; the FSM never sees it.

Boundary rule: this module imports the governance VALUE TYPES only. It knows
nothing about workers, the substrate, the main FSM, or the engine.

The ownership inversion this encodes:

    OLD (advice):   substrate.mediate(verdict) -> advice
                    runtime: "…so I must re-attempt"     (never did)

    NEW (dispatch): substrate.dispatch(verdict, task, transport)
                      -> worker.execute(task, transport)   [worker OWNS transport]
                           -> WorkerStatus                   [substrate CONSUMES]
                             -> FailureOutcome               [runtime READS]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..failure_fsm import FailureClass, FailureSeverity, FailureVerdict, RemediationKind


@dataclass(frozen=True)
class FailureTask:
    """The retryable unit the substrate dispatches to a bounded worker.

    ``intent`` carries what the runtime was attempting when it failed (e.g. the
    tool calls the LLM emitted) so a worker can deterministically re-attempt it.
    The substrate never invents the intent; the runtime hands it.
    """

    kind: str
    failure_class: FailureClass
    severity: FailureSeverity
    remediation: RemediationKind
    detail: str | None = None
    loop_tag: str | None = None
    position: int | None = None
    intent: dict[str, Any] | None = None
    budget: int = 0
    task_id: str = ""

    @classmethod
    def from_verdict(
        cls,
        verdict: FailureVerdict,
        *,
        detail: str | None = None,
        loop_tag: str | None = None,
        position: int | None = None,
        intent: dict[str, Any] | None = None,
        budget: int = 0,
        task_id: str = "",
    ) -> "FailureTask":
        """Compose a task from a governance verdict + the runtime's attempt context."""
        return cls(
            kind=verdict.kind,
            failure_class=verdict.failure_class,
            severity=verdict.severity,
            remediation=verdict.remediation,
            detail=detail,
            loop_tag=loop_tag,
            position=position,
            intent=intent,
            budget=budget,
            task_id=task_id or f"{verdict.kind}:{(detail or '')[:60]}",
        )


@dataclass(frozen=True)
class WorkerStatus:
    """What a bounded worker reports back after handling a task.

    The substrate is the SOLE consumer of this object. ``outcome`` is one of
    ``resolved`` / ``degraded`` / ``retrying`` / ``failed``. ``mirrored`` names
    any tools a worker re-ran deterministically on the runtime's behalf.
    """

    outcome: str
    resolved: bool = False
    degraded: bool = False
    retry_count: int = 0
    fallback_applied: bool = False
    findings: tuple[str, ...] = ()
    mirrored: tuple[str, ...] = ()
    report: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""

    @property
    def terminal_outcome(self) -> bool:
        """Whether this status means the failure cannot continue the cycle."""
        return self.outcome in ("failed",)


@runtime_checkable
class FailureTransport(Protocol):
    """The capability surface a worker OWNS while handling a task.

    A worker uses this to ACT — the substrate injects the concrete transport;
    the worker never constructs one, and the FSM never sees one.
    """

    async def dispatch_tool(
        self, calls: list[dict[str, Any]], **kwargs: Any,
    ) -> Any:
        """Re-dispatch tool calls through the AUTHORIZED path.

        This is the mirror's seam: it runs the calls through the SAME gate chain
        a normal dispatch uses (budget, cadence, position, FSM work
        authorization, redundancy) so the cycle's outcome ledger records them.
        A worker must use THIS, never a raw executor, so the FSM stays the law.
        """
        ...

    async def execute_tool(
        self, name: str, args: dict[str, Any], **kwargs: Any,
    ) -> tuple[Any, dict[str, Any]]:
        """Run one tool directly — for remediation that is NOT a re-dispatch."""
        ...

    async def call_llm(self, prompt: str) -> str:
        """One narration LLM call over the cycle-scoped system prompt."""
        ...


__all__ = [
    "FailureTask",
    "WorkerStatus",
    "FailureTransport",
]
