"""Calculation governance FSM — the SOLE legality authority of the calc plane.

SEMANTIC JURISDICTION: this file owns every rule that is specific to the
calculation worker plane. The shared mechanism (gate dispatch, common output
sanity, viewability) lives in ``fsm.PlaneGovernanceFSM``; the rules that make
the calculation plane *itself* live here and nowhere else.

Plane rules owned here:
  - ``section-out-of-graph`` — a calculation substrate may only claim
    sections that exist in the declared ``SUBSTRATE_GRAPH``.

The calc-plane FSM never imports a worker, never imports Redis. It judges
``(manifest, observation, event)`` only.
"""

from __future__ import annotations

from market_service.governance.contracts import (
    GovernanceEvent,
    GovernanceObservation,
    GovernancePlane,
    GovernanceVerdict,
    SubstrateManifest,
)
from market_service.governance.fsm import PlaneGovernanceFSM


class CalculationGovernanceFSM(PlaneGovernanceFSM):
    """Deterministic legality authority over the 12 calculation substrates."""

    PLANE = GovernancePlane.CALCULATION
    PLANE_RULE_IDS = ("section-out-of-graph",)

    def _plane_rules(
        self,
        manifest: SubstrateManifest,
        observation: GovernanceObservation,
        event: GovernanceEvent,
    ) -> GovernanceVerdict | None:
        """Calculation-plane legality beyond the common rules."""
        unknown = self.known_sections()
        if unknown and manifest.allowed_sections:
            stray = [s for s in manifest.allowed_sections if s not in unknown]
            if stray:
                return GovernanceVerdict.deny(
                    "section-out-of-graph",
                    f"{manifest.substrate!r} claims section(s) {stray} that the "
                    f"declared SUBSTRATE_GRAPH does not contain",
                    remedy="register the section in the graph, or drop the claim",
                )
        return None


__all__ = ["CalculationGovernanceFSM"]
