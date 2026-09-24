"""Regime analysis worker — deterministic regime verdict over substrates.

TRIGGER SEMANTICS (deterministic, no polling):
* Wake  — ONE blocking XREADGROUP on the tape substrate's state stream:
          the calculation plane's substrate fire IS the arrival event.
* Fire  — the composed regime VERDICT changed vs this worker's own last
          persisted state (probe-against-own-state: tape re-fires that
          compose the same verdict do NOT fire this worker).
* Floor — L3 staleness heartbeat (600s) keeps the projection alive in
          quiet markets; the verdict flip is the ceiling on fire frequency.

Compute imports exactly ONE analysis module (purity contract):
``regime_verdict`` from ``market_service.analysis.regime`` — the composed
input is the canonical shape that module documents (futures flow summary,
open interest, funding, L/S rows).

Dependencies are staleness-gated substrate latests: ``tape`` (flow summary),
``oi`` (open interest). ``funding``/``top_ls``/``global_ls`` ride the
derivative evidence cache (read-only; missing/stale cache → dormant, never
fabricated).
"""

from __future__ import annotations

from typing import Any

from market_service.analysis.regime import regime_verdict
from market_service.analysis_worker.core import AnalysisPlaneMixin
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore


def _last_row(rows: Any) -> dict[str, Any] | None:
    """Last row of a derivative series (None when absent/malformed)."""
    if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
        return rows[-1]
    return None


def _funding_rate(funding: Any) -> float | None:
    """Funding rate from the derivative-cache funding snapshot (None-safe)."""
    if not isinstance(funding, dict):
        return None
    for k in ("last_funding_rate", "lastFundingRate"):
        v = funding.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def compose_regime_input(window: dict[str, Any]) -> dict[str, Any] | None:
    """Pure composition: substrate deps + derivative cache → regime input.

    ``regime_verdict`` consumes ``{futures: {flow, open_interest, funding,
    mark_price, long_short_ratio, top_long_short_accounts}}`` where ``flow``
    is the canonical flow.summarize() output — exactly the tape substrate's
    ``futures_flow`` projection. Null discipline: no tape flow → no verdict.
    """
    deps = window.get("substrate_dependencies") or {}
    tape_out = (deps.get("tape") or {}).get("output") or {}
    oi_out = (deps.get("oi") or {}).get("output") or {}
    fut_cache = window.get("futures") or {}

    flow = tape_out.get("futures_flow")
    if not isinstance(flow, dict) or not flow:
        return None
    return {
        "futures": {
            "flow": flow,
            "open_interest": {"open_interest": oi_out.get("raw_open_interest")},
            "funding": fut_cache.get("funding") or {},
            "mark_price": {},
            "long_short_ratio": _last_row(fut_cache.get("global_ls")),
            "top_long_short_accounts": _last_row(fut_cache.get("top_ls")),
        },
    }


class RegimeWorker(AnalysisPlaneMixin, SubstrateWorkerCore):
    SUBSTRATE_NAME = "regime"
    # Trigger: the tape substrate's state stream (flow is the regime driver).
    INPUT_STREAMS = ("substrate:tape",)
    DEPENDENCIES = ("tape", "oi")
    DERIVATIVE_INPUTS = ("funding", "top_ls", "global_ls")
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=600)

    # ------------------------------------------------------------------
    # L2 — significance probe (probe-against-own-state verdict flip)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        composed = compose_regime_input(window)
        if composed is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_flow"})
        try:
            verdict, _reasons = regime_verdict(composed)
        except (KeyError, TypeError, ValueError):
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "regime_failed"})
        prev = ((last_state or {}).get("output") or {}).get("verdict")
        if prev is not None and verdict != prev:
            return TriggerDecision(fired=True, source="probe",
                                   predicates={"regime_flip": {
                                       "from": prev, "to": verdict}})
        return TriggerDecision(fired=False, source="probe", predicates={})

    # ------------------------------------------------------------------
    # Compute — regime substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        composed = compose_regime_input(evidence)
        if composed is None:
            # Null discipline: no flow → no regime verdict, ever.
            return {}
        verdict, reasons = regime_verdict(composed)
        fut_cache = evidence.get("futures") or {}
        oi_out = ((evidence.get("substrate_dependencies") or {})
                  .get("oi") or {}).get("output") or {}
        return {
            "verdict": verdict,
            "reasons": reasons,
            "funding_rate": _funding_rate(fut_cache.get("funding")),
            "raw_open_interest": oi_out.get("raw_open_interest"),
        }