"""Failure substrate — the deterministic MECHANICAL plane of failure handling.

The failure chain is three layers, each with ONE owner:

    engine/fsm.py            [router]      one intake edge: CYCLE_FAILED
    engine/failure_fsm.py    [GOVERNANCE]  classifies kinds -> verdicts
                                           (class, severity, policy, terminal)
    failure_substrate/       [MECHANICS]   worker selection, retry budgets,
                                           per-cycle ledger, coordination
    failure_substrate/workers.py [EXECUTION] bounded remediation units

This package is the MECHANICAL plane. It receives a decided
``FailureVerdict`` from the Failure FSM and coordinates a bounded worker. It
does NOT classify (that is governance) and it does NOT know terminals (except
to carry the verdict's terminal onto the report).

Boundary rule enforced by the charter test:
  - the substrate imports the Failure FSM's VALUE TYPES only;
  - the failure FSM never imports the substrate;
  - the main FSM imports the failure FSM (to delegate) but neither the
    substrate nor the workers.
"""

from __future__ import annotations

# --- governance vocabulary, re-exported for consumers -----------------------
from ..failure_fsm import (
    FAILURE_CLASSIFICATION,
    FAILURE_FSM,
    FAILURE_TERMINAL,
    FailureClass,
    FailureFSM,
    FailureKind,
    FailureSeverity,
    FailureVerdict,
    RemediationKind,
)

# --- the mechanical plane ---------------------------------------------------
from .report import FailureReport
from .signal import FailureOutcome, signal_failure
from .substrate import (
    FAILURE_SUBSTRATE,
    MAX_RETRIES,
    WORKER_FOR_KIND,
    FailureSubstrate,
    worker_for_verdict,
)
from .workers import (
    FAILURE_WORKER_REGISTRY,
    FailureWorker,
    RemediationOutcome,
    default_workers,
)


__all__ = [
    # governance vocabulary
    "FAILURE_CLASSIFICATION",
    "FAILURE_FSM",
    "FAILURE_TERMINAL",
    "FailureClass",
    "FailureFSM",
    "FailureKind",
    "FailureSeverity",
    "FailureVerdict",
    "RemediationKind",
    # mechanical plane
    "FAILURE_SUBSTRATE",
    "MAX_RETRIES",
    "WORKER_FOR_KIND",
    "FailureSubstrate",
    "FailureOutcome",
    "FailureReport",
    "FailureWorker",
    "RemediationOutcome",
    "FAILURE_WORKER_REGISTRY",
    "default_workers",
    "signal_failure",
    "worker_for_verdict",
]
