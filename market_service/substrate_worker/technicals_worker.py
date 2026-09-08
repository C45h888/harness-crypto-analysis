"""Technicals substrate worker — EMA/ATR/trend time-series math.

Bounded to exactly ONE substrate: ``calculations.substrates.technicals``.
(Tiered large flow + seller aggression are the large_print worker's — the
legacy ``technical`` module split along that seam.) Watches the 5m kline
series; fires on a new bar close or an EMA-position flip; computes the
technicals half of the composition technical section.

Klines arrive ONLY via the derivative evidence cache (cache-only — the
worker never touches Binance). The core merges ``klines`` into the window
before the probe; a missing/stale cache keeps the worker dormant.

Probe: new 5m bar close (kline count grew vs the persisted ``kline_count``)
OR ``ema_position`` flip vs last state (price vs ema21).
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.technicals import (
    atr_pct_from_klines,
    ema_position,
    ema_series,
    trend_drift,
    trend_slope,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

EMA_SHORT_KEY = "ema21"  # the watched EMA leg (price vs ema21)


def _closes(klines: Any) -> list[float]:
    out: list[float] = []
    for row in klines or []:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            try:
                out.append(float(row[4]))
            except (TypeError, ValueError):
                continue
    return out


class TechnicalsWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "technicals"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=300, rollovers=("bar_5m",))
    DERIVATIVE_INPUTS = ("klines",)

    # ------------------------------------------------------------------
    # L2 — significance probe (new bar / EMA-position flip)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        klines = (window.get("futures") or {}).get("klines") or []
        closes = _closes(klines)
        if not closes:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_klines"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}

        prev_count = prev_output.get("kline_count")
        if isinstance(prev_count, int) and len(klines) > prev_count:
            predicates["new_bar"] = {"from_count": prev_count, "to_count": len(klines)}

        current_pos = ema_position(closes).get(EMA_SHORT_KEY)
        prev_pos = (prev_output.get("ema_position") or {}).get(EMA_SHORT_KEY)
        if current_pos is not None and prev_pos is not None and current_pos != prev_pos:
            predicates["ema_position_flip"] = {
                "leg": EMA_SHORT_KEY, "from": prev_pos, "to": current_pos,
            }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — technicals substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        klines = (evidence.get("futures") or {}).get("klines") or []
        closes = _closes(klines)
        if not closes:
            # Null discipline: no klines means no time-series math.
            return {}
        # Mirrors composition._technical_builder (emas + source marker).
        return {
            "emas": ema_series(closes),
            "emas_source": "derivatives.klines_5m",
            "atr_pct": atr_pct_from_klines(klines, period=14),
            "trend_slope": trend_slope(closes),
            "trend_drift": trend_drift(closes),
            "ema_position": ema_position(closes),
            "kline_count": len(klines),
        }
