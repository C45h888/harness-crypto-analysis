"""OI-analysis worker — deterministic OI bar-class semantics over substrates.

TRIGGER SEMANTICS (deterministic, no polling):
* Wake  — ONE blocking XREADGROUP on the oi substrate's state stream.
* Fire  — the ``classify_bar`` CLASS of the latest aligned OI bar changed vs
          this worker's own last persisted projection (probe-against-own-state:
          oi re-fires that re-classify the same way do NOT fire this worker).
* Floor — L3 staleness heartbeat keeps the projection alive in quiet markets.

Compute imports exactly ONE analysis module (purity contract):
``market_service.analysis.oi`` — ``align_oi_rows`` / ``latest_oi_bar`` /
``proactiveness_verdict`` build + classify the OI/price/TBR bar grid and roll
it into the squeeze-vs-distribution proactiveness score (the crowding call).

Inputs are staleness-gated substrate latests (``oi``, ``technicals``) plus the
derivative cache (``oi_history``, ``taker_buy_sell``, ``klines`` = the 5m OI /
taker / price series). Null discipline: no aligned bar -> no verdict, ever.
"""

from __future__ import annotations

from typing import Any

from market_service.analysis.oi import (
    align_oi_rows,
    latest_oi_bar,
    proactiveness_verdict,
)
from market_service.analysis_worker.core import AnalysisPlaneMixin
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore


def _series(window: dict[str, Any]) -> tuple[list, list, list]:
    """(oi_history, taker_buy_sell, klines) from the derivative cache."""
    fut = window.get("futures") or {}
    return (fut.get("oi_history") or [], fut.get("taker_buy_sell") or [],
            fut.get("klines") or [])


def compose_oi_bar_input(window: dict[str, Any]) -> dict[str, Any] | None:
    """Pure composition: derivative-cache OI series -> latest classified bar."""
    oi_hist, tbr, klines = _series(window)
    return latest_oi_bar(oi_hist, tbr, klines)


class OIAnalysisWorker(AnalysisPlaneMixin, SubstrateWorkerCore):
    SUBSTRATE_NAME = "oi_analysis"
    # Trigger: the oi substrate's state stream (OI geometry is the driver).
    INPUT_STREAMS = ("substrate:oi",)
    DEPENDENCIES = ("oi", "technicals")
    DERIVATIVE_INPUTS = ("oi_history", "taker_buy_sell", "klines")
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=600)

    # ------------------------------------------------------------------
    # L2 — significance probe (probe-against-own-state bar-class flip)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        bar = compose_oi_bar_input(window)
        if bar is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_bar"})
        label = bar.get("class")
        prev = ((last_state or {}).get("output") or {}).get("bar_class")
        if prev is not None and label != prev:
            return TriggerDecision(fired=True, source="probe",
                                   predicates={"oi_bar_class": {
                                       "from": prev, "to": label}})
        return TriggerDecision(fired=False, source="probe", predicates={})

    # ------------------------------------------------------------------
    # Compute — oi substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        oi_hist, tbr, klines = _series(evidence)
        bars = align_oi_rows(oi_hist, tbr, klines)
        if not bars:
            # Null discipline: no aligned OI bar -> no positioning verdict, ever.
            return {}
        latest = bars[-1]
        oi_out = ((evidence.get("substrate_dependencies") or {})
                  .get("oi") or {}).get("output") or {}
        return {
            "bar_class": latest["class"],
            "proactiveness": proactiveness_verdict(bars),
            "raw_open_interest": oi_out.get("raw_open_interest"),
            "latest_bar": {k: latest.get(k) for k in
                           ("bucket", "oi_delta_pct", "px_delta_pct",
                            "tbr", "class", "proactiveness")},
        }
