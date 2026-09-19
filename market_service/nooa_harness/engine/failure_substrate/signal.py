"""signal_failure — the single entry point for the failure substrate.

The FSM (engine/fsm.py) is the sole authority for failure routing: it
designates BOTH the terminal AND the assigned worker via
``MembraneVerdict.assigned_worker`` when it decides a failure event.

This function:
1. Maps the failure-kind string to a ``GovernanceEventKind``
2. Gets the assigned worker from the FSM's ``_FAILURE_KIND_WORKER`` dict
3. Dispatches the worker for remediation (retry / fallback / degrade)
4. Returns a ``FailureOutcome``

Authority flow:

    FSM.decide() → MembraneVerdict {terminal, assigned_worker}
        └── signal_failure() reads assigned_worker
            └── dispatches FAILURE_WORKER_REGISTRY[worker].invoke()
                └── returns FailureOutcome
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..fsm import (
    FAILURE_EVENT_WORKERS,
    GovernanceEventKind,
)
from .membrane import (
    FailureSeverity,
    FailureVerdict,
    RemediationKind,
)
from .report import FailureReport
from .workers import FAILURE_WORKER_REGISTRY, RemediationOutcome


@dataclass(frozen=True)
class FailureOutcome:
    """The result of signaling a failure to the substrate.

    ``resolved`` — True if the substrate remediated the failure; the loop
                   should continue from its last good state. When False, the
                   runtime must call ``run_runtime_failure()``.
    ``failure_report`` — structured ``FailureReport`` for the artifact.
    ``verdict`` — the classification verdict (remediation, worker, severity).
    ``terminate_now`` — True if the runtime should NOT retry and MUST
                        terminate immediately (catastrophic or retry exhausted).
    """

    resolved: bool
    failure_report: FailureReport
    verdict: FailureVerdict
    terminate_now: bool = False


# Mapping from runtime failure-kind strings to FSM GovernanceEventKind values.
# These are the reverse of _FAILURE_KIND_TERMINAL — given a string, find the
# event kind. The runtime sets st.failure_kind as a string; the substrate
# maps it to the FSM's vocabulary.
_KIND_TO_EVENT: dict[str, GovernanceEventKind] = {
    "gate_refused": GovernanceEventKind.GATE_REFUSED,
    "narration_failed": GovernanceEventKind.NARRATION_FAILED,
    "parse_failed": GovernanceEventKind.PARSE_FAILED,
    "validation_failed": GovernanceEventKind.VALIDATION_FAILED,
    "budget_exhausted": GovernanceEventKind.BUDGET_EXHAUSTED,
    "infra_failed": GovernanceEventKind.INFRA_FAILED,
    # Inference plane strings — these are runtime signals that the FSM
    # routes through INFRA_FAILED by default (the inference plane workers
    # handle the actual remediation).
    "chain_halt": GovernanceEventKind.INFRA_FAILED,
    "chain_link_refused": GovernanceEventKind.INFRA_FAILED,
    "position_blocked": GovernanceEventKind.INFRA_FAILED,
    "position_out_of_order": GovernanceEventKind.INFRA_FAILED,
    "forecast_failed": GovernanceEventKind.INFRA_FAILED,
    "scenario_unevaluable": GovernanceEventKind.INFRA_FAILED,
}


def _worker_for_kind(kind: str) -> str:
    """Resolve the assigned worker for a failure-kind string.

    The FSM's ``FAILURE_EVENT_WORKERS`` maps ``GovernanceEventKind`` to
    worker names.  When the kind doesn't match a known FSM event (e.g.
    ``tool_suppressed``, ``memory_failed``, inference-plane kinds), this
    falls back to the failure substrate's ``FAILURE_CLASSIFICATION`` map.

    This is a PURE function — the FSM is the authority for FSM-level
    failure events; the substrate classification handles runtime-level
    failure strings.
    """
    event_kind = _KIND_TO_EVENT.get(kind)
    if event_kind is not None:
        return FAILURE_EVENT_WORKERS.get(event_kind, "infra")

    # Fall back to the failure substrate's classification map for
    # runtime-level failure strings (tool_suppressed, memory_failed,
    # inference-plane kinds, etc.)
    from .membrane import FAILURE_CLASSIFICATION as CLASS_MAP
    from .membrane import FailureSeverity
    entry = CLASS_MAP.get(kind, (None, "infra", FailureSeverity.TRANSIENT))
    return entry[1]  # worker name


async def signal_failure(
    kind: str,
    detail: str | None = None,
    *,
    retry_count: int = 0,
    is_transient_override: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> FailureOutcome:
    """Signal a failure to the failure substrate.

    The worker is assigned by the FSM (via the ``FAILURE_EVENT_WORKERS``
    mapping registered in ``fsm.py``).  This function dispatches the worker
    for remediation and returns a ``FailureOutcome``.

    The runtime must check ``outcome.resolved`` before proceeding:
      - ``True`` → continue the loop (retry succeeded or fallback applied)
      - ``False`` → call ``run_runtime_failure()`` with the failure_report
        attached to ``st.deterministic_state["failure_report"]``
    """
    # Step 1: Get the assigned worker from the FSM
    worker_name = _worker_for_kind(kind)

    # Step 2: Build the verdict (remediation decision from the worker's spec)
    from .membrane import MAX_RETRIES
    max_retries = MAX_RETRIES.get(worker_name, 0)

    worker = FAILURE_WORKER_REGISTRY.get(worker_name)
    if worker is None:
        # Unknown worker — fall through to terminal
        return FailureOutcome(
            resolved=False,
            terminate_now=True,
            verdict=FailureVerdict(
                remediation=RemediationKind.TERMINATE,
                worker_name=worker_name,
                max_retries=max_retries,
                reason=f"unknown worker {worker_name} for failure {kind}",
            ),
            failure_report=FailureReport(
                failure_kind=kind,
                worker_name=worker_name,
                severity="unknown",
                resolved=False,
                terminal=kind,
                detail=detail,
                remediation_detail=f"unknown worker {worker_name}",
            ),
        )

    # Step 3: Determine remediation from the worker's spec and the
    # failure's severity as classified by the substrate's
    # FAILURE_CLASSIFICATION map.  Severity takes precedence over retry
    # budget: DEGRADED failures always degrade (never retry), PERMANENT
    # always terminate, TRANSIENT retries if budget remains.
    from .membrane import FAILURE_CLASSIFICATION as CLASS_MAP
    from .membrane import FailureSeverity

    entry = CLASS_MAP.get(kind, (None, worker_name, FailureSeverity.TRANSIENT))
    _, _, severity = entry

    # Respect severity override from the caller
    if is_transient_override is True:
        severity = FailureSeverity.TRANSIENT
    elif is_transient_override is False:
        severity = FailureSeverity.PERMANENT

    if severity is FailureSeverity.DEGRADED:
        # DEGRADED: mark as degraded, continue without retry
        return FailureOutcome(
            resolved=True,
            verdict=FailureVerdict(
                remediation=RemediationKind.DEGRADE,
                worker_name=worker_name,
                max_retries=worker.max_retries,
                severity=FailureSeverity.DEGRADED,
                reason=f"degraded failure {kind}: {detail or 'no detail'}",
            ),
            failure_report=FailureReport(
                failure_kind=kind,
                worker_name=worker_name,
                severity="degraded",
                resolved=True,
                degraded=True,
                max_retries=worker.max_retries,
                retries_used=worker._retry_count,
                detail=detail,
                remediation_detail="degraded failure; cycle continues with degraded state",
            ),
        )

    if severity is FailureSeverity.PERMANENT:
        return FailureOutcome(
            resolved=False,
            terminate_now=True,
            verdict=FailureVerdict(
                remediation=RemediationKind.TERMINATE,
                worker_name=worker_name,
                max_retries=worker.max_retries,
                severity=FailureSeverity.PERMANENT,
                reason=f"permanent failure {kind}: {detail or 'no detail'}",
            ),
            failure_report=FailureReport(
                failure_kind=kind,
                worker_name=worker_name,
                severity="permanent",
                resolved=False,
                max_retries=worker.max_retries,
                retries_used=worker._retry_count,
                terminal=kind,
                detail=detail,
                remediation_detail="permanent failure; terminating",
            ),
        )

    # TRANSIENT — retry if budget remains, else terminate.
    if retry_count < max_retries and worker.can_retry():
        # Consume one retry attempt for tracking (not actual remediation).
        # The worker's invoke() returns resolved=False — the RUNTIME must
        # re-attempt the operation; the verdict is RETRY regardless.
        outcome = await worker.invoke(detail, extra=extra)
        return FailureOutcome(
            resolved=False,  # caller must retry the actual operation
            terminate_now=False,
            verdict=FailureVerdict(
                remediation=RemediationKind.RETRY,
                worker_name=worker_name,
                max_retries=worker.max_retries,
                severity=FailureSeverity.TRANSIENT,
                reason=(
                    f"transient {kind} (retry {outcome.retry_count}/"
                    f"{worker.max_retries}): {detail or ''}"
                ),
            ),
            failure_report=FailureReport(
                failure_kind=kind,
                worker_name=worker_name,
                severity="transient",
                resolved=False,
                retries_used=outcome.retry_count,
                max_retries=worker.max_retries,
                detail=detail,
                remediation_detail=(
                    f"retry {outcome.retry_count}/{worker.max_retries} consumed; "
                    f"caller must retry the actual operation"
                ),
            ),
        )

    # Retry budget exhausted — terminate
    return FailureOutcome(
        resolved=False,
        terminate_now=True,
        verdict=FailureVerdict(
            remediation=RemediationKind.TERMINATE,
            worker_name=worker_name,
            max_retries=max_retries,
            severity=FailureSeverity.TRANSIENT,
            reason=f"transient {kind} exhausted ({retry_count}/{max_retries}): "
                   f"{detail or 'no detail'}",
        ),
        failure_report=FailureReport(
            failure_kind=kind,
            worker_name=worker_name,
            severity="transient",
            resolved=False,
            retries_used=retry_count,
            max_retries=worker.max_retries,
            terminal=kind,
            detail=detail,
            remediation_detail=(
                f"retries exhausted ({retry_count}/{worker.max_retries}); "
                f"terminating"
            ),
        ),
    )


__all__ = [
    "FailureOutcome",
    "signal_failure",
]