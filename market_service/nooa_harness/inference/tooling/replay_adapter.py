"""Replay adapter — I/O pulls windowed tape, fitting owns the math."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry


async def _read_windowed_events(
    store: Any, symbol: str, venue: str, *, window_minutes: int,
) -> tuple[list[Any], list[Any], int, int, int]:
    """Read the ledger and anchor the reproducible window (I/O only, no math)."""
    import market_service.microstructure as fm

    payloads = await store.read_microstructure_events(venue, symbol.upper())
    events, dropped = fm.replay_events_from_payloads(payloads)
    if not events:
        return [], [], 0, 0, dropped
    end_ts = events[-1].current.exchange_ts_ms
    start_ts = end_ts - window_minutes * 60_000
    windowed = [e for e in events if e.current.exchange_ts_ms >= start_ts]
    return events, windowed, start_ts, end_ts, dropped


async def _forward_replay_inputs(
    store: Any, symbol: str, venue: str, *,
    window_minutes: int, tick_size: str,
) -> tuple[list[Any], list[Any], list[tuple[int, Any]], list[Any], dict[str, int]]:
    """Replay one forward window with interval features attached to events.

    I/O lives here (ledger + status reads); all vector/join math is owned
    by ``fitting_route_c.replay_forward_window`` so every forward consumer
    shares one deterministic semantic.
    """
    from decimal import Decimal

    import market_service.microstructure as fm

    events, windowed, start_ts, end_ts, _dropped = await _read_windowed_events(
        store, symbol, venue, window_minutes=window_minutes)
    if not events or not windowed:
        return [], [], [], [], {"excluded_total": 0}
    intervals = fm.replay_intervals(windowed, interval_ms=10_000)
    status = await store.read_microstructure_status(venue, symbol.upper())
    gaps = int((status or {}).get("sequence_gaps") or 0)
    vectors, mids, pairs, log = fm.replay_forward_window(
        windowed, intervals, symbol=symbol, venue=venue,
        tick_size=Decimal(str(tick_size)),
        window_start_ms=start_ts, window_end_ms=end_ts,
        sequence_gaps=gaps,
    )
    return windowed, vectors, mids, pairs, log


# Public alias — new code should import ``forward_replay_inputs``.
forward_replay_inputs = _forward_replay_inputs
