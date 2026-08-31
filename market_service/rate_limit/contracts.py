"""Rate-limit contracts — typed surfaces, errors, configs, bucket state.

The ``rate_limit`` package mirrors the ``microstructure`` / ``runtime``
package shape: ``contracts.py`` owns the typed vocabulary, ``worker.py``
owns one bounded worker per API surface, ``substrate.py`` instantiates
and routes. Nothing in this module performs I/O.

Binance rate-limit facts encoded here:

- Per-IP weight buckets. Spot (``api.binance.com``) and USD-M futures
  (``fapi.binance.com``) are separate 1-minute weight pools, and the
  ``/futures/data/*`` statistics endpoints sit in a TIGHTER sub-pool.
- The server reports live usage via ``X-MBX-USED-WEIGHT-1M`` /
  ``X-MBX-LIMIT-1M`` response headers — workers sync to these instead
  of trusting hardcoded schedules (the "dynamic" part of the system).
- HTTP 429 = soft limit; ignoring it escalates to a ban.
- HTTP 418 = IP ban ("I'm a teapot"). ANY request during the ban
  extends it, so the worker fails fast without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SurfaceId(str, Enum):
    """One API surface = one rate-limit bucket = one bounded worker."""

    SPOT = "spot"                    # api.binance.com /api/v3/*
    FUTURES = "futures"              # fapi.binance.com /fapi/v1/*
    FUTURES_DATA = "futures_data"    # fapi.binance.com /futures/data/* (tight pool)
    COINGECKO = "coingecko"          # api.coingecko.com (reserved — phase 2)


class RateLimitError(Exception):
    """HTTP 429 — soft rate limit hit on one surface.

    Callers should back off; the worker already paused the bucket for
    ``retry_after_s``. Never raised for 5xx / timeouts.
    """

    def __init__(self, surface: SurfaceId, retry_after_s: float) -> None:
        self.surface = surface
        self.retry_after_s = retry_after_s
        super().__init__(
            f"{surface.value}: rate limited (429); retry after {retry_after_s:.1f}s"
        )


class IpBanError(Exception):
    """HTTP 418 — IP ban ("I'm a teapot") on one surface.

    ``ban_remaining_s`` is the time until unban per Retry-After. The
    worker refuses every acquisition until the ban expires; making any
    request during a ban EXTENDS it, so this must fail fast.
    """

    def __init__(self, surface: SurfaceId, ban_remaining_s: float) -> None:
        self.surface = surface
        self.ban_remaining_s = ban_remaining_s
        super().__init__(
            f"{surface.value}: IP banned (418); unban in {ban_remaining_s:.1f}s"
        )


@dataclass(frozen=True)
class SurfaceConfig:
    """Static tuning for one surface's bucket.

    ``weight_ceiling`` is the pre-flight gate: acquisitions block once
    projected 1-minute usage would exceed it. Default deployment runs it
    at 80% of ``weight_limit`` so a burst never touches the hard wall.
    """

    surface: SurfaceId
    weight_limit: int = 6000          # server limit per 1-minute window
    weight_ceiling: int = 4800        # pre-flight gate (default 80%)
    default_retry_s: float = 60.0     # fallback when 429 Retry-After absent
    default_ban_s: float = 120.0      # fallback when 418 Retry-After absent

    def __post_init__(self) -> None:
        if self.weight_limit <= 0:
            raise ValueError(f"weight_limit must be positive, got {self.weight_limit}")
        if not 0 < self.weight_ceiling <= self.weight_limit:
            raise ValueError(
                f"weight_ceiling must satisfy 0 < ceiling <= limit, got "
                f"{self.weight_ceiling} vs {self.weight_limit}"
            )


@dataclass
class BucketState:
    """Live state owned by ONE SurfaceRateWorker; never shared across workers.

    Weight accounting: ``used_weight`` tracks the maximum weight observed
    in the current 1-minute window. It syncs to the server-reported
    ``X-MBX-USED-WEIGHT-1M`` after each response (server truth wins over
    local estimates) and decays to zero when the window rolls over.
    Local acquisitions between header syncs add their weight on top.
    """

    window_start: float = 0.0         # monotonic anchor of the 1-min window
    used_weight: int = 0              # max(server-reported, local) this window
    pause_until: float = 0.0          # 429 cooldown deadline (monotonic)
    ban_until: float = 0.0            # 418 ban deadline (monotonic)

    def window_expired(self, now: float, window_s: float = 60.0) -> bool:
        return now - self.window_start >= window_s

    def reset_window(self, now: float) -> None:
        self.window_start = now
        self.used_weight = 0
