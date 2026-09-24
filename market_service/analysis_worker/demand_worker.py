"""Demand analysis worker — deterministic spot-vs-leverage decomposition.

TRIGGER SEMANTICS (deterministic, no polling):
* Wake  — ONE blocking XREADGROUP on the tape substrate's state stream.
* Fire  — the ``demand_verdict`` label changed vs this worker's own last
          persisted projection (probe-against-own-state: tape re-fires that
          decompose the same way do NOT fire this worker). The 0.55 / 0.45
          buy-share bands live inside ``demand_verdict`` — never redefined here.
* Floor — L3 staleness heartbeat keeps the projection alive.

Compute imports exactly ONE analysis module (purity contract):
``market_service.analysis.demand`` — ``dx_from_flows`` composes the tape flow
summaries + derivative cache into the ``dx`` shape and ``demand_verdict`` maps
it to the SPOT-DEMAND / FUTURES-DOMINATED / DISTRIBUTION-ON-RIP ... ladder.

Composition seam (why ``dx_from_flows``, not ``decompose_demand``): the demand
worker's composed evidence carries substrate latests + the derivative cache,
never raw spot+futures trades/books — but the tape substrate already publishes
the ``summarize`` flow summaries, so the decomposition is a composition. The
macro_climate overlay (cross-asset) is intentionally deferred. Null discipline:
no futures flow -> no verdict, ever.
"""

from __future__ import annotations

from typing import Any

from market_service.analysis.demand import demand_verdict, dx_from_flows
from market_service.analysis_worker.core import AnalysisPlaneMixin
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore


def _last_row(rows: Any) -> dict[str, Any] | None:
    if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
        return rows[-1]
    return None


def _float(value: Any) -> float | None:
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _funding_rate(funding: Any) -> float | None:
    if not isinstance(funding, dict):
        return None
    for k in ("last_funding_rate", "lastFundingRate", "funding"):
        v = _float(funding.get(k))
        if v is not None:
            return v
    return None


def _long_pct(row: Any) -> float | None:
    if isinstance(row, dict):
        for k in ("long_account", "longAccount"):
            v = _float(row.get(k))
            if v is not None:
                return v
    return None


def _taker_buy_ratio(row: Any) -> float | None:
    if isinstance(row, dict):
        for k in ("buy_sell_ratio", "buySellRatio"):
            v = _float(row.get(k))
            if v is not None:
                return v
    return None


def compose_demand_input(window: dict[str, Any]) -> dict[str, Any] | None:
    """Pure composition: tape flows + oi substrate + derivative cache -> ``dx``."""
    deps = window.get("substrate_dependencies") or {}
    tape_out = (deps.get("tape") or {}).get("output") or {}
    oi_out = (deps.get("oi") or {}).get("output") or {}
    fut = window.get("futures") or {}
    fut_flow = tape_out.get("futures_flow")
    if not isinstance(fut_flow, dict) or not fut_flow:
        return None
    derivs = {
        "oi": oi_out.get("raw_open_interest"),
        "funding": _funding_rate(fut.get("funding")),
        "long_pct": _long_pct(_last_row(fut.get("global_ls"))),
        "top_long_pct": _long_pct(_last_row(fut.get("top_ls"))),
        "taker_buy_ratio": _taker_buy_ratio(_last_row(fut.get("taker_buy_sell"))),
    }
    return dx_from_flows(tape_out.get("spot_flow") or {}, fut_flow, derivs)


class DemandWorker(AnalysisPlaneMixin, SubstrateWorkerCore):
    SUBSTRATE_NAME = "demand"
    # Trigger: the tape substrate's state stream (flow is the demand driver).
    INPUT_STREAMS = ("substrate:tape",)
    DEPENDENCIES = ("tape", "oi", "delta")
    DERIVATIVE_INPUTS = ("funding", "top_ls", "global_ls", "taker_buy_sell")
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=600)

    # ------------------------------------------------------------------
    # L2 — significance probe (probe-against-own-state verdict flip)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        dx = compose_demand_input(window)
        if dx is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_flow"})
        try:
            verdict, _reasons = demand_verdict(dx)
        except (KeyError, TypeError, ValueError):
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "demand_failed"})
        prev = ((last_state or {}).get("output") or {}).get("verdict")
        if prev is not None and verdict != prev:
            return TriggerDecision(fired=True, source="probe",
                                   predicates={"demand_flip": {
                                       "from": prev, "to": verdict}})
        return TriggerDecision(fired=False, source="probe", predicates={})

    # ------------------------------------------------------------------
    # Compute — demand substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        dx = compose_demand_input(evidence)
        if dx is None:
            # Null discipline: no futures flow -> no demand verdict, ever.
            return {}
        verdict, reasons = demand_verdict(dx)
        return {
            "verdict": verdict,
            "reasons": reasons,
            "components": {
                "spot_buy_share": dx["spot"].get("buy_share"),
                "futures_buy_share": dx["futures"].get("buy_share"),
                "funding": dx["derivs"].get("funding"),
                "oi_change_pct": dx["derivs"].get("oi_change_pct"),
            },
        }
