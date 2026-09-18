"""D7 formal hypothesis testing (Track D, theory §11).

Independent module by design: fitting NEVER calls this. Call order is
always fit -> predict -> scenario -> [question exists] -> test_hypothesis().
p<0.05 is evidence, never an execution predicate (no signal/action field
exists on the output type).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext
from math import erf, sqrt as _sqrt
from typing import Any

from .contracts import (
    FORWARD_MODEL_VERSION,
    HYPOTHESIS_VERSION,
    ForwardFit,
    ForwardObservation,
    HypothesisEvidence,
)

PRECISION = 50
SPLIT_LABEL = "time-ordered 70/30 train/test"


def _phi(z: float) -> float:
    return 0.5 * (1.0 + erf(z / _sqrt(2.0)))


def _hypo_input_hash(fit: ForwardFit, hypothesis_id: str, h0: str, h1: str,
                     m_tests: int, method: str) -> str:
    payload = {
        "fit_id": fit.fit_id,
        "hypothesis_id": hypothesis_id,
        "h0": h0, "h1": h1,
        "horizon_ms": fit.horizon_ms,
        "m_tests": m_tests, "method": method,
        "version": HYPOTHESIS_VERSION,
        "model_version": FORWARD_MODEL_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_hypothesis(
    fit: ForwardFit,
    pairs: list[ForwardObservation],
    *,
    hypothesis_id: str,
    h0: str = "H0: E[dP|X]<=0",
    h1: str = "H1: E[dP|X]>0",
    m_tests: int = 1,
    method: str = "bonferroni",
) -> HypothesisEvidence:
    """One-sided test of E[dP|X]>0 from a FITTED ForwardFit (pure).

    Effect = expected Y(h) at the mean-x of usable pairs; SE from fit
    stderr projection (stated normal-approx). Multiplicity: bonferroni
    default (p_adj=min(1,p*m)); holm recorded as holm-m=N (same
    single-test adjustment, family noted); "none" only when m==1
    pre-registered single. Raises on insufficient fits.
    """
    if not hypothesis_id:
        raise ValueError("hypothesis_id is required (pre-registration)")
    if m_tests < 1:
        raise ValueError("m_tests must be >= 1")
    if fit.status not in ("validated", "provisional"):
        raise ValueError(f"cannot test hypothesis from {fit.status} fit {fit.fit_id}")
    usable = [p for p in pairs
              if p.x.quality == "exact_feed" and p.y_ticks.get(fit.horizon_ms) is not None]
    n = len(usable)
    digest = _hypo_input_hash(fit, hypothesis_id, h0, h1, m_tests, method)
    status = fit.status if n >= 30 else "insufficient"
    with localcontext() as ctx:
        ctx.prec = PRECISION
        betas = {k: Decimal(v) for k, v in fit.betas.items()}
        # Mean-x across usable pairs over keys present in the fit.
        keys = [k for k in betas if k != "intercept"]
        mean_x: dict[str, Decimal] = {}
        for k in keys:
            vals = [Decimal(p.x.fields[k]) for p in usable if k in p.x.fields]
            mean_x[k] = sum(vals, Decimal(0)) / Decimal(len(vals)) if vals else Decimal(0)
        effect = betas.get("intercept", Decimal(0)) + sum(
            betas[k] * mean_x[k] for k in keys)
        # SE projection from per-coefficient stderr (independence approx, stated).
        se: Decimal | None = None
        try:
            terms = []
            if fit.stderr.get("intercept") is not None:
                terms.append(Decimal(str(fit.stderr["intercept"])) ** 2)
            for k in keys:
                s = fit.stderr.get(k)
                if s is not None:
                    terms.append((Decimal(str(s)) * abs(mean_x[k])) ** 2)
            se = sum(terms, Decimal(0)).sqrt() if terms else None
        except Exception:
            se = None
        ci_lo = ci_hi = None
        p_val: Decimal | None = None
        if se is not None and se > 0:
            ci_lo = effect - Decimal("1.96") * se
            ci_hi = effect + Decimal("1.96") * se
            p_val = Decimal(str(1.0 - _phi(float(effect / se))))
        # Multiplicity adjustment (family size actually tried).
        adj_label: str
        p_adj = p_val
        if m_tests == 1 and method in ("none", "bonferroni"):
            adj_label = "none-prereg-single" if method == "none" else "bonferroni-m=1"
        elif method == "holm":
            adj_label = f"holm-m={m_tests}"
            if p_adj is not None:
                p_adj = min(Decimal(1), p_adj * Decimal(m_tests))
        else:  # bonferroni default
            adj_label = f"bonferroni-m={m_tests}"
            if p_adj is not None:
                p_adj = min(Decimal(1), p_adj * Decimal(m_tests))
        out: dict[str, Any] = {
            "hypothesis_id": hypothesis_id, "h0": h0, "h1": h1,
            "horizon_ms": fit.horizon_ms,
            "effect": format(effect, "f"),
            "se": format(se, "f") if se is not None else None,
            "ci_lo": format(ci_lo, "f") if ci_lo is not None else None,
            "ci_hi": format(ci_hi, "f") if ci_hi is not None else None,
            "p_value": format(p_adj, "f") if p_adj is not None else None,
            "equiv_stat": None,
            "method": f"ols-hc0-onesided-v1:{method}",
            "n": n, "split": SPLIT_LABEL,
            "oos_skill": fit.oos_skill,
            "multiplicity_adj": adj_label, "m_tests": m_tests,
            "model_version": FORWARD_MODEL_VERSION,
            "input_hash": digest, "status": status,
        }
        return HypothesisEvidence(**out)


# Pytest collects any `test_*` function — this is a library runner, not a test.
test_hypothesis.__test__ = False  # type: ignore[attr-defined]
