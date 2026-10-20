"""FailureSubstrate — the deterministic MECHANICAL plane of failure handling.

This module is ``failure_substrate/substrate.py``. It owns the mechanical and
technical logic of failure remediation — everything the Failure FSM
(``engine/failure_fsm.py``) deliberately does NOT own:

  - WHICH bounded worker handles a failure (``WORKER_FOR_KIND``);
  - the retry BUDGET per worker (``MAX_RETRIES``);
  - the per-cycle retry LEDGER (held on the substrate instance, never a
    process-global);
  - the act of ASSESSING a failure and DISPATCHING a worker to handle it.

The ownership (the inversion is fixed here — the substrate DISPATCHES):

    FailureFSM.classify(kind)      ->  FailureVerdict        [governance]
        FailureSubstrate.dispatch(verdict, task, transport)  [mechanics — here]
            worker.execute(task, transport)                  [execution — worker
                                                              OWNS transport]
              -> WorkerStatus                                 [consumed here]

The substrate ASSESSES the failure (which worker, whether the policy's budget is
spent) and then DISPATCHES the worker that handles the issue. The substrate
RECEIVES a decided verdict; it never re-classifies. Its only authority beyond the
verdict is ESCALATION: a RETRY/FALLBACK policy whose retry budget is exhausted
becomes TERMINATE. The substrate may never weaken a governance TERMINATE.

Boundary rule: this module imports the Failure FSM's VALUE TYPES only
(``failure_fsm``), the contracts, and the worker registry; it never imports the
main FSM and the main FSM never imports it. Retry state lives on the INSTANCE
(one per cycle), so two concurrent cycles cannot share a ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..failure_fsm import (
    FailureClass,
    FailureSeverity,
    FailureVerdict,
    RemediationKind,
)
from .contracts import FailureTask, FailureTransport, WorkerStatus
from .report import FailureReport
from .workers import (
    FAILURE_WORKER_REGISTRY,
    FailureWorker,
    RemediationOutcome,
    default_workers,
)


# ---------------------------------------------------------------------------
# Mechanical maps — kind -> worker, worker -> retry budget
# ---------------------------------------------------------------------------

#: Which bounded worker handles each failure kind. Kinds absent here resolve
#: by CLASS (the class value IS the worker name).
WORKER_FOR_KIND: dict[str, str] = {
    "narration_failed": "narration",
    # Signal B: stall verdicts route to the narration worker explicitly so
    # a class rename or fallback change cannot silently reroute them. The
    # worker branches on kind: no_bytes re-attempts identically, thinking
    # re-attempts against a caller-shrunk prompt (see recover paths).
    "narration_stalled_no_bytes": "narration",
    "narration_stalled_thinking": "narration",
    # F6: explicit (was class-fallback only) so a class rename or fallback
    # change cannot silently reroute caller-fault narration failures.
    "narration_refused": "narration",
    "parse_failed": "narration",
    "tool_denied": "tool",
    "tool_error": "tool",
    "tool_parse_failed": "tool",
    "tool_suppressed": "tool",
    "validation_failed": "validation",
    "budget_exhausted": "dispatch",
    "dispatch_failed": "dispatch",
    "memory_failed": "memory",
    "chain_halt": "inference",
    "chain_link_refused": "inference",
    "position_blocked": "inference",
    "position_out_of_order": "inference",
    "forecast_failed": "inference",
    "scenario_unevaluable": "inference",
    "gate_refused": "gate",
    "gate_timeout": "gate",
    "infra_failed": "infra",
}

#: Retry budgets, per worker. The bounded workers declare the same numbers
#: (``workers.FAILURE_WORKER_REGISTRY``); the charter test pins them equal.
MAX_RETRIES: dict[str, int] = {
    "narration": 1,       # one retry for LLM transport
    "tool": 2,            # two retries for tool dispatch
    "validation": 0,      # no retry; repair is the bounded retry
    "dispatch": 1,        # one retry for substrate dispatch
    "memory": 0,          # no retry; memory failures are permanent
    "gate": 0,            # no retry; gate failures are final
    "inference": 1,       # one retry for the inference plane
    "infra": 1,           # one retry for infrastructure
}

#: Policies the substrate may ESCALATE to terminate when budget is spent.
_ESCALATABLE = frozenset({RemediationKind.RETRY, RemediationKind.FALLBACK})


def worker_for_verdict(verdict: FailureVerdict) -> str:
    """Resolve the worker for a verdict (kind first, then class)."""
    name = WORKER_FOR_KIND.get(verdict.kind)
    if name is not None:
        return name
    return verdict.failure_class.value


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureOutcome:
    """The result of dispatching one failure through the substrate.

    ``resolved``        — True if the failure is cleared (degraded / fallback).
    ``terminate_now``   — True if the runtime must terminate immediately.
    ``continue_cycle``  — True if the runtime should resume (re-attempt / go on).
    ``verdict``         — the governance verdict (from the Failure FSM).
    ``worker_name``     — the mechanical worker that handled it.
    ``remediation``     — the POST-budget reconciliation of the policy.
    ``status``          — the WorkerStatus the substrate consumed (None on a
                          governance TERMINATE, where no worker was dispatched).
    ``attempts``        — retries consumed on the worker so far.
    ``failure_report``  — structured report for the artifact.
    """

    resolved: bool
    terminate_now: bool
    verdict: FailureVerdict
    worker_name: str
    remediation: RemediationKind
    failure_report: FailureReport
    status: WorkerStatus | None = None
    attempts: int = 0

    @property
    def continue_cycle(self) -> bool:
        """Whether the runtime should resume the cycle rather than terminate."""
        return not self.terminate_now


def _terminate_outcome(
    verdict: FailureVerdict,
    worker_name: str,
    detail: str | None,
    remediation_detail: str,
    *,
    retries_used: int = 0,
    max_retries: int = 0,
) -> FailureOutcome:
    """A terminating outcome (governance TERMINATE, or an escalated retry)."""
    return FailureOutcome(
        resolved=False,
        terminate_now=True,
        verdict=verdict,
        worker_name=worker_name,
        remediation=RemediationKind.TERMINATE,
        attempts=retries_used,
        failure_report=FailureReport(
            failure_kind=verdict.kind,
            worker_name=worker_name,
            severity=verdict.severity.value,
            resolved=False,
            retries_used=retries_used,
            max_retries=max_retries,
            terminal=verdict.terminal.value,
            detail=detail,
            remediation_detail=remediation_detail,
        ),
    )


# ---------------------------------------------------------------------------
# The substrate
# ---------------------------------------------------------------------------


class FailureSubstrate:
    """The deterministic mechanical plane — assess a failure, dispatch a worker.

    Construct one per cycle (or per test). Workers are injected (defaults are
    fresh instances), so the retry ledger is instance-scoped and never
    shared between cycles.
    """

    __slots__ = ("_workers",)

    def __init__(self, workers: dict[str, FailureWorker] | None = None) -> None:
        self._workers = workers if workers is not None else default_workers()

    # -- reads ---------------------------------------------------------

    @property
    def workers(self) -> dict[str, FailureWorker]:
        return self._workers

    def worker(self, name: str) -> FailureWorker | None:
        return self._workers.get(name)

    # -- the boundary: assess + dispatch -------------------------------

    async def dispatch(
        self,
        verdict: FailureVerdict,
        *,
        task: FailureTask | None = None,
        transport: FailureTransport | None = None,
        detail: str | None = None,
        retry_count: int = 0,
    ) -> FailureOutcome:
        """Assess a decided verdict and DISPATCH a bounded worker (never raises).

        The substrate ASSESSES (which worker; whether the policy's retry budget
        is spent) then DISPATCHES — the worker OWNS the transport while it
        handles the task and returns a ``WorkerStatus`` the substrate consumes.

        The governance POLICY (``verdict.remediation``) is honoured, except that
        a RETRY/FALLBACK whose budget is exhausted escalates to TERMINATE. A
        governance TERMINATE is never weakened and never dispatches a worker.
        ``retry_count`` seeds the worker's ledger when a caller has already spent
        attempts.
        """
        worker_name = worker_for_verdict(verdict)
        worker = self._workers.get(worker_name)
        if worker is not None and retry_count:
            worker.seed_retry(retry_count)

        if task is None:
            task = FailureTask.from_verdict(verdict, detail=detail)

        if worker is None:
            # Unknown worker — no mechanical path; terminate honestly.
            return _terminate_outcome(
                verdict, worker_name, detail,
                f"unknown worker {worker_name}",
            )

        remediation = verdict.remediation

        # Escalation: a policy that needs a retry it cannot afford terminates.
        if remediation in _ESCALATABLE and not worker.can_retry():
            return _terminate_outcome(
                verdict, worker_name, detail,
                f"{verdict.severity.value} failure; retry budget spent "
                f"({worker.retry_count}/{worker.max_retries}) — "
                f"terminating at {verdict.terminal.value}",
                retries_used=worker.retry_count,
                max_retries=worker.max_retries,
            )

        if remediation is RemediationKind.TERMINATE:
            return _terminate_outcome(
                verdict, worker_name, detail,
                f"{verdict.severity.value} failure; terminating at "
                f"{verdict.terminal.value}",
                retries_used=worker.retry_count,
                max_retries=worker.max_retries,
            )

        # DISPATCH — the worker owns the transport while handling the task.
        status: WorkerStatus = await worker.execute(task, transport)

        return FailureOutcome(
            resolved=bool(status.resolved),
            terminate_now=False,
            verdict=verdict,
            worker_name=worker_name,
            remediation=remediation,
            attempts=status.retry_count,
            status=status,
            failure_report=FailureReport(
                failure_kind=verdict.kind,
                worker_name=worker_name,
                severity=verdict.severity.value,
                resolved=bool(status.resolved),
                degraded=status.degraded,
                retries_used=status.retry_count,
                max_retries=worker.max_retries,
                fallback_applied=status.fallback_applied,
                fallback_name=worker.fallback,
                detail=detail,
                remediation_detail=(
                    f"{remediation.value} — {status.outcome} "
                    f"({status.retry_count}/{worker.max_retries} retries)"
                    + (
                        f"; findings: {'; '.join(status.findings)}"
                        if status.findings else ""
                    )
                ),
            ),
        )

    async def mediate(
        self,
        verdict: FailureVerdict,
        *,
        detail: str | None = None,
        retry_count: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> FailureOutcome:
        """Compatibility alias for ``dispatch()`` (legacy call shape).

        Prefer ``dispatch(verdict, task=..., transport=...)``; this exists so
        older callers keep working — it dispatches with no task/transport.
        """
        return await self.dispatch(
            verdict, detail=detail, retry_count=retry_count,
        )


#: Legacy module-level default substrate. Prefer constructing one per cycle;
#: this exists only so the compatibility ``signal_failure`` entry point works.
FAILURE_SUBSTRATE: FailureSubstrate = FailureSubstrate()


__all__ = [
    "FailureSubstrate",
    "FailureOutcome",
    "WORKER_FOR_KIND",
    "MAX_RETRIES",
    "worker_for_verdict",
    "FAILURE_SUBSTRATE",
    "FAILURE_WORKER_REGISTRY",
]
