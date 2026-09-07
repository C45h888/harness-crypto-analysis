"""Derivative-cache shared contract (moved verbatim from nooa_harness/bedrock.py).

The FETCH is interpretation-plane only (pipeline_interpretation.
fetch_derivative_evidence — the one Binance touch). What lives here is the
cache READING contract both planes use: freshness check + merge into the
evidence shape.
"""

from __future__ import annotations

from typing import Any

DERIV_TTL_S_DEFAULT = 300
DERIV_FRESH_MS_DEFAULT = 300_000  # 5 min — back-to-back cycles within this skip the fetch


def _is_deriv_fresh(deriv: dict[str, Any] | None, now_ms: int, fresh_ms: int) -> bool:
    if not deriv or not isinstance(deriv, dict):
        return False
    ts = deriv.get("observed_at_ms")
    if not isinstance(ts, (int, float)):
        return False
    age_ms = now_ms - int(ts)
    # Reject future timestamps (clock skew / corrupted cache) and over-age.
    if age_ms < 0:
        return False
    return age_ms <= fresh_ms


def _merge_derivatives(evidence: dict[str, Any], deriv: dict[str, Any] | None) -> dict[str, Any]:
    """Merge one derivative evidence dict into the canonical evidence shape.

    The merged fields are added to ``evidence.futures`` (so existing adapters
    like ``_adapt_oi``, ``_adapt_demand``, ``_adapt_regime`` pick them up
    without code changes) and to ``evidence.cross_asset`` (a new top-level
    key consumed by the demand adapter's macro_climate call).
    """
    if not deriv or not isinstance(deriv, dict):
        return evidence
    out = dict(evidence)
    out["futures"] = dict(evidence.get("futures") or {})
    deriv_fut = deriv.get("futures") or {}
    for key in ("oi_history", "taker_buy_sell", "top_ls", "global_ls", "klines",
                "funding_history"):
        if key in deriv_fut and deriv_fut[key] is not None:
            out["futures"][key] = deriv_fut[key]
    if "cross_asset" in deriv and deriv["cross_asset"]:
        out["cross_asset"] = deriv["cross_asset"]
    out["derivative_observed_at_ms"] = deriv.get("observed_at_ms")
    # Bar-horizon metadata: every derivative series is 5-minute bars, so a
    # series of N bars covers N x 5 minutes — regardless of the requested
    # 15m/1h/4h analysis window. Making the horizon explicit stops downstream
    # readers from misreading the series as window-aligned. funding_history
    # is 8-hour-settled and is recorded separately for downstream readers.
    deriv_fut = deriv.get("futures") or {}
    out["derivatives_meta"] = {
        "bar_period_s": 300,
        "funding_history_period_s": 8 * 3600,
        "series": {
            key: (len(deriv_fut[key]) if isinstance(deriv_fut.get(key), list) else None)
            for key in ("oi_history", "taker_buy_sell", "top_ls", "global_ls", "klines")
        },
        "funding_history_count": (
            len(deriv_fut["funding_history"])
            if isinstance(deriv_fut.get("funding_history"), list) else None
        ),
    }
    return out
