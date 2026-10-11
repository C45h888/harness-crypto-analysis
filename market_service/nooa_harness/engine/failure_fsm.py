"""FailureFSM — the PRIMARY failure node: governance over all failure semantics.

This module is ``engine/failure_fsm.py``. It is the canonical failure
constitution of the system. Every failure — engine-plane (narration,
parse, validation, gate, budget, infra) and inference-plane (chain halt,
position blocked, forecast/scenario) — is CLASSIFIED and DECIDED here, and
only here.

WHERE THIS SITS (the three-layer failure chain):

    cycle fails
      -> engine/fsm.py intake edge (CYCLE_FAILED)        [router only]
        -> FailureFSM.classify(kind)                     [GOVERNANCE — here]
          -> failure_substrate.substrate.mediate(verdict) [MECHANICAL]
            -> failure_substrate.workers.*.invoke()       [EXECUTION]

The main AgenticLoopMembrane keeps exactly ONE failure-related edge: it
RECEIVES the bare "the cycle failed" signal and forwards it to this node.
It holds no failure table, no classification, no worker knowledge. This
module holds the rational governance behind all failure semantics.

WHAT THIS OWNS (governance — semantic decisions):
  - the failure-kind vocabulary (``FailureKind``);
  - kind -> ``LoopTerminal`` — the endpoint each failure routes to
    (``FAILURE_TERMINAL``);
  - kind -> (``FailureClass``, ``FailureSeverity``) — the classification
    table (``FAILURE_CLASSIFICATION``);
  - the POLICY: severity -> ``RemediationKind`` (retry / fallback /
    degrade / terminate);
  - one composed ``FailureVerdict`` per failure.

WHAT THIS DELIBERATELY DOES NOT OWN (mechanical — the substrate's job):
  - WHICH WORKER handles the failure (worker names are substrate-local);
  - retry budgets and the retry ledger (per-cycle, substrate);
  - the act of remediating (retrying / falling back / degrading).

Boundary rule: this module imports ``loop_states`` (shared endpoint
vocabulary) and NOTHING else local. The substrate imports this module's
VALUE TYPES; this module never imports the substrate. The main FSM imports
``FAILURE_FSM`` to obtain the terminal for a delegated failure; the
substrate never imports the main FSM.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .loop_states import LoopTerminal


# ---------------------------------------------------------------------------
# Value vocabulary (shared leaf — imported by the substrate as value types)
# ---------------------------------------------------------------------------


class FailureClass(str, Enum):
    """Classification of the failure source — what substrate owns it."""

    NARRATION = "narration"     # LLM transport / parse failures
    TOOL = "tool"               # tool dispatch failures (denied, error)
    GATE = "gate"               # deterministic gate failures
    DISPATCH = "dispatch"       # substrate worker failures
    MEMORY = "memory"           # memory store failures
    VALIDATION = "validation"   # validation gate failures
    INFERENCE = "inference"     # statistical inference plane failures
    INFRA = "infra"             # infrastructure failures (store, db)


class FailureSeverity(str, Enum):
    """How severe the failure is — determines remediation scope."""

    TRANSIENT = "transient"         # may resolve on retry (network blip)
    PERMANENT = "permanent"         # will not resolve on retry (bad input)
    DEGRADED = "degraded"           # partial failure; system can continue
    CATASTROPHIC = "catastrophic"   # cannot continue under any remediation


class RemediationKind(str, Enum):
    """The governance POLICY for a failure — what the substrate should do."""

    RETRY = "retry"             # attempt a bounded retry
    FALLBACK = "fallback"       # apply a degraded fallback, continue
    DEGRADE = "degrade"         # mark as degraded, continue without retry
    TERMINATE = "terminate"     # route to terminal immediately


class FailureKind(str, Enum):
    """The canonical failure-kind vocabulary — one namespace for the system.

    Terminal-bearing kinds route to a dedicated ``LoopTerminal``;
    substrate-routed kinds (tool/memory/inference signals the runtime sets)
    default to ``INFRA_FAILED`` while their class drives remediation.
    """

    # --- terminal-bearing kinds (dedicated LoopTerminal) ---
    GATE_REFUSED = "gate_refused"
    NARRATION_FAILED = "narration_failed"
    PARSE_FAILED = "parse_failed"
    VALIDATION_FAILED = "validation_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INFRA_FAILED = "infra_failed"
    # --- substrate-routed kinds (default terminal INFRA_FAILED) ---
    TOOL_DENIED = "tool_denied"
    TOOL_ERROR = "tool_error"
    TOOL_SUPPRESSED = "tool_suppressed"
    DISPATCH_FAILED = "dispatch_failed"
    MEMORY_FAILED = "memory_failed"
    CHAIN_HALT = "chain_halt"
    CHAIN_LINK_REFUSED = "chain_link_refused"
    POSITION_BLOCKED = "position_blocked"
    POSITION_OUT_OF_ORDER = "position_out_of_order"
    FORECAST_FAILED = "forecast_failed"
    SCENARIO_UNEVALUABLE = "scenario_unevaluable"
    GATE_TIMEOUT = "gate_timeout"


# ---------------------------------------------------------------------------
# The constitution — kind -> terminal, kind -> (class, severity)
# ---------------------------------------------------------------------------

#: The endpoint each failure kind routes to. Kinds absent here default to
#: ``INFRA_FAILED`` (the honest "we do not know what this was" terminal).
FAILURE_TERMINAL: dict[str, LoopTerminal] = {
    FailureKind.GATE_REFUSED.value: LoopTerminal.GATE_REFUSED,
    FailureKind.NARRATION_FAILED.value: LoopTerminal.NARRATION_FAILED,
    FailureKind.PARSE_FAILED.value: LoopTerminal.PARSE_FAILED,
    FailureKind.VALIDATION_FAILED.value: LoopTerminal.VALIDATION_FAILED,
    FailureKind.BUDGET_EXHAUSTED.value: LoopTerminal.BUDGET_EXHAUSTED,
    FailureKind.INFRA_FAILED.value: LoopTerminal.INFRA_FAILED,
}

#: The classification of every failure kind: (class, severity). Worker
#: assignment is deliberately NOT here — that is the substrate's affair.
FAILURE_CLASSIFICATION: dict[str, tuple[FailureClass, FailureSeverity]] = {
    # narration (LLM transport / parse)
    FailureKind.NARRATION_FAILED.value: (FailureClass.NARRATION, FailureSeverity.TRANSIENT),
    FailureKind.PARSE_FAILED.value: (FailureClass.NARRATION, FailureSeverity.PERMANENT),
    # tool
    FailureKind.TOOL_DENIED.value: (FailureClass.TOOL, FailureSeverity.PERMANENT),
    FailureKind.TOOL_ERROR.value: (FailureClass.TOOL, FailureSeverity.TRANSIENT),
    FailureKind.TOOL_SUPPRESSED.value: (FailureClass.TOOL, FailureSeverity.DEGRADED),
    # validation
    FailureKind.VALIDATION_FAILED.value: (FailureClass.VALIDATION, FailureSeverity.PERMANENT),
    # budget / dispatch
    FailureKind.BUDGET_EXHAUSTED.value: (FailureClass.DISPATCH, FailureSeverity.PERMANENT),
    FailureKind.DISPATCH_FAILED.value: (FailureClass.DISPATCH, FailureSeverity.TRANSIENT),
    # memory
    FailureKind.MEMORY_FAILED.value: (FailureClass.MEMORY, FailureSeverity.DEGRADED),
    # inference plane
    FailureKind.CHAIN_HALT.value: (FailureClass.INFERENCE, FailureSeverity.PERMANENT),
    FailureKind.CHAIN_LINK_REFUSED.value: (FailureClass.INFERENCE, FailureSeverity.DEGRADED),
    FailureKind.POSITION_BLOCKED.value: (FailureClass.INFERENCE, FailureSeverity.PERMANENT),
    FailureKind.POSITION_OUT_OF_ORDER.value: (FailureClass.INFERENCE, FailureSeverity.PERMANENT),
    FailureKind.FORECAST_FAILED.value: (FailureClass.INFERENCE, FailureSeverity.TRANSIENT),
    FailureKind.SCENARIO_UNEVALUABLE.value: (FailureClass.INFERENCE, FailureSeverity.DEGRADED),
    # gate
    FailureKind.GATE_REFUSED.value: (FailureClass.GATE, FailureSeverity.PERMANENT),
    FailureKind.GATE_TIMEOUT.value: (FailureClass.GATE, FailureSeverity.TRANSIENT),
    # infrastructure (catch-all)
    FailureKind.INFRA_FAILED.value: (FailureClass.INFRA, FailureSeverity.TRANSIENT),
}

#: Unknown kinds classify as infra/transient — the honest fallback.
_DEFAULT_CLASSIFICATION: tuple[FailureClass, FailureSeverity] = (
    FailureClass.INFRA, FailureSeverity.TRANSIENT,
)

#: The POLICY: severity -> remediation. This is the governance decision.
#: The substrate may ESCALATE retry -> terminate when its retry budget is
#: exhausted; it may never weaken a terminate.
_SEVERITY_REMEDIATION: dict[FailureSeverity, RemediationKind] = {
    FailureSeverity.TRANSIENT: RemediationKind.RETRY,
    FailureSeverity.DEGRADED: RemediationKind.DEGRADE,
    FailureSeverity.PERMANENT: RemediationKind.TERMINATE,
    FailureSeverity.CATASTROPHIC: RemediationKind.TERMINATE,
}

#: The remediation kinds the substrate is allowed to escalate to terminate.
_ESCALATABLE = frozenset({RemediationKind.RETRY, RemediationKind.FALLBACK})


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureVerdict:
    """The Failure FSM's composed governance decision for one failure.

    ``kind``          — the failure-kind string (the canonical vocabulary).
    ``failure_class`` — which substrate owns it.
    ``severity``      — transient / permanent / degraded / catastrophic.
    ``remediation``   — the POLICY (retry / fallback / degrade / terminate).
    ``terminal``      — the ``LoopTerminal`` the cycle ends at.
    ``reason``        — human-readable justification.

    This deliberately carries NO worker name and NO retry budget — those are
    mechanical facts the substrate owns. Governance decides policy; the
    substrate decides mechanics.
    """

    kind: str
    failure_class: FailureClass
    severity: FailureSeverity
    remediation: RemediationKind
    terminal: LoopTerminal
    reason: str = ""

    @property
    def terminates(self) -> bool:
        """Whether the policy is an immediate terminal (no remediation)."""
        return self.remediation is RemediationKind.TERMINATE


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class FailureFSM:
    """The frozen failure constitution — pure, stateless, total.

    ``classify(kind, ...)`` returns a composed ``FailureVerdict``; it never
    raises and never returns None (unknown kinds classify as infra/transient
    and route to ``INFRA_FAILED``).
    """

    __slots__ = ()

    # ------------------------------------------------------------------
    # Classification (the governance decision)
    # ------------------------------------------------------------------

    def classify(
        self,
        kind: object,
        *,
        is_transient_override: bool | None = None,
    ) -> FailureVerdict:
        """Classify one failure kind into a composed verdict (never raises).

        ``is_transient_override`` forces the severity classification
        (``True`` -> transient, ``False`` -> permanent) for callers that
        know better than the table.
        """
        key = str(kind or "")
        failure_class, severity = FAILURE_CLASSIFICATION.get(
            key, _DEFAULT_CLASSIFICATION,
        )
        if is_transient_override is True:
            severity = FailureSeverity.TRANSIENT
        elif is_transient_override is False:
            severity = FailureSeverity.PERMANENT
        remediation = _SEVERITY_REMEDIATION[severity]
        terminal = FAILURE_TERMINAL.get(key, LoopTerminal.INFRA_FAILED)
        return FailureVerdict(
            kind=key,
            failure_class=failure_class,
            severity=severity,
            remediation=remediation,
            terminal=terminal,
            reason=(
                f"failure {key or '<unknown>'}: class={failure_class.value} "
                f"severity={severity.value} -> {remediation.value} "
                f"(terminal={terminal.value})"
            ),
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def terminal_for(self, kind: object) -> LoopTerminal:
        """The terminal a failure kind routes to (INFRA_FAILED when unknown)."""
        return FAILURE_TERMINAL.get(str(kind or ""), LoopTerminal.INFRA_FAILED)

    @staticmethod
    def is_known_kind(kind: object) -> bool:
        """Whether the kind is in the canonical vocabulary."""
        return str(kind or "") in {member.value for member in FailureKind}

    @staticmethod
    def kinds() -> tuple[str, ...]:
        """The canonical failure-kind vocabulary, in declaration order."""
        return tuple(member.value for member in FailureKind)

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def self_check(self) -> None:
        """Fail fast if the failure constitution drifts."""
        if set(FAILURE_CLASSIFICATION) != {m.value for m in FailureKind}:
            raise ValueError(
                "FAILURE_CLASSIFICATION must cover every FailureKind exactly"
            )
        for kind, (failure_class, severity) in FAILURE_CLASSIFICATION.items():
            if not isinstance(failure_class, FailureClass):
                raise ValueError(f"{kind}: bad failure class {failure_class!r}")
            if not isinstance(severity, FailureSeverity):
                raise ValueError(f"{kind}: bad severity {severity!r}")
            if severity not in _SEVERITY_REMEDIATION:
                raise ValueError(f"{kind}: severity {severity!r} has no policy")
        for kind, terminal in FAILURE_TERMINAL.items():
            if not isinstance(terminal, LoopTerminal):
                raise ValueError(f"{kind}: bad terminal {terminal!r}")
        if FAILURE_TERMINAL.get(FailureKind.INFRA_FAILED.value) is None:
            raise ValueError("INFRA_FAILED must map to a terminal")
        # Every kind must classify to a verdict with a terminal (total).
        for member in FailureKind:
            verdict = self.classify(member.value)
            if verdict.terminal is None or not isinstance(verdict, FailureVerdict):
                raise ValueError(f"{member.value}: classify is not total")


#: The canonical, frozen failure constitution (single source of truth).
FAILURE_FSM: FailureFSM = FailureFSM()
FAILURE_FSM.self_check()


__all__ = [
    "FailureClass",
    "FailureSeverity",
    "RemediationKind",
    "FailureKind",
    "FailureVerdict",
    "FailureFSM",
    "FAILURE_FSM",
    "FAILURE_CLASSIFICATION",
    "FAILURE_TERMINAL",
]
