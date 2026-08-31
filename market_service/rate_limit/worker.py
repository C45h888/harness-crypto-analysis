"""SurfaceRateWorker — one bounded rate-limit worker per API surface.

The bounded unit of the rate-limit substrate. Each worker owns exactly one
:class:`BucketState` and one ``asyncio.Lock``; no other worker touches either.
A 12-endpoint ``asyncio.gather`` on one surface can never overshoot the
weight ceiling because every acquisition serializes through this worker.

Design notes:

- **Dynamic**: weight accounting syncs to the server-reported
  ``X-MBX-USED-WEIGHT-1M`` header after each response — real usage wins
  over local estimates. Falls back to local reservation math when headers
  are absent (non-Binance surfaces, error responses).
- **Fail-fast on ban**: once a 418 lands, every subsequent ``acquire()``
  raises :class:`IpBanError` without touching the network — a request
  during a ban EXTENDS the ban.
- **No I/O in this module**: the worker only computes, waits, and raises.
  The transport layer performs the HTTP calls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn

from market_service.rate_limit.contracts import (
    BucketState,
    IpBanError,
    RateLimitError,
    SurfaceConfig,
)

log = logging.getLogger(__name__)

_WINDOW_S = 60.0  # Binance weight window length


class SurfaceRateWorker:
    """Bounded worker owning one surface's rate-limit bucket."""

    def __init__(
        self,
        config: SurfaceConfig,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._clock = clock
        self._sleep = sleep
        self._state = BucketState(window_start=clock())
        self._lock = asyncio.Lock()

    @property
    def surface(self):
        return self.config.surface

    @property
    def state(self) -> BucketState:
        """Expose state for observability (poller status, tests)."""
        return self._state

    # ------------------------------------------------------------------
    # request path
    # ------------------------------------------------------------------

    async def acquire(self, weight: int = 1) -> None:
        """Pre-flight gate. Call BEFORE issuing the HTTP request.

        Semantics, in order:

        1. Ban active on this surface → raise ``IpBanError`` immediately
           (no network call; a request during a ban extends it).
        2. 429 pause active → wait until the pause expires.
        3. Window expired → reset the weight counter.
        4. Projected usage (``used_weight + weight``) would exceed
           ``weight_ceiling`` → sleep until the window rolls over, reset,
           then proceed.
        5. Reserve ``weight`` in the local counter.

        Serialized by an ``asyncio.Lock`` so concurrent acquisitions on
        this surface can't jointly overshoot the ceiling.
        """
        if weight <= 0:
            raise ValueError(f"weight must be positive, got {weight}")
        async with self._lock:
            now = self._clock()
            if now < self._state.ban_until:
                raise IpBanError(self.surface, self._state.ban_until - now)
            if now < self._state.pause_until:
                await self._sleep(self._state.pause_until - now)
                now = self._clock()
            if self._state.window_expired(now, _WINDOW_S):
                self._state.reset_window(now)
            projected = self._state.used_weight + weight
            if projected > self.config.weight_ceiling:
                # Cannot fit in the current window without breaching the
                # ceiling. Wait for the window to roll, then reserve fresh.
                wait_s = _WINDOW_S - (now - self._state.window_start)
                if wait_s > 0:
                    log.debug(
                        "rate-limit %s: weight gate — waiting %.1fs for window rollover "
                        "(used=%d + %d > ceiling=%d)",
                        self.surface.value, wait_s,
                        self._state.used_weight, weight, self.config.weight_ceiling,
                    )
                    await self._sleep(wait_s)
                self._state.reset_window(self._clock())
            self._state.used_weight += weight

    # ------------------------------------------------------------------
    # response path
    # ------------------------------------------------------------------

    def record_headers(self, headers: Mapping[str, str]) -> None:
        """Sync the bucket to server truth after a response.

        Reads ``X-MBX-USED-WEIGHT-1M`` (used weight in the current 1-minute
        window) and ``X-MBX-LIMIT-1M``. Server-reported usage wins over the
        local estimate — but never drops below a local reservation made
        since the last header (``max``), so a burst between responses is
        still accounted. Absent headers → no-op (non-Binance surfaces).
        """
        used_raw = _header(headers, "X-MBX-USED-WEIGHT-1M")
        if used_raw is None:
            return
        now = self._clock()
        if self._state.window_expired(now, _WINDOW_S):
            self._state.reset_window(now)
        self._state.used_weight = max(self._state.used_weight, used_raw)
        limit_raw = _header(headers, "X-MBX-LIMIT-1M")
        if limit_raw is not None and 0 < limit_raw < self.config.weight_limit:
            # Server reports a tighter limit than configured. SurfaceConfig
            # is frozen; the ceiling is re-derived from env at substrate
            # construction, so this is observability only.
            log.debug(
                "rate-limit %s: server limit %d below configured %d",
                self.surface.value, limit_raw, self.config.weight_limit,
            )

    def handle_error(self, status: int, headers: Mapping[str, str]) -> NoReturn:
        """Translate a 429/418 response into bucket state + typed error.

        Always raises: 429 → ``RateLimitError`` after pausing the bucket;
        418 → ``IpBanError`` after setting the ban deadline. Other statuses
        raise ``ValueError`` — callers must only route rate responses here.
        """
        now = self._clock()
        if status == 429:
            retry_s = _retry_after(headers) or self.config.default_retry_s
            self._state.pause_until = now + retry_s
            log.warning(
                "rate-limit %s: 429 soft limit — pausing bucket for %.1fs",
                self.surface.value, retry_s,
            )
            raise RateLimitError(self.surface, retry_s)
        if status == 418:
            ban_s = _retry_after(headers) or self.config.default_ban_s
            self._state.ban_until = now + ban_s
            log.error(
                "rate-limit %s: 418 IP BAN — bucket sealed for %.1fs; "
                "all acquisitions fail fast until unban",
                self.surface.value, ban_s,
            )
            raise IpBanError(self.surface, ban_s)
        raise ValueError(f"handle_error: not a rate-limit status: {status}")


# ----------------------------------------------------------------------
# header parsing helpers
# ----------------------------------------------------------------------


def _header(headers: Mapping[str, str], name: str) -> int | None:
    """Case-insensitive int header lookup; None when absent/unparseable."""
    raw = headers.get(name) or headers.get(name.lower())
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Retry-After seconds (int form per RFC 7231); None when absent."""
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
