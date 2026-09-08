"""Delta substrate worker — the signed -2..+2 DELTA variable.

Bounded to exactly ONE substrate: ``calculations.substrates.delta``. Watches
the futures book + taker buy/sell alignment; fires when the DELTA state band
flips or the taker-buy share crosses the neutral band; computes the delta
section the composition root runs (``composition.run_analysis`` delta
adapter).

Taker buy/sell arrives via the derivative evidence cache (cache-only — the
worker never touches Binance). The core merges ``taker_buy_sell`` into the
window before the probe runs, so the probe stays pure; a missing/stale cache
keeps the worker dormant with a supervisor reason instead of firing.

Probe (deterministic, imported substrate semantics):

  delta_state_flip — ``delta_state(new) != delta_state(last)`` (±0.25/±1.0
                     boundaries owned by the substrate).
  tbr_band_cross   — latest ``tbr_last_pct`` crossed the 50±5 neutral band
                     (sides -1/0/+1 vs [45, 55] differ between last and new).
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.delta import delta_state, delta_variable
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

# TBR neutral band: 50% taker-buy is neutral flow; ±5 marks the
# composition delta adapter's alignment-significance edge.
TBR_BAND = (45.0, 55.0)


def _pairs(levels: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in levels or ():
        try:
            out.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _mid(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


def _tbr_side(tbr: float | None) -> int | None:
    if tbr is None:
        return None
    try:
        v = float(tbr)
    except (TypeError, ValueError):
        return None
    lo, hi = TBR_BAND
    if v < lo:
        return -1
    if v > hi:
        return 1
    return 0


def _delta_of(window: dict[str, Any]) -> dict[str, Any] | None:
    """Pure DELTA evaluation over a window (probe + compute share it)."""
    fut = window.get("futures") or {}
    book = fut.get("order_book") or {}
    mid = _mid(_pairs(book.get("bids")), _pairs(book.get("asks")))
    if mid is None or mid <= 0:
        return None
    tbr_series = fut.get("taker_buy_sell")
    try:
        payload = delta_variable(book, tbr_series, mid, half_range=0.75, band_step=0.10)
    except (ValueError, TypeError):
        return None
    payload["state"] = delta_state(payload["delta"])
    return payload


class DeltaWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "delta"
    INPUT_STREAMS = ("raw", "microstructure")
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=180, ws_input=True)
    DERIVATIVE_INPUTS = ("taker_buy_sell",)

    # ------------------------------------------------------------------
    # L2 — significance probe (state-band flip / TBR band cross)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        current = _delta_of(window)
        if current is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_book_mid"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}

        prev_state = prev_output.get("state")
        if prev_state is not None and current["state"] != prev_state:
            predicates["delta_state_flip"] = {
                "from": prev_state, "to": current["state"],
                "delta": current["delta"],
            }

        prev_side = _tbr_side(prev_output.get("tbr_last_pct"))
        cur_side = _tbr_side(current.get("tbr_last_pct"))
        if prev_side is not None and cur_side is not None and prev_side != cur_side:
            predicates["tbr_band_cross"] = {
                "from": prev_output.get("tbr_last_pct"), "to": current.get("tbr_last_pct"),
                "band": list(TBR_BAND),
            }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — delta substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        current = _delta_of(evidence)
        if current is None:
            # Null discipline: without a real mid the DELTA math cannot run.
            return {}
        return {
            "delta": current["delta"],
            "delta_raw": current["delta_raw"],
            "wall_imbalance": current["wall_imbalance"],
            "flow_alignment": current["flow_alignment"],
            "tbr_last_pct": current["tbr_last_pct"],
            "tbr_3avg_pct": current["tbr_3avg_pct"],
            "n_bands": current["n_bands"],
            "bands": current["bands"],
            "range": current["range"],
            "state": current["state"],
        }
