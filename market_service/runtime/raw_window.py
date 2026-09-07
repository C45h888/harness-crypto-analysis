"""Raw evidence window builder — shared by the harness planes and substrate workers.

Extracted from ``nooa_harness.bedrock.read_raw_window`` so the substrate
workers can build the same evidence window from the Redis raw stream WITHOUT
importing the harness layer (substrate_worker never imports nooa_harness).
``calculations.composition`` re-exports this as ``read_raw_window`` — zero behavior change.

Reads the latest raw snapshot (point-in-time: order books, funding, OI,
tickers) and accumulates the window's trades from the stream, deduped by
trade id, with measured (not requested) coverage.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from market_service.config import default_depth_levels
from market_service.runtime.redis_store import RedisRuntimeStore


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


async def build_raw_window(
    redis: RedisRuntimeStore,
    symbol: str,
    window_minutes: int,
) -> dict[str, Any]:
    """Read the latest raw evidence snapshot and accumulate trades within the window.

    The poller writes a full snapshot every ``poll_seconds``. We read the latest
    snapshot for order book / funding / OI / tickers (point-in-time), and use the
    stream to accumulate trades across the window. Because each snapshot's
    ``trades_normalized`` is a rolling window (``flow_window_seconds``,
    poll interval), overlapping snapshots repeat the same trade ids; we **dedupe by
    trade ``id``** (stable Binance aggregate id) so volumes/CVD are not inflated
    by redeclaring each trade once per snapshot it appears in.
    """
    latest = await redis.read_raw_latest(symbol)
    if latest is None:
        return {
            "observed_at": _utc_iso(),
            "observed_at_ms": int(time.time() * 1000),
            "fetch_window_ms": window_minutes * 60_000,
            "depth_levels": default_depth_levels(),
            "errors": [{"endpoint": "all", "error": "no raw evidence in Redis"}],
            "coverage": {
                "requested_window_seconds": window_minutes * 60,
                "snapshots_used": 0,
                "latest_observed_at_ms": None,
                "stream_staleness_ms": None,
                "spot_trades": {"trade_count": 0, "raw_trade_count": 0,
                                "duplicates_removed": 0, "first_trade_ms": None,
                                "last_trade_ms": None, "span_seconds": None},
                "futures_trades": {"trade_count": 0, "raw_trade_count": 0,
                                   "duplicates_removed": 0, "first_trade_ms": None,
                                   "last_trade_ms": None, "span_seconds": None},
            },
            "spot": {"ticker_24h": None, "order_book": {}, "trades_raw": [], "trades_normalized": []},
            "futures": {"ticker_24h": None, "order_book": {}, "trades_raw": [], "trades_normalized": [],
                        "funding": {}, "open_interest": {}},
        }

    since_ms = int(time.time() * 1000) - window_minutes * 60_000
    snapshots = await redis.read_raw_window(symbol, since_ms)

    def _dedupe(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            if tid is not None:
                try:
                    key = int(tid)
                except (TypeError, ValueError):
                    continue
                if key in seen:
                    continue
                seen.add(key)
            out.append(t)
        return out

    # Accumulate trades across all snapshots in the window (already oldest-first).
    spot_raw: list[dict[str, Any]] = []
    fut_raw: list[dict[str, Any]] = []
    for snap in snapshots:
        spot_raw.extend(snap.get("spot", {}).get("trades_normalized", []))
        fut_raw.extend(snap.get("futures", {}).get("trades_normalized", []))

    spot_trades = _dedupe(spot_raw)
    fut_trades = _dedupe(fut_raw)

    # Actual coverage — measured, not requested. The doctrine requires the
    # envelope to record what the window REALLY contains: the true trade span,
    # dedupe effectiveness, how many snapshots fed the window, and how stale
    # the latest stream entry is. Never claim a window the data doesn't cover.
    def _trade_coverage(trades: list[dict[str, Any]], raw_count: int) -> dict[str, Any]:
        tss: list[int] = []
        for t in trades:
            try:
                tss.append(int(t["ts"]))
            except (KeyError, TypeError, ValueError):
                continue
        if tss:
            span = {"first_trade_ms": min(tss), "last_trade_ms": max(tss),
                    "span_seconds": (max(tss) - min(tss)) / 1000.0}
        else:
            span = {"first_trade_ms": None, "last_trade_ms": None,
                    "span_seconds": None}
        return {"trade_count": len(trades), "raw_trade_count": raw_count,
                "duplicates_removed": raw_count - len(trades), **span}

    raw_spot_count = sum(len(s.get("spot", {}).get("trades_normalized") or []) for s in snapshots)
    raw_fut_count = sum(len(s.get("futures", {}).get("trades_normalized") or []) for s in snapshots)
    latest_observed_ms = latest.get("observed_at_ms")
    now_ms = int(time.time() * 1000)
    coverage = {
        "requested_window_seconds": window_minutes * 60,
        "snapshots_used": len(snapshots),
        "latest_observed_at_ms": latest_observed_ms,
        "stream_staleness_ms": (
            now_ms - int(latest_observed_ms)
            if isinstance(latest_observed_ms, (int, float)) else None
        ),
        "spot_trades": _trade_coverage(spot_trades, raw_spot_count),
        "futures_trades": _trade_coverage(fut_trades, raw_fut_count),
    }

    return {
        # Evidence time: when the source snapshot was observed, not when the
        # harness happened to read it. The read instant is coverage
        # information, not evidence identity.
        "observed_at": latest.get("observed_at") or _utc_iso(),
        "observed_at_ms": int(time.time() * 1000),
        "fetch_window_ms": window_minutes * 60_000,
        "depth_levels": latest.get("depth_levels") or default_depth_levels(),
        "errors": latest.get("errors", []),
        "coverage": coverage,
        "spot": {
            "ticker_24h": latest.get("spot", {}).get("ticker_24h"),
            "order_book": latest.get("spot", {}).get("order_book") or {},
            "trades_raw": latest.get("spot", {}).get("trades_raw") or [],
            "trades_normalized": spot_trades,
        },
        "futures": {
            "ticker_24h": latest.get("futures", {}).get("ticker_24h"),
            "order_book": latest.get("futures", {}).get("order_book") or {},
            "trades_raw": latest.get("futures", {}).get("trades_raw") or [],
            "trades_normalized": fut_trades,
            "funding": latest.get("futures", {}).get("funding") or {},
            "open_interest": latest.get("futures", {}).get("open_interest") or {},
        },
    }