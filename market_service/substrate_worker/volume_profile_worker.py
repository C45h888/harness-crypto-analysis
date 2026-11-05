"""Volume-profile substrate worker — POC / value-area geometry.

Bounded to exactly ONE substrate: ``calculations.substrates.volume_profile``.
Watches the futures trade distribution across price; fires when the point of
control moves or the value area shifts; computes the volume_profile section
the composition root runs.

Probe (frame: absolute bucket id — the profile is price-absolute semantics,
not mid-relative): POC bucket changed OR VAH/VAL moved >= 1 bucket width.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.volume_profile import (
    build_volume_profile,
    side_split,
    volume_profile_summary,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core import SubstrateWorkerCore

BUCKET_SIZE = 0.05  # composition volume_profile section convention
SIDE_SPLIT_TOP_N = 8  # substrate side_split default


class VolumeProfileWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "volume_profile"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=300)

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: POC move or VA boundary shift of a full bucket. Cooldown 60s probe-only (hygiene bypasses);
    # staleness 300s.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """VolumeProfileWorker: POC move or VA boundary shift of a full bucket."""
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
    # L2 — significance probe (absolute bucket-id frame)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        fut_trades = (window.get("futures") or {}).get("trades_normalized") or []
        if not fut_trades:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_futures_trades"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}
        prev_profile = prev_output.get("fut_volume_profile") or {}
        prev_summary = prev_profile.get("summary") if isinstance(prev_profile, dict) else None
        if not isinstance(prev_summary, dict):
            return TriggerDecision(fired=False, source="probe", predicates={})

        try:
            buckets = build_volume_profile(fut_trades, BUCKET_SIZE)
            summary = volume_profile_summary(buckets)
        except (ValueError, TypeError, KeyError):
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "profile_failed"})
        if not summary:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "empty_profile"})

        if summary.get("poc") != prev_summary.get("poc"):
            predicates["poc_moved"] = {
                "from": prev_summary.get("poc"), "to": summary.get("poc"),
            }
        for edge in ("vah", "val"):
            try:
                moved = abs(float(summary[edge]) - float(prev_summary[edge]))
            except (KeyError, TypeError, ValueError):
                continue
            if moved >= BUCKET_SIZE:
                predicates[f"{edge}_moved"] = {
                    "from": prev_summary[edge], "to": summary[edge],
                    "buckets_moved": round(moved / BUCKET_SIZE, 2),
                }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — volume_profile substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut_trades = (evidence.get("futures") or {}).get("trades_normalized") or []
        if not fut_trades:
            # Null discipline: no tape means no distribution geometry.
            return {}
        # Built ONCE and shared (mirrors composition._volume_profile_builder).
        profile = build_volume_profile(fut_trades, BUCKET_SIZE)
        summary = volume_profile_summary(profile)
        if not summary:
            return {}
        return {
            "fut_volume_profile": {
                "buckets": profile,
                "summary": summary,
                "side_split": side_split(profile, SIDE_SPLIT_TOP_N),
            },
        }
