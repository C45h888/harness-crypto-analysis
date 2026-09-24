"""Liquidation analysis worker — deterministic OI-vs-price divergence grid.

TRIGGER SEMANTICS (deterministic, no polling):
* Wake  — ONE blocking XREADGROUP on the oi substrate's state stream.
* Fire  — the ``liquidation_signal`` grid cell changed vs this worker's own
          last persisted projection (probe-against-own-state: OI-price
          divergence that re-classifies the same way does NOT fire).
* Floor — L3 staleness heartbeat keeps the projection alive.

Compute imports exactly ONE analysis module (purity contract):
``market_service.analysis.liquidations`` — ``oi_price_divergence`` aligns OI
vs price over the 5m series and ``liquidation_signal`` maps the cumulative
(OI change, price change) to the LONG/SHORT-LIQUIDATION / ORGANIC / ADDING grid.
This is the P1 OI-price proxy; the true forceOrder tape is the P2 upgrade.

Inputs are the derivative cache (``oi_history``, ``klines`` = 5m series; the
divergence buckets to 5m so the 5m klines are the native resolution). Null
discipline: no aligned series -> no signal, ever.
"""

from __future__ import annotations

from typing import Any

from market_service.analysis.liquidations import (
    liquidation_signal,
    oi_price_divergence,
)
from market_service.analysis_worker.core import AnalysisPlaneMixin
from market_service.substrate_worker.contracts import (
    CadenceProfile,
    TriggerDecision,
    bound_arrays,
)
from market_service.substrate_worker.core import SubstrateWorkerCore


def _series(window: dict[str, Any]) -> tuple[list, list]:
    """(oi_history, klines) from the derivative cache."""
    fut = window.get("futures") or {}
    return (fut.get("oi_history") or [], fut.get("klines") or [])


def compose_liquidation_input(window: dict[str, Any]) -> dict[str, Any] | None:
    """Pure composition: derivative-cache OI/price series -> divergence result."""
    oi_hist, klines = _series(window)
    if not oi_hist or not klines:
        return None
    return oi_price_divergence(oi_hist, klines)


class LiquidationWorker(AnalysisPlaneMixin, SubstrateWorkerCore):
    SUBSTRATE_NAME = "liquidation"
    # Trigger: the oi substrate's state stream (unwinding shows in OI first).
    INPUT_STREAMS = ("substrate:oi",)
    DEPENDENCIES = ("oi", "technicals")
    DERIVATIVE_INPUTS = ("oi_history", "klines")
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=600)

    # ------------------------------------------------------------------
    # L2 — significance probe (probe-against-own-state signal flip)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        result = compose_liquidation_input(window)
        if result is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_series"})
        signal = result.get("signal")
        prev = ((last_state or {}).get("output") or {}).get("signal")
        if prev is not None and signal != prev:
            return TriggerDecision(fired=True, source="probe",
                                   predicates={"liquidation_signal": {
                                       "from": prev, "to": signal}})
        return TriggerDecision(fired=False, source="probe", predicates={})

    # ------------------------------------------------------------------
    # Compute — liquidations substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        result = compose_liquidation_input(evidence)
        if result is None:
            # Null discipline: no OI/price series -> no liquidation call, ever.
            return {}
        return {
            "signal": result["signal"],
            "cumulative_oi_change_pct": result["cumulative_oi_change_pct"],
            "cumulative_price_change_pct": result["cumulative_price_change_pct"],
            "divergence_rows": bound_arrays(result["rows"], 24),
        }
