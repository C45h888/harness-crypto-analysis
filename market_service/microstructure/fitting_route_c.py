"""Route C — Full multivariate, horizon-native forward model.

Statistical inference (plain-ASCII form; LaTeX backslashes removed so the
module parses without escape warnings):

    Y_t(h) = beta_0 + sum_j beta_j * X_t,j + epsilon_t

with the feature information set

    X_t = [OFI, AD, DMU, Spread, OBI, CVD_slope, Skew]

and the forward target

    Y_t(h) = Mid(t+h) - Mid(t)          for h in {1s, 5s, 30s, 60s, ...}

This substrate answers: "Given the complete observable market state at event
time t, what future price-displacement distribution does the model imply at
horizon h?"

It produces mu_C = beta_0 + sum_j beta_j * X_j and sigma_C (residual
uncertainty), and — only if the probability validation gate passes — the
conditional exceedance probability

    P(Y > theta | X) = Phi((mu_C - theta) / sigma_C)

Route C is independent of Route A and Route B, sharing only contracts and
common utilities.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from decimal import Decimal, localcontext
from typing import Any, Iterable

from .contracts import (
    FEATURE_VECTOR_VERSION,
    FORWARD_MODEL_VERSION,
    BestQuoteState,
    FeatureVector,
    ForwardFit,
    ForwardObservation,
)
from .fitting_common import PRECISION, MIN_OBSERVATIONS, _fit_id

# ---------------------------------------------------------------------------
# Route C frozen constants
# ---------------------------------------------------------------------------

FORWARD_HORIZONS_MS: tuple[int, ...] = (1_000, 5_000, 30_000, 60_000)
MIN_OOS_OBSERVATIONS = 30
OOS_SPLIT_METHOD = "time-ordered-70/30"
FORWARD_SCENARIO_VERSION = "fwd-scenario-v1"

# Frozen per-field definition labels for xt-v2.
FEATURE_DEF_VERSIONS: dict[str, str] = {
    "ofi_10s": "interval_ofi-v2",
    "ad_10s": "event_mean_best_bid_ask_v1",
    "dmu": "microprice-v1",
    "spread_bps": "spread-v1",
    "obi_top": "obi-top-v1",
    "cvd_slope_60s": "cvd-slope-v1",
    "skew_bps": "skew-v1",
}


# ---------------------------------------------------------------------------
# Feature schema & input hashing
# ---------------------------------------------------------------------------


def feature_schema_hash(
    *, vector_version: str, feature_keys: Iterable[str],
    def_versions: dict[str, str], quality_policy: str = "null-omission-v1",
) -> str:
    """Hash the exact feature information set and its definition versions."""
    keys = tuple(sorted(str(k) for k in feature_keys))
    payload = {
        "vector_version": vector_version,
        "feature_keys": keys,
        "def_versions": {k: def_versions.get(k, "") for k in keys},
        "quality_policy": quality_policy,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def feature_input_hash(
    *, symbol: str, venue: str, ts_ms: int,
    fields: dict[str, str], def_versions: dict[str, str],
) -> str:
    """SHA-256 over the canonical feature-vector bytes (replay guarantee)."""
    keys = tuple(sorted(fields))
    payload = {
        "symbol": symbol.upper(), "venue": venue, "ts_ms": ts_ms,
        "fields": fields, "def_versions": def_versions,
        "feature_keys": keys,
        "feature_schema_hash": feature_schema_hash(
            vector_version=FEATURE_VECTOR_VERSION,
            feature_keys=keys, def_versions=def_versions,
        ),
        "vector_version": FEATURE_VECTOR_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Feature vector builder
# ---------------------------------------------------------------------------


def build_feature_vector(
    *,
    symbol: str,
    venue: str,
    ts_ms: int,
    ofi: Decimal | None = None,
    average_depth: Decimal | None = None,
    quote: BestQuoteState | None = None,
    displacement: Decimal | None = None,
    cvd_slope: Decimal | None = None,
    quality: str = "exact_feed",
) -> FeatureVector:
    """Promote Decimal measurements into a versioned xt-v2 vector (pure).

    Floats from substrates/poller must NEVER be passed here — recompute in
    Decimal from raw events/trades first. Unavailable fields are omitted
    (NULL = not-provided, never zero-filled). ``displacement`` may be passed
    explicitly (D1 output) or derived from ``quote``; an explicit value wins.
    """
    if not symbol or not venue:
        raise ValueError("symbol and venue are required")
    if ts_ms <= 0:
        raise ValueError("ts_ms must be positive")
    for name, value in (("ofi", ofi), ("average_depth", average_depth),
                         ("displacement", displacement), ("cvd_slope", cvd_slope)):
        if value is not None and isinstance(value, float):
            raise TypeError(f"{name} must be Decimal, not float — recompute, never copy")
    fields: dict[str, str] = {}
    defs: dict[str, str] = {}

    def _put(key: str, value: Decimal | None) -> None:
        if value is not None:
            fields[key] = format(value, "f")
            defs[key] = FEATURE_DEF_VERSIONS[key]

    _put("ofi_10s", ofi)
    _put("ad_10s", average_depth)
    dmu = displacement
    if dmu is None and quote is not None:
        from .microprice import displacement as _dmu
        try:
            dmu = _dmu(quote)
        except ValueError:
            dmu = None
    _put("dmu", dmu)
    if quote is not None:
        quote.validate()
        with localcontext() as ctx:
            ctx.prec = PRECISION
            mid = (quote.bid_price + quote.ask_price) / Decimal(2)
            if mid > 0:
                _put("spread_bps", (quote.ask_price - quote.bid_price) / mid * Decimal(10000))
        queue = quote.bid_qty + quote.ask_qty
        _put("obi_top", (quote.bid_qty - quote.ask_qty) / queue if queue != 0 else None)
        with localcontext() as ctx:
            ctx.prec = PRECISION
            mu = ((quote.ask_price * quote.bid_qty + quote.bid_price * quote.ask_qty)
                  / queue) if queue != 0 else None
            if mu is not None and mid > 0:
                _put("skew_bps", (mu / mid - Decimal(1)) * Decimal(10000))
    _put("cvd_slope_60s", cvd_slope)
    keys = tuple(sorted(fields))
    schema_digest = feature_schema_hash(
        vector_version=FEATURE_VECTOR_VERSION,
        feature_keys=keys, def_versions=defs,
    )
    digest = feature_input_hash(
        symbol=symbol, venue=venue, ts_ms=ts_ms, fields=fields, def_versions=defs,
    )
    return FeatureVector(
        symbol=symbol.upper(), venue=venue, ts_ms=ts_ms,
        vector_version=FEATURE_VECTOR_VERSION, fields=fields, def_versions=defs,
        quality=quality, input_hash=digest,
        feature_keys=keys, feature_schema_hash=schema_digest,
    )


# ---------------------------------------------------------------------------
# Forward observation builder
# ---------------------------------------------------------------------------


def build_forward_observations(
    vectors: list[FeatureVector],
    mids: list[tuple[int, Decimal]],
    *,
    tick_size: Decimal,
    venue: str,
    quality_spans: list[dict[str, Any]] | None = None,
) -> tuple[list[ForwardObservation], dict[str, int]]:
    """Join event-grain X_t to forward mid changes Y_t(h) (pure, no lookahead).

    ``mids`` is a sorted (ts_ms, mid) series from microstructure events.
    Each horizon resolves independently: gaps/halts/end-of-window/venue
    mismatch yield NULL + a counted reason, never a bridged value.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if isinstance(tick_size, float):
        raise TypeError("tick_size must be Decimal, not float")
    if not vectors:
        return [], {"excluded_total": 0}
    series = sorted(mids, key=lambda row: row[0])
    stamps = [ts for ts, _ in series]

    def _at_or_after(t: int) -> Decimal | None:
        """First mid with ts >= t (bisect; identical to linear scan)."""
        i = bisect.bisect_left(stamps, t)
        return series[i][1] if i < len(series) else None
    log: dict[str, int] = {"excluded_total": 0}
    out: list[ForwardObservation] = []
    for vec in vectors:
        if vec.venue != venue:
            y_t = {h: None for h in FORWARD_HORIZONS_MS}
            y_q = {h: None for h in FORWARD_HORIZONS_MS}
            exc = {h: "venue_mismatch" for h in FORWARD_HORIZONS_MS}
            for h in FORWARD_HORIZONS_MS:
                log[f"excluded_{h}"] = log.get(f"excluded_{h}", 0) + 1
            log["excluded_total"] += len(FORWARD_HORIZONS_MS)
            out.append(ForwardObservation(x=vec, y_ticks=y_t, y_quote=y_q,
                                          price_source="microstructure_mid", excluded=exc))
            continue
        base = _at_or_after(vec.ts_ms)
        y_t: dict[int, str | None] = {}
        y_q: dict[int, str | None] = {}
        exc2: dict[int, str] = {}
        for h in FORWARD_HORIZONS_MS:
            target = vec.ts_ms + h
            fwd = _at_or_after(target)
            bad_span = None
            for span in quality_spans or ():
                try:
                    span_lo = int(span["start_ts_ms"])
                    span_hi = int(span["end_ts_ms"])
                    span_quality = str(span.get("quality") or "")
                    if span_quality != "exact_feed" and span_lo < target and span_hi > vec.ts_ms:
                        bad_span = str(span.get("reason") or span_quality or "invalid_quality")
                        break
                except (KeyError, TypeError, ValueError):
                    bad_span = "invalid_quality_span"
                    break
            if bad_span is not None:
                y_t[h], y_q[h], exc2[h] = None, None, bad_span
                log[f"excluded_{h}"] = log.get(f"excluded_{h}", 0) + 1
                log["excluded_total"] += 1
                continue
            if base is None or fwd is None:
                y_t[h], y_q[h], exc2[h] = None, None, "end_of_window"
                log[f"excluded_{h}"] = log.get(f"excluded_{h}", 0) + 1
                log["excluded_total"] += 1
                continue
            with localcontext() as ctx:
                ctx.prec = PRECISION
                dq = fwd - base
                y_q[h] = format(dq, "f")
                y_t[h] = format(dq / tick_size, "f")
        out.append(ForwardObservation(x=vec, y_ticks=y_t, y_quote=y_q,
                                      price_source="microstructure_mid", excluded=exc2))
    return out, log


# ---------------------------------------------------------------------------
# Internal helpers for the OLS solver
# ---------------------------------------------------------------------------


def _forward_input_hash(symbol: str, venue: str, horizon_ms: int,
                        pairs: list[ForwardObservation], *,
                        feature_keys: tuple[str, ...] = (),
                        feature_schema_hash_value: str = "") -> str:
    payload = {
        "symbol": symbol.upper(), "venue": venue, "horizon_ms": horizon_ms,
        "model_version": FORWARD_MODEL_VERSION,
        "feature_keys": feature_keys,
        "feature_schema_hash": feature_schema_hash_value,
        "xs": [p.x.to_dict() for p in pairs],
        "ys": [p.y_ticks.get(horizon_ms) for p in pairs],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _solve_ols(
    X: list[list[Decimal]], y: list[Decimal],
) -> tuple[list[Decimal], list[list[Decimal]]] | None:
    """Solve a small OLS system and return coefficients plus (X'X)^-1."""
    n = len(y)
    if not X or n == 0:
        return None
    k = len(X[0])
    if n < k:
        return None
    xtx = [[sum(X[i][a] * X[i][b] for i in range(n))
             for b in range(k)] for a in range(k)]
    xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
    aug = [row[:] + [xty[a]] for a, row in enumerate(xtx)]
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(aug[r][col]))
        if aug[pivot][col] == 0:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_value = aug[col][col]
        aug[col] = [v / pivot_value for v in aug[col]]
        for r in range(k):
            if r == col or aug[r][col] == 0:
                continue
            factor = aug[r][col]
            aug[r] = [rv - factor * cv for rv, cv in zip(aug[r], aug[col])]
    return [aug[a][k] for a in range(k)], [row[:k] for row in aug]


def _ols_diagnostics(
    X: list[list[Decimal]], y: list[Decimal], beta: list[Decimal],
    inverse: list[list[Decimal]],
) -> dict[str, Any]:
    """Calculate fit diagnostics, HC0 errors, and heteroskedasticity flag."""
    n = len(y)
    k = len(beta)
    yhat = [sum(b * v for b, v in zip(beta, row)) for row in X]
    residuals = [actual - predicted for actual, predicted in zip(y, yhat)]
    ybar = sum(y, Decimal(0)) / Decimal(n)
    sst = sum((value - ybar) ** 2 for value in y)
    ssr = sum(value ** 2 for value in residuals)
    r2 = (Decimal(1) - ssr / sst) if sst != 0 else None
    resid_std = (ssr / Decimal(max(n - k, 1))).sqrt() if n > k else None
    meat = [[sum((residuals[i] ** 2) * X[i][a] * X[i][b]
                 for i in range(n)) for b in range(k)] for a in range(k)]
    stderr: list[Decimal | None] = []
    for j in range(k):
        try:
            variance = sum(inverse[j][a] * meat[a][b] * inverse[j][b]
                           for a in range(k) for b in range(k))
            stderr.append(variance.sqrt() if variance >= 0 else None)
        except Exception:
            stderr.append(None)
    order = sorted(range(n), key=lambda i: abs(yhat[i] - ybar))
    half = n // 2
    low = sum(residuals[i] ** 2 for i in order[:half]) / Decimal(max(half, 1))
    high = sum(residuals[i] ** 2 for i in order[half:]) / Decimal(max(n - half, 1))
    return {
        "yhat": yhat, "residuals": residuals, "r2": r2,
        "resid_std": resid_std, "stderr": stderr,
        "hetero": bool((high > low * 2) or (low > high * 2)) if n >= 4 else False,
    }


def _prediction_metrics(actual: list[Decimal], predicted: list[Decimal],
                        baseline: Decimal) -> dict[str, Decimal | None]:
    """OOS metrics against a fixed baseline mean."""
    if not actual:
        return {"r2": None, "mae": None, "rmse": None}
    errors = [a - p for a, p in zip(actual, predicted)]
    sse = sum(e ** 2 for e in errors)
    sst = sum((a - baseline) ** 2 for a in actual)
    return {
        "r2": (Decimal(1) - sse / sst) if sst != 0 else None,
        "mae": sum(abs(e) for e in errors) / Decimal(len(errors)),
        "rmse": (sse / Decimal(len(errors))).sqrt(),
    }


# ---------------------------------------------------------------------------
# Per-horizon multivariate OLS
# ---------------------------------------------------------------------------


def fit_forward_ols(
    pairs: list[ForwardObservation],
    *,
    symbol: str,
    venue: str,
    horizon_ms: int,
    feature_keys: list[str] | None = None,
    min_observations: int = MIN_OBSERVATIONS,
    min_oos_observations: int = MIN_OOS_OBSERVATIONS,
) -> tuple[ForwardFit, list[ForwardObservation]]:
    """Per-horizon multivariate OLS Y(h) ~ X (Decimal, time-ordered OOS).

    Usable rows: vector quality ``exact_feed`` with a non-NULL y for this
    horizon. Design columns: ``feature_keys`` (default: explicit sorted
    union of present fields, complete-case rows only, intercept always added).
    Time-ordered 70/30 train/test split is fitted on train only; OOS metrics
    score fixed train coefficients against the held-out segment. ``status``
    remains the compatibility projection, while ``estimation_status`` and
    ``validation_status`` distinguish a fitted model from a validated one.
    """
    if horizon_ms not in FORWARD_HORIZONS_MS:
        raise ValueError(f"unsupported horizon_ms: {horizon_ms!r}")
    candidates = [p for p in pairs
                  if p.x.quality == "exact_feed" and p.y_ticks.get(horizon_ms) is not None]
    requested_keys = tuple(sorted(feature_keys or {k for p in candidates for k in p.x.fields}))
    # Complete-case rows are explicit: missing is not zero and cannot silently
    # disappear during prediction. A different subset is a different model.
    usable = [p for p in candidates if all(k in p.x.fields for k in requested_keys)]
    n_excluded = len(pairs) - len(usable)
    definition_versions: dict[str, str] = {}
    for p in usable:
        for key in requested_keys:
            value = p.x.def_versions.get(key)
            if value is not None:
                definition_versions.setdefault(key, value)
    schema_digest = feature_schema_hash(
        vector_version=FEATURE_VECTOR_VERSION,
        feature_keys=requested_keys, def_versions=definition_versions,
    )
    digest = _forward_input_hash(
        symbol, venue, horizon_ms, pairs,
        feature_keys=requested_keys, feature_schema_hash_value=schema_digest,
    )
    model_meta: dict[str, Any] = {
        "feature_keys": requested_keys,
        "feature_schema_hash": schema_digest,
        "feature_definition_versions": definition_versions,
        "n_train": 0, "n_oos": 0, "oos_cut": 0,
        "split_method": OOS_SPLIT_METHOD,
        "train_r2": None, "oos_r2": None, "oos_mae": None,
        "oos_rmse": None, "baseline_oos_r2": None,
        "baseline_oos_mae": None, "estimation_status": "insufficient",
        "validation_status": "unvalidated", "probability_status": "not_requested",
        "oos_betas": {}, "oos_resid_std": None,
    }

    def _make(betas: dict[str, Decimal], stderr: dict[str, Decimal | None],
              r2: Decimal | None, resid: Decimal | None, hetero: bool,
              oos: Decimal | None, comp: dict[str, str],
              status: str, n: int) -> ForwardFit:
        return ForwardFit(
            fit_id=_fit_id("fwd", digest), symbol=symbol.upper(), venue=venue,
            horizon_ms=horizon_ms,
            betas={k: format(v, "f") for k, v in betas.items()},
            stderr={k: (format(v, "f") if v is not None else None) for k, v in stderr.items()},
            r2=format(r2, "f") if r2 is not None else None,
            resid_std=format(resid, "f") if resid is not None else None,
            hetero_flag=hetero, n_obs=n, n_excluded=n_excluded,
            oos_skill=format(oos, "f") if oos is not None else None,
            comparator=comp, input_hash=digest,
            model_version=FORWARD_MODEL_VERSION, status=status,
            feature_keys=model_meta["feature_keys"],
            feature_schema_hash=model_meta["feature_schema_hash"],
            feature_definition_versions=model_meta["feature_definition_versions"],
            n_train=model_meta["n_train"], n_oos=model_meta["n_oos"],
            split_method=model_meta["split_method"],
            train_r2=(format(model_meta["train_r2"], "f")
                      if model_meta["train_r2"] is not None else None),
            oos_r2=(format(model_meta["oos_r2"], "f")
                    if model_meta["oos_r2"] is not None else None),
            oos_mae=(format(model_meta["oos_mae"], "f")
                     if model_meta["oos_mae"] is not None else None),
            oos_rmse=(format(model_meta["oos_rmse"], "f")
                      if model_meta["oos_rmse"] is not None else None),
            baseline_oos_r2=(format(model_meta["baseline_oos_r2"], "f")
                             if model_meta["baseline_oos_r2"] is not None else None),
            baseline_oos_mae=(format(model_meta["baseline_oos_mae"], "f")
                             if model_meta["baseline_oos_mae"] is not None else None),
            estimation_status=model_meta["estimation_status"],
            validation_status=model_meta["validation_status"],
            probability_status=model_meta["probability_status"],
            oos_betas={k: format(v, "f") for k, v in model_meta["oos_betas"].items()},
            oos_resid_std=(format(model_meta["oos_resid_std"], "f")
                           if model_meta["oos_resid_std"] is not None else None),
            oos_cut=model_meta["oos_cut"],
        )

    keys = list(requested_keys)
    # Drop zero-variance columns (constant fields are collinear with the
    # intercept and would singularize the normal equations). Dropped keys
    # are simply absent from betas/stderr — documented, never imputed.
    if usable:
        counts = len(usable)
        kept: list[str] = []
        for k in keys:
            vals = [p.x.fields.get(k) for p in usable]
            if all(v == vals[0] for v in vals):
                continue
            kept.append(k)
        keys = kept
    with localcontext() as ctx:
        ctx.prec = PRECISION
        if len(usable) < 2:
            return _make({"intercept": Decimal(0)}, {"intercept": None},
                          None, None, False, None, {}, "insufficient", len(usable)), usable
        cols = ["intercept", *keys]
        X: list[list[Decimal]] = []
        y: list[Decimal] = []
        kept: list[ForwardObservation] = []
        for p in usable:
            try:
                row = [Decimal(1)] + [Decimal(p.x.fields[k]) for k in keys]
                yval = Decimal(str(p.y_ticks[horizon_ms]))
            except Exception:
                continue
            X.append(row)
            y.append(yval)
            kept.append(p)
        n = len(y)
        if n < 2:
            return _make({"intercept": Decimal(0)}, {"intercept": None},
                          None, None, False, None, {}, "insufficient", n), usable
        if not keys:
            # Intercept-only null model: estimable but carries no feature
            # information — must not wear validated/provisional.
            with localcontext() as ctx:
                ctx.prec = PRECISION
                ybar0 = sum(y, Decimal(0)) / Decimal(n)
            return _make({"intercept": ybar0}, {"intercept": None},
                          None, None, False, None, {}, "insufficient", n), kept
        k = len(cols)
        solved = _solve_ols(X, y)
        if solved is None:
            return _make({c: Decimal(0) for c in cols}, {c: None for c in cols},
                          None, None, False, None, {}, "insufficient", n), kept

        beta, inverse = solved
        full_stats = _ols_diagnostics(X, y, beta, inverse)
        betas = dict(zip(cols, beta))
        se = dict(zip(cols, full_stats["stderr"]))
        r2 = full_stats["r2"]
        resid_std = full_stats["resid_std"]
        hetero = bool(full_stats["hetero"])

        # Genuine time-ordered OOS evaluation: coefficients are fitted on the
        # first 70% only and never use the held-out targets.
        cut = max(1, int(n * 0.7))
        train_X, train_y = X[:cut], y[:cut]
        test_X, test_y = X[cut:], y[cut:]
        train_solved = _solve_ols(train_X, train_y)
        oos: Decimal | None = None
        oos_metrics: dict[str, Decimal | None] = {"r2": None, "mae": None, "rmse": None}
        train_stats: dict[str, Any] | None = None
        if train_solved is not None:
            train_beta, train_inverse = train_solved
            train_stats = _ols_diagnostics(train_X, train_y, train_beta, train_inverse)
            train_mean = sum(train_y, Decimal(0)) / Decimal(len(train_y))
            test_hat = [sum(b * v for b, v in zip(train_beta, row)) for row in test_X]
            oos_metrics = _prediction_metrics(test_y, test_hat, train_mean)
            oos = oos_metrics["r2"]
            model_meta["oos_betas"] = dict(zip(cols, train_beta))
            model_meta["oos_resid_std"] = train_stats["resid_std"]
        model_meta["n_train"] = len(train_y)
        model_meta["n_oos"] = len(test_y)
        model_meta["oos_cut"] = cut
        model_meta["train_r2"] = train_stats["r2"] if train_stats else None
        model_meta["oos_r2"] = oos_metrics["r2"]
        model_meta["oos_mae"] = oos_metrics["mae"]
        model_meta["oos_rmse"] = oos_metrics["rmse"]
        # Univariate OFI comparator on the identical time split.
        comp: dict[str, str] = {}
        baseline_oos: dict[str, Decimal | None] = {"r2": None, "mae": None, "rmse": None}
        if "ofi_10s" in keys:
            ofi_index = 1 + keys.index("ofi_10s")
            baseline_X = [[Decimal(1), row[ofi_index]] for row in X]
            baseline_train = _solve_ols(baseline_X[:cut], y[:cut])
            if baseline_train is not None:
                baseline_beta, _baseline_inverse = baseline_train
                baseline_mean = sum(y[:cut], Decimal(0)) / Decimal(max(cut, 1))
                baseline_hat = [sum(b * v for b, v in zip(baseline_beta, row))
                                for row in baseline_X[cut:]]
                baseline_oos = _prediction_metrics(y[cut:], baseline_hat, baseline_mean)
                comp = {
                    "beta_ofi": format(baseline_beta[1], "f"),
                    "n": str(n),
                    "oos_r2": (format(baseline_oos["r2"], "f")
                               if baseline_oos["r2"] is not None else ""),
                    "oos_mae": (format(baseline_oos["mae"], "f")
                                if baseline_oos["mae"] is not None else ""),
                }
        model_meta["baseline_oos_r2"] = baseline_oos["r2"]
        model_meta["baseline_oos_mae"] = baseline_oos["mae"]

        if n < min_observations:
            status = "insufficient"
            model_meta["estimation_status"] = "insufficient"
            model_meta["validation_status"] = "unvalidated"
        else:
            model_meta["estimation_status"] = "fitted"
            enough_oos = len(test_y) >= min_oos_observations
            enough_train = len(train_y) >= min_observations
            positive_skill = oos is not None and oos > 0
            baseline_available = baseline_oos["r2"] is not None
            if not enough_oos or oos is None or not enough_train:
                model_meta["validation_status"] = "unvalidated"
            elif positive_skill and baseline_available and not hetero:
                model_meta["validation_status"] = "validated"
            else:
                model_meta["validation_status"] = "provisional"
            status = ("validated" if model_meta["validation_status"] == "validated"
                      else "provisional")
            model_meta["probability_status"] = "model_implied"
        return _make(betas, se, r2, resid_std, hetero, oos, comp, status, n), kept


# ---------------------------------------------------------------------------
# Distribution prediction & calibration
# ---------------------------------------------------------------------------


def predict_distribution(
    fit: ForwardFit, x: FeatureVector, *, theta_ticks: Decimal | None = None,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expected Y(h) plus intervals and explicitly gated probabilities.

    The Gaussian calculation is retained, but probabilities are only emitted
    when an explicit OOS calibration report has ``status == 'passed'``.
    Without that report the mean and residual interval remain available while
    probability fields are null with a refusal reason.
    """
    if fit.status not in ("validated", "provisional"):
        raise ValueError(f"cannot predict from {fit.status} forward fit {fit.fit_id}")
    if (x.symbol.upper(), x.venue) != (fit.symbol.upper(), fit.venue):
        raise ValueError("feature vector belongs to a different instrument")
    if fit.feature_keys:
        if fit.feature_schema_hash != x.feature_schema_hash:
            raise ValueError(
                "feature schema hash mismatch: "
                f"fit={fit.feature_schema_hash!r}, vector={x.feature_schema_hash!r}"
            )
        expected = tuple(sorted(k for k in fit.feature_keys if k != "intercept"))
        actual = tuple(sorted(x.fields))
        if actual != expected:
            raise ValueError(
                "feature schema mismatch: "
                f"fit={expected!r}, vector={actual!r}"
            )
        if fit.feature_definition_versions:
            for key in expected:
                expected_def = fit.feature_definition_versions.get(key)
                actual_def = x.def_versions.get(key)
                if expected_def and actual_def != expected_def:
                    raise ValueError(
                        f"feature definition mismatch for {key}: "
                        f"fit={expected_def!r}, vector={actual_def!r}"
                    )
    calibration_status = (calibration or {}).get("status") if calibration else None
    probability_allowed = (
        calibration_status == "passed"
        and fit.validation_status == "validated"
    )
    if probability_allowed:
        probability_reason = None
    elif fit.validation_status != "validated":
        probability_reason = "forecast_validation_not_passed"
    else:
        probability_reason = (
            "calibration_not_provided" if calibration is None
            else str((calibration or {}).get("reason") or "calibration_not_passed")
        )
    with localcontext() as ctx:
        ctx.prec = PRECISION
        betas = {kk: Decimal(vv) for kk, vv in fit.betas.items()}
        exp = betas.get("intercept", Decimal(0)) + sum(
            betas.get(kk, Decimal(0)) * Decimal(x.fields[kk])
            for kk in x.fields if kk in betas
        )
        resid = Decimal(fit.resid_std) if fit.resid_std is not None else None
        out: dict[str, Any] = {
            "expected_ticks": format(exp, "f"),
            "variance_ticks": format(resid * resid, "f") if resid is not None else None,
            "interval_lo_95": format(exp - Decimal("1.96") * resid, "f") if resid is not None else None,
            "interval_hi_95": format(exp + Decimal("1.96") * resid, "f") if resid is not None else None,
            "p_positive": None,
            "p_above_theta": None,
            "probability_status": "model_implied" if probability_allowed else "refused",
            "probability_reason": probability_reason,
            "resid_std": format(resid, "f") if resid is not None else None,
            "fit_id": fit.fit_id,
            "horizon_ms": fit.horizon_ms,
            "forecast_type": "native_forecast",
            "horizon_regime": "native",
            "assumptions": {
                "gaussian_probability": True,
                "linear_impact_scaling": False,
            },
            "assumption": "normal-approx probabilities from residual std; stated, not proven",
            "calibration": calibration,
        }
        if probability_allowed and resid is not None and resid > 0:
            from math import erf, sqrt as _sqrt
            z = float(exp / resid)
            out["p_positive"] = str(0.5 * (1.0 + erf(z / _sqrt(2.0))))
            if theta_ticks is not None:
                zt = float((exp - theta_ticks) / resid)
                out["p_above_theta"] = str(0.5 * (1.0 + erf(zt / _sqrt(2.0))))
        return out


def calibration_report(
    fit: ForwardFit,
    pairs: list[ForwardObservation],
    *,
    n_bins: int = 5,
    theta_ticks: Decimal | None = None,
    min_observations: int = MIN_OOS_OBSERVATIONS,
    max_bin_error: Decimal = Decimal("0.10"),
    min_bin_observations: int = 5,
) -> dict[str, Any]:
    """OOS reliability report for the Gaussian event probability.

    The report uses only the held-out segment and the train-only coefficients
    stored on ``ForwardFit``. It compares forecast probabilities with the
    realized binary event ``Y > theta``. A failed or thin report refuses the
    probability; it does not change the underlying mean forecast.
    """
    if n_bins < 2:
        raise ValueError("n_bins must be >= 2")
    if max_bin_error < 0 or min_bin_observations < 1:
        raise ValueError("invalid calibration gate configuration")
    candidates = [p for p in pairs
                  if p.x.quality == "exact_feed"
                  and p.y_ticks.get(fit.horizon_ms) is not None]
    keys = tuple(k for k in fit.feature_keys if k != "intercept")
    eligible = [p for p in candidates if all(k in p.x.fields for k in keys)]
    cut = fit.oos_cut if fit.oos_cut > 0 else max(1, int(len(eligible) * 0.7))
    oos_pairs = eligible[cut:]
    sigma_raw = fit.oos_resid_std
    if not fit.oos_betas or sigma_raw is None:
        return {
            "fit_id": fit.fit_id, "horizon_ms": fit.horizon_ms,
            "n": 0, "n_bins": n_bins, "bins": [],
            "status": "insufficient", "calibrated": False,
            "reason": "train_only_oos_predictions_unavailable",
            "refusal": "probability refused: OOS prediction population unavailable",
        }
    sigma = Decimal(str(sigma_raw))
    if sigma <= 0 or len(oos_pairs) < min_observations:
        return {
            "fit_id": fit.fit_id, "horizon_ms": fit.horizon_ms,
            "n": len(oos_pairs), "n_bins": n_bins, "bins": [],
            "status": "insufficient", "calibrated": False,
            "reason": "insufficient_oos_probability_observations",
            "refusal": "probability refused: insufficient OOS calibration sample",
        }
    from math import erf, sqrt as _sqrt
    threshold = theta_ticks or Decimal(0)
    scored: list[tuple[Decimal, int]] = []
    betas = {key: Decimal(value) for key, value in fit.oos_betas.items()}
    for pair in oos_pairs:
        expected = betas.get("intercept", Decimal(0)) + sum(
            betas.get(key, Decimal(0)) * Decimal(pair.x.fields[key])
            for key in keys
        )
        z = float((expected - threshold) / sigma)
        probability = Decimal(str(0.5 * (1.0 + erf(z / _sqrt(2.0)))))
        realized = int(Decimal(str(pair.y_ticks[fit.horizon_ms])) > threshold)
        scored.append((probability, realized))
    bins: list[dict[str, Any]] = []
    max_error = Decimal(0)
    min_bin_ok = True
    for b in range(n_bins):
        lo = Decimal(b) / Decimal(n_bins)
        hi = Decimal(b + 1) / Decimal(n_bins)
        chunk = [row for row in scored if (lo <= row[0] < hi) or
                 (b == n_bins - 1 and row[0] == hi)]
        if not chunk:
            bins.append({"bin": b, "n": 0, "predicted": None,
                         "realized": None, "absolute_error": None})
            min_bin_ok = False
            continue
        predicted = sum((row[0] for row in chunk), Decimal(0)) / Decimal(len(chunk))
        realized = Decimal(sum(row[1] for row in chunk)) / Decimal(len(chunk))
        error = abs(predicted - realized)
        max_error = max(max_error, error)
        if len(chunk) < min_bin_observations:
            min_bin_ok = False
        bins.append({
            "bin": b, "n": len(chunk),
            "predicted": format(predicted, "f"),
            "realized": format(realized, "f"),
            "absolute_error": format(error, "f"),
        })
    passed = min_bin_ok and max_error <= max_bin_error
    return {
        "fit_id": fit.fit_id, "horizon_ms": fit.horizon_ms,
        "theta_ticks": format(threshold, "f"), "n": len(scored),
        "n_bins": n_bins, "bins": bins,
        "max_bin_error": format(max_error, "f"),
        "max_allowed_bin_error": format(max_bin_error, "f"),
        "status": "passed" if passed else "failed",
        "calibrated": passed,
        "reason": None if passed else "reliability_gate_failed",
        "refusal": None if passed else "probability refused: OOS calibration gate failed",
    }


# ---------------------------------------------------------------------------
# D6 horizon-native scenario evaluation
# ---------------------------------------------------------------------------


def evaluate_forward_scenario(
    fit: ForwardFit,
    x: FeatureVector,
    *,
    spot_price: Decimal,
    targets: list[Decimal],
    invalidations: list[Decimal],
    tick_size: Decimal,
    calibration: dict[str, Any] | None = None,
    calibrated: bool = True,
    calibration_refusal: str | None = None,
) -> dict[str, Any]:
    """D6 horizon-native P(P_{t+h}>=T|X) / P(P_{t+h}<=S|X) curves (pure).

    Read-only over D4/D5. Normal-approx is a stated assumption, recorded on
    every output. Miscalibrated horizon -> probs NULL (never 0).
    """
    if isinstance(tick_size, float):
        raise TypeError("tick_size must be Decimal, not float")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if isinstance(spot_price, float):
        raise TypeError("spot_price must be Decimal, not float")
    if fit.status not in ("validated", "provisional"):
        raise ValueError(f"cannot scenario from {fit.status} fit {fit.fit_id}")
    if (x.symbol.upper(), x.venue) != (fit.symbol.upper(), fit.venue):
        raise ValueError("feature vector belongs to a different instrument")
    if not targets and not invalidations:
        raise ValueError("at least one target or invalidation is required")
    # ``calibrated`` is retained only as a compatibility input. A true value
    # without a calibration report is not sufficient to unlock probabilities.
    if calibrated is False and calibration is None:
        calibration = {"status": "failed", "reason": calibration_refusal or "caller_refused"}
    if calibration is not None and "refusal" not in calibration:
        calibration = dict(calibration)
        if calibration.get("status") != "passed":
            calibration["refusal"] = calibration_refusal or "probability refused: calibration gate failed"
    dist = predict_distribution(fit, x, calibration=calibration)
    mu = Decimal(str(dist["expected_ticks"]))
    sig_raw = dist.get("resid_std")
    sig = Decimal(str(sig_raw)) if sig_raw is not None else None
    null_probs = (sig is None or sig <= 0 or not calibrated)
    # SE projection for bands.
    band_method = "null-no-se"
    se_mu: Decimal | None = None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        try:
            terms: list[Decimal] = []
            if fit.stderr.get("intercept") is not None:
                terms.append(Decimal(str(fit.stderr["intercept"])) ** 2)
            for kk, vv in x.fields.items():
                s = fit.betas.get(kk) and fit.stderr.get(kk)
                if s is not None:
                    terms.append((Decimal(str(s)) * abs(Decimal(vv))) ** 2)
            if terms:
                se_mu = sum(terms, Decimal(0)).sqrt()
                band_method = "se-projection-v1"
            elif sig is not None and fit.n_obs > 0:
                se_mu = sig / Decimal(fit.n_obs).sqrt()
                band_method = "resid-over-sqrt-n-v1"
        except Exception:
            se_mu = None
            band_method = "null-no-se"
    out: dict[str, Any] = {
        "scenario_version": FORWARD_SCENARIO_VERSION,
        "fit_id": fit.fit_id, "horizon_ms": fit.horizon_ms,
        "vector_version": x.vector_version,
        "expected_ticks": format(mu, "f"),
        "expected_quote": format(spot_price + mu * tick_size, "f"),
        "resid_std_ticks": format(sig, "f") if sig is not None else None,
        "spot_price": format(spot_price, "f"),
        "tick_size": format(tick_size, "f"),
        "band_method": band_method,
        "band_note": None if se_mu is not None else "stderr null: no band range",
        "assumption": "normal-approx from D5 residual std; stated, not proven",
        "calibration": calibration or {
            "status": "not_run", "calibrated": calibrated,
            "refusal": "probability refused: calibration report required",
        },
        "forecast_type": "native_forecast",
        "horizon_regime": "native",
        "assumptions": {
            "gaussian_probability": True,
            "linear_impact_scaling": False,
        },
        "probability_status": "validated" if not null_probs else "refused",
        "probability_reason": None if not null_probs else dist.get("probability_reason"),
        "kind": "forward-conditional (horizon-native)",
        "targets": [], "invalidations": [],
    }
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for T in targets:
            if isinstance(T, float):
                raise TypeError("targets must be Decimal, not float")
            d_req = (T - spot_price) / tick_size
            row: dict[str, Any] = {"T": format(T, "f"), "d_req_ticks": format(d_req, "f"),
                                   "p_ge": None, "p_lo": None, "p_hi": None}
            if not null_probs and sig is not None and sig > 0:
                assert sig > 0
                row["p_ge"] = str(1.0 - _norm_cdf(float((d_req - mu) / sig)))
                if se_mu is not None:
                    row["p_lo"] = str(1.0 - _norm_cdf(float((d_req - (mu + Decimal("1.96") * se_mu)) / sig)))
                    row["p_hi"] = str(1.0 - _norm_cdf(float((d_req - (mu - Decimal("1.96") * se_mu)) / sig)))
                    lo, hi = row["p_lo"], row["p_hi"]
                    if lo is not None and hi is not None and float(lo) > float(hi):
                        row["p_lo"], row["p_hi"] = hi, lo
            out["targets"].append(row)
        for S in invalidations:
            if isinstance(S, float):
                raise TypeError("invalidations must be Decimal, not float")
            d_req = (S - spot_price) / tick_size
            row2: dict[str, Any] = {"S": format(S, "f"), "d_req_ticks": format(d_req, "f"),
                                    "p_le": None, "p_lo": None, "p_hi": None}
            if not null_probs and sig is not None and sig > 0:
                assert sig > 0
                row2["p_le"] = str(_norm_cdf(float((d_req - mu) / sig)))
                if se_mu is not None:
                    row2["p_lo"] = str(_norm_cdf(float((d_req - (mu + Decimal("1.96") * se_mu)) / sig)))
                    row2["p_hi"] = str(_norm_cdf(float((d_req - (mu - Decimal("1.96") * se_mu)) / sig)))
                    lo2, hi2 = row2["p_lo"], row2["p_hi"]
                    if lo2 is not None and hi2 is not None and float(lo2) > float(hi2):
                        row2["p_lo"], row2["p_hi"] = hi2, lo2
            out["invalidations"].append(row2)
    return out


# ---------------------------------------------------------------------------
# D9 skill decay & population key
# ---------------------------------------------------------------------------


def population_key(x: FeatureVector, horizon_ms: int, regime: str | None) -> str:
    """D9 population-membership key (statistical, not architectural)."""
    import hashlib as _hl
    import json as _js
    payload = {"vector_version": x.vector_version, "horizon_ms": horizon_ms,
               "regime": regime if regime is not None else "all",
               "quality": x.quality}
    return _hl.sha256(_js.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def skill_decay_report(
    pairs: list[ForwardObservation],
    fits: dict[int, ForwardFit],
    *,
    regime_of: dict[int, str] | None = None,
) -> dict[str, Any]:
    """D9 per-horizon skill-decay curves with bands (read-only over D4 OOS)."""
    per_h: dict[str, Any] = {}
    nulls: list[str] = []
    for h in sorted(fits):
        f = fits[h]
        n = sum(1 for p in pairs if p.y_ticks.get(h) is not None
                and p.x.quality == "exact_feed")
        per_h[str(h)] = {"oos_skill": f.oos_skill, "n": n, "status": f.status,
                         "validation_status": f.validation_status,
                         "n_oos": f.n_oos}
        if (f.status == "insufficient" or f.oos_skill is None
                or f.validation_status != "validated"):
            nulls.append(
                f"{h}: no validated skill (status={f.status}, "
                f"validation={f.validation_status}, n_oos={f.n_oos}) — null is a result"
            )
    finalized = [h for h in sorted(fits)
                 if fits[h].status != "insufficient"
                 and fits[h].validation_status == "validated"
                 and fits[h].oos_skill is not None]
    per_regime = None
    if regime_of:
        per_regime = {}
        regs = sorted(set(regime_of.values()))
        for r in regs:
            per_regime[r] = {str(h): fits[h].oos_skill for h in sorted(fits)}
    return {
        "per_horizon": per_h,
        "per_regime": per_regime,
        "decay_curve": [{"h": h, "skill": fits[h].oos_skill} for h in sorted(fits)],
        "finalized_horizons": finalized,
        "feed_resolution_note": "1s/5s require event grain (D3); 10s intervals cannot resolve them",
        "null_results": nulls,
    }


# Re-export _norm_cdf from common to avoid breaking import chains.
# (fitting_common._norm_cdf is the canonical source; this alias is for
#  callers that resolved from fitting_route_c in the old single-file setup.)
from .fitting_common import _norm_cdf  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Forward window replay (pure — owns the interval-attach math)
# ---------------------------------------------------------------------------

def replay_forward_window(
    windowed: list[Any],
    intervals: list[Any],
    *,
    symbol: str,
    venue: str,
    tick_size: Decimal,
    window_start_ms: int,
    window_end_ms: int,
    degraded_spans: list[dict[str, Any]] | None = None,
) -> tuple[list[Any], list[tuple[int, Decimal]], list[Any], dict[str, int]]:
    """Attach enclosing-interval OFI/AD to each event and join forward pairs.

    Pure deterministic helper owned by the fitting plane: tooling supplies
    already-replayed ``windowed`` events + ``intervals`` + pre-derived
    ``degraded_spans`` and the window bounds; all vector math + quality spans
    + forward joins happen here so every forward consumer (forecast,
    distribution, scenario, hypothesis, decay, discipline) shares one
    semantic. Degraded spans are WINDOW-SCOPED [start_ts_ms, end_ts_ms]
    intervals of tape degradation (capture gaps, discontinuities, degraded
    feed) — a target is refused only when it CROSSES a span, never because a
    cumulative transport counter is non-zero. Nothing is bridged across an
    unknown discontinuity.
    """
    from .ofi import _mid

    interval_by_start = {iv.start_ts_ms: iv for iv in intervals}
    vectors: list[Any] = []
    quality_spans: list[dict[str, Any]] = [
        span for span in (degraded_spans or ())
        if isinstance(span, dict) and "start_ts_ms" in span and "end_ts_ms" in span
    ]
    for index, event in enumerate(windowed):
        interval_start = (event.current.exchange_ts_ms // 10_000) * 10_000
        interval = interval_by_start.get(interval_start)
        quality = event.source_quality
        if interval is not None and interval.quality != "exact_feed":
            quality = interval.quality
        try:
            vectors.append(build_feature_vector(
                symbol=symbol, venue=venue, ts_ms=event.current.exchange_ts_ms,
                ofi=interval.ofi if interval is not None else None,
                average_depth=interval.average_depth if interval is not None else None,
                quote=event.current, quality=quality,
            ))
        except (TypeError, ValueError):
            continue
        if quality != "exact_feed":
            next_ts = (windowed[index + 1].current.exchange_ts_ms
                       if index + 1 < len(windowed) else event.current.exchange_ts_ms + 1)
            quality_spans.append({
                "start_ts_ms": event.current.exchange_ts_ms,
                "end_ts_ms": next_ts,
                "quality": quality,
                "reason": quality,
            })
    mids = [(e.current.exchange_ts_ms, _mid(e.current)) for e in windowed]
    pairs, log = build_forward_observations(
        vectors, mids, tick_size=tick_size, venue=venue,
        quality_spans=quality_spans,
    )
    return vectors, mids, pairs, log
