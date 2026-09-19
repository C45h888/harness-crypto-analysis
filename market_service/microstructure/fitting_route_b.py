"""Route B — Depth-scaled OFI model (log-log depth scaling).

Statistical inference — first estimate the relationship between price impact
and depth, then scale the current impact coefficient by current depth:

    beta_i = c * AD_i^(-lambda)                 (block-level log-log fit)
    beta_hat(AD_t) = c_hat * AD_t^(-lambda_hat) (current-depth scenario)

This substrate answers: "How does the OFI-implied price impact change when we
condition on available depth?"  It is a depth-scaled impact scenario, not a
fully materialized combined structural model.

Route B depends on PriceImpactFit outputs from Route A but does NOT import
from fitting_route_a — it receives fits as function arguments.  This keeps
the dependency graph acyclic (both consume contracts).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext

from .contracts import FIT_MODEL_VERSION, DepthScalingFit, PriceImpactFit
from .ofi import DEPTH_ESTIMATOR
from .fitting_common import PRECISION, MIN_DEPTH_BLOCKS, _fit_id


def fit_depth_scaling(
    block_fits: list[PriceImpactFit],
    *,
    symbol: str,
    venue: str,
    min_blocks: int = MIN_DEPTH_BLOCKS,
) -> DepthScalingFit:
    """Log-log fit ln(β_i) = ln(c) − λ·ln(AD_i) across estimation blocks.

    A single 30-minute window produces ONE β — depth scaling is unidentified
    from one point. At least ``min_blocks`` blocks with positive β, positive
    AD, and at least two distinct AD values are required; otherwise the fit
    is returned with status ``insufficient`` and null coefficients (never a
    fabricated value).
    """
    usable = [
        f for f in block_fits
        if f.mean_ad is not None and f.mean_ad > 0 and f.beta > 0
        and f.status in ("validated", "provisional")
        and not f.sensitivity
    ]
    fit_ids = tuple(f.fit_id for f in block_fits)
    digest = hashlib.sha256(json.dumps({
        "fit_ids": sorted(fit_ids), "model_version": FIT_MODEL_VERSION,
        "min_blocks": min_blocks,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _make(c: Decimal | None, lam: Decimal | None, stderr: Decimal | None,
              r2: Decimal | None, n_blocks: int, status: str) -> DepthScalingFit:
        return DepthScalingFit(
            fit_id=_fit_id("depth", digest), symbol=symbol.upper(), venue=venue,
            c=c, lambda_=lam, stderr_lambda=stderr, n_blocks=n_blocks, r2=r2,
            fit_ids=fit_ids, depth_estimator=DEPTH_ESTIMATOR,
            model_version=FIT_MODEL_VERSION, status=status,
        )

    if len(usable) < min_blocks:
        return _make(None, None, None, None, len(usable), "insufficient")
    if len({f.mean_ad for f in usable}) < 2:
        return _make(None, None, None, None, len(usable), "insufficient")

    with localcontext() as ctx:
        ctx.prec = PRECISION
        # usable filter guarantees non-None positive mean_ad; materialize the
        # narrowed values explicitly so the type checker sees Decimal, not
        # Optional[Decimal].
        pairs: list[tuple[Decimal, Decimal]] = []
        for fit in usable:
            ad = fit.mean_ad
            assert ad is not None  # guaranteed by the usable filter above
            pairs.append((ad, fit.beta))
        xs = [ad.ln() for ad, _beta in pairs]
        ys = [beta.ln() for _ad, beta in pairs]
        n = len(usable)
        x_bar = sum(xs, Decimal(0)) / Decimal(n)
        y_bar = sum(ys, Decimal(0)) / Decimal(n)
        sxx = sum(((x - x_bar) ** 2 for x in xs), Decimal(0))
        sxy = sum(((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)), Decimal(0))
        sst = sum(((y - y_bar) ** 2 for y in ys), Decimal(0))
        if sxx == 0:
            return _make(None, None, None, None, n, "insufficient")
        slope = sxy / sxx          # = −λ
        intercept = y_bar - slope * x_bar  # = ln(c)
        lam = -slope
        c = intercept.exp()
        residuals = [y - intercept - slope * x for x, y in zip(xs, ys)]
        ssr = sum((e ** 2 for e in residuals), Decimal(0))
        r2 = (Decimal(1) - ssr / sst) if sst != 0 else None
        hc0_var = sum(((e ** 2) * ((x - x_bar) ** 2) for e, x in zip(residuals, xs)), Decimal(0))
        stderr_lambda = (hc0_var.sqrt() / sxx) if n > 2 else None
        status = "validated" if n >= 2 * min_blocks and r2 is not None else "provisional"
        return _make(c, lam, stderr_lambda, r2, n, status)