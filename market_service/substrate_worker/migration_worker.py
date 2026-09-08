"""Migration substrate worker — hourly keystone migration.

Bounded to exactly ONE substrate: ``calculations.substrates.migration``.
Watches where aggressive taker-buy notional concentrates hour by hour; fires
when the hour bucket rolls or the keystone argmax bucket changes; computes
the hourly migration the composition orderbook section runs.

``keystone_cycle_migration`` stays read-plane (it consumes the durable
ledger, not the window) — never computed here. Worker purity.

Probe (frame: absolute bucket id — the same scale the migration verdict
consumes): hour-bucket rollover OR the keystone argmax bucket changed. The
CADENCE ``("hour",)`` rollover gives the same boundary via stream time; the
probe covers it in trade-time (hours with no stream traffic still roll).
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.migration import hourly_keystone_migration
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

MIGRATION_BUCKET = 0.05  # composition orderbook section convention
MIGRATION_WIDTH = 0.20  # keystone width / UP-DOWN-vs-FLAT scale


def _rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    mig = output.get("hourly_keystone_migration") or {}
    rows = mig.get("hourly") if isinstance(mig, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict)]


class MigrationWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "migration"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=300, staleness_s=900, rollovers=("hour",))

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
        prev_rows = _rows((last_state or {}).get("output") or {})
        if not prev_rows:
            return TriggerDecision(fired=False, source="probe", predicates={})

        current = hourly_keystone_migration(fut_trades, MIGRATION_BUCKET, MIGRATION_WIDTH)
        cur_rows = [r for r in (current.get("hourly") or []) if isinstance(r, dict)]
        if not cur_rows:
            return TriggerDecision(fired=False, source="probe", predicates={})

        prev_hour = prev_rows[-1].get("hour_start_ms")
        cur_hour = cur_rows[-1].get("hour_start_ms")
        if cur_hour != prev_hour:
            predicates["hour_rollover"] = {"from": prev_hour, "to": cur_hour}

        prev_key = prev_rows[-1].get("keystone")
        cur_key = cur_rows[-1].get("keystone")
        if cur_key is not None and prev_key is not None and cur_key != prev_key:
            predicates["keystone_bucket_changed"] = {"from": prev_key, "to": cur_key}

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — migration substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut_trades = (evidence.get("futures") or {}).get("trades_normalized") or []
        if not fut_trades:
            # Null discipline: no tape means no migration geometry.
            return {}
        return {
            "hourly_keystone_migration": hourly_keystone_migration(
                fut_trades, MIGRATION_BUCKET, MIGRATION_WIDTH),
        }
