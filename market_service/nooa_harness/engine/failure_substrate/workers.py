"""Bounded failure workers — each worker handles one failure substrate.

Every worker is a bounded, deterministic remediation unit:
  - ``invoke()`` — run the remediation (retry, fallback, degrade)
  - ``retry_count`` / ``max_retries`` — bounded retry budget
  - ``fallback`` — what to do when retries are exhausted
  - ``report()`` — produce the structured failure payload

Workers are registered in ``FAILURE_WORKER_REGISTRY`` and dispatched by
``signal_failure()``. They never construct their own stores — dependencies
are caller-injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..failure_fsm import FailureClass, FailureSeverity, RemediationKind
from .contracts import FailureTask, FailureTransport, WorkerStatus

#: Defaults for the legacy ``invoke()`` shim (a plain RETRY-policy task).
_DEFAULT_CLASS = FailureClass.INFRA
_DEFAULT_SEVERITY = FailureSeverity.TRANSIENT


class RemediationOutcome:
    """What happened when the worker ran remediation.

    ``resolved`` — True if the failure is no longer blocking; the loop may
                   continue from its last good state.
    ``retry_count`` — how many retries were consumed.
    ``fallback_applied`` — whether a fallback was used instead of success.
    ``degraded`` — True if the cycle should be marked degraded.
    ``report`` — structured failure payload for the artifact.
    """
    __slots__ = ("resolved", "retry_count", "fallback_applied",
                 "degraded", "report")

    def __init__(
        self,
        resolved: bool = False,
        retry_count: int = 0,
        fallback_applied: bool = False,
        degraded: bool = False,
        report: dict[str, Any] | None = None,
    ):
        self.resolved = resolved
        self.retry_count = retry_count
        self.fallback_applied = fallback_applied
        self.degraded = degraded
        self.report = report or {}


@dataclass
class FailureWorker:
    """A bounded failure remediation unit.

    ``name`` — the worker's registry name (matches FAILURE_CLASSIFICATION).
    ``max_retries`` — how many times to retry before giving up.
    ``fallback`` — the fallback strategy name (varies per worker).
    ``cooldown_ms`` — minimum ms between retry attempts.
    ``_retry_count`` — current retry count (mutated during remediation).
    """

    name: str
    max_retries: int = 0
    fallback: str = "terminate"
    cooldown_ms: int = 0
    _retry_count: int = 0

    def can_retry(self) -> bool:
        """Whether the retry budget is not yet exhausted."""
        return self._retry_count < self.max_retries

    @property
    def retry_count(self) -> int:
        """Retries consumed so far (read-only view of the ledger)."""
        return self._retry_count

    def consume_retry(self) -> int:
        """Increment retry count and return the new count."""
        self._retry_count += 1
        return self._retry_count

    def seed_retry(self, count: int) -> None:
        """Seed the ledger from a caller that has already spent attempts."""
        self._retry_count = max(self._retry_count, int(count))

    def reset(self) -> None:
        """Reset retry count (for testing / fresh cycles)."""
        self._retry_count = 0

    def report(self, verdict: Any, detail: str | None = None) -> dict[str, Any]:
        """Produce the structured failure report."""
        return {
            "worker": self.name,
            "retries_used": self._retry_count,
            "max_retries": self.max_retries,
            "fallback": self.fallback,
            "resolved": self._retry_count < self.max_retries,
            "detail": detail,
        }

    # ---- Remediation (the boundary contract) ----------------------------
    # A worker OWNS the transport while it handles a task: `execute()` is the
    # one method the substrate dispatches, and it is where a worker may call
    # `transport.execute_tool(...)` / `transport.call_llm(...)` to ACT.
    #
    # The base implementations below are bounded and deterministic — they
    # honour the governance POLICY on the task (DEGRADE continues; RETRY /
    # FALLBACK consume the retry budget). Workers with real bodies override
    # `execute()`.

    async def execute(
        self,
        task: FailureTask,
        transport: FailureTransport | None = None,
    ) -> WorkerStatus:
        """Handle one dispatched task (never raises). The worker owns `transport`.

        Base implementation: DEGRADE resolves immediately; RETRY/FALLBACK
        consume the retry budget and, on exhaustion, apply the declared
        fallback. Workers that need actual remediation override this.
        """
        task_id = task.task_id if task is not None else ""
        remediation = task.remediation if task is not None else RemediationKind.RETRY

        if remediation is RemediationKind.DEGRADE:
            return WorkerStatus(
                outcome="degraded",
                resolved=True,
                degraded=True,
                retry_count=self._retry_count,
                findings=(f"degraded: continuing without retry ({task.detail or ''})",),
                task_id=task_id,
            )

        # RETRY / FALLBACK — one bounded attempt.
        if self.can_retry():
            self.consume_retry()
            return WorkerStatus(
                outcome="retrying",
                resolved=False,
                retry_count=self._retry_count,
                findings=(f"retry {self._retry_count}/{self.max_retries}",),
                task_id=task_id,
            )
        # Budget exhausted; apply the declared fallback.
        resolved = self.fallback != "terminate"
        return WorkerStatus(
            outcome=(
                "degraded" if self.fallback == "degrade"
                else ("resolved" if resolved else "failed")
            ),
            resolved=resolved,
            degraded=(self.fallback == "degrade"),
            retry_count=self._retry_count,
            fallback_applied=True,
            findings=(f"retries exhausted; fallback={self.fallback}",),
            task_id=task_id,
        )

    async def invoke(
        self,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> RemediationOutcome:
        """Compatibility shim over ``execute()`` (legacy call shape).

        Prefer ``execute(task, transport)``; this exists so older callers and
        tests keep working. It synthesises a RETRY-policy task.
        """
        task = FailureTask(
            kind="",
            failure_class=_DEFAULT_CLASS,
            severity=_DEFAULT_SEVERITY,
            remediation=RemediationKind.RETRY,
            detail=detail,
            intent=extra,
        )
        status = await self.execute(task, None)
        return RemediationOutcome(
            resolved=status.resolved,
            retry_count=status.retry_count,
            fallback_applied=status.fallback_applied,
            degraded=status.degraded,
            report=self.report(None, detail),
        )


# ---------------------------------------------------------------------------
# Registry — one worker per failure substrate
# ---------------------------------------------------------------------------

FAILURE_WORKER_REGISTRY: dict[str, FailureWorker] = {
    "narration": FailureWorker(
        name="narration",
        max_retries=1,
        fallback="json",        # fallback: accept raw JSON from LLM
        cooldown_ms=5000,
    ),
    "tool": FailureWorker(
        name="tool",
        max_retries=2,
        fallback="skip",         # fallback: skip the tool, record finding
        cooldown_ms=1000,
    ),
    "validation": FailureWorker(
        name="validation",
        max_retries=0,           # no retry; repair is the bounded retry
        fallback="terminate",
    ),
    "dispatch": FailureWorker(
        name="dispatch",
        max_retries=1,
        fallback="degrade",      # fallback: mark as degraded, continue
        cooldown_ms=2000,
    ),
    "memory": FailureWorker(
        name="memory",
        max_retries=0,
        fallback="empty",        # fallback: continue without memory
    ),
    "gate": FailureWorker(
        name="gate",
        max_retries=0,
        fallback="terminate",    # gate failures are final
    ),
    "inference": FailureWorker(
        name="inference",
        max_retries=1,
        fallback="degrade",     # inferential failure degrades the cycle, not kills it
        cooldown_ms=2000,
    ),
    "infra": FailureWorker(
        name="infra",
        max_retries=1,
        fallback="degrade",
        cooldown_ms=2000,
    ),
}


def default_workers() -> dict[str, FailureWorker]:
    """Build a FRESH worker set (one per cycle — no shared retry ledger).

    The substrate constructs its workers through this factory rather than
    reusing the module-global registry, so two concurrent cycles can never
    share retry state. ``FAILURE_WORKER_REGISTRY`` remains as the declared
    template the factory copies from.
    """
    return {
        name: FailureWorker(
            name=worker.name,
            max_retries=worker.max_retries,
            fallback=worker.fallback,
            cooldown_ms=worker.cooldown_ms,
        )
        for name, worker in FAILURE_WORKER_REGISTRY.items()
    }


__all__ = [
    "FAILURE_WORKER_REGISTRY",
    "FailureWorker",
    "RemediationOutcome",
    "default_workers",
]