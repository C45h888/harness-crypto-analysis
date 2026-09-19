"""Failure substrate — bounded remediation layer for governance-reported failures.

The FSM (``engine/fsm.py``) is the **authority** — it designates that a
cycle is in a failure state by routing a ``GovernanceEventKind`` to a
``LoopTerminal``. This substrate is the **executor** — it receives a failure
signal from the runtime, classifies it, applies bounded remediation (retry /
fallback / degrade), and either returns the loop to work or lets the failure
pass through to ``run_runtime_failure()``.

ARCHITECTURE:

    runtime loop  ──→  FSM (fsm.py)  ──designates─→  failure substrate
                                                          │
                                                    ┌─────┴──────┐
                                                    │  retry?    │
                                                    │  fallback? │
                                                    │  degrade?  │
                                                    └─────┬──────┘
                                                          │
                                               ┌──────────┴─────────┐
                                               │                    │
                                          resolved            exhausted
                                               │                    │
                                         back to loop     run_runtime_failure()
                                                              (existing path)

The substrate is a pure addition: the existing ``run_runtime_failure()`` and
``run_validation_terminal()`` paths are untouched. When the substrate is
present it intercepts the failure; when absent or when it declares exhaustion,
the existing terminal path runs as before.
"""

from __future__ import annotations

from .membrane import (
    FAILURE_CLASSIFICATION,
    MAX_RETRIES,
    FailureClass,
    FailureSeverity,
    FailureSubstrateMembrane,
    FailureVerdict,
    RemediationKind,
)
from .report import FailureReport
from .signal import FailureOutcome, signal_failure
from .workers import FAILURE_WORKER_REGISTRY, FailureWorker, RemediationOutcome

# Re-export the public API surface
__all__ = [
    "FAILURE_CLASSIFICATION",
    "FAILURE_WORKER_REGISTRY",
    "FailureClass",
    "FailureOutcome",
    "FailureReport",
    "FailureSeverity",
    "FailureSubstrateMembrane",
    "FailureVerdict",
    "FailureWorker",
    "MAX_RETRIES",
    "RemediationKind",
    "RemediationOutcome",
    "signal_failure",
]