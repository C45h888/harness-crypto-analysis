"""FailureSubstrate — the deterministic MECHANICAL plane of failure handling.

This module is ``failure_substrate/substrate.py``. It owns the mechanical and
technical logic of failure remediation — everything the Failure FSM
(``engine/failure_fsm.py``) deliberately does NOT own:

  - WHICH bounded worker handles a failure (``WORKER_FOR_KIND``);
  - the retry BUDGET per worker (``MAX_RETRIES``);
  - the per-cycle retry LEDGER (held on the substrate instance, never a
    process-global);
  - the act of coordinating a worker (select -> drive -> reconcile).

It is driven BY the Failure FSM, never the reverse:

    FailureFSM.classify(kind)  ->  FailureVerdict        [governance]
        FailureSubstrate.mediate(verdict)                [mechanics — here]
            worker.invoke()                              [execution]

The substrate RECEIVES a decided verdict; it never re-classifies. Its only
authority beyond the verdict is ESCALATION: a RETRY/FALLBACK policy whose
retry budget is exhausted becomes TERMINATE. The substrate may never weaken
a governance TERMINATE.

Boundary rule: this module imports the Failure FSM's VALUE TYPES only
(``failure_fsm``) and the worker registry; it never imports the main FSM and
the main FSM never imports it. Retry state lives on the INSTANCE (one per
cycle), so two concurrent cycles cannot share a ledger.
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
    "parse_failed": "narration",
    "tool_denied": "tool",
    "tool_error": "tool",
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
    """The result of mediating one failure through the substrate.

    ``resolved``        — True if the cycle continues (degraded / fallback).
    ``terminate_now``   — True if the runtime must terminate immediately.
    ``verdict``         — the governance verdict (from the Failure FSM).
    ``worker_name``     — the mechanical worker that coordinated it.
    ``remediation``     — the POST-budget reconciliation of the policy.
    ``attempts``        — retries consumed on the worker so far.
    ``failure_report``  — structured report for the artifact.
    """

    resolved: bool
    terminate_now: bool
    verdict: FailureVerdict
    worker_name: str
    remediation: RemediationKind
    failure_report: FailureReport
    attempts: int = 0


# ---------------------------------------------------------------------------
# The substrate
# ---------------------------------------------------------------------------


class FailureSubstrate:
    """The deterministic mechanical plane — coordinate bounded workers.

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

    # -- coordination --------------------------------------------------

    async def mediate(
        self,
        verdict: FailureVerdict,
        *,
        detail: str | None = None,
        retry_count: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> FailureOutcome:
        """Coordinate remediation for a decided verdict (never raises).

        The governance POLICY (``verdict.remediation``) is honoured, except
        that a RETRY/FALLBACK whose budget is exhausted escalates to
        TERMINATE. A governance TERMINATE is never weakened. ``retry_count``
        seeds the worker's ledger when a caller has already spent attempts.
        """
        worker_name = worker_for_verdict(verdict)
        worker = self._workers.get(worker_name)
        if worker is not None and retry_count:
            worker.seed_retry(retry_count)

        if worker is None:
            # Unknown worker — no mechanical path; terminate honestly.
            return FailureOutcome(
                resolved=False,
                terminate_now=True,
                verdict=verdict,
                worker_name=worker_name,
                remediation=RemediationKind.TERMINATE,
                failure_report=FailureReport(
                    failure_kind=verdict.kind,
                    worker_name=worker_name,
                    severity=verdict.severity.value,
                    resolved=False,
                    terminal=verdict.terminal.value,
                    detail=detail,
                    remediation_detail=f"unknown worker {worker_name}",
                ),
            )

        remediation = verdict.remediation

        # Escalation: a policy that needs a retry it cannot afford terminates.
        if remediation in _ESCALATABLE and not worker.can_retry():
            remediation = RemediationKind.TERMINATE

        if remediation is RemediationKind.TERMINATE:
            return FailureOutcome(
                resolved=False,
                terminate_now=True,
                verdict=verdict,
                worker_name=worker_name,
                remediation=remediation,
                attempts=worker.retry_count,
                failure_report=FailureReport(
                    failure_kind=verdict.kind,
                    worker_name=worker_name,
                    severity=verdict.severity.value,
                    resolved=False,
                    retries_used=worker.retry_count,
                    max_retries=worker.max_retries,
                    terminal=verdict.terminal.value,
                    detail=detail,
                    remediation_detail=(
                        f"{verdict.severity.value} failure; terminating at "
                        f"{verdict.terminal.value}"
                    ),
                ),
            )

        if remediation is RemediationKind.DEGRADE:
            return FailureOutcome(
                resolved=True,
                terminate_now=False,
                verdict=verdict,
                worker_name=worker_name,
                remediation=remediation,
                attempts=worker.retry_count,
                failure_report=FailureReport(
                    failure_kind=verdict.kind,
                    worker_name=worker_name,
                    severity=verdict.severity.value,
                    resolved=True,
                    degraded=True,
                    retries_used=worker.retry_count,
                    max_retries=worker.max_retries,
                    detail=detail,
                    remediation_detail=(
                        "degraded failure; cycle continues with degraded state"
                    ),
                ),
            )

        # RETRY / FALLBACK — drive the worker for one bounded attempt.
        outcome: RemediationOutcome = await worker.invoke(detail, extra=extra)
        return FailureOutcome(
            resolved=bool(outcome.resolved and not outcome.degraded),
            terminate_now=False,
            verdict=verdict,
            worker_name=worker_name,
            remediation=remediation,
            attempts=outcome.retry_count,
            failure_report=FailureReport(
                failure_kind=verdict.kind,
                worker_name=worker_name,
                severity=verdict.severity.value,
                resolved=bool(outcome.resolved),
                retries_used=outcome.retry_count,
                max_retries=worker.max_retries,
                fallback_applied=outcome.fallback_applied,
                fallback_name=worker.fallback,
                degraded=outcome.degraded,
                detail=detail,
                remediation_detail=(
                    f"{remediation.value} {outcome.retry_count}/"
                    f"{worker.max_retries} consumed"
                    + (" (fallback applied)" if outcome.fallback_applied else "")
                ),
            ),
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
