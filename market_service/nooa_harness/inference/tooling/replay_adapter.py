"""Replay adapter — I/O pulls the FORCED tape window, fitting owns the math.

Two seams live here:

1. DURABLE-FORCED READ — events are read from the durable Postgres tape
   (microstructure_event_tape) for the requested window and merged with the
   live Redis stream tail (rows newer than the durable watermark). Tape
   reads are no longer bounded by the Redis stream maxlen. When Postgres is
   unavailable or the engine has no durable store, reads degrade to the
   stream exactly as before (never fabricated, never raised).

2. WINDOW-SCOPED DEGRADED SPANS — quality spans are derived from the
   capture status-TRANSITION ledger (a transition to a non-running state
   opens a span, the next transition back to ``running`` closes it) plus an
   inter-event discontinuity backstop. The cumulative ``sequence_gaps``
   counter in the latest-key status payload is transport telemetry ONLY and
   is never consumed for data quality: one reconnect must not poison every
   future window.
"""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry

# The capture heartbeat is the interval cadence (default 10s) and a healthy
# perps book yields events far more often than that. An inter-event hole of
# this size or larger is treated as a degraded tape span (discontinuity
# backstop for lost status history).  The default is 1.5× the 10s interval
# cadence (15s) so that 10s-spaced synthetic test tapes are not falsely
# flagged while real gaps (missed heartbeat + half the window) are detected.
DEFAULT_MAX_EVENT_GAP_MS = 15_000

# Sentinel end for a span that is still open (capture degraded at read time):
# refuses every target after the open boundary — we do not know the tape
# recovered, so we do not bridge it.
_OPEN_SPAN_END = 2**62


async def _read_windowed_events(
    store: Any, symbol: str, venue: str, *, window_minutes: int,
    postgres: Any | None = None,
) -> tuple[list[Any], list[Any], int, int, int]:
    """Read the FORCED tape window (durable-first, live-merged).

    I/O only, no math. Returns ``(events, windowed, start_ts, end_ts,
    dropped)``. The window is anchored on the newest event (live or durable)
    and [start, end] is a reproducible closed interval.
    """
    import market_service.microstructure as fm

    payloads = await _read_tape_payloads(store, symbol, venue, postgres=postgres)
    events, dropped = fm.replay_events_from_payloads(payloads)
    if not events:
        return [], [], 0, 0, dropped
    end_ts = events[-1].current.exchange_ts_ms
    start_ts = end_ts - window_minutes * 60_000
    windowed = [e for e in events if e.current.exchange_ts_ms >= start_ts]
    return events, windowed, start_ts, end_ts, dropped


async def _read_tape_payloads(
    store: Any, symbol: str, venue: str, *, postgres: Any | None = None,
) -> list[dict[str, Any]]:
    """Durable-forced tape read: durable rows merged with the live stream.

    Dedupe key is (update_id, exchange_ts_ms) — the same idempotency the
    durable table enforces. Sort is (exchange_ts_ms, update_id). Durable
    failures fall back to stream-only silently (logged by the store layer).
    """
    sym = symbol.upper()
    live = await store.read_microstructure_events(venue, sym)
    if postgres is None:
        return live
    try:
        watermark = await postgres.latest_microstructure_tape_ts(sym, venue)
    except Exception:
        return live
    if watermark is None:
        return live
    try:
        durable = await postgres.read_microstructure_tape_events(sym, venue)
    except Exception:
        return live
    durable_keys = {
        ((row.get("current") or {}).get("update_id"),
         (row.get("current") or {}).get("exchange_ts_ms"))
        for row in durable
    }
    merged = list(durable)
    seen = set(durable_keys)
    for row in live:
        current = row.get("current") or {}
        key = (current.get("update_id"), current.get("exchange_ts_ms"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda row: (
        int((row.get("current") or {}).get("exchange_ts_ms") or 0),
        int((row.get("current") or {}).get("update_id") or 0),
    ))
    return merged


def _spans_from_status_transitions(
    transitions: list[Any],
) -> list[dict[str, Any]]:
    """Derive window-scoped degraded spans from capture status transitions.

    A transition INTO any state other than ``running`` opens a degraded span
    at its ``updated_at_ms``; the next transition back to ``running`` closes
    it. A still-open span at read time ends at the sentinel (refuses every
    target after the open boundary — the tape has not provably recovered).
    """
    spans: list[dict[str, Any]] = []
    open_start: int | None = None
    open_reason: str | None = None
    for row in transitions:
        if not isinstance(row, dict):
            continue
        state = str(row.get("to_state") or row.get("state") or "").lower()
        ts = _safe_ts(row)
        if ts is None:
            continue
        if state == "running":
            if open_start is not None:
                spans.append({
                    "start_ts_ms": open_start, "end_ts_ms": ts,
                    "quality": open_reason or "capture_gap",
                    "reason": open_reason or "capture_gap",
                })
                open_start, open_reason = None, None
        elif open_start is None:
            open_start = ts
            open_reason = f"capture_state_{state}" if state else "capture_gap"
    if open_start is not None:
        spans.append({
            "start_ts_ms": open_start, "end_ts_ms": _OPEN_SPAN_END,
            "quality": open_reason or "capture_gap",
            "reason": open_reason or "capture_gap",
        })
    return spans


def _spans_from_discontinuities(
    windowed: list[Any], max_event_gap_ms: int,
) -> list[dict[str, Any]]:
    """Discontinuity backstop: inter-event holes >= max_event_gap_ms.

    Covers degradation when the status-transition history itself is lost
    (e.g. Redis flush) — the event timestamps are the last line of truth.
    """
    spans: list[dict[str, Any]] = []
    prev_ts: int | None = None
    for event in windowed:
        try:
            ts = int(event.current.exchange_ts_ms)
        except (AttributeError, TypeError, ValueError):
            prev_ts = None
            continue
        if prev_ts is not None and ts - prev_ts >= max_event_gap_ms:
            spans.append({
                "start_ts_ms": prev_ts, "end_ts_ms": ts,
                "quality": "tape_discontinuity",
                "reason": f"inter_event_gap_{ts - prev_ts}ms",
            })
        prev_ts = ts
    return spans


async def _degraded_spans(
    store: Any, symbol: str, venue: str, *,
    windowed: list[Any], max_event_gap_ms: int,
) -> list[dict[str, Any]]:
    """Window-scoped quality spans: status-transition ledger + discontinuities."""
    spans = _spans_from_discontinuities(windowed, max_event_gap_ms)
    try:
        transitions = await store.read_microstructure_status_transitions(venue, symbol.upper())
    except Exception:
        transitions = []
    status_spans = _spans_from_status_transitions(transitions)
    return spans + status_spans


def _safe_ts(row: dict[str, Any]) -> int | None:
    raw = row.get("updated_at_ms")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def degraded_spans_in_window(
    store: Any, symbol: str, venue: str, *, window_ms: int,
) -> int:
    """Count degraded spans overlapping the recent window (gate input).

    Window-scoped: spans fully older than the window do not gate the cycle.
    Never reads the cumulative ``sequence_gaps`` transport counter.
    """
    try:
        transitions = await store.read_microstructure_status_transitions(venue, symbol.upper())
    except Exception:
        return 0
    rows = [t for t in transitions if isinstance(t, dict)]
    if not rows:
        return 0
    now = max((_safe_ts(t) or 0) for t in rows)
    if now <= 0:
        return 0
    spans = _spans_from_status_transitions(rows)
    return sum(1 for s in spans if s["end_ts_ms"] > now - window_ms)


async def _forward_replay_inputs(
    store: Any, symbol: str, venue: str, *,
    window_minutes: int, tick_size: str,
    postgres: Any | None = None,
    max_event_gap_ms: int = DEFAULT_MAX_EVENT_GAP_MS,
) -> tuple[list[Any], list[Any], list[tuple[int, Any]], list[Any], dict[str, int]]:
    """Replay one forward window with interval features attached to events.

    I/O lives here (durable tape + ledger + status reads); all vector/join
    math is owned by ``fitting_route_c.replay_forward_window`` so every
    forward consumer shares one deterministic semantic.
    """
    from decimal import Decimal

    import market_service.microstructure as fm

    events, windowed, start_ts, end_ts, _dropped = await _read_windowed_events(
        store, symbol, venue, window_minutes=window_minutes, postgres=postgres)
    if not events or not windowed:
        return [], [], [], [], {"excluded_total": 0}
    intervals = fm.replay_intervals(windowed, interval_ms=10_000)
    spans = await _degraded_spans(
        store, symbol, venue, windowed=windowed, max_event_gap_ms=max_event_gap_ms)
    vectors, mids, pairs, log = fm.replay_forward_window(
        windowed, intervals, symbol=symbol, venue=venue,
        tick_size=Decimal(str(tick_size)),
        window_start_ms=start_ts, window_end_ms=end_ts,
        degraded_spans=spans,
    )
    return windowed, vectors, mids, pairs, log


# Public alias — new code should import ``forward_replay_inputs``.
forward_replay_inputs = _forward_replay_inputs
