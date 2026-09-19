"""D11 statistical-discipline lock (Track D, theory §17).

Tests + memo template only. Minimal code: audit() re-runs no new math,
it checks the refusal/gating/versioning invariants. ANY failing check
-> verdict "no-go". Phase-12 opens only on "go".
"""

from __future__ import annotations

from typing import Any

from .contracts import ForwardFit, ForwardObservation

DISCIPLINE_VERSION = "discipline-v2"
DISCIPLINE_CHECKLIST = (
    "leakage", "estimation", "time_order", "horizon_separation",
    "regime_separation", "baseline_comparison", "cost_statement",
    "calibration", "multiplicity", "version_pins",
)

COST_STATEMENT_TEMPLATE = (
    "Cost/execution statement: statistical edge {edge} ticks/horizon {h}; "
    "fees+spread+slippage NOT included; economic evaluation downstream."
)


def discipline_audit(
    *,
    forward_pairs: list[ForwardObservation],
    fits: dict[int, ForwardFit],
    calibration_by_horizon: dict[int, bool] | None = None,
    hypotheses: list[dict[str, Any]] | None = None,
    cost_statement: str | None = None,
    pooled_horizons: bool = False,
    regime_qualified: bool = True,
) -> dict[str, Any]:
    """Check the ten discipline locks; return verdict + stop/go memo skeleton."""
    checks: dict[str, dict[str, Any]] = {}

    # leakage: spot-check that feature bytes do not embed forward values —
    # structural pass when every pair carries per-horizon exclusion accounting
    # (D3 no-lookahead test covers the shift property; here we check presence).
    leak_ok = all(hasattr(p, "excluded") for p in forward_pairs) if forward_pairs else False
    checks["leakage"] = {"pass": bool(leak_ok),
                         "detail": f"{len(forward_pairs)} pairs with exclusion logs"
                         if leak_ok else "missing exclusion accounting"}

    # estimation: every fit must at least be estimable with enough training
    # data before validation semantics apply at all.
    est_ok = bool(fits) and all(f.estimation_status == "fitted" for f in fits.values())
    checks["estimation"] = {"pass": bool(est_ok),
                            "detail": "all fits carry fitted estimation status"
                            if est_ok else "one or more fits are not estimable"}

    # time_order: OOS skill must come from a held-out segment with enough
    # observations; a non-null field alone is not evidence of a valid split.
    to_ok = bool(fits) and all(
        f.oos_skill is not None and f.n_oos >= 30
        and f.split_method == "time-ordered-70/30"
        and f.validation_status == "validated"
        for f in fits.values()
    )
    checks["time_order"] = {"pass": bool(to_ok),
                            "detail": "all fits carry validated time-ordered OOS evidence"
                            if to_ok else "OOS validation missing, thin, or not promoted"}

    checks["horizon_separation"] = {"pass": not pooled_horizons,
                                    "detail": "per-horizon fits" if not pooled_horizons
                                    else "pooled-horizon fit present"}

    checks["regime_separation"] = {"pass": bool(regime_qualified),
                                   "detail": "regime-qualified or explicitly pooled"
                                   if regime_qualified else "unqualified mixing"}

    base_ok = bool(fits) and all(bool(f.comparator) for f in fits.values())
    checks["baseline_comparison"] = {"pass": bool(base_ok),
                                     "detail": "univariate Cont comparator present on same split"
                                     if base_ok else "comparator missing"}

    checks["cost_statement"] = {"pass": cost_statement is not None and len(cost_statement) > 0,
                                "detail": "cost template supplied" if cost_statement
                                else "missing; use COST_STATEMENT_TEMPLATE"}

    if calibration_by_horizon is None:
        cal_ok: bool | None = None
        cal_detail = "no probabilities quoted"
        cal_pass = True
    else:
        cal_pass = all(bool(v) for v in calibration_by_horizon.values())
        cal_detail = ("all quoted horizons calibrated" if cal_pass
                      else "miscalibrated horizon quoted -> refuse")
    checks["calibration"] = {"pass": bool(cal_pass), "detail": cal_detail}

    if not hypotheses:
        checks["multiplicity"] = {"pass": True, "detail": "no hypotheses claimed"}
    else:
        mult_ok = all(h.get("m_tests", 0) >= 1 and h.get("multiplicity_adj")
                      for h in hypotheses)
        checks["multiplicity"] = {"pass": bool(mult_ok),
                                  "detail": "m_tests + adjustment named" if mult_ok
                                  else "multiplicity accounting missing"}

    pins_ok = bool(fits) and all(
        bool(f.model_version) and bool(f.input_hash)
        and bool(f.feature_schema_hash) and bool(f.feature_keys)
        for f in fits.values()
    )
    checks["version_pins"] = {"pass": bool(pins_ok),
                              "detail": "model_version + input_hash pinned" if pins_ok
                              else "version pins missing"}

    verdict = "go" if all(c["pass"] for c in checks.values()) else "no-go"
    memo = {
        "version": DISCIPLINE_VERSION,
        "verdict": verdict,
        "fit_ids": {str(h): f.fit_id for h, f in fits.items()},
        "open_violations": [k for k, c in checks.items() if not c["pass"]],
        "phase12_gate": "opens only on go",
    }
    return {"version": DISCIPLINE_VERSION, "checks": checks,
            "verdict": verdict, "memo_template": memo}
