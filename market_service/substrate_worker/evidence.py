"""Worker-owned shared evidence builder — absorbed from fire.py.

The process-global window cache lived in core/fire.py::_EVIDENCE_CACHE and
made the core the owner of WHEN evidence is built. Workers own their fire
logic now; this module is the shared PERFORMANCE helper they all call —
no policy, no decisions, just one heavy build per (symbol, window) per TTL
slice so 12 workers on one asyncio loop do not each pay ~1.4s per arrival.

Policy (hygiene vs probe, staleness, cooldown) stays in the worker files.
Coordination (dedupe, publish/PG gates) stays in governance.
"""

from __future__ import annotations

import time
from typing import Any

from market_service.runtime.raw_window import (
    build_raw_window,
    build_ws_surface,
    overlay_ws_into_window,
)

_EVIDENCE_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
EVIDENCE_CACHE_TTL_S = 2.5


async def cached_window(
    store: Any,
    symbol: str,
    window_minutes: int,
    *,
    ws: bool = False,
    ws_venue: str = "futures",
) -> dict[str, Any]:
    """One heavy raw (+optional WS) window build per TTL slice, shared."""
    key = (symbol, int(window_minutes), bool(ws))
    cached = _EVIDENCE_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < EVIDENCE_CACHE_TTL_S:
        return cached[1]
    evidence = await build_raw_window(store, symbol, int(window_minutes))
    if ws:
        try:
            surface = await build_ws_surface(
                store, symbol, ws_venue, window_minutes=int(window_minutes)
            )
        except Exception:
            surface = None
        if surface is not None:
            evidence = overlay_ws_into_window(evidence, surface)
    _EVIDENCE_CACHE[key] = (time.monotonic(), evidence)
    return evidence


async def horizon_window(
    store: Any,
    symbol: str,
    minutes: int,
) -> dict[str, Any]:
    """One widened horizon build per TTL slice, shared across workers."""
    key = (symbol, int(minutes), False)
    cached = _EVIDENCE_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < EVIDENCE_CACHE_TTL_S:
        return cached[1]
    built = await build_raw_window(store, symbol, int(minutes))
    _EVIDENCE_CACHE[key] = (time.monotonic(), built)
    return built
