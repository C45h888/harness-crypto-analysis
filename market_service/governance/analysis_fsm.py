"""Analysis governance FSM — the SOLE legality authority of the analysis plane.

SEMANTIC JURISDICTION: this file owns every rule that is specific to the
analysis worker plane. The shared mechanism lives in
``fsm.PlaneGovernanceFSM``; the rules that make the analysis plane *itself*
live here and nowhere else.

Plane rules owned here:
  - ``analysis-no-substrate-root`` — an analysis worker is a REDUCTION of
    substrate state and must therefore declare at least one
    ``substrate:<name>`` input. An analysis worker with no substrate root is
    not an analysis worker; it is an undeclared re-derivation of raw data.

The analysis FSM never imports a worker, never imports Redis. It judges
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


class AnalysisGovernanceFSM(PlaneGovernanceFSM):
    """Deterministic legality authority over the analysis substrates."""

    PLANE = GovernancePlane.ANALYSIS
    PLANE_RULE_IDS = ("analysis-no-substrate-root",)

    def _plane_rules(
        self,
        manifest: SubstrateManifest,
        observation: GovernanceObservation,
        event: GovernanceEvent,
    ) -> GovernanceVerdict | None:
        """Analysis-plane legality beyond the common rules."""
        declared = manifest.declared_inputs
        if not any(
            isinstance(i, str) and i.startswith("substrate:")
            for i in declared
        ):
            return GovernanceVerdict.deny(
                "analysis-no-substrate-root",
                f"{manifest.substrate!r} declares no substrate:* input "
                f"(declared: {list(declared)}) — an analysis worker must be a "
                f"reduction of substrate state, never a raw re-derivation",
                remedy="declare the primary dependency substrate state stream "
                       "(e.g. 'substrate:tape') as the evidence layer",
            )
        return None


__all__ = ["AnalysisGovernanceFSM"]
