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

Phase H1 (docs/HORIZON_RETENTION_SPEC.md) — the pilot horizon worker:
``HORIZONS = ("1h", "4h")``. The core cadence layer maintains the
IST-aligned fold state and attaches per-horizon deduped evidence windows;
this file owns the horizon INTERPRETATION:

* probe — horizon-tagged predicates: the keystone argmax on the 1h/4h
  tape compared against the last fired horizon output;
* compute — per-horizon ``hourly_keystone_migration`` in the payload's
  ``horizons`` block (extracted by the core), base 15m output unchanged;
* cadence — 4h horizons do not heartbeat at 15m staleness; per-horizon
  staleness bounds live in the CadenceProfile.
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


def _hz_migration(hz_window: dict[str, Any]) -> dict[str, Any] | None:
    """Full hourly migration dict for one horizon's deduped evidence window."""
    fut_trades = (hz_window.get("futures") or {}).get("trades_normalized") or []
    if not fut_trades:
        return None
    return hourly_keystone_migration(fut_trades, MIGRATION_BUCKET, MIGRATION_WIDTH)


class MigrationWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "migration"
    INPUT_STREAMS = ("raw",)
    # Phase H1 — pilot horizon declaration. The 4h horizon answers the
    # "where is notional concentrating across the session" question the
    # 15m window cannot; cadence/staleness for it live in the profile.
    HORIZONS = ("1h", "4h")
    CADENCE = CadenceProfile(
        cooldown_s=300, staleness_s=900, rollovers=("hour",),
        horizons=("1h", "4h"),
        # 1h horizon: quiet-but-alive bound; 4h horizon: hours-scale bound.
        horizon_staleness_s=(("1h", 1800), ("4h", 4 * 3600)),
    )

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

        # Phase H1 — horizon-tagged predicates: keystone argmax on the
        # declared horizons' tape vs the last fired horizon output. The
        # predicate names carry the horizon so the audit record shows
        # WHICH tape moved.
        last_horizons = (last_state or {}).get("horizons") or {}
        evidence_horizons = window.get("horizons") or {}
        for hz in self.horizons:
            hz_window = evidence_horizons.get(hz)
            if not isinstance(hz_window, dict):
                continue
            cur_hz_rows = _hz_migration(hz_window)
            cur_rows = [r for r in (cur_hz_rows.get("hourly") or []) if isinstance(r, dict)] \
                if isinstance(cur_hz_rows, dict) else []
            if not cur_rows:
                continue
            prev_hz_rows = _rows((last_horizons.get(hz) or {}).get("output") or {})
            prev_keystone = prev_hz_rows[-1].get("keystone") if prev_hz_rows else None
            cur_keystone = cur_rows[-1].get("keystone")
            if cur_keystone is not None and prev_keystone is not None \
                    and cur_keystone != prev_keystone:
                predicates[f"{hz}:keystone_changed"] = {
                    "from": prev_keystone, "to": cur_keystone,
                }

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
        out: dict[str, Any] = {
            "hourly_keystone_migration": hourly_keystone_migration(
                fut_trades, MIGRATION_BUCKET, MIGRATION_WIDTH),
        }
        # Phase H1 — per-horizon verdicts. The core extracts the
        # "horizons" key into the payload's horizons block; each entry
        # carries the SAME substrate function over that horizon's deduped
        # tape — the math module stays horizon-agnostic (it buckets by
        # hour whatever population it is handed).
        evidence_horizons = evidence.get("horizons") or {}
        hz_outputs: dict[str, Any] = {}
        for hz in self.horizons:
            hz_window = evidence_horizons.get(hz)
            if not isinstance(hz_window, dict):
                continue
            rows = _hz_migration(hz_window)
            if rows:
                hz_outputs[hz] = {"hourly_keystone_migration": rows}
        if hz_outputs:
            out["horizons"] = hz_outputs
        return out
