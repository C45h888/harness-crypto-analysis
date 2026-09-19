"""Structured failure report — the payload produced when a failure is remediated.

The report is attached to ``deterministic_state["failure_report"]`` in the
artifact so the LLM output and downstream consumers know exactly what went
wrong and what was done about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FailureReport:
    """Structured failure report attached to the artifact.

    ``failure_kind`` — the original failure-kind string.
    ``worker_name`` — the substrate worker that handled it.
    ``severity`` — transient / permanent / degraded / catastrophic.
    ``resolved`` — True if the cycle continued after remediation.
    ``retries_used`` — how many retries were consumed.
    ``max_retries`` — the worker's retry budget.
    ``fallback_applied`` — whether a fallback strategy was used.
    ``fallback_name`` — the name of the fallback strategy.
    ``degraded`` — whether the cycle was marked as degraded.
    ``terminal`` — the LoopTerminal the cycle ended at (if terminated).
    ``detail`` — human-readable detail from the failure site.
    ``remediation_detail`` — what remediation was attempted.
    """

    failure_kind: str
    worker_name: str
    severity: str = "transient"
    resolved: bool = False
    retries_used: int = 0
    max_retries: int = 0
    fallback_applied: bool = False
    fallback_name: str = ""
    degraded: bool = False
    terminal: str | None = None
    detail: str | None = None
    remediation_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict for JSON serialization."""
        return {
            "failure_kind": self.failure_kind,
            "worker_name": self.worker_name,
            "severity": self.severity,
            "resolved": self.resolved,
            "retries_used": self.retries_used,
            "max_retries": self.max_retries,
            "fallback_applied": self.fallback_applied,
            "fallback_name": self.fallback_name,
            "degraded": self.degraded,
            "terminal": self.terminal,
            "detail": self.detail,
            "remediation_detail": self.remediation_detail,
        }


__all__ = ["FailureReport"]