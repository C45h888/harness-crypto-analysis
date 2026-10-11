"""Manifest derivation — the ONE seam that reads a plane into authority.

SEMANTIC JURISDICTION: this module is the single place in the governance
package that touches a worker plane. It reads the *declared* class attributes
of the registered workers (``SUBSTRATE_NAME``, ``INPUT_STREAMS``,
``DEPENDENCIES``, ``DERIVATIVE_INPUTS``, ``CADENCE``, ``PLANE``) plus the
declared ``SUBSTRATE_GRAPH`` and turns them into ``SubstrateManifest``
authority records.

It never instantiates a worker, never calls one, never imports a
``SubstrateWorkerCore`` instance. It reads class-level declarations only.
Every other governance module stays plane-free, so the FSMs judge authority
records and never workers — which is exactly the doctrine:

    FSM -> substrate manifest -> worker binding.

No rule is mirrored here. Cadence, dependencies and evidence layers are read
from their declaring owner (the worker contract) rather than re-typed, so the
charter's "no mirrored constants" rule holds.
"""

from __future__ import annotations

from typing import Any

from market_service.governance.contracts import (
    GovernancePlane,
    SubstrateManifest,
)

DEFAULT_COOLDOWN_S = 30
DEFAULT_STALENESS_S = 120


def plane_status_vocabulary() -> tuple[str, ...]:
    """The status words OWNED by the worker contract (import, never mirror).

    STATE_CHARTER_SPEC.md: planes consume owners by IMPORT. The owner is
    ``substrate_worker.contracts.PAYLOAD_STATUSES`` (the analysis plane reuses
    the same payload contract), so governance reads it from there and injects
    it rather than re-declaring it.
    """
    from market_service.substrate_worker.contracts import PAYLOAD_STATUSES

    return tuple(PAYLOAD_STATUSES)


def _plane_of(worker_cls: type) -> GovernancePlane:
    """Map a worker class's ``PLANE`` word onto the governance plane."""
    word = str(getattr(worker_cls, "PLANE", "substrate")).lower()
    return GovernancePlane.ANALYSIS if word == "analysis" else GovernancePlane.CALCULATION


def _keyspace_of(plane: GovernancePlane) -> str:
    return "analysis" if plane is GovernancePlane.ANALYSIS else "substrate"


def _primary_stream(worker_cls: type) -> str:
    streams = tuple(getattr(worker_cls, "INPUT_STREAMS", ()) or ())
    return str(streams[0]) if streams else ""


def _cadence(worker_cls: type) -> tuple[int, int, tuple[str, ...]]:
    profile = getattr(worker_cls, "CADENCE", None)
    if profile is None:
        return DEFAULT_COOLDOWN_S, DEFAULT_STALENESS_S, ()
    return (
        int(getattr(profile, "cooldown_s", DEFAULT_COOLDOWN_S)),
        int(getattr(profile, "staleness_s", DEFAULT_STALENESS_S)),
        tuple(getattr(profile, "rollovers", ()) or ()),
    )


def _sections_for(substrate: str, graph: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    """Graph sections this substrate owns (by name or by ``analysis.<name>``)."""
    owned: list[str] = []
    for section, entry in graph.items():
        owners = entry.get("substrates") or ()
        if substrate in owners or f"analysis.{substrate}" in owners:
            owned.append(section)
    return tuple(sorted(owned))


def _manifest_from(
    worker_cls: type,
    graph: dict[str, dict[str, Any]],
    schema_version: int,
    status_vocabulary: tuple[str, ...],
) -> SubstrateManifest:
    plane = _plane_of(worker_cls)
    substrate = str(getattr(worker_cls, "SUBSTRATE_NAME", "")).lower()
    cooldown_s, staleness_s, rollovers = _cadence(worker_cls)
    streams = tuple(getattr(worker_cls, "INPUT_STREAMS", ()) or ())
    return SubstrateManifest(
        plane=plane,
        substrate=substrate,
        primary_stream=_primary_stream(worker_cls),
        writes_keyspace=_keyspace_of(plane),
        input_streams=streams,
        dependencies=tuple(getattr(worker_cls, "DEPENDENCIES", ()) or ()),
        derived_inputs=tuple(getattr(worker_cls, "DERIVATIVE_INPUTS", ()) or ()),
        allowed_sections=_sections_for(substrate, graph),
        schema_version=schema_version,
        status_family=frozenset(status_vocabulary),
        cooldown_s=cooldown_s,
        staleness_s=staleness_s,
        rollovers=rollovers,
    )


def _substrate_graph() -> dict[str, dict[str, Any]]:
    from market_service.calculations.composition import SUBSTRATE_GRAPH

    return SUBSTRATE_GRAPH


def _schema_version() -> int:
    from market_service.substrate_worker.contracts import (
        SUBSTRATE_STATE_SCHEMA_VERSION,
    )

    return int(SUBSTRATE_STATE_SCHEMA_VERSION)


def build_calculation_manifest() -> dict[str, SubstrateManifest]:
    """Authority records for every registered calculation substrate.

    Derived from ``WORKER_REGISTRY`` (the declared worker classes) — the
    registry is the plane's own declaration of what exists.
    """
    from market_service.substrate_worker import WORKER_REGISTRY

    graph = _substrate_graph()
    schema_version = _schema_version()
    vocabulary = plane_status_vocabulary()
    out = {
        name: _manifest_from(cls, graph, schema_version, vocabulary)
        for name, cls in WORKER_REGISTRY.items()
    }
    return {
        name: manifest
        for name, manifest in out.items()
        if manifest.plane is GovernancePlane.CALCULATION
    }


def build_analysis_manifest() -> dict[str, SubstrateManifest]:
    """Authority records for every registered analysis worker."""
    from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY

    graph = _substrate_graph()
    schema_version = _schema_version()
    vocabulary = plane_status_vocabulary()
    return {
        name: _manifest_from(cls, graph, schema_version, vocabulary)
        for name, cls in ANALYSIS_WORKER_REGISTRY.items()
    }


def known_graph_sections() -> frozenset[str]:
    """The declared section vocabulary both planes are checked against."""
    return frozenset(_substrate_graph().keys())


__all__ = [
    "build_analysis_manifest",
    "build_calculation_manifest",
    "known_graph_sections",
    "plane_status_vocabulary",
]
