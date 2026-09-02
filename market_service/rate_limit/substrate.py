"""RateLimitSubstrate — instantiates one bounded worker per API surface.

The substrate is the single object the transport layer holds. It owns the
worker map but delegates ALL rate-limit state to the workers — the substrate
itself is stateless beyond routing. One :class:`SurfaceRateWorker` per
:class:`SurfaceId`; a surface's worker is created lazily on first use from
the configured defaults, so adding a new surface (e.g. CoinGecko) needs
only a config entry, not a code change here.

Disabled mode (``enabled=False``) makes every method a no-op so the
transport wiring can stay unconditional.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping

from market_service.rate_limit.contracts import (
    SurfaceConfig,
    SurfaceId,
)
from market_service.rate_limit.worker import SurfaceRateWorker


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def default_configs() -> dict[SurfaceId, SurfaceConfig]:
    """Per-surface defaults resolved from env.

    Env vars (all optional):
      BINANCE_WEIGHT_LIMIT          spot/futures 1-min weight limit (default 6000)
      BINANCE_WEIGHT_CEILING        pre-flight gate (default 80% of limit)
      BINANCE_DATA_WEIGHT_LIMIT     /futures/data tight-pool limit (default 3000)
      BINANCE_DATA_WEIGHT_CEILING   /futures/data gate (default 80% of pool)
      BINANCE_DEFAULT_RETRY_S       fallback 429 pause (default 60)
      BINANCE_DEFAULT_BAN_S         fallback 418 ban (default 120)
    """
    weight_limit = _env_int("BINANCE_WEIGHT_LIMIT", 6000)
    weight_ceiling = _env_int("BINANCE_WEIGHT_CEILING", int(weight_limit * 0.8))
    data_limit = _env_int("BINANCE_DATA_WEIGHT_LIMIT", 3000)
    data_ceiling = _env_int("BINANCE_DATA_WEIGHT_CEILING", int(data_limit * 0.8))
    default_retry = _env_float("BINANCE_DEFAULT_RETRY_S", 60.0)
    default_ban = _env_float("BINANCE_DEFAULT_BAN_S", 120.0)

    binance_kwargs = dict(
        default_retry_s=default_retry, default_ban_s=default_ban,
    )
    return {
        SurfaceId.SPOT: SurfaceConfig(
            SurfaceId.SPOT, weight_limit=weight_limit,
            weight_ceiling=weight_ceiling, **binance_kwargs,
        ),
        SurfaceId.FUTURES: SurfaceConfig(
            SurfaceId.FUTURES, weight_limit=weight_limit,
            weight_ceiling=weight_ceiling, **binance_kwargs,
        ),
        SurfaceId.FUTURES_DATA: SurfaceConfig(
            SurfaceId.FUTURES_DATA, weight_limit=data_limit,
            weight_ceiling=data_ceiling, **binance_kwargs,
        ),
        SurfaceId.COINGECKO: SurfaceConfig(
            SurfaceId.COINGECKO, weight_limit=30, weight_ceiling=24,
            default_retry_s=default_retry, default_ban_s=default_ban,
        ),
    }


class RateLimitSubstrate:
    """Instantiates + routes bounded rate-limit workers per API surface."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        configs: Mapping[SurfaceId, SurfaceConfig] | None = None,
    ) -> None:
        self.enabled = enabled
        self._configs: dict[SurfaceId, SurfaceConfig] = dict(
            configs if configs is not None else default_configs()
        )
        self._workers: dict[SurfaceId, SurfaceRateWorker] = {}

    @classmethod
    def from_env(cls) -> "RateLimitSubstrate":
        """Build from env. ``BINANCE_RATE_LIMIT_ENABLED=0`` disables the
        whole substrate (every call becomes a no-op)."""
        enabled = os.getenv("BINANCE_RATE_LIMIT_ENABLED", "1").strip() != "0"
        return cls(enabled=enabled, configs=default_configs())

    def worker(self, surface: SurfaceId) -> SurfaceRateWorker:
        """Return (lazily creating) the bounded worker for one surface."""
        worker = self._workers.get(surface)
        if worker is None:
            config = self._configs.get(surface)
            if config is None:
                config = SurfaceConfig(surface)
                self._configs[surface] = config
            worker = SurfaceRateWorker(config, clock=time.monotonic)
            self._workers[surface] = worker
        return worker

    # ------------------------------------------------------------------
    # transport-facing API (no-ops when disabled)
    # ------------------------------------------------------------------

    async def acquire(self, surface: SurfaceId, weight: int = 1) -> None:
        if not self.enabled:
            return
        await self.worker(surface).acquire(weight)

    async def rollback(self, surface: SurfaceId, weight: int = 1) -> None:
        """Release a reservation when the HTTP call never produced a
        response. Mirrors :meth:`acquire` — transports wrap their HTTP
        call in try/except and call ``rollback`` on any network failure."""
        if not self.enabled:
            return
        await self.worker(surface).rollback(weight)

    def record(self, surface: SurfaceId, headers: Mapping[str, str]) -> None:
        if not self.enabled:
            return
        self.worker(surface).record_headers(headers)

    def handle_error(
        self, surface: SurfaceId, status: int, headers: Mapping[str, str],
    ) -> None:
        """Translate a 429/418 into bucket state + typed error.

        When the substrate is disabled this is a no-op: the transport's
        ``raise_for_status`` then produces the legacy aiohttp error, so
        ``BINANCE_RATE_LIMIT_ENABLED=0`` restores pre-substrate behavior
        exactly. When enabled, always raises (typed error).
        """
        if not self.enabled:
            return
        self.worker(surface).handle_error(status, headers)
