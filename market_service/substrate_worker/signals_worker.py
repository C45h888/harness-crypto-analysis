"""Signals substrate worker — deterministic state-change signals.

Bounded to exactly ONE substrate module: ``calculations.substrates.signals``.
This worker IS the signal producer: it builds the market snapshot from the
tape worker's latest projection + the raw snapshot's open interest, runs
``deterministic_signals(snapshot, previous)``, and persists both the signals
and the snapshot (so the next cycle has its ``previous``).

The one declared cross-worker data dependency (decided): ``DEPENDENCIES =
("tape",)``. The core loads ``tape:latest`` at fire time into the window /
evidence under ``substrate_dependencies``; a stale/missing dependency keeps
the probe dormant and turns a hygiene fire into insufficient_data. No worker
imports another worker — the dependency travels via class attribute + store.

Probe: ``deterministic_signals(new_snapshot, previous_snapshot)`` non-empty
→ fire, with the tripped signals as predicates.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.signals import deterministic_signals
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore


def _snapshot_from(window: dict[str, Any]) -> dict[str, Any] | None:
    """Build the signal snapshot from tape:latest + raw open interest.

    Returns None when the tape dependency is unusable — the caller reports
    dormant/insufficient, never a fabricated snapshot.
    """
    deps = window.get("substrate_dependencies") or {}
    tape = deps.get("tape") or {}
    if tape.get("available") is False:
        return None
    tape_out = tape.get("output") or {}
    spot_flow = tape_out.get("spot_flow") or {}
    fut_flow = tape_out.get("futures_flow") or {}
    if not spot_flow and not fut_flow:
        return None
    oi_raw = (window.get("futures") or {}).get("open_interest") or {}
    oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
    return {
        "spot_buy_share": spot_flow.get("buy_share"),
        "futures_buy_share": fut_flow.get("buy_share"),
        "spot_obi_top_n": spot_flow.get("obi"),
        "open_interest": oi_value,
    }


class SignalsWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "signals"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=15, staleness_s=120)
    DEPENDENCIES = ("tape",)

    # ------------------------------------------------------------------
    # L2 — significance probe (any rule trips)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        snapshot = _snapshot_from(window)
        if snapshot is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "tape_dependency_unusable"})
        previous = ((last_state or {}).get("output") or {}).get("snapshot")
        signals = deterministic_signals(snapshot, previous)
        if not signals:
            return TriggerDecision(fired=False, source="probe", predicates={})
        predicates = {
            s.get("signal_type", f"signal_{i}"): s.get("evidence", {})
            for i, s in enumerate(signals) if isinstance(s, dict)
        }
        return TriggerDecision(fired=True, source="probe", predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — signals substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        snapshot = _snapshot_from(evidence)
        if snapshot is None:
            # Null discipline: without the tape dependency there is no
            # snapshot to evaluate — insufficient, never empty signals.
            return {}
        previous = (evidence.get("own_last_output") or {}).get("snapshot")
        return {
            "signals": deterministic_signals(snapshot, previous),
            "snapshot": snapshot,
        }
