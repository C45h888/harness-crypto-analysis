"""Stage analysis worker — deterministic Wyckoff-style 4h stage inference.

TRIGGER SEMANTICS (deterministic, no polling):
* Wake  — ONE blocking XREADGROUP on the technicals substrate's state stream.
* Fire  — the ``infer_stage`` STAGE label changed vs this worker's own last
          persisted projection (probe-against-own-state: technicals re-fires
          that infer the same stage do NOT fire this worker).
* Floor — L3 staleness heartbeat keeps the projection alive; the 4h stage is
          the slow regime context by design (cooldown is wide).

Compute imports exactly ONE analysis module (purity contract):
``market_service.analysis.stage`` — ``stage_window_inputs`` derives the 4h
scalars (the 48 x 5m klines in the derivative cache ARE a 4h span) and
``infer_stage`` maps the scalars + keystone-migration steps to
ACCUMULATION / MARKUP / DISTRIBUTION / MARKDOWN / TRANSITION.

Inputs: derivative cache (``klines`` 5m, ``oi_history``, ``funding``,
``top_ls``/``global_ls``) + the ``migration`` substrate (keystone up/down
steps). Null discipline: no klines -> no stage, ever.
"""

from __future__ import annotations

from typing import Any

from market_service.analysis.stage import infer_stage, stage_window_inputs
from market_service.analysis_worker.core import AnalysisPlaneMixin
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore


def _float(value: Any) -> float | None:
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _long_pct(row: Any) -> float | None:
    if isinstance(row, dict):
        for k in ("long_account", "longAccount"):
            v = _float(row.get(k))
            if v is not None:
                return v
    return None


def _migration_steps(dep: dict[str, Any] | None) -> tuple[int, int]:
    """(up_steps, down_steps) from the migration substrate's hourly keystones."""
    out = (dep or {}).get("output") or {}
    rows = (out.get("hourly_keystone_migration") or {}).get("hourly") or []
    up = sum(1 for r in rows if isinstance(r, dict) and r.get("migration") == "UP")
    down = sum(1 for r in rows if isinstance(r, dict) and r.get("migration") == "DOWN")
    return up, down


def compose_stage_input(window: dict[str, Any]) -> dict[str, Any] | None:
    """Pure composition: derivative cache + migration substrate -> ``infer_stage`` kwargs."""
    fut = window.get("futures") or {}
    klines = fut.get("klines") or []
    if not klines:
        return None
    scalars = stage_window_inputs(klines, fut.get("oi_history") or [])
    deps = window.get("substrate_dependencies") or {}
    up_steps, down_steps = _migration_steps(deps.get("migration"))
    funding = fut.get("funding") or {}
    funding_rate = None
    for k in ("last_funding_rate", "lastFundingRate", "funding"):
        funding_rate = _float(funding.get(k))
        if funding_rate is not None:
            break
    return {
        "px_chg_4h": scalars["px_chg_4h"],
        "oi_chg_4h": scalars["oi_chg_4h"],
        "up_pct_4h": scalars["up_pct_4h"],
        "up_steps": up_steps,
        "down_steps": down_steps,
        "funding_bps": (funding_rate * 10_000) if funding_rate is not None else None,
        "top_long_pct": _long_pct(fut.get("top_ls")[-1]
                                  if isinstance(fut.get("top_ls"), list) and fut["top_ls"]
                                  else None),
        "global_long_pct": _long_pct(fut.get("global_ls")[-1]
                                     if isinstance(fut.get("global_ls"), list) and fut["global_ls"]
                                     else None),
    }


class StageWorker(AnalysisPlaneMixin, SubstrateWorkerCore):
    SUBSTRATE_NAME = "stage"
    # Trigger: the technicals substrate's state stream (4h context).
    INPUT_STREAMS = ("substrate:technicals",)
    DEPENDENCIES = ("technicals", "oi", "migration")
    DERIVATIVE_INPUTS = ("klines", "oi_history", "funding", "top_ls", "global_ls")
    CADENCE = CadenceProfile(cooldown_s=300, staleness_s=600)

    # ------------------------------------------------------------------
    # L2 — significance probe (probe-against-own-state stage transition)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        kw = compose_stage_input(window)
        if kw is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_klines"})
        try:
            result = infer_stage(**kw)
        except (KeyError, TypeError, ValueError):
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "stage_failed"})
        stage = result.get("stage")
        prev = ((last_state or {}).get("output") or {}).get("stage")
        if prev is not None and stage != prev:
            return TriggerDecision(fired=True, source="probe",
                                   predicates={"stage_transition": {
                                       "from": prev, "to": stage}})
        return TriggerDecision(fired=False, source="probe", predicates={})

    # ------------------------------------------------------------------
    # Compute — stage substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        kw = compose_stage_input(evidence)
        if kw is None:
            # Null discipline: no klines -> no market stage, ever.
            return {}
        result = infer_stage(**kw)
        return {
            "stage": result["stage"],
            "score": result["score"],
            "confidence": result["confidence"],
            "reasons": result["reasons"],
            "inputs": result["inputs"],
        }
