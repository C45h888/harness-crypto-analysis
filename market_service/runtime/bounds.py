"""Group-envelope emission bounds + evidence headlines (moved verbatim from nooa_harness/bedrock.py).

Hard per-array cap at contract-emission level. The largest legitimate
arrays (volume-profile buckets over a 4h window, ask-wall ladders) fit
comfortably; anything larger is capped WITH an explicit __truncated__
marker — never silently dropped, and never left to break the LLM param
limit downstream.
"""

from __future__ import annotations

from typing import Any

_GROUP_ARRAY_CAP = 128


def _bound_arrays(value: Any, cap: int = _GROUP_ARRAY_CAP) -> Any:
    """Deterministically cap any list/tuple at ``cap`` items, explicitly marked."""
    if isinstance(value, dict):
        return {k: _bound_arrays(v, cap) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_bound_arrays(v, cap) for v in value[:cap]]
        if len(value) > cap:
            return {"__truncated__": True, "count": len(value), "items": items}
        return items
    return value


def _evidence_headlines(evidence: dict[str, Any]) -> dict[str, Any]:
    """Compact shared evidence context (snake_case scalars, null-preserving).

    Every group envelope carries these so any specialist has the baseline
    market state without raw arrays. Mirrors the fixed path discipline of
    runtime.contracts._envelope_summary — pure path-reads, no arithmetic.
    """
    fut = (evidence or {}).get("futures") or {}
    spot = (evidence or {}).get("spot") or {}

    def _p(d: Any, *keys: str) -> Any:
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    ticker = _p(fut, "ticker_24h") or {}
    spot_ticker = _p(spot, "ticker_24h") or {}
    funding = _p(fut, "funding") or {}
    oi = _p(fut, "open_interest") or {}
    return {
        "last_price": _p(fut, "ticker_24h", "last_price") or _p(fut, "ticker_24h", "lastPrice"),
        "spot_last_price": _p(spot, "ticker_24h", "last_price") or _p(spot, "ticker_24h", "lastPrice"),
        "funding_rate": _p(funding, "last_funding_rate") or _p(funding, "lastFundingRate"),
        "mark_price": _p(funding, "mark_price") or _p(funding, "markPrice"),
        "open_interest": _p(oi, "open_interest") or _p(oi, "openInterest"),
        "high_24h": _p(ticker, "high_price") or _p(ticker, "highPrice"),
        "low_24h": _p(ticker, "low_price") or _p(ticker, "lowPrice"),
        "quote_volume_24h": _p(ticker, "quote_volume") or _p(ticker, "quoteVolume"),
        "spot_quote_volume_24h": _p(spot_ticker, "quote_volume") or _p(spot_ticker, "quoteVolume"),
    }
