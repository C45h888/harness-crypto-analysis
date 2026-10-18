"""Plane governance FSM — the shared PURE mechanism over plane rule sets.

SEMANTIC JURISDICTION: this file owns the MECHANISM of a plane governance
FSM and nothing plane-specific:

* the manifest lookup (authority is always resolved by substrate name);
* the common deterministic rule set every plane inherits;
* the ``decide`` dispatch over the three gates;
* the ``self_check`` / ``render`` viewability surface.

It does NOT own the rules that differ per plane — ``calculation_fsm`` and
``analysis_fsm`` own those (each is the sole source of its plane's legality).

Purity: no I/O, no clock, no worker import, no plane import. Every decision
is a pure function of ``(manifest, observation, event)``, so the same inputs
always yield the same verdict (replay-safe), exactly like
``nooa_harness/engine/fsm.py``\\ 's ``AgenticLoopMembrane``.
"""

from __future__ import annotations

from typing import Any, Mapping

from market_service.governance.contracts import (
    DispatchDecision,
    DispatchTicket,
    GovernanceEvent,
    GovernanceGate,
    GovernanceObservation,
    GovernancePlane,
    GovernanceVerdict,
    SubstrateManifest,
)

#: Cross-plane dispatch lease TTL (bounded await, never indefinite).
DISPATCH_LEASE_S = 30

#: A binding/schema denial quarantines the substrate from dispatch until fixed.
DISPATCH_QUARANTINE_RULES = (
    "binding-drift", "schema-drift", "manifest-invalid", "unknown-substrate",
)

#: The common rule ids every plane FSM can emit. A plane FSM adds its own
#: ids via ``_plane_rules``; ``self_check`` proves no id is dead.
COMMON_RULE_IDS: tuple[str, ...] = (
    "allow",
    "unknown-substrate",
    "plane-mismatch",
    "manifest-invalid",
    "keyspace-trespass",
    "status-invalid",
    "schema-drift",
    "output-fabricated",
    "trigger-missing",
    "out-of-order-write",
    "binding-drift",
    "observation-unavailable",
)


class PlaneGovernanceFSM:
    """Deterministic legality authority over ONE worker plane.

    Stateless by construction: the manifests are the constitution, the
    observations are the facts, and ``decide`` is the pure judgment. No
    mutating method exists; the instance is safe to share.
    """

    PLANE: GovernancePlane
    #: Plane-specific rule ids contributed by ``_plane_rules`` (subclasses).
    PLANE_RULE_IDS: tuple[str, ...] = ()

    def __init__(
        self,
        manifests: Mapping[str, SubstrateManifest],
        *,
        known_sections: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
        status_vocabulary: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
    ) -> None:
        self._manifests: dict[str, SubstrateManifest] = {
            str(name).lower(): manifest for name, manifest in manifests.items()
        }
        # The graph sections this plane is allowed to claim, injected by the
        # manifest builder (the one derivation seam). Empty = no claim check.
        self._known_sections: frozenset[str] = frozenset(known_sections)
        # The status vocabulary owned by the worker contract, injected by the
        # derivation seam (never mirrored here). Empty = no vocabulary check.
        self._status_vocabulary: frozenset[str] = frozenset(status_vocabulary)

    # ------------------------------------------------------------------
    # Authority lookup
    # ------------------------------------------------------------------

    def manifest_for(self, substrate: str) -> SubstrateManifest | None:
        return self._manifests.get(str(substrate).lower())

    def substrates(self) -> tuple[str, ...]:
        return tuple(sorted(self._manifests))

    def manifests(self) -> tuple[SubstrateManifest, ...]:
        return tuple(self._manifests[name] for name in self.substrates())

    def known_sections(self) -> frozenset[str]:
        """The graph sections this plane may claim (injected by the manifest
        builder — governance itself never imports the plane)."""
        return self._known_sections

    def status_vocabulary(self) -> frozenset[str]:
        """The plane's owned status words (injected; empty = unchecked)."""
        return self._status_vocabulary

    # ------------------------------------------------------------------
    # The decision
    # ------------------------------------------------------------------

    def decide(
        self, observation: GovernanceObservation, event: GovernanceEvent,
    ) -> GovernanceVerdict:
        """Judge one gate. Never raises; always names its rule."""
        manifest = self.manifest_for(event.substrate)
        if manifest is None:
            return GovernanceVerdict.deny(
                "unknown-substrate",
                f"{event.substrate!r} is not a registered {self.PLANE.value} "
                f"substrate (registered: {list(self.substrates())})",
            )
        if not manifest.is_valid():
            return GovernanceVerdict.deny(
                "manifest-invalid",
                f"manifest for {manifest.substrate!r} carries no evidence "
                f"layer / keyspace / status family",
            )
        if manifest.plane is not self.PLANE or observation.plane is not self.PLANE:
            return GovernanceVerdict.deny(
                "plane-mismatch",
                f"{manifest.substrate!r} is a {manifest.plane.value} substrate; "
                f"this FSM governs {self.PLANE.value}",
            )

        if event.gate is GovernanceGate.FIRE:
            verdict = self._check_fire(manifest, observation, event)
        elif event.gate is GovernanceGate.PUBLISH:
            verdict = self._check_publish(manifest, observation, event)
        elif event.gate is GovernanceGate.PG_WRITE:
            verdict = self._check_pg_write(manifest, observation, event)
        else:  # pragma: no cover - enum is total
            return GovernanceVerdict.deny(
                "unknown-gate", f"unknown governance gate {event.gate!r}",
            )
        if not verdict.allowed:
            return verdict

        plane_verdict = self._plane_rules(manifest, observation, event)
        if plane_verdict is not None and not plane_verdict.allowed:
            return plane_verdict
        findings = verdict.findings
        if plane_verdict is not None:
            findings = (*verdict.findings, *plane_verdict.findings)
        return GovernanceVerdict.allow(
            verdict.rule, verdict.reason, findings=findings, remedy=verdict.remedy,
        )

    # ------------------------------------------------------------------
    # Gate checks (common rules — every plane inherits these)
    # ------------------------------------------------------------------

    def _check_fire(
        self,
        manifest: SubstrateManifest,
        observation: GovernanceObservation,
        event: GovernanceEvent,
    ) -> GovernanceVerdict:
        """May this substrate compute at all?

        A fire is legal when the substrate is registered for this plane, its
        manifest declares a non-empty evidence layer, and the worker's RUNTIME
        input surface still matches the authority record the FSM holds.

        ``binding-drift`` is the real invariant here, not a formality: the
        declaration the running worker reports is compared against the
        manifest derived at governor-build time. It catches a worker whose
        input surface changed after the manifest was derived, a governor
        attached to the wrong worker class, and a stale/mismatched manifest.
        """
        drifted = tuple(
            i for i in observation.declared_inputs
            if i not in manifest.declared_inputs
        )
        missing = tuple(
            i for i in manifest.declared_inputs
            if i not in observation.declared_inputs
        )
        if drifted or missing:
            return GovernanceVerdict.deny(
                "binding-drift",
                f"{manifest.substrate!r} reports inputs {list(observation.declared_inputs)} "
                f"but the authority record declares {list(manifest.declared_inputs)}"
                + (f" (undeclared: {list(drifted)})" if drifted else "")
                + (f" (missing: {list(missing)})" if missing else ""),
                remedy="re-derive the manifest from the running plane, or fix the "
                       "worker's declaration so the binding matches",
            )
        return GovernanceVerdict.allow(
            "allow",
            f"{manifest.substrate!r} may fire on its declared evidence layer "
            f"{manifest.primary_stream!r}",
        )

    def _check_publish(
        self,
        manifest: SubstrateManifest,
        observation: GovernanceObservation,
        event: GovernanceEvent,
    ) -> GovernanceVerdict:
        """May this payload enter the plane's keyspace?"""
        verdict = self._check_output(manifest, observation, event)
        if not verdict.allowed:
            return verdict
        return GovernanceVerdict.allow(
            "allow",
            f"{manifest.substrate!r} may publish into "
            f"{manifest.writes_keyspace!r}",
        )

    def _check_pg_write(
        self,
        manifest: SubstrateManifest,
        observation: GovernanceObservation,
        event: GovernanceEvent,
    ) -> GovernanceVerdict:
        """May this fire be durably recorded? The sanity gate before PG.

        Strictly stronger than PUBLISH: a durable row must additionally carry
        WHY it fired (a trigger source). An unattributable ledger row is not
        evidence — it is noise the durable ledger must never hold.
        """
        verdict = self._check_output(manifest, observation, event)
        if not verdict.allowed:
            return verdict
        if not str(observation.trigger_source or "").strip():
            return GovernanceVerdict.deny(
                "trigger-missing",
                f"{manifest.substrate!r} payload has no trigger source; a durable "
                f"row must record WHY it fired",
                remedy="populate payload.trigger.source before the ledger write",
            )
        return GovernanceVerdict.allow(
            "allow",
            f"{manifest.substrate!r} may be durably recorded with trigger "
            f"{observation.trigger_source!r}",
        )

    def _check_output(
        self,
        manifest: SubstrateManifest,
        observation: GovernanceObservation,
        event: GovernanceEvent,
    ) -> GovernanceVerdict:
        """The shared output-sanity rules (keyspace, status, schema, honesty)."""
        if observation.read_failed:
            return GovernanceVerdict.deny(
                "observation-unavailable",
                f"{manifest.substrate!r}: the plane's current projection could not "
                f"be read, so the FSM cannot prove this output is in order",
                remedy="restore the plane's Redis read path; an unverifiable "
                       "output is not published",
            )
        if observation.keyspace != manifest.writes_keyspace:
            return GovernanceVerdict.deny(
                "keyspace-trespass",
                f"{manifest.substrate!r} targets keyspace "
                f"{observation.keyspace!r} but owns "
                f"{manifest.writes_keyspace!r}",
                remedy=f"write only the {manifest.writes_keyspace!r} keyspace",
            )
        if observation.status is not None and observation.status not in manifest.status_family:
            return GovernanceVerdict.deny(
                "status-invalid",
                f"{manifest.substrate!r} proposes status {observation.status!r}; "
                f"the declared family is {sorted(manifest.status_family)}",
            )
        if (
            observation.schema_version is not None
            and observation.schema_version != manifest.schema_version
        ):
            return GovernanceVerdict.deny(
                "schema-drift",
                f"{manifest.substrate!r} proposes schema_version "
                f"{observation.schema_version} but the registry declares "
                f"{manifest.schema_version}",
            )
        if (
            observation.status == "insufficient_data"
            and not observation.missing_inputs
        ):
            return GovernanceVerdict.deny(
                "output-fabricated",
                f"{manifest.substrate!r} claims insufficient_data without naming "
                f"missing_inputs — an un-evidenced degradation",
                remedy="name the missing input(s), or compute a real output",
            )
        if (
            observation.previous_computed_at_ms is not None
            and observation.computed_at_ms is not None
            and observation.computed_at_ms < observation.previous_computed_at_ms
        ):
            return GovernanceVerdict.deny(
                "out-of-order-write",
                f"{manifest.substrate!r} proposes computed_at_ms "
                f"{observation.computed_at_ms} which precedes the projection "
                f"already in Redis ({observation.previous_computed_at_ms})",
                remedy="drop the stale fire; a projection must not move backwards",
            )
        return GovernanceVerdict.allow("allow", "output shape is legal")

    # ------------------------------------------------------------------
    # Plane rules (subclass hook)
    # ------------------------------------------------------------------

    def _plane_rules(
        self,
        manifest: SubstrateManifest,
        observation: GovernanceObservation,
        event: GovernanceEvent,
    ) -> GovernanceVerdict | None:
        """Plane-specific legality. ``None`` = no opinion (common rules stand)."""
        return None

    # ------------------------------------------------------------------
    # Dispatch guard (PURE pre-fire: should we spend compute at all?)
    # Substrates ask via the governor; the FSM checks trigger predicates
    # derived from manifests (cooldown / staleness / quarantine) and answers
    # with a ticket. No I/O, no clock authority — ages and occupancy arrive
    # as inputs, so the same inputs always yield the same ticket.
    # ------------------------------------------------------------------

    def dispatch_group_rank(self, substrate: str) -> int:
        """Batch order rank for one substrate (plane override hook)."""
        return 1

    def dispatch_intra_parallel(self) -> int:
        """Intra-plane parallelism for this plane (plane override hook)."""
        return 4

    @staticmethod
    def quarantine_from_denials(
        denials: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None,
    ) -> frozenset[str]:
        """Substrates quarantined by recent governance denials (pure)."""
        out: set[str] = set()
        for entry in denials or ():
            if not isinstance(entry, Mapping):
                continue
            rule = str(entry.get("rule") or "")
            substrate = str(entry.get("substrate") or "").lower()
            if substrate and rule in DISPATCH_QUARANTINE_RULES:
                out.add(substrate)
        return frozenset(out)

    def decide_dispatch(
        self,
        *,
        symbol: str,
        requested: list[str] | tuple[str, ...] | None,
        ages_ms: Mapping[str, int | None],
        quarantined: frozenset[str] | set[str] | None = None,
        other_plane_busy: bool = False,
    ) -> DispatchTicket:
        """Pure dispatch plan: due-check, quarantine, batch, cross-plane defer."""
        plane_word = self.PLANE.value
        symbol_word = str(symbol).upper()
        reg = [str(n).lower() for n in self.substrates()]
        reg_set = set(reg)
        quarantine = frozenset(str(s).lower() for s in (quarantined or frozenset()))
        if other_plane_busy:
            return DispatchTicket(
                plane=plane_word, symbol=symbol_word, allowed=(), denied=(),
                batches=(), deferred=True,
                defer_reason=("other plane holds the dispatch lease; await lease "
                              "expiry then request the next batch (reads remain allowed)"),
                rule="dispatch-deferred-cross-plane",
                reason="cross-plane mutex: one compute batch at a time",
            )
        names = [str(n).lower() for n in (requested or reg)]
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        allowed: list[tuple[str, int]] = []
        denied: list[DispatchDecision] = []
        for name in ordered:
            manifest = self.manifest_for(name)
            if manifest is None or name not in reg_set:
                denied.append(DispatchDecision(
                    substrate=name, allowed=False,
                    reason=f"{name!r} is not a registered {plane_word} worker",
                    rule="unknown-substrate"))
                continue
            if name in quarantine:
                denied.append(DispatchDecision(
                    substrate=name, allowed=False,
                    reason=(f"{name!r} quarantined by recent binding/schema denial; "
                            "fix the manifest binding before spending compute"),
                    rule="quarantined-binding"))
                continue
            age = ages_ms.get(name)
            cooldown = int(manifest.cooldown_s or 30)
            stale = int(manifest.staleness_s or 120)
            if age is None:
                allowed.append((name, 0))
                continue
            try:
                age_int = int(age)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                allowed.append((name, 0))
                continue
            if age_int >= stale * 1000:
                allowed.append((name, 0))
            elif age_int >= cooldown * 1000:
                allowed.append((name, 1))
            else:
                denied.append(DispatchDecision(
                    substrate=name, allowed=False,
                    reason=(f"{name!r} cooldown-hot (age {age_int}ms < cooldown "
                            f"{cooldown * 1000}ms); no compute spent"),
                    rule="cooldown",
                    retry_after_ms=max(0, cooldown * 1000 - age_int)))
        batch0 = sorted([n for n, b in allowed if b == 0],
                        key=lambda n: (self.dispatch_group_rank(n), n))
        batch1 = sorted([n for n, b in allowed if b == 1],
                        key=lambda n: (self.dispatch_group_rank(n), n))
        batches: list[tuple[str, ...]] = []
        if batch0:
            batches.append(tuple(batch0))
        if batch1:
            batches.append(tuple(batch1))
        flat = tuple([*batch0, *batch1])
        if not flat:
            reason = "no requested worker is due (all cooldown-hot or quarantined)"
            if denied and all(d.rule == "quarantined-binding" for d in denied):
                reason = "all requested workers quarantined by binding/schema denial"
            return DispatchTicket(
                plane=plane_word, symbol=symbol_word, allowed=(),
                denied=tuple(denied), batches=(), deferred=False,
                rule="dispatch-none-due", reason=reason)
        return DispatchTicket(
            plane=plane_word, symbol=symbol_word, allowed=flat,
            denied=tuple(denied), batches=tuple(batches), deferred=False,
            rule="dispatch-allow",
            reason=(f"{len(flat)} due in {len(batches)} batch(es); "
                    f"{len(denied)} denied without compute"))

    # ------------------------------------------------------------------
    # Viewability
    # ------------------------------------------------------------------

    def rule_ids(self) -> tuple[str, ...]:
        return (*COMMON_RULE_IDS, *self.PLANE_RULE_IDS)

    def render(self) -> str:
        lines = [f"[{self.PLANE.value} governance] substrates: {list(self.substrates())}"]
        for manifest in self.manifests():
            lines.append(
                f"  {manifest.substrate:<16} layer={manifest.primary_stream:<22} "
                f"keyspace={manifest.writes_keyspace:<10} "
                f"deps={list(manifest.dependencies)} "
                f"cadence={manifest.cooldown_s}s/{manifest.staleness_s}s"
            )
        lines.append(f"  rules: {list(self.rule_ids())}")
        return "\n".join(lines)

    def self_check(self) -> None:
        """The constitution is not silently dead or silently broken."""
        if not isinstance(self.PLANE, GovernancePlane):
            raise ValueError(f"{type(self).__name__}.PLANE must be a GovernancePlane")
        if not self._manifests:
            raise ValueError(f"{type(self).__name__}: no manifests registered")
        unknown = self.known_sections()
        for manifest in self.manifests():
            if not manifest.is_valid():
                raise ValueError(
                    f"invalid manifest for {manifest.substrate!r} "
                    f"(no evidence layer / keyspace / status family)"
                )
            if manifest.plane is not self.PLANE:
                raise ValueError(
                    f"manifest {manifest.substrate!r} is {manifest.plane.value}, "
                    f"not {self.PLANE.value}"
                )
            if not manifest.primary_stream:
                raise ValueError(
                    f"{manifest.substrate!r}: no primary evidence layer declared"
                )
            if (
                self._status_vocabulary
                and not set(manifest.status_family) <= set(self._status_vocabulary)
            ):
                raise ValueError(
                    f"{manifest.substrate!r}: status family borrows words from "
                    f"outside the owned vocabulary {sorted(self._status_vocabulary)}"
                )
            if unknown:
                stray = [s for s in manifest.allowed_sections if s not in unknown]
                if stray:
                    raise ValueError(
                        f"{manifest.substrate!r}: claims sections {stray} that do "
                        f"not exist in the declared graph"
                    )
        # Every declared plane rule id must be a real id (no typos, no dead ids).
        for rid in self.PLANE_RULE_IDS:
            if not str(rid).strip():
                raise ValueError(f"{type(self).__name__}: empty plane rule id")


__all__ = ["COMMON_RULE_IDS", "DISPATCH_LEASE_S", "DISPATCH_QUARANTINE_RULES", "PlaneGovernanceFSM"]
