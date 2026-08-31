"""Raw-Redis read paths — the post-envelope consumer discipline.

This module replaces the ``MarketRunEnvelope`` dataclass as the read-side
interface for collated market runs. The envelope JSON stored under
``{prefix}:latest:{SYMBOL}:collated`` is read directly: one GET, one
``json.loads``, one schema-version guard, then bounded path-read
projections. No dataclass re-wrap, no ``to_dict()`` round trip.

Why this exists (debt pass, 2026-08-30): the frozen ``MarketRunEnvelope``
contract was the interpretation interface of the deleted AnalyzerSuite /
ControllerAgent plane. Every consumer (outer CLI, inner CLI, the OO agent
``market.*`` tool family) round-tripped dict -> dataclass -> dict -> JSON
for zero semantic work — 5+ translations per read. The contract's only
load-bearing properties were (a) the schema-version guard and (b) NaN
sanitization; both live here now: the guard on read, the sanitization
owned by the writer (pipeline publish path).

Doctrine carried forward:
- Null means not-provided. Projections preserve ``None``; never
  zero-substitute.
- Projections perform path-reads ONLY — every value here was computed by
  the calculations/analysis layers before publish. No arithmetic beyond
  the sign-sum over already-bucketed CVD (see ``cvd_sign_series``).
- Schema mismatch raises. Never coerce a stale writer's shape.
"""

from __future__ import annotations

import json
import math
from typing import Any

from market_service.runtime.contracts import MARKET_RUN_SCHEMA_VERSION

__all__ = [
    "MARKET_RUN_SCHEMA_VERSION",
    "cvd_sign_series",
    "json_safe",
    "json_safe_dumps",
    "market_inventory",
    "market_snapshot",
    "path_read",
    "read_collated",
    "read_collated_by_run",
]


def path_read(value: Any, *keys: str) -> Any:
    """Safe nested-dict traversal. Returns None on any non-dict hop.

    The canonical double nesting (``canonical_state.calculations
    .calculations.*`` and ``canonical_state.analysis.analysis.*``) is a
    known shape of the persisted payload; readers descend it here, once,
    instead of each consumer hand-rolling its own ``_path`` helper.
    """
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _json_default(value: Any) -> Any:
    """Render non-JSON-native values as str (Decimal, datetime, ...).

    NOTE: this cannot catch NaN/Infinity — float is JSON-serializable for
    Python's json and emits the invalid bare ``NaN``/``Infinity`` tokens
    before ``default`` is consulted. Strict renders go through
    :func:`json_safe_dumps` (tree-walk sanitization) instead.
    """
    return str(value)


def json_safe(payload: Any) -> Any:
    """Tree-walk a value into strictly-JSON-safe form: non-finite floats
    -> None, other values unchanged. (Consumers receiving pipeline payloads
    — which may hold live NaN/Inf — must pass results through this before
    handing them to json.dumps/LLM tool seams.)"""
    return _json_safe_walk(payload)


def json_safe_dumps(payload: Any) -> str:
    """Serialize to strict valid JSON: non-finite floats -> null, other
    non-native values -> str. The post-envelope replacement for the
    dataclass ``to_json`` sanitization at any consumer seam.
    """
    return json.dumps(_json_safe_walk(payload), default=_json_default)


def _json_safe_walk(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe_walk(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_walk(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)  # Decimal/datetime etc — same norm as the write gate


def _dict(value: Any) -> dict[str, Any]:
    """Return the value if it is a dict, else {} (absent-section norm)."""
    return value if isinstance(value, dict) else {}


async def read_collated(store: Any, symbol: str) -> dict[str, Any] | None:
    """Read the latest collated run straight from Redis as a plain dict.

    ``store`` is any object exposing ``collated_latest_key(symbol)`` and a
    ``redis`` client (i.e. RedisRuntimeStore). Applies the schema-version
    guard the dataclass used to enforce; returns None when nothing is
    persisted (null = absent, per discipline).
    """
    raw = await store.redis.get(store.collated_latest_key(symbol.upper()))
    return _guard_payload(raw)


async def read_collated_by_run(store: Any, run_id: str) -> dict[str, Any] | None:
    """Read one run-addressed collated payload by run id (Redis dedupe key)."""
    raw = await store.redis.get(f"{store.prefix}:run:{run_id}")
    return _guard_payload(raw)


def _guard_payload(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("collated payload is not a JSON object")
    version = payload.get("schema_version")
    if version != MARKET_RUN_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported market run schema version: {version!r} "
            f"(reader is on {MARKET_RUN_SCHEMA_VERSION})"
        )
    return payload


# ---------------------------------------------------------------------------
# Projections — the agent-facing bounded views of a collated payload.
# ---------------------------------------------------------------------------

# Windows for the institutional delta-flip read, seconds, newest-first cut.
CVD_SIGN_WINDOWS_S: tuple[int, ...] = (900, 300, 120, 60, 30)

_HEADLINE_ORDERBOOK_PATHS = {
    "fut_keystone_bid": ("fut_keystone", "bid"),
    "fut_keystone_ask": ("fut_keystone", "ask"),
    "keystone_bid_qty": ("keystone_bid_stack", "tight", "total_qty"),
    "keystone_ask_qty": ("keystone_ask_stack", "tight", "total_qty"),
    "ask_ladder_notional": ("ask_wall_ladder", "total_notional"),
    "bid_ladder_notional": ("bid_wall_ladder", "total_notional"),
    "keystone_trade_buy_qty": ("keystone_trade_intensity", "tight", "buy_qty"),
    "keystone_trade_sell_qty": ("keystone_trade_intensity", "tight", "sell_qty"),
    "hourly_keystone_verdict": ("hourly_keystone_migration", "verdict"),
}


def cvd_sign_series(
    payload: dict[str, Any], *, windows_s: tuple[int, ...] = CVD_SIGN_WINDOWS_S,
) -> list[dict[str, Any]]:
    """Sign of cumulative USD delta over trailing windows of bucketed CVD.

    Reads the futures bucketed series published by the calculations layer
    (``canonical_state.calculations.calculations.bucketed_cvd
    .futures_bucketed_cvd`` — list of ``{t, delta, delta_usd, ...}``
    buckets keyed by epoch-seconds). The only derivation performed is the
    trailing-window sum and its sign: this is the institutional delta-flip
    detector view (15m/5m/2m/60s/30s) that neither the briefing projection
    nor the CLI inventory carried, forcing readers to dump raw buckets.

    ``sign`` is 1 / 0 / -1 (0 covers an exact flat or empty window —
    reported with sum so a flat print is distinguishable from absence);
    ``None`` values mark not-provided fields, never zero-substituted.
    """
    buckets = path_read(
        payload, "canonical_state", "calculations", "calculations",
        "bucketed_cvd", "futures_bucketed_cvd",
    )
    if not isinstance(buckets, list) or not buckets:
        return []
    stamps: list[int] = []
    for b in buckets:
        if isinstance(b, dict) and b.get("t"):
            stamps.append(int(b["t"]))
    if not stamps:
        return []
    anchor = max(stamps)
    series: list[dict[str, Any]] = []
    for window in windows_s:
        cutoff = anchor - window
        sums: list[float] = []
        for b in buckets:
            if not isinstance(b, dict) or b.get("t") is None or int(b["t"]) <= cutoff:
                continue
            delta = b.get("delta_usd")
            if delta is None:
                continue  # not-provided — excluded, never zero-substituted
            value = float(delta)
            if math.isfinite(value):
                sums.append(value)
        if not sums:
            series.append({"window_seconds": window, "buckets": 0,
                           "delta_usd_sum": None, "sign": None})
            continue
        total = sum(sums)
        series.append({
            "window_seconds": window,
            "buckets": len(sums),
            "delta_usd_sum": total,
            "sign": (1 if total > 0 else -1 if total < 0 else 0),
        })
    return series


def market_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Bounded headline view of a collated payload — the agent's primary
    market read. Merges the two legacy projections (briefing-owned
    ``_envelope_summary`` scalars + CLI ``_projection`` orderbook
    headlines) and adds the CVD multi-window sign series.

    Small by construction (scalars only, no raw arrays): it is meant to
    survive the engine's tool-result budget without hitting the 40k
    truncation collapse. Anything richer is a deliberate
    ``mode="full"`` deep-dive, not an accidental prefix cut.
    """
    data_access = _dict(path_read(payload, "canonical_state", "data-access"))
    evidence = _dict(data_access.get("evidence"))
    futures = _dict(evidence.get("futures"))
    calculations = _dict(path_read(payload, "canonical_state", "calculations", "calculations"))
    analysis = _dict(path_read(payload, "canonical_state", "analysis", "analysis"))
    flow = _dict(calculations.get("flow"))
    orderbook = _dict(calculations.get("orderbook"))
    technical = _dict(calculations.get("technical"))
    demand = _dict(analysis.get("demand"))
    decomposition = _dict(demand.get("decomposition"))
    wall_migration = _dict(analysis.get("wall_migration"))

    ticker = _dict(futures.get("ticker_24h"))
    funding = _dict(futures.get("funding"))
    open_interest = _dict(futures.get("open_interest"))
    spot_flow = _dict(flow.get("spot_flow"))
    fut_flow = _dict(flow.get("futures_flow"))

    coverage = payload.get("coverage")
    domain_status = path_read(coverage, "domain_status") if isinstance(coverage, dict) else None

    snapshot: dict[str, Any] = {
        "schema_version": payload.get("schema_version"),
        "run_id": payload.get("run_id"),
        "symbol": payload.get("symbol"),
        "status": payload.get("status"),
        "generated_at": payload.get("generated_at"),
        "completed_at": payload.get("completed_at"),
        "data_source": payload.get("data_source"),
        "domain_status": domain_status,
        "error_count": len(payload.get("errors") or []),
        # Tape / derivatives headlines (briefing-projection set).
        "last_price": ticker.get("last_price"),
        "volume_24h": ticker.get("quote_volume"),
        "high_24h": ticker.get("high_price"),
        "low_24h": ticker.get("low_price"),
        "funding_rate": funding.get("last_funding_rate"),
        "mark_price": funding.get("mark_price"),
        "open_interest": open_interest.get("open_interest"),
        "spot_cvd": spot_flow.get("cvd"),
        "futures_cvd": fut_flow.get("cvd"),
        "spot_obi": path_read(decomposition, "spot", "obi"),
        "futures_obi": path_read(decomposition, "futures", "obi"),
    }

    # Orderbook / wall headlines (CLI-projection set, merged).
    for name, keys in _HEADLINE_ORDERBOOK_PATHS.items():
        snapshot[name] = path_read(orderbook, *keys)
    snapshot["seller_aggression"] = path_read(technical, "seller_aggression", "classification")
    # Round-number bid anchors surfaced from wall_migration analysis.
    snapshot["bid_anchor_count"] = path_read(wall_migration, "round_anchors", "count")
    snapshot["mega_tier_pct"] = path_read(wall_migration, "tiers", "mega", "pct")
    snapshot["fut_microprice_skew_bps"] = orderbook.get("fut_microprice_skew_bps")

    # The delta-flip series — new in this deviation (see cvd_sign_series).
    snapshot["cvd_sign_series"] = cvd_sign_series(payload)
    return snapshot


def market_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    """Signal-inventory view: which sections/groups exist, never raw arrays.

    Successor of the CLI ``--envelope-summary`` projection: header fields +
    sorted key lists per section + the headline snapshot.
    """
    calculations = _dict(path_read(payload, "canonical_state", "calculations", "calculations"))
    analysis = _dict(path_read(payload, "canonical_state", "analysis", "analysis"))
    orderbook = _dict(calculations.get("orderbook"))
    technical = _dict(calculations.get("technical"))
    inventory: dict[str, Any] = {
        "schema_version": payload.get("schema_version"),
        "symbol": payload.get("symbol"),
        "status": payload.get("status"),
        "run_id": payload.get("run_id"),
        "generated_at": payload.get("generated_at"),
        "completed_at": payload.get("completed_at"),
        "coverage": payload.get("coverage"),
        "analysis_keys": sorted(analysis.keys()),
        "calculations_keys": sorted(calculations.keys()),
        "orderbook_keys": sorted(orderbook.keys()),
        "technical_keys": sorted(technical.keys()),
    }
    inventory["snapshot"] = market_snapshot(payload)
    return inventory
