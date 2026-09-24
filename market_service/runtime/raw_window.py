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


def _num_pair(rows: Any) -> list[Any]:
    """Coerce WS raw price-level rows (strings of the ``["price","qty"]``
    Delta shape) into the float-pair shape the REST book uses."""
    out: list[Any] = []
    for row in rows or ():
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            p, q = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        out.append([p, q])
    return out


def _has_prices(levels: Any) -> bool:
    """True when a level array carries any usable price/qty pair."""
    return any(
        isinstance(r, (list, tuple)) and len(r) >= 2
        for r in (levels or ())
    )


def _overlay_book(rest_book: dict[str, Any], ws_deltas: list[dict[str, Any]]) -> dict[str, Any]:
    """Fuse the poller REST book with the latest WS depth delta.

    The poller provides the base level grid (spot/futures REST book); the WS
    capture provides higher-frequency cumulative price levels. We overlay the
    MOST RECENT WS delta that actually carries price levels onto the REST
    book — treating WS as the fresher authority when it has data, and falling
    back to REST intact when WS is absent. Decode both sides to float pairs;
    preserve the REST shape when no WS data is available.
    """
    if not rest_book:
        rest_book = {}
    bids = rest_book.get("bids") or []
    asks = rest_book.get("asks") or []
    if not _has_prices(bids) and not _has_prices(asks):
        # REST book came in nested? guard no-op.
        pass
    ws_bids = ws_asks = None
    for d in ws_deltas:
        if _has_prices(d.get("bids")) or _has_prices(d.get("asks")):
            ws_bids = _num_pair(d.get("bids"))
            ws_asks = _num_pair(d.get("asks"))
            break  # newest delta carries the freshest cumulative book
    # Prefer WS levels when present; else keep REST.
    fused_bids = ws_bids if ws_bids is not None else _num_pair(bids)
    fused_asks = ws_asks if ws_asks is not None else _num_pair(asks)
    return {
        "bids": fused_bids or [],
        "asks": fused_asks or [],
        "source": "ws" if ws_bids is not None else "rest",
    }


def ws_evidence_from_deltas(ws_deltas: list[dict[str, Any]], *, now_ms: int) -> dict[str, Any]:
    """Shape the WS delta surface for the substrate workers.

    Provides the fused book choice (newest cumulative WS book) plus the raw
    delta series tagged with age, so the multi-timeframe aggregations can
    bucket WS book deltas into 5m / 15m / 4h windows without re-reading
    redis. ``now_ms`` anchors the age/timestamp math in one place.
    """
    by_ts: list[dict[str, Any]] = []
    for d in ws_deltas:
        ts = d.get("exchange_ts_ms") or d.get("received_ts_ms")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = None
        by_ts.append({
            "exchange_ts_ms": ts,
            "received_ts_ms": d.get("received_ts_ms"),
            "age_ms": (now_ms - ts) if ts is not None else None,
            "bids": _num_pair(d.get("bids")),
            "asks": _num_pair(d.get("asks")),
        })
    # Newest-first.
    by_ts.sort(key=lambda x: (x["exchange_ts_ms"] or 0), reverse=True)
    return {
        "source": "microstructure_ws",
        "delta_count": len(by_ts),
        "deltas": by_ts,
    }


async def build_ws_surface(
    redis: RedisRuntimeStore,
    symbol: str,
    venue: str = "futures",
    *, window_minutes: int = 15,
) -> dict[str, Any]:
    """Read the WS microstructure raw deltas for ``window_minutes`` back.

    Returns a surface with the fused book overlay + the delta series. When
    the store/venue/symbol is absent or empty, returns an empty surface
    (``delta_count=0``) instead of raising — WS is optional evidence that
    augments, never replaces, the poller REST path.
    """
    now_ms = int(time.time() * 1000)
    try:
        reader = getattr(redis, "read_microstructure_raw", None)
        if not callable(reader):
            return {"source": "microstructure_ws", "delta_count": 0, "deltas": []}
        # Read the window (oldest-first xrange); bucket via age below.
        deltas = await reader(venue, symbol, start="-", end="+",
                              count=int(window_minutes * 60 * 1000 // 100))
    except Exception:
        # WS is best-effort; never let an absent WS surface fail a REST fire.
        return {"source": "microstructure_ws", "delta_count": 0, "deltas": []}
    return ws_evidence_from_deltas(deltas, now_ms=now_ms)


def overlay_ws_into_window(
    window: dict[str, Any],
    ws_surface: dict[str, Any],
) -> dict[str, Any]:
    """Merge the WS surface into a REST evidence window.

    Attaches ``evidence.ws`` (high-frequency delta series) and swaps the
    futures book to the fused WS overlay when WS is present. Returns a new
    dict; does not mutate the caller's window.
    """
    out = {**window}
    fut = dict(window.get("futures") or {})
    ws_deltas = ws_surface.get("deltas") or []
    rest_book = fut.get("order_book") or {}
    fused = _overlay_book(rest_book, ws_deltas)
    if fused.get("source") == "ws":
        # Preserve REST depth as the base and record the overlay provenance.
        fut["order_book"] = fused
        fut["ws_source"] = fused.get("source")
    fut["ws_deltas"] = ws_deltas  # full series for multi-timeframe bucketing
    out["futures"] = fut
    out["ws"] = ws_surface
    return out