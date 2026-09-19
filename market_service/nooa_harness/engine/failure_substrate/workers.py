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

    def consume_retry(self) -> int:
        """Increment retry count and return the new count."""
        self._retry_count += 1
        return self._retry_count

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

    # ---- Remediation implementations ----
    # Each worker type overrides these to implement its actual logic.
    # The base class provides identity-style implementations suitable for
    # tools that just need the report.

    async def invoke(
        self,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> RemediationOutcome:
        """Run the worker's remediation (retry or fallback).

        Base implementation: if can_retry, consume a retry and return
        resolved=False (caller retries the actual operation). If exhausted,
        apply the fallback. Workers that need actual logic override this.
        """
        if self.can_retry():
            self.consume_retry()
            return RemediationOutcome(
                resolved=False,
                retry_count=self._retry_count,
                report=self.report(None, detail),
            )
        # Retries exhausted; apply fallback
        return RemediationOutcome(
            resolved=self.fallback != "terminate",
            retry_count=self._retry_count,
            fallback_applied=True,
            degraded=(self.fallback == "degrade"),
            report=self.report(None, f"retries exhausted; fallback={self.fallback}: {detail or ''}"),
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


__all__ = [
    "FAILURE_WORKER_REGISTRY",
    "FailureWorker",
    "RemediationOutcome",
]