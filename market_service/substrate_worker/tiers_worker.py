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
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
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
    # L2 — significance probe (verdict flip / mega-ratio band cross)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
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

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
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
