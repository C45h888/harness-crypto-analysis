"""Rate-limit substrate — bounded per-surface workers for API transports.

Public surface: :class:`RateLimitSubstrate` (instantiates + routes),
:class:`SurfaceRateWorker` (one bounded bucket), typed errors
(:class:`RateLimitError` / :class:`IpBanError`), and :class:`SurfaceId`.

Package shape mirrors ``microstructure`` / ``runtime``: ``contracts.py``
owns the typed vocabulary, ``worker.py`` owns the bounded per-surface unit,
``substrate.py`` owns instantiation + routing.
"""

from market_service.rate_limit.contracts import (
    BucketState,
    IpBanError,
    RateLimitError,
    SurfaceConfig,
    SurfaceId,
)
from market_service.rate_limit.substrate import RateLimitSubstrate, default_configs
from market_service.rate_limit.worker import SurfaceRateWorker

__all__ = [
    "BucketState",
    "IpBanError",
    "RateLimitError",
    "RateLimitSubstrate",
    "SurfaceConfig",
    "SurfaceId",
    "SurfaceRateWorker",
    "default_configs",
]
