"""Plane governance — two deterministic legality engines for the worker planes.

Doctrine (docs/PLANE_GOVERNANCE_SPEC.md):

* ``CalculationGovernanceFSM`` and ``AnalysisGovernanceFSM`` are TWO SEPARATE
  governance engines, one per worker plane. Each is the SOLE source of its
  plane's legality.
* They are entirely deterministic: no timers, no sleep, no clock authority.
  A wake is a Redis stream arrival or an explicit invoke.
* They check the outputs that live in Redis and gate the durable Postgres
  write (the sanity gate before the write).
* They have NO contractual connection to the workers. Authority flows
  FSM -> substrate manifest -> worker binding; the worker is gated at its
  fire seams by duck-typed calls with primitives only.

Import cost: importing this package is cheap. The builders below import the
worker planes lazily, so a caller that only needs the contracts or the FSMs
never drags the worker package in.
"""

from __future__ import annotations

from typing import Any

from market_service.governance.analysis_fsm import AnalysisGovernanceFSM
from market_service.governance.calculation_fsm import CalculationGovernanceFSM
from market_service.governance.contracts import (
    GOVERNANCE_LEDGER_SCHEMA_VERSION,
    DispatchDecision,
    DispatchTicket,
    GovernanceEvent,
    GovernanceGate,
    GovernanceLedgerRecord,
    GovernanceObservation,
    GovernancePlane,
    GovernanceVerdict,
    SubstrateManifest,
)
from market_service.governance.fsm import (
    COMMON_RULE_IDS,
    DISPATCH_LEASE_S,
    DISPATCH_QUARANTINE_RULES,
    PlaneGovernanceFSM,
)
from market_service.governance.gates import GovernanceGovernor
from market_service.governance.ledger import GovernanceLedger


def build_calculation_governor(
    store: Any, pg_store: Any | None = None,
) -> GovernanceGovernor:
    """The calculation plane's governor (one FSM, one ledger)."""
    from market_service.governance.manifest import (
        build_calculation_manifest,
        known_graph_sections,
        plane_status_vocabulary,
    )

    manifests = build_calculation_manifest()
    fsm = CalculationGovernanceFSM(
        manifests,
        known_sections=known_graph_sections(),
        status_vocabulary=plane_status_vocabulary(),
    )
    fsm.self_check()
    return GovernanceGovernor(
        fsm, store, ledger=GovernanceLedger(store, pg_store),
    )


def build_analysis_governor(
    store: Any, pg_store: Any | None = None,
) -> GovernanceGovernor:
    """The analysis plane's governor (one FSM, one ledger)."""
    from market_service.governance.manifest import (
        build_analysis_manifest,
        known_graph_sections,
        plane_status_vocabulary,
    )

    manifests = build_analysis_manifest()
    fsm = AnalysisGovernanceFSM(
        manifests,
        known_sections=known_graph_sections(),
        status_vocabulary=plane_status_vocabulary(),
    )
    fsm.self_check()
    return GovernanceGovernor(
        fsm, store, ledger=GovernanceLedger(store, pg_store),
    )


def governor_for_plane(
    plane: str, store: Any, pg_store: Any | None = None,
) -> GovernanceGovernor:
    """Build the governor for a plane word ('calculation' | 'analysis')."""
    if str(plane).lower() == GovernancePlane.ANALYSIS.value:
        return build_analysis_governor(store, pg_store)
    return build_calculation_governor(store, pg_store)


__all__ = [
    "COMMON_RULE_IDS",
    "DISPATCH_LEASE_S",
    "DISPATCH_QUARANTINE_RULES",
    "GOVERNANCE_LEDGER_SCHEMA_VERSION",
    "AnalysisGovernanceFSM",
    "CalculationGovernanceFSM",
    "DispatchDecision",
    "DispatchTicket",
    "GovernanceEvent",
    "GovernanceGate",
    "GovernanceGovernor",
    "GovernanceLedger",
    "GovernanceLedgerRecord",
    "GovernanceObservation",
    "GovernancePlane",
    "GovernanceVerdict",
    "PlaneGovernanceFSM",
    "SubstrateManifest",
    "build_analysis_governor",
    "build_calculation_governor",
    "governor_for_plane",
]
