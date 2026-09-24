"""Wall-migration analysis worker — deterministic wall-event detection.

THE reference worker for derived-trigger semantics: fires when the WALL SET
itself changes, not when the density substrate happens to fire.

TRIGGER SEMANTICS (deterministic, no polling):
* Wake  — ONE blocking XREADGROUP on the density substrate's state stream.
* Fire  — probe-against-own-state: the probe diffs the density payload's
          top-density windows against the WALL SET THIS WORKER PERSISTED
          LAST FIRE. Density re-fires that carry no wall-set change (qty
          noise under the direction threshold, cooldown re-fires, staleness
          heartbeats) are wake events — never fire causes.
* Floor — L3 staleness heartbeat (600s) keeps the projection alive.

Imported semantics (never redefined — the ONE threshold table):
* ``wall_delta`` direction threshold 1.15 (analysis.wall_migration) — the
  BUILT UP / ERODED scale;
* keystone width 0.20 — the ``find_keystone`` default and the migration
  verdict's UP/DOWN/FLAT displacement scale;
* ``MIN_WALL_QTY`` — the defended-level floor (operator-tunable; promoted to
  an analysis-module constant in Pass 2 alongside the depth-book enrichment).

Compute imports exactly ONE analysis module (purity contract):
``market_service.analysis.wall_migration`` — its functions shape the wall
event provenance and the threshold ladder this worker documents.

Book-depth enrichment (``fuel_ratio``, ``keystone_wall_balance``,
``wall_trap_assessment`` probabilities) is Pass 2: it needs a depth-book
surface; today's microstructure book key carries top-of-book only. The
P1 compute emits the deterministic EVENT layer (wall formed / absorbed /
migrated, keystone travel) plus the keystone-holds factor inputs from the
tape/delta/oi substrates — never fabricated from partial state.
"""

from __future__ import annotations

from typing import Any

from market_service.analysis.wall_migration import wall_delta  # noqa: F401 (imported semantics)
from market_service.analysis_worker.core import AnalysisPlaneMixin
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

# Wall-set classification threshold (SOL window qty). Imported semantics:
# the calc plane's significant-level / density conventions scale; this floor
# separates "defended" from "ambient depth" deterministically. Promoted to
# the analysis module in Pass 2 with the depth-book enrichment.
MIN_WALL_QTY = 10_000.0
# wall_delta's direction threshold (analysis.wall_migration.wall_delta) —
# the BUILT UP / ERODED scale. Referenced by name here; the probe uses the
# same ratio for absorption so detection and analysis cannot drift.
WALL_DIRECTION_THRESHOLD = 1.15
# Keystone displacement scale — find_keystone(width=0.20) / migration verdict.
KEYSTONE_WIDTH = 0.20


def _density_view(dep: dict[str, Any] | None) -> dict[str, Any] | None:
    """Density-substrate output view → {windows, keystone} (None-safe)."""
    out = ((dep or {}).get("output") or {})
    if not out:
        return None
    return {
        "windows": out.get("fut_top_density_bids") or [],
        "keystone": out.get("fut_keystone") or {},
    }


def _defended_windows(windows: Any, min_wall_qty: float) -> dict[float, float]:
    """{level: window_qty} for windows at or above the wall floor."""
    out: dict[float, float] = {}
    for w in windows or []:
        if not isinstance(w, dict):
            continue
        try:
            price = float(w["price"])
            qty = float(w.get("window_qty") or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        if qty >= min_wall_qty:
            out[round(price, 4)] = qty
    return out


def evaluate_wall_conditions(
    cur_windows: Any,
    cur_keystone: Any,
    prev_output: dict[str, Any],
    *,
    min_wall_qty: float = MIN_WALL_QTY,
) -> dict[str, Any]:
    """Pure deterministic wall-event evaluation (probe + compute share it).

    ``prev_output`` is THIS worker's own last projection — the wall-set diff
    is against our own persisted walls, not the density worker's last fire
    (density may fire many times without the wall set changing).
    """
    events: list[dict[str, Any]] = []
    defended_now = _defended_windows(cur_windows, min_wall_qty)
    prev_walls = prev_output.get("walls") or {}

    # Walls that exist now: formed vs eroded (BUILT UP / ERODED scale).
    for level, qty in sorted(defended_now.items()):
        prev_qty = prev_walls.get(level)
        if prev_qty is None or prev_qty < min_wall_qty:
            events.append({"event": "wall_formed", "level": level, "qty": qty})
        elif qty < prev_qty / WALL_DIRECTION_THRESHOLD:
            events.append({"event": "wall_absorbed", "level": level,
                           "prior": prev_qty, "qty": qty})
    # Walls that were defended and vanished entirely — absorbed.
    for level, prev_qty in sorted(prev_walls.items()):
        if prev_qty >= min_wall_qty and level not in defended_now:
            events.append({"event": "wall_absorbed", "level": level,
                           "prior": prev_qty, "qty": None})

    # Keystone travel — the migration verdict's exact displacement scale.
    # Persisted shape: {"keystone": level} (mirrors the density payload's
    # fut_keystone shape) — read defensively: older projections may carry
    # the bare level float (schema migration), treat as prior value only.
    keystone_travel = None
    cur_kz = cur_keystone.get("keystone") if isinstance(cur_keystone, dict) else None
    prev_kz = prev_output.get("keystone")
    if isinstance(prev_kz, dict):
        prev_kz = prev_kz.get("keystone")
    if cur_kz is not None and prev_kz is not None:
        try:
            travel = float(cur_kz) - float(prev_kz)
        except (TypeError, ValueError):
            travel = None
        if travel is not None:
            keystone_travel = round(travel, 6)
            if abs(travel) > KEYSTONE_WIDTH:
                events.append({"event": "keystone_migrated",
                               "from": prev_kz, "to": cur_kz,
                               "travel": keystone_travel})

    return {"events": events, "walls": defended_now,
            "keystone": {"keystone": cur_kz},
            "keystone_travel": keystone_travel}


class WallMigrationWorker(AnalysisPlaneMixin, SubstrateWorkerCore):
    SUBSTRATE_NAME = "wall_migration"
    # Trigger: the density substrate's state stream (keystone/wall geometry).
    INPUT_STREAMS = ("substrate:density",)
    DEPENDENCIES = ("density", "tape", "delta", "oi")
    DERIVATIVE_INPUTS = ()  # pure substrate composition — cache not needed
    CADENCE = CadenceProfile(cooldown_s=30, staleness_s=600)

    # ------------------------------------------------------------------
    # L2 — significance probe (wall-set diff against own persisted walls)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        deps = window.get("substrate_dependencies") or {}
        density = _density_view(deps.get("density"))
        if density is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_density"})
        prev_output = (last_state or {}).get("output") or {}
        result = evaluate_wall_conditions(
            density["windows"], density["keystone"], prev_output)
        if not result["events"]:
            return TriggerDecision(fired=False, source="probe", predicates={})
        return TriggerDecision(fired=True, source="probe",
                               predicates={"wall_events": result["events"]})

    # ------------------------------------------------------------------
    # Compute — wall_migration substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        deps = evidence.get("substrate_dependencies") or {}
        density = _density_view(deps.get("density"))
        if density is None:
            # Null discipline: no density geometry → no wall analysis, ever.
            return {}
        prev_output = evidence.get("own_last_output") or {}
        result = evaluate_wall_conditions(
            density["windows"], density["keystone"], prev_output)

        tape_out = (deps.get("tape") or {}).get("output") or {}
        delta_out = ((evidence.get("substrate_dependencies") or {})
                     .get("delta") or {}).get("output") or {}
        oi_out = ((evidence.get("substrate_dependencies") or {})
                  .get("oi") or {}).get("output") or {}
        fut_book_flow = (tape_out.get("futures_flow") or {})

        return {
            **result,
            # Keystone-holds factor inputs (scorecard itself needs the
            # depth-book bid/ask ratio — Pass 2 enrichment; null here, never
            # fabricated).
            "scorecard_factors": {
                "tbr_last_pct": delta_out.get("tbr_last_pct"),
                "net_buy_ratio": fut_book_flow.get("buy_share"),
                "oi_change_pct_1h": (
                    ((oi_out.get("inflow_outflow") or {}).get("1h") or {}).get("change_pct")
                ),
                "top_long_pct": _top_long_pct(oi_out),
                "bid_ask_qty_ratio": None,  # needs depth book — Pass 2
            },
        }


def _top_long_pct(oi_out: dict[str, Any]) -> float | None:
    """Top-trader long share of open interest (None-safe)."""
    oi = oi_out.get("raw_open_interest")
    wc = oi_out.get("weighted_contracts") or {}
    top_long = wc.get("top_long_contracts")
    if isinstance(oi, (int, float)) and isinstance(top_long, (int, float)) and oi:
        try:
            return float(top_long) / float(oi)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None