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
    DispatchTicket,
    GovernanceEvent,
    GovernanceGate,
    GovernanceObservation,
    GovernancePlane,
    GovernanceVerdict,
)
from market_service.governance.fsm import DISPATCH_LEASE_S, PlaneGovernanceFSM
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
                    previous = await self.store.read(f"analysis.{substrate.lower()}", ids={"symbol": symbol})
                else:
                    previous = await self.store.read(f"substrate.{substrate.lower()}", ids={"symbol": symbol})
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
    # Dispatch guard — substrates ask, the FSM allows (pre-fire, no compute)
    # ------------------------------------------------------------------

    def _dispatch_lease_key(self, plane: str, symbol: str) -> str:
        prefix = str(getattr(self.store, "prefix", "marketflow") or "marketflow")
        return f"{prefix}:governance:dispatch:lease:{str(plane).lower()}:{str(symbol).upper()}"

    async def _dispatch_ages(
        self, *, symbol: str, names: list[str],
    ) -> dict[str, int | None]:
        """Latest projection ages per substrate (None = no history)."""
        ages: dict[str, int | None] = {}
        is_analysis = self.plane is GovernancePlane.ANALYSIS
        for name in names:
            try:
                if is_analysis:
                    entry = await self.store.read(f"analysis.{name.lower()}", ids={"symbol": symbol})
                else:
                    entry = await self.store.read(f"substrate.{name.lower()}", ids={"symbol": symbol})
            except Exception:
                ages[name] = None
                continue
            if not isinstance(entry, dict):
                ages[name] = None
                continue
            age = entry.get("age_ms")
            if isinstance(age, (int, float)):
                ages[name] = int(age)
                continue
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            computed = (payload or {}).get("computed_at_ms")
            if isinstance(computed, (int, float)):
                ages[name] = int(time.time() * 1000) - int(computed)
            else:
                ages[name] = None
        return ages

    async def authorize_dispatch(
        self,
        *,
        symbol: str,
        requested: list[str] | tuple[str, ...] | None = None,
        dry_run: bool = False,
    ) -> DispatchTicket:
        """Pre-fire ticket: due-check, quarantine, batch, cross-plane defer.

        Never spends worker compute, never blocks reads, never sleeps.
        Fail-open is behavioral, not labelled: per-name read failures
        degrade to cold_start, so an unreadable store bottoms out as
        all-due `dispatch-allow` and the invoke still happens. Dry runs
        never hold a lease. The ooAgent treats the ticket as advisory and
        always serves reads from cache.
        """
        symbol_word = str(symbol).upper()
        registered = [str(n).lower() for n in self.fsm.substrates()]
        names = [str(n).lower() for n in (requested or registered)]
        ages = await self._dispatch_ages(symbol=symbol_word, names=names)
        try:
            denials = await self.store.read("governance.denials", ids={"plane": self.plane.value, "symbol": symbol_word}, count=20)
        except Exception:
            denials = []
        quarantined = self.fsm.quarantine_from_denials(denials)
        other = ("analysis" if self.plane is GovernancePlane.CALCULATION else "calculation")
        try:
            other_busy = bool(await self.store.redis.exists(
                self._dispatch_lease_key(other, symbol_word)))
        except Exception:
            other_busy = False
        ticket = self.fsm.decide_dispatch(
            symbol=symbol_word, requested=names or None, ages_ms=ages,
            quarantined=quarantined, other_plane_busy=other_busy)
        if not ticket.allowed or ticket.deferred:
            return ticket
        if dry_run:
            return ticket
        try:
            acquired = await self.store.redis.set(
                self._dispatch_lease_key(self.plane.value, symbol_word),
                "1", nx=True, ex=DISPATCH_LEASE_S)
        except Exception:
            return ticket
        if not acquired:
            return DispatchTicket(
                plane=self.plane.value, symbol=symbol_word, allowed=(),
                denied=ticket.denied, batches=(), deferred=True,
                defer_reason=("own plane holds an active dispatch lease; await "
                              "lease expiry then request the next batch"),
                rule="dispatch-deferred-own-plane",
                reason="one compute batch at a time per plane",
            )
        return ticket

    async def release_dispatch_lease(self, *, symbol: str) -> None:
        """Release this plane's dispatch lease early (next batch may fire)."""
        try:
            await self.store.redis.delete(
                self._dispatch_lease_key(self.plane.value, symbol))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def inspect(self, symbol: str) -> dict[str, Any]:
        report = await self.ledger.inspect(self.plane.value, symbol)
        report["ledger"] = self.ledger.stats()
        return report


__all__ = ["GovernanceGovernor", "GovernanceLedger"]
