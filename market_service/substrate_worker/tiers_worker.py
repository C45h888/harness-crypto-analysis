"""Tiers substrate worker — USD-notional bid tiers + institutional balance.

Bounded to exactly ONE substrate: ``calculations.substrates.tiers``. Watches
the futures book's institutional composition; fires when the tier-balance
verdict flips or the mega ratio crosses the bias threshold; computes the
tiers + tier_balance the composition wall adapter runs.

``mega_at_keystone`` is DELEGATED to the read plane (it needs density's
keystone) — never computed here. Worker purity.

``TierConfig`` resolves from ``Settings.wall_tier_config`` (the same field
the composition root reads via ``_resolve_tier_config``), with the substrate
default on any failure — the worker never crashes on config.
"""

from __future__ import annotations

import math
from typing import Any

from market_service.calculations.substrates.tiers import (
    TierConfig,
    bid_tier_balance,
    compute_bid_tiers_usd,
)
from market_service.substrate_worker.contracts import ArtifactBundle, CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core import SubstrateWorkerCore

# Bias threshold — the institutional verdict edge owned by the
# bid_tier_balance substrate (ratio > 1.2 bid-heavy, < 1/1.2 ask-heavy).
BIAS_THRESHOLD = 1.2


def _tier_config() -> TierConfig:
    """Resolve TierConfig from Settings (composition convention).

    Same field the composition root reads; the substrate default on any
    failure (missing settings, bad values — never crash the worker).
    """
    try:
        from market_service.config import Settings
        raw = (Settings().wall_tier_config or {})
        return TierConfig(
            mega_usd=float(raw.get("mega_usd", 250_000.0)),
            large_usd=float(raw.get("large_usd", 50_000.0)),
            medium_usd=float(raw.get("medium_usd", 10_000.0)),
        )
    except Exception:  # noqa: BLE001 — config must never crash the worker
        return TierConfig()


def _ratio_side(ratio: Any) -> int | None:
    if ratio is None:
        return None
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return None
    if math.isnan(r):  # NaN — no evidence, no decision
        return None
    if r == float("inf"):
        return 1
    if r == float("-inf"):
        return -1
    if r > BIAS_THRESHOLD:
        return 1
    if r < 1.0 / BIAS_THRESHOLD:
        return -1
    return 0


class TiersWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "tiers"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=30, staleness_s=120)

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: tier-balance verdict flip or mega-ratio cross. Cooldown 30s probe-only (hygiene bypasses);
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
        """TiersWorker: tier-balance verdict flip or mega-ratio cross."""
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
        """Stage-one: tiers probe over latest-only book (exact)."""
        window = {"futures": (latest.get("futures") or {}), "spot": (latest.get("spot") or {})}
        return self.probe(window, last_state, now_ms).fired

    # ------------------------------------------------------------------
    # L2 — significance probe (verdict flip / mega-ratio band cross)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
        seed: dict[str, Any] | None = None,
    ) -> TriggerDecision:
        fut_book = (window.get("futures") or {}).get("order_book") or {}
        bids_raw = fut_book.get("bids") or []
        asks_raw = fut_book.get("asks") or []
        if not bids_raw and not asks_raw:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "empty_book"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}
        prev_balance = prev_output.get("tier_balance") or {}

        current = bid_tier_balance(bids_raw, asks_raw, tier_config=_tier_config())
        prev_verdict = prev_balance.get("verdict") if isinstance(prev_balance, dict) else None
        if prev_verdict is not None and current["verdict"] != prev_verdict:
            predicates["tier_verdict_flip"] = {
                "from": prev_verdict, "to": current["verdict"],
                "ratio": current["ratio"],
            }

        prev_side = _ratio_side(prev_balance.get("ratio")) if isinstance(
            prev_balance, dict) else None
        cur_side = _ratio_side(current["ratio"])
        if prev_side is not None and cur_side is not None and prev_side != cur_side:
            predicates["mega_ratio_cross"] = {
                "from": prev_balance.get("ratio"), "to": current["ratio"],
                "bias_threshold": BIAS_THRESHOLD,
            }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — tiers substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int,
                artifacts: ArtifactBundle | None = None) -> dict[str, Any]:
        fut_book = (evidence.get("futures") or {}).get("order_book") or {}
        bids_raw = fut_book.get("bids") or []
        asks_raw = fut_book.get("asks") or []
        if not bids_raw and not asks_raw:
            # Null discipline: no book means no tier composition.
            return {}
        cfg = _tier_config()
        return {
            "tiers": compute_bid_tiers_usd(bids_raw, cfg),
            "tier_balance": bid_tier_balance(bids_raw, asks_raw, tier_config=cfg),
        }
