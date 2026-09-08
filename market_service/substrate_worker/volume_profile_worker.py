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
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

BUCKET_SIZE = 0.05  # composition volume_profile section convention
SIDE_SPLIT_TOP_N = 8  # substrate side_split default


class VolumeProfileWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "volume_profile"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=300)

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
