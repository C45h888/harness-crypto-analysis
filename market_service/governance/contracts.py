"""Plane governance contracts — the typed surface of the two worker-plane FSMs.

SEMANTIC JURISDICTION: this module owns the SHAPE of plane governance and
nothing else. It carries no rules, no I/O, no plane imports. The two
concrete FSMs (``calculation_fsm`` / ``analysis_fsm``) own the rules; the
``GovernanceGovernor`` (``gates``) owns the I/O; ``manifest`` owns the
derivation of authority records from the planes.

Design doctrine (docs/PLANE_GOVERNANCE_SPEC.md):

* **Deterministic, never timer-driven.** No clock is ever an authority; a
  timestamp is only ever an input to a predicate. Wake comes from Redis
  stream events or an explicit invoke — the same doctrine as
  ``substrate_worker/core/supervisor.py`` ("blocking reads are the only
  cadence; no timer task").
* **The FSM is the sole source of legality.** A fire, a publish, and a
  durable PG write are LEGAL only if the plane's FSM says so.
* **Rational boundary.** Every verdict carries a non-empty ``reason`` and a
  ``rule`` id. A denial without a named rule is a governance error, so it is
  impossible by construction (``__post_init__``).
* **No contractual connection to workers.** Governance speaks only in
  ``(plane, substrate, symbol, payload_dict)``. It never imports a worker
  class, never instantiates one, never references ``SubstrateWorkerCore``.
  Authority flows FSM -> substrate manifest -> worker binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

# The governance ledger contract's own version (its own axis; the charter
# forbids borrowing another plane's version word).
GOVERNANCE_LEDGER_SCHEMA_VERSION = 1


class GovernancePlane(str, Enum):
    """Which worker plane a governance engine governs."""

    CALCULATION = "calculation"
    ANALYSIS = "analysis"


class GovernanceGate(str, Enum):
    """The three gated seams of a worker's fire cycle."""

    FIRE = "fire"           # may this substrate compute at all?
    PUBLISH = "publish"     # may this payload enter the plane's keyspace?
    PG_WRITE = "pg_write"   # may this fire be durably recorded?


# Status family is NOT declared here. The owner is
# ``substrate_worker.contracts.PAYLOAD_STATUSES``; the derivation seam
# (``manifest.py``) imports it and injects it. Governance never mirrors it —
# STATE_CHARTER_SPEC.md: "Planes consume owners by IMPORT, never by mirrored
# constants." The FSM is handed the vocabulary at construction so it can
# still assert the charter without importing the plane.


@dataclass(frozen=True)
class SubstrateManifest:
    """The authority record the FSM controls.

    Derived once from the plane (worker class attributes + the declared
    ``SUBSTRATE_GRAPH``), never hand-maintained, never mirrored. The
    ``primary_stream`` field IS the primary evidence layer: the one stream
    whose arrival authorizes work, and the declared input the plane may not
    silently change.
    """

    plane: GovernancePlane
    substrate: str
    primary_stream: str
    writes_keyspace: str
    input_streams: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    derived_inputs: tuple[str, ...] = ()
    allowed_sections: tuple[str, ...] = ()
    schema_version: int = 1
    status_family: frozenset[str] = frozenset()
    cooldown_s: int = 0
    staleness_s: int = 0
    rollovers: tuple[str, ...] = ()

    def is_valid(self) -> bool:
        """A manifest without identity, an evidence layer, or a keyspace is
        not a grant of authority — it is a bug."""
        return bool(
            self.substrate
            and self.primary_stream
            and self.writes_keyspace
            and isinstance(self.plane, GovernancePlane)
            and self.status_family
            and self.schema_version >= 1
        )

    @property
    def declared_inputs(self) -> tuple[str, ...]:
        """Every input this substrate declares (evidence layer first)."""
        streams = self.input_streams or ((self.primary_stream,) if self.primary_stream else ())
        merged = (*streams, *self.dependencies, *self.derived_inputs)
        return tuple(dict.fromkeys(merged))

    def to_dict(self) -> dict[str, Any]:
        return {
            "plane": self.plane.value,
            "substrate": self.substrate,
            "primary_stream": self.primary_stream,
            "writes_keyspace": self.writes_keyspace,
            "input_streams": list(self.input_streams),
            "dependencies": list(self.dependencies),
            "derived_inputs": list(self.derived_inputs),
            "allowed_sections": list(self.allowed_sections),
            "schema_version": self.schema_version,
            "status_family": sorted(self.status_family),
            "cooldown_s": self.cooldown_s,
            "staleness_s": self.staleness_s,
            "rollovers": list(self.rollovers),
        }


@dataclass(frozen=True)
class GovernanceObservation:
    """What the FSM SEES: the proposed output plus the plane's own Redis state.

    ``status`` / ``schema_version`` / ``missing_inputs`` are the PROPOSED
    payload's values (the output about to enter Redis). ``previous_*`` are
    read back from the plane's own ``latest`` projection — the FSM checks the
    output that is inside Redis against the output about to replace it.
    """

    plane: GovernancePlane
    substrate: str
    keyspace: str
    latest_present: bool = False
    latest_age_ms: int | None = None
    previous_status: str | None = None
    previous_schema_version: int | None = None
    previous_computed_at_ms: int | None = None
    status: str | None = None
    schema_version: int | None = None
    missing_inputs: tuple[str, ...] = ()
    declared_inputs: tuple[str, ...] = ()
    observed_inputs: tuple[str, ...] = ()
    supervisor_ttl_ms: int | None = None
    trigger_source: str | None = None
    computed_at_ms: int | None = None
    #: True when the plane's previous projection could NOT be read. The FSM
    #: fails CLOSED on this (``observation-unavailable``) rather than treating
    #: an unreadable history as "no history" — the same posture as a governor
    #: error. Never set for the FIRE gate (which reads no history).
    read_failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plane": self.plane.value,
            "substrate": self.substrate,
            "keyspace": self.keyspace,
            "latest_present": self.latest_present,
            "latest_age_ms": self.latest_age_ms,
            "previous_status": self.previous_status,
            "previous_schema_version": self.previous_schema_version,
            "previous_computed_at_ms": self.previous_computed_at_ms,
            "status": self.status,
            "schema_version": self.schema_version,
            "missing_inputs": list(self.missing_inputs),
            "declared_inputs": list(self.declared_inputs),
            "observed_inputs": list(self.observed_inputs),
            "supervisor_ttl_ms": self.supervisor_ttl_ms,
            "trigger_source": self.trigger_source,
            "computed_at_ms": self.computed_at_ms,
            "read_failed": self.read_failed,
        }


@dataclass(frozen=True)
class GovernanceEvent:
    """What is being asked of the FSM."""

    gate: GovernanceGate
    substrate: str
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class GovernanceVerdict:
    """The FSM's answer. NEVER raises; every verdict is reasoned.

    ``allowed``     — whether the gated action is legal.
    ``reason``      — mandatory, non-empty (the rational boundary).
    ``rule``        — the rule id that decided (mandatory; auditable).
    ``findings``    — advisory observations attached to the decision.
    ``remedy``      — what a compliant action would look like (advisory).
    """

    allowed: bool
    reason: str
    rule: str = "allow"
    findings: tuple[str, ...] = ()
    remedy: str | None = None

    def __post_init__(self) -> None:
        if not str(self.reason).strip():
            raise ValueError("GovernanceVerdict.reason must be non-empty "
                             "(a denial or an allowance without a reason is a "
                             "governance error)")
        if not str(self.rule).strip():
            raise ValueError("GovernanceVerdict.rule must be non-empty "
                             "(every verdict names its deciding rule)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rule": self.rule,
            "findings": list(self.findings),
            "remedy": self.remedy,
        }

    @classmethod
    def allow(
        cls, rule: str, reason: str, *,
        findings: tuple[str, ...] = (), remedy: str | None = None,
    ) -> "GovernanceVerdict":
        return cls(True, reason, rule, findings, remedy)

    @classmethod
    def deny(
        cls, rule: str, reason: str, *,
        findings: tuple[str, ...] = (), remedy: str | None = None,
    ) -> "GovernanceVerdict":
        return cls(False, reason, rule, findings, remedy)


@dataclass(frozen=True)
class DispatchDecision:
    """One substrate's pre-fire dispatch verdict (pure, no compute spent)."""

    substrate: str
    allowed: bool
    reason: str
    rule: str
    batch: int = 0
    retry_after_ms: int | None = None


@dataclass(frozen=True)
class DispatchTicket:
    """The dispatch guard's answer for one request (pure, replay-safe)."""

    plane: str
    symbol: str
    allowed: tuple[str, ...] = ()
    denied: tuple[DispatchDecision, ...] = ()
    batches: tuple[tuple[str, ...], ...] = ()
    deferred: bool = False
    defer_reason: str | None = None
    rule: str = "dispatch-allow"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plane": self.plane,
            "symbol": self.symbol,
            "allowed": list(self.allowed),
            "denied": [
                {
                    "substrate": d.substrate,
                    "allowed": d.allowed,
                    "reason": d.reason,
                    "rule": d.rule,
                    "batch": d.batch,
                    "retry_after_ms": d.retry_after_ms,
                }
                for d in self.denied
            ],
            "batches": [list(b) for b in self.batches],
            "deferred": self.deferred,
            "defer_reason": self.defer_reason,
            "rule": self.rule,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GovernanceLedgerRecord:
    """One persisted governance decision (durable audit of legality)."""

    plane: str
    substrate: str
    symbol: str
    gate: str
    allowed: bool
    rule: str
    reason: str
    findings: tuple[str, ...] = ()
    observation: Mapping[str, Any] = field(default_factory=dict)
    decided_at_ms: int = 0
    schema_version: int = GOVERNANCE_LEDGER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "plane": self.plane,
            "substrate": self.substrate,
            "symbol": self.symbol,
            "gate": self.gate,
            "allowed": self.allowed,
            "rule": self.rule,
            "reason": self.reason,
            "findings": list(self.findings),
            "observation": dict(self.observation),
            "decided_at_ms": self.decided_at_ms,
            "schema_version": self.schema_version,
        }


__all__ = [
    "GOVERNANCE_LEDGER_SCHEMA_VERSION",
    "DispatchDecision",
    "DispatchTicket",
    "GovernanceEvent",
    "GovernanceGate",
    "GovernanceLedgerRecord",
    "GovernanceObservation",
    "GovernancePlane",
    "GovernanceVerdict",
    "SubstrateManifest",
]
