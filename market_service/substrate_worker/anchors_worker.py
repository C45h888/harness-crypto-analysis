"""Anchors substrate worker — dynamic round-number bid anchors.

Bounded to exactly ONE substrate: ``calculations.substrates.anchors``.
Watches the futures bid book against the round-number anchor grid; fires
when the grid shifts or an anchor's attributed qty builds/erodes; computes
the anchor grid + aggregation the composition wall adapter runs.

The tick/step heuristic is adapter-level marshalling copied from the
composition wall adapter (composition.py wall section): tick is the minimum
price increment estimate, step the round-anchor spacing. Documented here so
the worker matches the pull path exactly.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.anchors import (
    compute_round_anchors,
    derive_round_anchors,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

# Anchor qty direction threshold — the BUILT UP / ERODED scale shared with
# the density/ladders workers.
WALL_DIRECTION_THRESHOLD = 1.15


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


def _tick_step(price: float) -> tuple[float, float]:
    """Adapter-level marshalling (copied from the composition wall adapter).

    tick_size is the minimum-increment estimate; step is the round-anchor
    spacing (5 ticks, with a floor that scales at 4-figure prices).
    """
    tick_size = max(price * 0.0001, 0.0001)
    step = max(tick_size * 5, 0.05) if price < 1000 else max(tick_size * 5, 5.0)
    return tick_size, step


class AnchorsWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "anchors"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=30, staleness_s=120)

    # ------------------------------------------------------------------
    # L2 — significance probe (grid shift / anchor qty change)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        fut_book = (window.get("futures") or {}).get("order_book") or {}
        bids = _pairs(fut_book.get("bids"))
        asks = _pairs(fut_book.get("asks"))
        mid = _mid(bids, asks)
        if mid is None or mid <= 0:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_futures_book"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}
        prev_anchors = prev_output.get("anchors") or []

        tick_size, step = _tick_step(mid)
        try:
            current = derive_round_anchors(mid, tick_size, step)
        except (ValueError, TypeError):
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "anchor_derive_failed"})
        current_levels = [a["level"] for a in current if isinstance(a, dict)]
        prev_levels = [a.get("level") for a in prev_anchors if isinstance(a, dict)]
        if prev_levels and current_levels != prev_levels:
            predicates["grid_shift"] = {"from": prev_levels, "to": current_levels}

        if prev_levels:
            cur_agg = compute_round_anchors(fut_book.get("bids") or [], current)
            prev_by_level = {
                a.get("level"): float((a or {}).get("qty") or 0.0)
                for a in (prev_output.get("aggregation") or {}).get("anchors", [])
                if isinstance(a, dict)
            }
            for row in (cur_agg.get("anchors") or []):
                if not isinstance(row, dict):
                    continue
                prev_qty = prev_by_level.get(row.get("level"))
                if prev_qty is None or prev_qty <= 0:
                    continue
                ratio = float(row.get("qty") or 0.0) / prev_qty
                if ratio > WALL_DIRECTION_THRESHOLD or ratio < 1.0 / WALL_DIRECTION_THRESHOLD:
                    predicates[f"anchor_shift@{row.get('level')}"] = {
                        "level": row.get("level"),
                        "from": prev_qty, "to": float(row.get("qty") or 0.0),
                        "ratio": round(ratio, 4),
                        "threshold": WALL_DIRECTION_THRESHOLD,
                    }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — anchors substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut_book = (evidence.get("futures") or {}).get("order_book") or {}
        bids = _pairs(fut_book.get("bids"))
        asks = _pairs(fut_book.get("asks"))
        mid = _mid(bids, asks)
        if mid is None or mid <= 0:
            # Null discipline: the anchor grid is derived from a real price.
            return {}
        tick_size, step = _tick_step(mid)
        anchors = derive_round_anchors(mid, tick_size, step)
        return {
            "anchors": anchors,
            "aggregation": compute_round_anchors(fut_book.get("bids") or [], anchors),
            "anchor_source": "dynamic",
            "tick_size": tick_size,
            "step": step,
        }
