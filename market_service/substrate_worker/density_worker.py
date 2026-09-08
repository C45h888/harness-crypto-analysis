"""Density substrate worker — walls / keystones / density zones (flag bearer).

Phase 1 of the substrate-worker plane (spec: docs/SUBSTRATE_WORKER_SPEC.md).
Bounded to exactly ONE substrate: ``calculations.substrates.density``. The
wake conditions (probe) live HERE; the working math is imported from the
substrate; the transport is inherited from ``SubstrateWorkerCore``.

Probe (deterministic, substrate constants only):

  keystone_delta — the buyer keystone center on the new futures book moved
                   more than the substrate's keystone ``width`` (0.20) from
                   the last recorded keystone. This is the same displacement
                   scale the migration verdict uses (UP/DOWN vs FLAT).
  wall_shift     — a top density bid window's center moved, or its window
                   qty changed by more than the wall direction threshold
                   (1.15) — the same BUILT UP / ERODED semantics
                   ``analysis.wall_migration.wall_delta`` uses.

Compute emits ONLY density-substrate outputs (keystones fut+spot with the
ask-alias enrichment, top density windows, significant levels, keystone
trade intensity composed from the worker's OWN keystone). Outputs that need
another substrate (absorption/ask-wall ladders, keystone bid stack,
microprice skew) are delegated to the read plane — worker purity.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.density import (
    find_keystone,
    keystone_trade_intensity,
    significant_levels,
    top_density_windows,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

# Probe thresholds — IMPORTED SEMANTICS, not a second magic-number table.
# These mirror the constants the substrate calculators themselves use.
KEYSTONE_WIDTH = 0.20          # find_keystone default width / migration verdict scale
WALL_DIRECTION_THRESHOLD = 1.15  # wall_delta BUILT UP / ERODED semantics
TOP_DENSITY_WIDTH = 0.5        # top_density_windows width (harness convention)
TOP_DENSITY_COUNT = 5          # windows the probe compares
SIGNIFICANT_MIN_QTY = 0.0      # harness orderbook section convention (all levels)


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


def _enrich_fut_keystone(keystone: dict[str, Any], asks: list[list[float]]) -> dict[str, Any]:
    """Resolve the ``ask`` alias for the futures keystone (harness adapter logic).

    The nearest ask at-or-above the keystone price — the first price sellers
    defend. Same semantics as the harness orderbook adapter.
    """
    if not isinstance(keystone, dict):
        return keystone
    bid = keystone.get("bid") or keystone.get("keystone")
    keystone.setdefault("ask", None)
    if bid is not None and asks:
        try:
            bid_f = float(bid)
        except (TypeError, ValueError):
            return keystone
        best = None
        for p, q in asks:
            try:
                pf, qf = float(p), float(q)
            except (TypeError, ValueError):
                continue
            if pf >= bid_f and (best is None or pf < best[0]):
                best = (pf, qf)
        if best is not None:
            keystone["ask"] = best[0]
    return keystone


class DensityWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "density"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=30, staleness_s=120)

    # ------------------------------------------------------------------
    # L2 — significance probe (substrate constants only)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        fut_book = (window.get("futures") or {}).get("order_book") or {}
        bids = _pairs(fut_book.get("bids"))
        asks = _pairs(fut_book.get("asks"))
        mid = _mid(bids, asks)
        if mid is None or mid <= 0:
            # Null discipline: no book means no probe decision — the worker
            # stays dormant rather than fabricating a fire.
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_futures_book"})

        # Reference frames — per predicate, by substrate semantics:
        #   keystone_delta  ABSOLUTE: the keystone is a defended PRICE
        #     level; its travel (>= width) is the market fact (the same
        #     scale the migration verdict consumes). A uniform book drift
        #     that carries the keystone 0.20+ IS market movement.
        #   wall_shift      MID-RELATIVE: ladder STRUCTURE vs price. A
        #     whole-book drift with unchanged structure must NOT fire;
        #     a level building/eroding while others stay must.
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}

        # keystone_delta — absolute displacement of the defended level.
        keystone = find_keystone(bids, mid, KEYSTONE_WIDTH, -0.30, -0.05, None)
        kz_price = keystone.get("keystone")
        prev_keystone = ((prev_output.get("fut_keystone") or {}).get("keystone"))
        if kz_price is not None and prev_keystone is not None:
            try:
                delta = float(kz_price) - float(prev_keystone)
            except (TypeError, ValueError):
                delta = None
            if delta is not None and abs(delta) > KEYSTONE_WIDTH:
                predicates["keystone_delta"] = {
                    "from": prev_keystone, "to": kz_price,
                    "delta": round(delta, 6), "threshold": KEYSTONE_WIDTH,
                    "reference": "absolute",
                }

        # wall_shift — top density bid windows keyed by their distance below
        # mid (BUILT UP / ERODED scale, price-relative).
        current_windows = top_density_windows(
            {"bids": fut_book.get("bids") or []}, TOP_DENSITY_WIDTH, "bid", TOP_DENSITY_COUNT,
        )
        prev_windows = prev_output.get("fut_top_density_bids") or []
        prev_by_offset = {
            round(float(w.get("price")) - float(prev_output["fut_keystone"]["mid"]), 4):
                float(w.get("window_qty") or 0.0)
            for w in prev_windows if isinstance(w, dict)
            and isinstance(prev_output.get("fut_keystone"), dict)
            and prev_output["fut_keystone"].get("mid") is not None
        }
        for w in current_windows:
            offset_key = round(float(w["price"]) - mid, 4)
            prev_qty = prev_by_offset.get(offset_key)
            if prev_qty is None or prev_qty <= 0:
                continue
            ratio = float(w["window_qty"]) / prev_qty
            if ratio > WALL_DIRECTION_THRESHOLD or ratio < 1.0 / WALL_DIRECTION_THRESHOLD:
                predicates[f"wall_shift@{offset_key}"] = {
                    "offset_below_mid": offset_key,
                    "from": prev_qty, "to": float(w["window_qty"]),
                    "ratio": round(ratio, 4), "threshold": WALL_DIRECTION_THRESHOLD,
                    "reference": "mid",
                }

        return TriggerDecision(
            fired=bool(predicates), source="probe", predicates=predicates,
        )

    # ------------------------------------------------------------------
    # Compute — density substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut_book = (evidence.get("futures") or {}).get("order_book") or {}
        spot_book = (evidence.get("spot") or {}).get("order_book") or {}
        fut_bids_raw = fut_book.get("bids") or []
        fut_asks_raw = fut_book.get("asks") or []
        fut_bids = _pairs(fut_bids_raw)
        fut_asks = _pairs(fut_asks_raw)
        spot_bids = _pairs(spot_book.get("bids") or [])

        mid = _mid(fut_bids, fut_asks)
        if mid is None or mid <= 0:
            # Null discipline: without a real mid the density math cannot run.
            return {}

        fut_keystone = _enrich_fut_keystone(
            find_keystone(fut_bids, mid, KEYSTONE_WIDTH, -0.30, -0.05, None),
            fut_asks_raw,
        )
        # ``mid`` rides on the output so the probe can rebuild the prior
        # reference frame from the last persisted state (structural offsets
        # are mid-relative — see probe()).
        fut_keystone["mid"] = mid
        spot_keystone = find_keystone(spot_bids, mid, KEYSTONE_WIDTH, -0.30, -0.05, None)

        kz_price = fut_keystone.get("keystone")
        kz_tight = fut_keystone.get("tight") or {}
        kz_wide = fut_keystone.get("wide") or {}
        trades = (evidence.get("futures") or {}).get("trades_normalized") or []

        return {
            "fut_keystone": fut_keystone,
            "spot_keystone": spot_keystone,
            "fut_top_density_bids": top_density_windows(
                {"bids": fut_bids_raw}, TOP_DENSITY_WIDTH, "bid", TOP_DENSITY_COUNT,
            ),
            "fut_significant_levels": significant_levels(
                fut_bids_raw + fut_asks_raw, SIGNIFICANT_MIN_QTY,
            ),
            # keystone_trade_intensity composes the worker's OWN keystone
            # bands with the tape — same substrate, allowed composition.
            "keystone_trade_intensity": (
                keystone_trade_intensity(
                    trades,
                    kz_tight.get("lo"), kz_tight.get("hi"),
                    kz_wide.get("lo"), kz_wide.get("hi"),
                )
                if kz_price is not None
                and kz_tight.get("lo") is not None and kz_wide.get("hi") is not None
                else None
            ),
        }