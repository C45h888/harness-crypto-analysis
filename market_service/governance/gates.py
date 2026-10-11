"""Governance governor — the async adapter between Redis and the pure FSM.

SEMANTIC JURISDICTION: this file owns the I/O of governing, never the
legality. It builds a ``GovernanceObservation`` from the plane's own Redis
state and the proposed payload, asks the pure FSM, and records the verdict on
the ledger. The FSM stays a pure function; the governor is the one component
that touches the world.

This is what a worker receives. It is duck-typed: the worker calls the
governor's ``evaluate`` / ``record`` (or the ``authorize_*`` conveniences)
with primitives only — never an imported governance type — so the worker
plane has no contractual dependency on governance and governance has none on
the workers.

    FSM (legality)  <--  Governor (I/O)  <--  worker call site
                              |
                        GovernanceLedger (Redis + PG)

**One row per fire.** The worker evaluates the gates it reaches and records
only the decision that sticks (the last gate reached). ``evaluate`` and
``record`` are therefore separate operations: evaluate many, record once.

Deterministic, never timer-driven: the governor is awakened by a worker's
fire cycle (which is itself woken by Redis stream events). It creates no
task, sleeps nowhere, and never schedules.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from market_service.governance.contracts import (
    GovernanceEvent,
    GovernanceGate,
    GovernanceObservation,
    GovernancePlane,
    GovernanceVerdict,
)
from market_service.governance.fsm import PlaneGovernanceFSM
from market_service.governance.ledger import GovernanceLedger

log = logging.getLogger(__name__)


def _payload_fields(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract the governance-relevant facts from a proposed payload."""
    if not isinstance(payload, Mapping):
        return {}
    trigger = payload.get("trigger")
    trigger_source = trigger.get("source") if isinstance(trigger, Mapping) else None
    missing = payload.get("missing_inputs") or ()
    if not isinstance(missing, (list, tuple)):
        missing = (missing,)
    return {
        "status": payload.get("status"),
        "schema_version": payload.get("schema_version"),
        "missing_inputs": tuple(str(m) for m in missing),
        "trigger_source": trigger_source,
        "computed_at_ms": payload.get("computed_at_ms"),
    }


class GovernanceGovernor:
    """The per-plane governor a worker is gated by.

    ``fsm`` is the plane's sole legality authority. ``store`` is the plane's
    Redis plane (read previous projections; write decisions via the ledger).
    ``inputs`` is the worker's declared input surface, passed as primitives
    at call time so the governor can prove the binding still matches.
    """

    def __init__(
        self,
        fsm: PlaneGovernanceFSM,
        store: Any,
        *,
        ledger: GovernanceLedger | None = None,
    ) -> None:
        self.fsm = fsm
        self.store = store
        self.ledger = ledger if ledger is not None else GovernanceLedger(store)

    @property
    def plane(self) -> GovernancePlane:
        return self.fsm.PLANE

    def manifest(self, substrate: str):
        return self.fsm.manifest_for(substrate)

    # ------------------------------------------------------------------
    # Observation — what the FSM sees (the output in Redis + the proposal)
    # ------------------------------------------------------------------

    async def observe(
        self,
        *,
        substrate: str,
        symbol: str,
        payload: Mapping[str, Any] | None = None,
        inputs: tuple[str, ...] | list[str] | None = None,
        read_previous: bool = True,
    ) -> GovernanceObservation:
        """Build the observation: previous Redis projection + proposed output.

        ``read_previous=False`` skips the Redis read (the FIRE gate needs no
        history). A FAILED read sets ``read_failed`` so the FSM can fail
        closed instead of treating an unreadable history as an absent one.
        """
        substrate = substrate.lower()
        symbol = symbol.upper()
        manifest = self.fsm.manifest_for(substrate)
        keyspace = manifest.writes_keyspace if manifest is not None else ""
        declared_inputs = tuple(inputs or (manifest.declared_inputs if manifest else ()))

        previous: Mapping[str, Any] | None = None
        read_failed = False
        if read_previous:
            try:
                if self.plane is GovernancePlane.ANALYSIS:
                    previous = await self.store.read_analysis_latest(substrate, symbol)
                else:
                    previous = await self.store.read_substrate_latest(substrate, symbol)
            except Exception:
                read_failed = True
                previous = None
            previous = previous if isinstance(previous, Mapping) else None

        latest_present = previous is not None
        previous_computed = (
            previous.get("computed_at_ms") if previous is not None else None
        )
        age_ms: int | None = None
        if isinstance(previous_computed, (int, float)):
            age_ms = int(time.time() * 1000) - int(previous_computed)

        fields = _payload_fields(payload)
        return GovernanceObservation(
            plane=self.plane,
            substrate=substrate,
            keyspace=keyspace,
            latest_present=latest_present,
            latest_age_ms=age_ms,
            previous_status=(previous.get("status") if previous else None),
            previous_schema_version=(
                previous.get("schema_version") if previous else None
            ),
            previous_computed_at_ms=(
                int(previous_computed)
                if isinstance(previous_computed, (int, float)) else None
            ),
            status=fields.get("status"),
            schema_version=fields.get("schema_version"),
            missing_inputs=fields.get("missing_inputs", ()),
            declared_inputs=declared_inputs,
            observed_inputs=declared_inputs,
            trigger_source=fields.get("trigger_source"),
            computed_at_ms=(
                int(fields["computed_at_ms"])
                if isinstance(fields.get("computed_at_ms"), (int, float)) else None
            ),
            read_failed=read_failed,
        )

    # ------------------------------------------------------------------
    # Evaluate / record — the two halves (evaluate many, record once)
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        gate: GovernanceGate,
        *,
        substrate: str,
        symbol: str,
        payload: Mapping[str, Any] | None = None,
        inputs: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[GovernanceObservation, GovernanceVerdict]:
        """Ask the FSM. No ledger write — the caller decides what sticks."""
        observation = await self.observe(
            substrate=substrate, symbol=symbol, payload=payload, inputs=inputs,
            # The FIRE gate reasons about the binding only; it needs no history.
            read_previous=gate is not GovernanceGate.FIRE,
        )
        event = GovernanceEvent(
            gate=gate, substrate=substrate.lower(), payload=payload,
        )
        return observation, self.fsm.decide(observation, event)

    async def record(
        self,
        gate: GovernanceGate,
        *,
        substrate: str,
        symbol: str,
        observation: GovernanceObservation,
        verdict: GovernanceVerdict,
    ) -> None:
        """Persist one decision (the one that sticks)."""
        await self.ledger.record(
            plane=self.plane.value,
            substrate=substrate,
            symbol=symbol,
            gate=gate.value,
            verdict=verdict,
            observation=observation,
        )

    # ------------------------------------------------------------------
    # Convenience — evaluate + record in one call (§tests, simple callers)
    # ------------------------------------------------------------------

    async def authorize(
        self,
        gate: GovernanceGate,
        *,
        substrate: str,
        symbol: str,
        payload: Mapping[str, Any] | None = None,
        inputs: tuple[str, ...] | list[str] | None = None,
        record: bool = True,
    ) -> GovernanceVerdict:
        observation, verdict = await self.evaluate(
            gate, substrate=substrate, symbol=symbol, payload=payload, inputs=inputs,
        )
        if record:
            await self.record(
                gate, substrate=substrate, symbol=symbol,
                observation=observation, verdict=verdict,
            )
        return verdict

    async def authorize_fire(
        self, *, substrate: str, symbol: str,
        payload: Mapping[str, Any] | None = None,
        inputs: tuple[str, ...] | list[str] | None = None,
        record: bool = True,
    ) -> GovernanceVerdict:
        return await self.authorize(
            GovernanceGate.FIRE, substrate=substrate, symbol=symbol,
            payload=payload, inputs=inputs, record=record,
        )

    async def authorize_publish(
        self, *, substrate: str, symbol: str,
        payload: Mapping[str, Any] | None = None,
        inputs: tuple[str, ...] | list[str] | None = None,
        record: bool = True,
    ) -> GovernanceVerdict:
        return await self.authorize(
            GovernanceGate.PUBLISH, substrate=substrate, symbol=symbol,
            payload=payload, inputs=inputs, record=record,
        )

    async def authorize_pg_write(
        self, *, substrate: str, symbol: str,
        payload: Mapping[str, Any] | None = None,
        inputs: tuple[str, ...] | list[str] | None = None,
        record: bool = True,
    ) -> GovernanceVerdict:
        return await self.authorize(
            GovernanceGate.PG_WRITE, substrate=substrate, symbol=symbol,
            payload=payload, inputs=inputs, record=record,
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def inspect(self, symbol: str) -> dict[str, Any]:
        report = await self.ledger.inspect(self.plane.value, symbol)
        report["ledger"] = self.ledger.stats()
        return report


__all__ = ["GovernanceGovernor", "GovernanceLedger"]
