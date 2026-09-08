"""Positioning (OI) substrate worker — walls + contract composition.

Bounded to exactly ONE substrate: ``calculations.substrates.positioning``.
Watches open interest against the seller-wall grid; fires when OI moves
>= 1% vs last state; computes the oi section the composition root runs
(``composition._adapt_oi``).

OI history + long/short ratios arrive ONLY via the derivative evidence
cache (cache-only — the worker never touches Binance). The core merges
``oi_history``/``top_ls``/``global_ls`` into the window before the probe;
a missing/stale cache keeps the worker dormant.

Probe: ``|ΔOI| >= 1%`` vs last state's ``raw_open_interest`` (provenance:
the deterministic_signals ``open_interest_shift`` 0.01 threshold).

The ``_oi_series`` / ``_oi_value_series`` / ``_ls_last_pct`` extractors are
adapter-level marshalling copied from the composition oi adapter (same
class as the anchors tick/step heuristic): they read the raw derivative
bar shapes into the substrate's list inputs.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.positioning import (
    find_walls,
    oi_implied_value,
    oi_inflow_outflow,
    oi_weighted_contracts,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

# OI move threshold — the deterministic_signals open_interest_shift rule
# (abs(current - previous) / previous >= 0.01).
OI_MOVE_PCT = 1.0


def _pairs(levels: Any, depth: int) -> list[list[float]]:
    out: list[list[float]] = []
    for row in (levels or [])[:depth]:
        try:
            out.append([float(row[0]), float(row[1])])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _mid(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


def _oi_series(oi_hist: Any) -> list[float]:
    """Adapter marshalling (copied from the composition oi adapter)."""
    out: list[float] = []
    for row in oi_hist or []:
        if not isinstance(row, dict):
            continue
        v = (row.get("sumOpenInterest") or row.get("sum_open_interest")
             or row.get("openInterest") or row.get("open_interest"))
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _oi_value_series(oi_hist: Any) -> list[float]:
    """Adapter marshalling (copied from the composition oi adapter)."""
    out: list[float] = []
    for row in oi_hist or []:
        if not isinstance(row, dict):
            continue
        v = row.get("sumOpenInterestValue") or row.get("sum_open_interest_value")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _ls_last_pct(series: Any) -> float | None:
    """Adapter marshalling (copied from the composition oi adapter)."""
    if not series:
        return None
    last = series[-1] if isinstance(series[-1], dict) else None
    if not last:
        return None
    for k in ("longAccount", "long_account"):
        if k in last and last[k] is not None:
            try:
                return float(last[k])
            except (TypeError, ValueError):
                continue
    return None


class OiWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "oi"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=300)
    DERIVATIVE_INPUTS = ("oi_history", "top_ls", "global_ls")

    # ------------------------------------------------------------------
    # L2 — significance probe (|ΔOI| >= 1%)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        fut = window.get("futures") or {}
        oi_raw = fut.get("open_interest") or {}
        oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
        try:
            current = float(oi_value) if isinstance(oi_value, (int, float)) else None
        except (TypeError, ValueError):
            current = None
        if current is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_open_interest"})
        prev = ((last_state or {}).get("output") or {}).get("raw_open_interest")
        try:
            prev_f = float(prev) if isinstance(prev, (int, float)) else None
        except (TypeError, ValueError):
            prev_f = None
        if not prev_f:
            return TriggerDecision(fired=False, source="probe", predicates={})
        move_pct = abs(current - prev_f) / abs(prev_f) * 100.0
        if move_pct < OI_MOVE_PCT:
            return TriggerDecision(fired=False, source="probe", predicates={})
        return TriggerDecision(
            fired=True, source="probe",
            predicates={"oi_move": {"from": prev_f, "to": current,
                                    "move_pct": round(move_pct, 4),
                                    "threshold_pct": OI_MOVE_PCT}})

    # ------------------------------------------------------------------
    # Compute — positioning substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut = evidence.get("futures") or {}
        fut_book = fut.get("order_book") or {}
        asks = _pairs(fut_book.get("asks"), depth)
        bids = _pairs(fut_book.get("bids"), depth)
        last_price = _mid(bids, asks)
        oi_raw = fut.get("open_interest") or {}
        oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
        oi_float = float(oi_value) if isinstance(oi_value, (int, float)) else None
        oi_hist = fut.get("oi_history") or []
        if (last_price is None or last_price <= 0) and oi_float is None and not oi_hist:
            # Null discipline: no price, no OI, no history — nothing to say.
            return {}
        # Mirrors composition._adapt_oi section for section.
        walls = (find_walls(asks, float(last_price), 0.005, 0.05)
                 if last_price is not None and last_price > 0 else [])
        oi_series = _oi_series(oi_hist)
        oi_value_series = _oi_value_series(oi_hist)
        weighted = (
            oi_weighted_contracts(oi_float, _ls_last_pct(fut.get("top_ls")),
                                  _ls_last_pct(fut.get("global_ls")))
            if oi_float is not None
            else {"oi": None, "top_long_contracts": None,
                  "global_long_contracts": None}
        )
        implied_rows = [
            {"bucket": i * 300_000, "oi": v, "oi_value": nv}
            for i, (v, nv) in enumerate(zip(oi_series, oi_value_series))
        ]
        return {
            "walls": walls,
            "weighted_contracts": weighted,
            "inflow_outflow": oi_inflow_outflow(oi_series),
            "implied_value": oi_implied_value(implied_rows),
            "raw_open_interest": oi_float,
            "bars_available": len(oi_series),
        }
