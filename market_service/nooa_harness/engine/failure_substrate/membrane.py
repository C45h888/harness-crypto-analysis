"""Failure substrate FSM membrane — classifies failures and routes to remediation.

The FSM (engine/fsm.py) designates THAT a failure occurred by routing a
GovernanceEventKind to a terminal. This membrane is a **secondary** FSM that
takes that designation and decides WHAT KIND of failure it is, whether it is
TRANSIENT, and what REMEDIATION to apply before letting it reach the terminal.

Authority flow:

    runtime loop detects error
        ──→ runtime sets st.failure_kind
            ──→ signal_failure(kind, detail, context)
                ──→ FailureSubstrateMembrane.classify(kind, detail)
                    ──→ returns FailureVerdict:
                            ├── retry (attempt bounded retry)
                            ├── fallback (apply degraded fallback)
                            └── terminate (pass through to run_runtime_failure)

The membrane is a pure function: same inputs → same verdict. No I/O, no
state mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureClass(str, Enum):
    """Classification of the failure source — what substrate owns it."""
    NARRATION = "narration"       # LLM transport / parse failures
    TOOL = "tool"                 # tool dispatch failures (denied, error)
    GATE = "gate"                 # deterministic gate failures
    DISPATCH = "dispatch"         # substrate worker failures
    MEMORY = "memory"             # memory store failures
    VALIDATION = "validation"     # validation gate failures
    INFERENCE = "inference"       # statistical inference plane failures
    INFRA = "infra"               # infrastructure failures (store, db)


class FailureSeverity(str, Enum):
    """How severe the failure is — determines remediation scope."""
    TRANSIENT = "transient"       # may resolve on retry (network blip)
    PERMANENT = "permanent"       # will not resolve on retry (bad input)
    DEGRADED = "degraded"         # partial failure; system can continue
    CATASTROPHIC = "catastrophic" # cannot continue under any remediation


class RemediationKind(str, Enum):
    """What the substrate should do with this failure."""
    RETRY = "retry"               # attempt a bounded retry
    FALLBACK = "fallback"         # apply a degraded fallback, continue
    DEGRADE = "degrade"           # mark as degraded, continue without retry
    TERMINATE = "terminate"       # route to terminal immediately


@dataclass(frozen=True)
class FailureVerdict:
    """The membrane's decision for one failure signal.

    ``remediation`` — what to do (retry / fallback / degrade / terminate).
    ``worker_name`` — which bounded worker handles this failure.
    ``max_retries`` — max retries before giving up.
    ``severity``    — the classified severity.
    ``reason``      — human-readable justification.
    """

    remediation: RemediationKind
    worker_name: str
    max_retries: int = 0
    severity: FailureSeverity = FailureSeverity.TRANSIENT
    reason: str = ""


# ---------------------------------------------------------------------------
# Failure-kind → (class, worker, severity) mapping
# ---------------------------------------------------------------------------
# This is the constitution of the failure substrate. It maps the flat
# failure-kind strings the runtime currently sets on CycleRuntimeState
# (and the GovernanceEventKind values the FSM uses) into the substrate's
# vocabulary: class, worker, and default severity.

FAILURE_CLASSIFICATION: dict[str, tuple[FailureClass, str, FailureSeverity]] = {
    # Narration failures (LLM transport / parse)
    "narration_failed": (
        FailureClass.NARRATION, "narration", FailureSeverity.TRANSIENT,
    ),
    "parse_failed": (
        FailureClass.NARRATION, "narration", FailureSeverity.PERMANENT,
    ),
    # Tool failures (dispatch-level, not tool-level)
    "tool_denied": (
        FailureClass.TOOL, "tool", FailureSeverity.PERMANENT,
    ),
    "tool_error": (
        FailureClass.TOOL, "tool", FailureSeverity.TRANSIENT,
    ),
    "tool_suppressed": (
        FailureClass.TOOL, "tool", FailureSeverity.DEGRADED,
    ),
    # Validation failures (the final gate rejected)
    "validation_failed": (
        FailureClass.VALIDATION, "validation", FailureSeverity.PERMANENT,
    ),
    # Budget exhaustion
    "budget_exhausted": (
        FailureClass.DISPATCH, "dispatch", FailureSeverity.PERMANENT,
    ),
    "dispatch_failed": (
        FailureClass.DISPATCH, "dispatch", FailureSeverity.TRANSIENT,
    ),
    # Memory failures
    "memory_failed": (
        FailureClass.MEMORY, "memory", FailureSeverity.DEGRADED,
    ),
    # Statistical inference plane failures
    "chain_halt": (
        FailureClass.INFERENCE, "inference", FailureSeverity.PERMANENT,
    ),
    "chain_link_refused": (
        FailureClass.INFERENCE, "inference", FailureSeverity.DEGRADED,
    ),
    "position_blocked": (
        FailureClass.INFERENCE, "inference", FailureSeverity.PERMANENT,
    ),
    "position_out_of_order": (
        FailureClass.INFERENCE, "inference", FailureSeverity.PERMANENT,
    ),
    "forecast_failed": (
        FailureClass.INFERENCE, "inference", FailureSeverity.TRANSIENT,
    ),
    "scenario_unevaluable": (
        FailureClass.INFERENCE, "inference", FailureSeverity.DEGRADED,
    ),
    # Gate failures
    "gate_refused": (
        FailureClass.GATE, "gate", FailureSeverity.PERMANENT,
    ),
    "gate_timeout": (
        FailureClass.GATE, "gate", FailureSeverity.TRANSIENT,
    ),
    # Infrastructure failures (catch-all)
    "infra_failed": (
        FailureClass.INFRA, "infra", FailureSeverity.TRANSIENT,
    ),
}


# Per-substrate retry budgets. These match what the bounded workers declare.
MAX_RETRIES: dict[str, int] = {
    "narration": 1,       # one retry for LLM transport
    "tool": 2,            # two retries for tool dispatch
    "validation": 0,      # no retry; repair is the bounded retry
    "dispatch": 1,        # one retry for substrate dispatch
    "memory": 0,          # no retry; memory failures are permanent
    "gate": 0,            # no retry; gate failures are final
    "inference": 1,           # one retry for inference plane
    "infra": 1,           # one retry for infrastructure
}


class FailureSubstrateMembrane:
    """The failure classification membrane — pure function, no state.

    ``classify(kind, detail)`` returns a ``FailureVerdict`` that tells the
    runtime what to do. Never raises.
    """

    @classmethod
    def classify(
        cls,
        kind: str,
        detail: str | None = None,
        *,
        retry_count: int = 0,
        is_transient_override: bool | None = None,
    ) -> FailureVerdict:
        """Classify a failure signal into a verdict.

        ``kind`` — the failure-kind string from CycleRuntimeState or the
                   GovernanceEventKind value from the FSM.
        ``detail`` — human-readable detail for the report.
        ``retry_count`` — how many retries have already been attempted.
        ``is_transient_override`` — force transient/permanent classification.

        The verdict determines what happens next:
          - RETRY: runtime calls the worker's invoke() for a bounded retry
          - FALLBACK: runtime applies the worker's fallback, continues
          - DEGRADE: runtime marks the cycle as degraded, continues
          - TERMINATE: runtime routes to run_runtime_failure() / terminal
        """
        # Look up the classification; unknown kinds default to infra.
        entry = FAILURE_CLASSIFICATION.get(
            kind, (FailureClass.INFRA, "infra", FailureSeverity.TRANSIENT)
        )
        failure_class, worker_name, severity = entry

        # Allow override of transient/permanent for this call.
        if is_transient_override is True:
            severity = FailureSeverity.TRANSIENT
        elif is_transient_override is False:
            severity = FailureSeverity.PERMANENT

        max_retries = MAX_RETRIES.get(worker_name, 0)

        # --- Decision logic ---
        if severity is FailureSeverity.CATASTROPHIC:
            return FailureVerdict(
                remediation=RemediationKind.TERMINATE,
                worker_name=worker_name,
                max_retries=max_retries,
                severity=severity,
                reason=f"catastrophic failure {kind}: {detail or 'no detail'}",
            )

        if severity is FailureSeverity.PERMANENT:
            return FailureVerdict(
                remediation=RemediationKind.TERMINATE,
                worker_name=worker_name,
                max_retries=max_retries,
                severity=severity,
                reason=f"permanent failure {kind}: {detail or 'no detail'}",
            )

        if severity is FailureSeverity.DEGRADED:
            return FailureVerdict(
                remediation=RemediationKind.DEGRADE,
                worker_name=worker_name,
                max_retries=max_retries,
                severity=severity,
                reason=f"degraded failure {kind}: {detail or 'no detail'}",
            )

        # TRANSIENT — retry if budget remains, else terminate.
        if severity is FailureSeverity.TRANSIENT:
            if retry_count < max_retries:
                return FailureVerdict(
                    remediation=RemediationKind.RETRY,
                    worker_name=worker_name,
                    max_retries=max_retries,
                    severity=severity,
                    reason=f"transient {kind} (retry {retry_count + 1}/{max_retries}): "
                           f"{detail or 'no detail'}",
                )
            return FailureVerdict(
                remediation=RemediationKind.TERMINATE,
                worker_name=worker_name,
                max_retries=max_retries,
                severity=severity,
                reason=f"transient {kind} exhausted ({retry_count}/{max_retries}): "
                       f"{detail or 'no detail'}",
            )

        # Fallback — unknown severity treated as terminate
        return FailureVerdict(
            remediation=RemediationKind.TERMINATE,
            worker_name=worker_name,
            max_retries=max_retries,
            severity=FailureSeverity.PERMANENT,
            reason=f"unclassified severity for {kind}: {detail or 'no detail'}",
        )

    @classmethod
    def remediation_for_kind(cls, kind: str) -> RemediationKind | None:
        """Quick lookup: what remediation does this kind get? (no context)."""
        entry = FAILURE_CLASSIFICATION.get(
            kind, (FailureClass.INFRA, "infra", FailureSeverity.TRANSIENT)
        )
        _, worker_name, severity = entry

        if severity is FailureSeverity.CATASTROPHIC:
            return RemediationKind.TERMINATE
        if severity is FailureSeverity.PERMANENT:
            return RemediationKind.TERMINATE
        if severity is FailureSeverity.DEGRADED:
            return RemediationKind.DEGRADE
        # TRANSIENT
        max_r = MAX_RETRIES.get(worker_name, 0)
        if max_r > 0:
            return RemediationKind.RETRY
        return RemediationKind.TERMINATE


__all__ = [
    "FailureClass",
    "FailureSeverity",
    "FailureSubstrateMembrane",
    "FailureVerdict",
    "FAILURE_CLASSIFICATION",
    "MAX_RETRIES",
    "RemediationKind",
]