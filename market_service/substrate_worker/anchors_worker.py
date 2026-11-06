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
from market_service.substrate_worker.contracts import ArtifactBundle, CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
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
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: anchor grid shift or qty move vs last state. Cooldown 30s probe-only (hygiene bypasses);
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
        """AnchorsWorker: anchor grid shift or qty move vs last state."""
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

        # Stage-one screen (ns.read discipline): the probe inputs for
        # this worker are point-in-time (latest snapshot + last state),
        # so run the SAME probe on latest-only state first. Quiet screen
        # skips the heavy window materialization entirely; a firing screen
        # proceeds to the full window + confirming probe below. Zero miss
        # risk: screen and probe read identical inputs here.
        latest = await self.store.read("raw.latest", ids={"symbol": self.symbol})
        if latest is not None and not await self.screen(latest, last_state, now_ms):
            self._last_dormant_reason = "screen: latest state shows no likely flip"
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
    async def screen(
        self, latest: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> bool:
        """Stage-one: anchors probe over latest-only book (exact)."""
        window = {"futures": (latest.get("futures") or {}), "spot": (latest.get("spot") or {})}
        return self.probe(window, last_state, now_ms).fired

    # ------------------------------------------------------------------
    # L2 — significance probe (grid shift / anchor qty change)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
        seed: dict[str, Any] | None = None,
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

    def compute(self, evidence: dict[str, Any], depth: int,
                artifacts: ArtifactBundle | None = None) -> dict[str, Any]:
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
