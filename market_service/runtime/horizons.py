"""Time-horizon semantics — IST-aligned buckets + the horizon fold primitives.

Phase H1 (docs/HORIZON_RETENTION_SPEC.md): the substrate worker plane can
declare per-worker time horizons (``15m`` / ``1h`` / ``4h``). This module is
the SINGLE SOURCE OF TRUTH for:

* horizon identity — period lengths and their rollover-contract names;
* IST alignment — bucket boundaries are Indian Standard Time aligned
  (UTC+05:30, no DST). A 4h bucket starts at IST 00:00/04:00/…/20:00, which
  is what a trader on IST reads off their chart; UTC wall times of the
  boundaries sit at :30 offsets.
* the fold primitives — incremental, replay-safe per-horizon trade
  accumulators updated by the worker cadence layer on every read tick.

Purity rules (same contract as the calculation substrates): no I/O, no
imports from runtime/clients — the fold primitives take dicts in and return
dicts out. The cadence layer (substrate_worker/core) owns persistence.
"""

from __future__ import annotations

from typing import Any

# Indian Standard Time — fixed offset, no DST (19_800_000 ms = 5h30m).
IST_OFFSET_MS = 19_800_000

# ---------------------------------------------------------------------------
# Phase S-2 (docs/STATE_CHARTER_SPEC.md) — the TREE-WIDE horizon numeric
# owner. Two resolution grains share this vocabulary:
#
#   * LONG_HORIZONS_MS — the long-horizon grain (task directives, scenario
#     evaluation, the long-horizon bridge) AND the worker cadence widths
#     (H1: HORIZON_PERIOD_MS aliases this dict — identity, never a copy).
#   * NATIVE_HORIZONS_MS — the native forward-fit horizons (1s/5s/30s/60s)
#     the deterministic plane fits at (fitting_route_c) and the horizon
#     spans cover.
#
# Every plane (worker / engine / microstructure / interaction) imports from
# HERE. Before S-2 the same numbers were defined independently in six files
# — the charter test (TestHorizonNamespace) pins this module as the single
# numeric definition.
# ---------------------------------------------------------------------------
NATIVE_HORIZONS_MS: dict[str, int] = {
    "1s": 1_000,
    "5s": 5_000,
    "30s": 30_000,
    "60s": 60_000,
}
LONG_HORIZONS_SECONDS: dict[str, int] = {
    "15m": 900,
    "1h": 3_600,
    "4h": 14_400,
}
LONG_HORIZONS_MS: dict[str, int] = {
    key: seconds * 1_000 for key, seconds in LONG_HORIZONS_SECONDS.items()
}

# Ordered numeric tuples — the exact shapes the numeric consumers index
# (fitting route C's FORWARD_HORIZONS_MS; the long-horizon bridge's spans).
NATIVE_HORIZON_ORDER: tuple[int, ...] = tuple(NATIVE_HORIZONS_MS.values())
LONG_HORIZON_ORDER: tuple[int, ...] = tuple(LONG_HORIZONS_MS.values())

# Name vocabularies (frozen tuples).
NATIVE_HORIZON_NAMES: tuple[str, ...] = tuple(NATIVE_HORIZONS_MS)
HORIZON_NAMES: tuple[str, ...] = tuple(LONG_HORIZONS_MS)
SPAN_HORIZON_NAMES: tuple[str, ...] = NATIVE_HORIZON_NAMES + HORIZON_NAMES

# Worker-plane cadence widths (H1) — the SAME numbers as the long-horizon
# grain, by identity. Every existing HORIZON_PERIOD_MS consumer keeps
# working; the charter test pins this alias so the widths can never fork.
HORIZON_PERIOD_MS: dict[str, int] = LONG_HORIZONS_MS

# Horizon → rollover-contract name (see contracts.ROLLOVER_PERIOD_MS).
HORIZON_ROLLOVER: dict[str, str] = {
    "15m": "bar_15m",
    "1h": "bar_1h",
    "4h": "bar_4h",
}

# Rollover periods whose bucket identity is IST-aligned. The IST offset
# (5.5h) is an exact multiple of bar_5m (66×) and bar_15m (22×), so those
# align identically to the epoch grid; bar_1h and bar_4h boundaries sit at
# UTC :30 offsets (IST-clock hours/days — what a trader on IST reads).
# The legacy ``hour`` period is deliberately NOT aligned: its epoch-hour
# bucket ids are pinned by existing payloads/tests.
IST_ALIGNED_ROLLOVERS = {"bar_15m", "bar_1h", "bar_4h"}

# Phase S-2: the rollover names keyed by horizon name — the width→rollover
# pairing the WS/status planes validate against (one namespace, not per-plane
# copies).
HORIZON_ROLLOVER_MS: dict[str, int] = {
    name: ROLLOVER_LENGTH for name, ROLLOVER_LENGTH in (
        ("15m", 900_000), ("1h", 3_600_000), ("4h", 14_400_000),
    )
}


def bucket_start_ms(t_ms: int, period_ms: int, *, ist_aligned: bool = False) -> int:
    """Start of the period bucket containing ``t_ms``.

    IST-aligned buckets count period boundaries from IST midnight: add the
    IST offset, floor to the period grid, subtract it back. E.g. a 4h grid
    anchored at IST midnight puts boundaries at UTC 18:30/22:30/02:30…
    """
    offset = IST_OFFSET_MS if ist_aligned else 0
    return ((int(t_ms) + offset) // period_ms) * period_ms - offset


def horizon_bucket(horizon: str, t_ms: int) -> int:
    """IST-aligned bucket START timestamp for one declared horizon."""
    period = HORIZON_PERIOD_MS[horizon]
    return bucket_start_ms(t_ms, period, ist_aligned=True)


def horizon_minutes(horizon: str) -> int:
    return HORIZON_PERIOD_MS[horizon] // 60_000


# ---------------------------------------------------------------------------
# Fold primitives — incremental per-horizon trade accumulators.
# ---------------------------------------------------------------------------

def new_fold_state(horizon: str) -> dict[str, Any]:
    """Fresh fold state for one horizon: sealed+active segment ring +
    per-venue monotonic trade-id high-waters (overlap dedupe)."""
    if horizon not in HORIZON_PERIOD_MS:
        raise ValueError(f"unknown horizon {horizon!r}; expected one of {sorted(HORIZON_PERIOD_MS)}")
    return {
        "horizon": horizon,
        # Oldest-first list of segment aggregates; the LAST entry is active.
        "segments": [],
        "hwm": {"spot": -1, "futures": -1},
        "late_dropped": 0,
        "duplicates_skipped": 0,
    }


def _segment_len_ms(horizon: str) -> int:
    """Internal fold granularity per horizon (5m for 15m/1h, 15m for 4h)."""
    return {
        "15m": 300_000,
        "1h": 300_000,
        "4h": 900_000,
    }[horizon]


def _segment_aggregate(segment_ms: int, horizon: str) -> dict[str, Any]:
    return {
        "bucket_start_ms": horizon_bucket(horizon, segment_ms),
        "segment_start_ms": segment_ms,
        "trade_count": 0,
        "notional": 0.0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "first_trade_ms": None,
        "last_trade_ms": None,
        "sealed": False,
        # Phase A rollout: trade-price extremes per segment — the raw
        # material for per-horizon fold candles (technicals) without any
        # extra derivative fetch. Combined across venues (spot/futures
        # prices move together; per-venue notional rides in ``venues``).
        "first_px": None,
        "last_px": None,
        "high_px": None,
        "low_px": None,
        # Per-venue split (Phase A rollout): tape consumes spot vs futures
        # buy/sell shares separately — combined-only segments would blur
        # the two venues' flow.
        "venues": {},
    }


def _venue_fold(agg: dict[str, Any], trade: dict[str, Any], venue: str) -> None:
    try:
        price = float(trade["price"])
        qty = float(trade["qty"])
    except (KeyError, TypeError, ValueError):
        return
    notional = price * qty
    agg["trade_count"] += 1
    agg["notional"] += notional
    # Binance convention: is_buyer_maker=True → the taker SOLD (aggressive sell).
    if trade.get("is_buyer_maker"):
        agg["sell_notional"] += notional
    else:
        agg["buy_notional"] += notional
    ts = trade.get("ts")
    if isinstance(ts, (int, float)):
        ts = int(ts)
        if agg["first_trade_ms"] is None:
            agg["first_trade_ms"] = ts
        agg["last_trade_ms"] = ts
    # Price extremes (combined across venues).
    if agg["first_px"] is None:
        agg["first_px"] = price
        agg["high_px"] = price
        agg["low_px"] = price
    else:
        if price > agg["high_px"]:
            agg["high_px"] = price
        if price < agg["low_px"]:
            agg["low_px"] = price
    agg["last_px"] = price
    # Per-venue split.
    v = agg["venues"].setdefault(venue, {
        "trade_count": 0, "notional": 0.0,
        "buy_notional": 0.0, "sell_notional": 0.0,
    })
    v["trade_count"] += 1
    v["notional"] += notional
    if trade.get("is_buyer_maker"):
        v["sell_notional"] += notional
    else:
        v["buy_notional"] += notional


def fold_trades(state: dict[str, Any], trades: list[dict[str, Any]], venue: str) -> int:
    """Fold one (deduped-by-arrival-order) batch of trades into the horizon.

    Dedupe: Binance aggregate ids are monotonic per venue, so the fold keeps
    a per-venue high-water and skips ``id <= hwm`` — O(1) memory, exact.
    Sealing: a trade newer than the active segment seals every older one.
    Late data (older than the sealed frontier) is DROPPED and counted —
    never folded into a sealed segment (that would make the fold
    order-dependent and break replay determinism).

    Phase A rollout — trailing-horizon prune: segments older than the
    horizon span behind the newest folded trade are evicted, so the fold
    is a bounded ring whose totals genuinely mean "the trailing horizon"
    regardless of market tempo (a quiet tape does not accumulate stale
    segments forever).

    Returns the number of trades folded.
    """
    seg_len = _segment_len_ms(state["horizon"])
    folded = 0
    last_folded_ts: int | None = None
    hwm = state["hwm"]
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        try:
            tid = int(trade.get("id"))
        except (TypeError, ValueError):
            continue
        if tid <= hwm.get(venue, -1):
            state["duplicates_skipped"] += 1
            continue
        hwm[venue] = tid
        ts = trade.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        ts = int(ts)
        seg_start = (ts // seg_len) * seg_len
        segments = state["segments"]
        last = segments[-1] if segments else None
        if last is not None and seg_start < last["segment_start_ms"]:
            state["late_dropped"] += 1
            continue
        if last is None or seg_start > last["segment_start_ms"]:
            if last is not None:
                last["sealed"] = True
            last = _segment_aggregate(seg_start, state["horizon"])
            segments.append(last)
        _venue_fold(last, trade, venue)
        folded += 1
        last_folded_ts = ts
    if last_folded_ts is not None:
        _prune(state, newest_ms=last_folded_ts)
    return folded


def _prune(state: dict[str, Any], *, newest_ms: int | None) -> None:
    """Trailing-horizon eviction: drop segments older than the horizon
    span behind the newest folded trade (count-bounded fallback too, so
    the ring can never exceed a small multiple of the horizon)."""
    if newest_ms is not None:
        cutoff = int(newest_ms) - HORIZON_PERIOD_MS[state["horizon"]]
        segments = state["segments"]
        while segments and (segments[0]["segment_start_ms"] or 0) < cutoff:
            segments.pop(0)
    # Absolute count fallback (per horizon: horizon span in segments + slack).
    cap = {"15m": 5, "1h": 14, "4h": 18}[state["horizon"]]
    segments = state["segments"]
    while len(segments) > cap:
        segments.pop(0)


def fold_candles(state: dict[str, Any], *, include_partial: bool = True) -> list[list[float]]:
    """Binance-kline-shaped rows from the fold segments: one row per
    segment, ``[open_time, open, high, low, close, volume]`` — the exact
    shape ``technicals.atr_pct_from_klines`` consumes. ``open`` is the
    segment's first trade price, ``close`` the last, ``volume`` the
    combined notional. The active (unsealed) segment is included as a
    partial candle unless ``include_partial`` is False."""
    rows: list[list[float]] = []
    for s in state["segments"]:
        if s.get("first_px") is None:
            continue
        if not include_partial and not s.get("sealed"):
            continue
        rows.append([
            s["segment_start_ms"], s["first_px"], s["high_px"],
            s["low_px"], s["last_px"], s["notional"],
        ])
    return rows


def fold_venue_totals(state: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Per-venue aggregated flow across ALL of the fold's segments:
    ``{venue: {trade_count, notional, buy_notional, sell_notional}}`` —
    the fold-native horizon input the tape worker consumes (buy shares,
    CVD proxies) without any extra evidence window."""
    out: dict[str, dict[str, float]] = {}
    for s in state["segments"]:
        for venue, v in (s.get("venues") or {}).items():
            acc = out.setdefault(venue, {
                "trade_count": 0, "notional": 0.0,
                "buy_notional": 0.0, "sell_notional": 0.0,
            })
            for k in acc:
                acc[k] += v.get(k) or 0
    return out


def fold_snapshot(state: dict[str, Any], snapshot: dict[str, Any], venue: str) -> int:
    """Fold one raw-evidence snapshot's ``trades_normalized`` for ``venue``.

    The snapshot's rolling trade window overlaps its predecessors — the
    fold's monotonic-id dedupe is what makes overlapping snapshots safe.
    """
    side = snapshot.get(venue) or {}
    trades = side.get("trades_normalized") if isinstance(side, dict) else None
    if not isinstance(trades, list):
        return 0
    return fold_trades(state, trades, venue)


def fold_state_summary(state: dict[str, Any], *, segment_cap: int = 64) -> dict[str, Any]:
    """Bounded, JSON-safe summary for payload persistence / PG checkpoints.

    Keeps at most the newest ``segment_cap`` segments (explicit truncation
    marker, same convention as every payload array). The high-waters ride
    along so a restored state resumes dedupe exactly where it left off.
    """
    segments = state["segments"]
    kept = segments[-segment_cap:]
    return {
        "horizon": state["horizon"],
        "hwm": dict(state["hwm"]),
        "late_dropped": state["late_dropped"],
        "duplicates_skipped": state["duplicates_skipped"],
        "segments_total": len(segments),
        "__segments_truncated__": len(segments) > len(kept),
        "segments": [dict(s) for s in kept],
    }


def restore_fold_state(summary: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a fold state from its persisted summary (PG rebuild path)."""
    state = new_fold_state(summary["horizon"])
    state["hwm"] = {k: int(v) for k, v in (summary.get("hwm") or {}).items()}
    state["late_dropped"] = int(summary.get("late_dropped") or 0)
    state["duplicates_skipped"] = int(summary.get("duplicates_skipped") or 0)
    state["segments"] = [dict(s) for s in (summary.get("segments") or [])]
    return state
