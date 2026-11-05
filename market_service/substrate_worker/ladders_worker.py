"""Ladders substrate worker — absorption + ask-wall ladders.

Bounded to exactly ONE substrate: ``calculations.substrates.ladders``.
Watches the futures book structure; fires when a ladder rung builds or
erodes past the wall-direction threshold; computes the absorption ladder
and the ask-wall ladder the composition root runs.

``keystone_bid_stack`` is DELEGATED to the read plane (it needs density's
keystone output) — never computed here. Worker purity.

Probe (deterministic, mid-relative frame — same convention as the density
wall_shift): per-rung qty keyed by rung offset below mid; a rung whose qty
ratio vs last state exceeds 1.15 (the BUILT UP / ERODED semantics) fires.
A whole-book drift with unchanged structure must NOT fire.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.ladders import (
    absorption_ladder,
    ask_wall_ladder,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core import SubstrateWorkerCore

# Wall direction threshold — the BUILT UP / ERODED semantics shared with
# the density worker (wall_delta convention).
WALL_DIRECTION_THRESHOLD = 1.15
ABSORPTION_COUNT = 10  # composition orderbook section convention
ASK_STEP = 0.05  # composition orderbook section convention
ASK_BUCKETS = 60  # composition orderbook section convention


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


class LaddersWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "ladders"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=30, staleness_s=120)

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: mid-relative rung qty move vs last ladder. Cooldown 30s probe-only (hygiene bypasses);
    # staleness 120s.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """LaddersWorker: mid-relative rung qty move vs last ladder."""
        last_state = await self._read_own_state()
        if last_state is not None:
            try:
                seen_version = int(last_state.get("schema_version") or 0)
            except (TypeError, ValueError):
                seen_version = 0
            if seen_version != SUBSTRATE_STATE_SCHEMA_VERSION:
                last_state = None

        ws_rows = ws_rows or []
        arrival = bool(rows)

        # L4 — cold start: data arrived but no usable prior projection.
        if last_state is None:
            if not arrival:
                return
            decision = TriggerDecision(fired=True, source="cold_start",
                                       predicates={"consumed_entries": len(rows)})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L4 — capture recovery (gap/reconnecting -> running) bypasses cooldown.
        if recovery:
            decision = TriggerDecision(fired=True, source="recovery",
                                       predicates={"transition": "gap/reconnecting->running"})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L3 — staleness override (hygiene, bypasses cooldown).
        computed_at = last_state.get("computed_at_ms")
        age_ms = (now_ms - int(computed_at)) if isinstance(computed_at, (int, float)) else None
        if age_ms is not None and age_ms > self.staleness_s * 1_000:
            decision = TriggerDecision(
                fired=True, source="staleness",
                predicates={"age_ms": age_ms, "staleness_s": self.staleness_s},
            )
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L3 — window rollover.
        rollover = self._rollover_decision(rows)
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L1 — arrival gate: no new entries, no window build, no probe.
        if not arrival:
            return

        window = await self._cached_evidence(now_ms)
        self._last_dormant_reason = None
        profile = type(self).CADENCE
        min_depth = profile.min_book_depth if profile is not None else 1
        min_trades = profile.min_trade_count if profile is not None else 0
        if min_depth > 1 or min_trades > 0:
            fut_book = (window.get("futures") or {}).get("order_book") or {}
            book_depth = max(len(fut_book.get("bids") or []), len(fut_book.get("asks") or []))
            coverage = window.get("coverage") or {}
            trade_count = (
                ((coverage.get("spot_trades") or {}).get("trade_count") or 0)
                + ((coverage.get("futures_trades") or {}).get("trade_count") or 0)
            )
            if book_depth < min_depth or trade_count < min_trades:
                self._last_dormant_reason = (
                    f"insufficient_inputs: book_depth={book_depth} "
                    f"(min {min_depth}) trade_count={trade_count} (min {min_trades})"
                )
                return
        self._last_dormant_reason = None

        decision = self.probe(window, last_state, now_ms)
        if not decision.fired:
            return
        # Cooldown gates ONLY probe-source fires.
        if self._last_fire_ms is not None:
            if now_ms - self._last_fire_ms < self.cooldown_s * 1_000:
                return
        # Evidence-once: thread the probe snapshot into compute.
        await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows),
                         evidence=window)
    # ------------------------------------------------------------------
    # L2 — significance probe (mid-relative rung structure)
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
        prev_ladder = prev_output.get("fut_absorption_ladder") or []
        prev_mid = ((prev_output.get("reference") or {}).get("mid")
                    if isinstance(prev_output.get("reference"), dict) else None)
        if not isinstance(prev_ladder, list) or prev_mid is None:
            return TriggerDecision(fired=False, source="probe", predicates={})

        prev_by_offset: dict[float, float] = {}
        for rung in prev_ladder:
            if not isinstance(rung, dict):
                continue
            try:
                offset = round(float(rung["price"]) - float(prev_mid), 4)
                prev_by_offset[offset] = float(rung.get("qty") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue

        current = absorption_ladder(fut_book.get("bids") or [], mid,
                                    count=ABSORPTION_COUNT)
        for rung in current:
            offset = round(float(rung["price"]) - mid, 4)
            prev_qty = prev_by_offset.get(offset)
            if prev_qty is None or prev_qty <= 0:
                continue
            ratio = float(rung["qty"]) / prev_qty
            if ratio > WALL_DIRECTION_THRESHOLD or ratio < 1.0 / WALL_DIRECTION_THRESHOLD:
                predicates[f"ladder_shift@{offset}"] = {
                    "offset_below_mid": offset,
                    "from": prev_qty, "to": float(rung["qty"]),
                    "ratio": round(ratio, 4), "threshold": WALL_DIRECTION_THRESHOLD,
                    "reference": "mid",
                }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — ladders substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut_book = (evidence.get("futures") or {}).get("order_book") or {}
        bids = _pairs(fut_book.get("bids"))
        asks = _pairs(fut_book.get("asks"))
        mid = _mid(bids, asks)
        if mid is None or mid <= 0:
            # Null discipline: ladder geometry needs a real mid.
            return {}
        return {
            "fut_absorption_ladder": absorption_ladder(
                fut_book.get("bids") or [], mid, count=ABSORPTION_COUNT),
            "ask_wall_ladder": ask_wall_ladder(
                fut_book.get("asks") or [], mid, None, ASK_STEP, ASK_BUCKETS),
            # Reference frame so the next probe can rebuild mid-relative
            # offsets from the last persisted state (same pattern as
            # density's fut_keystone.mid).
            "reference": {"mid": mid},
        }
