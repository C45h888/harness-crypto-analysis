"""Analysis-plane core — the plane seam over the shared substrate machinery.

The substrate worker core (fire cascade, reader, supervisor) is reused
byte-for-byte. This module supplies ONLY what differs per plane:

* ``PLANE = "analysis"`` → the base routes own-state reads/publishes and the
  supervisor key into the ``analysis:`` keyspace (see
  ``substrate_worker.core.base``);
* ``GROUP_PREFIX = "analysis"`` → consumer groups on the dependency substrate
  state streams are human-identifiable;
* ``_build_evidence`` override → analysis evidence is the COMPOSED dependency
  latests (attached by ``_attach_dependencies``) plus the derivative cache —
  never a raw REST evidence window.

Deterministic trigger doctrine (no polling):
* Wake   — ONE blocking XREADGROUP on the primary dependency substrate's
           state stream. A substrate fire IS the arrival event.
* Fire   — a market-semantic probe over the composed state, evaluated against
           the worker's OWN last persisted projection (probe-against-own-state).
           Substrate re-fires that carry no semantic change do NOT fire the
           analysis worker — cooldown re-fires and staleness heartbeats of the
           dependency are wake events, never fire causes by themselves.
* Floor  — the core's L3 staleness heartbeat keeps projections alive in
           quiet markets; the semantic probe is the ceiling on fire frequency.
"""

from __future__ import annotations

from typing import Any

from market_service.substrate_worker.core import SubstrateWorkerCore


class AnalysisPlaneMixin:
    """Plane seam: analysis keyspace + composed-evidence binding."""

    PLANE = "analysis"
    GROUP_PREFIX = "analysis"

    async def _build_evidence(
        self, now_ms: int, *, rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Composed evidence: the dependency latests ride in via
        ``_attach_dependencies`` (staleness-gated) and the derivative cache
        via ``_attach_derivatives``. The consumed trigger entries are
        accounted (count only) — never embedded: the dependency projections
        are the freshest truth, the entries are just the wake."""
        return {
            "observed_at_ms": now_ms,
            "trigger_rows": len(rows or []),
        }