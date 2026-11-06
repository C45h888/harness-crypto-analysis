"""Worker-owned shared evidence builder — absorbed from fire.py.

The process-global window cache lived in core/fire.py::_EVIDENCE_CACHE and
made the core the owner of WHEN evidence is built. Workers own their fire
logic now; this module is the shared PERFORMANCE helper they all call —
no policy, no decisions, just one heavy build per (symbol, window) per TTL
slice so 12 workers on one asyncio loop do not each pay ~1.4s per arrival.

Policy (hygiene vs probe, staleness, cooldown) stays in the worker files.
Coordination (dedupe, publish/PG gates) stays in governance.

Vector-4 discipline: the cache is BOUNDED (size cap + TTL sweep on every
write, so dead symbols cannot accumulate) and SINGLE-FLIGHT (concurrent
ticks awaiting the same key share one build instead of each paying it —
the miss stampede that let N workers each materialize the same window).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Awaitable, Callable

from market_service.runtime.raw_window import (
    build_raw_window,
    build_ws_surface,
    overlay_ws_into_window,
)

_EVIDENCE_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_EVIDENCE_CACHE_TTL_S = 2.5
EVIDENCE_CACHE_TTL_S = _EVIDENCE_CACHE_TTL_S  # re-exported test seam
# Bound process-global retention: active keys per tick are
# (symbols x windows x ws-flag); anything beyond this is stale.
_EVIDENCE_MAX_KEYS = 64
# Monotonic build identity: every window the shared builders hand out gets
# a unique nonce (vector 5 artifact binding — probe/compute reuse proofs).
_NONCE = itertools.count(1)


def _stamp(evidence: dict[str, Any]) -> dict[str, Any]:
    try:
        evidence["_artifact_nonce"] = next(_NONCE)
    except Exception:
        pass
    return evidence


# In-progress builds keyed identically: concurrent awaiters share one.
_INFLIGHT: dict[tuple, asyncio.Future] = {}


async def _shared(key: tuple, build: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Single-flight cached build: one materialization per key per TTL.

    No tasks are spawned: the first arriver builds inline (identical tick
    behavior to a direct await — fire tasks still settle in-batch), while
    concurrent arrivers awaiting the in-progress future share the result.
    """
    now = time.monotonic()
    cached = _EVIDENCE_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _EVIDENCE_CACHE_TTL_S:
        return cached[1]
    inflight = _INFLIGHT.get(key)
    if inflight is not None:
        return await inflight
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _INFLIGHT[key] = future
    try:
        evidence = await build()
    except Exception as exc:
        if not future.done():
            future.set_exception(exc)
            future.exception()  # retrieved here; later awaiters still raise
        raise
    else:
        if not future.done():
            future.set_result(evidence)
    finally:
        _INFLIGHT.pop(key, None)
    # Sweep expired entries, then evict stalest past the cap.
    now = time.monotonic()
    for old in [k for k, (ts, _) in list(_EVIDENCE_CACHE.items())
                if now - ts >= _EVIDENCE_CACHE_TTL_S]:
        _EVIDENCE_CACHE.pop(old, None)
    while len(_EVIDENCE_CACHE) >= _EVIDENCE_MAX_KEYS:
        stalest = min(_EVIDENCE_CACHE, key=lambda k: _EVIDENCE_CACHE[k][0])
        _EVIDENCE_CACHE.pop(stalest, None)
    _EVIDENCE_CACHE[key] = (now, evidence)
    return evidence


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

    async def _build() -> dict[str, Any]:
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
        return _stamp(evidence)

    return await _shared(key, _build)


async def horizon_window(
    store: Any,
    symbol: str,
    minutes: int,
) -> dict[str, Any]:
    """One widened horizon build per TTL slice, shared across workers."""
    key = (symbol, int(minutes), False)

    async def _build() -> dict[str, Any]:
        return _stamp(await build_raw_window(store, symbol, int(minutes)))

    return await _shared(key, _build)
