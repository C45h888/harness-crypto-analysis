from __future__ import annotations

import os
from dataclasses import dataclass


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    redis_key_prefix: str
    redis_stream_maxlen: int
    symbols: tuple[str, ...]
    poll_seconds: int
    flow_window_seconds: int
    depth_levels: int
    max_domain_state_age_seconds: int = 90
    wall_history_maxlen: int = 200

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.getenv("DATABASE_URL")
        if not url:
            raise ValueError("DATABASE_URL is required")
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        symbols = tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip())
        if not symbols:
            raise ValueError("SYMBOLS must contain at least one symbol")
        return cls(
            database_url=url,
            redis_url=redis_url,
            redis_key_prefix=os.getenv("REDIS_KEY_PREFIX", "marketflow"),
            redis_stream_maxlen=_positive_int("REDIS_STREAM_MAXLEN", 10000),
            wall_history_maxlen=_positive_int("WALL_HISTORY_MAXLEN", 200),
            symbols=symbols,
            poll_seconds=_positive_int("POLL_SECONDS", 30),
            flow_window_seconds=_positive_int("FLOW_WINDOW_SECONDS", 300),
            depth_levels=_positive_int("DEPTH_LEVELS", 20),
            max_domain_state_age_seconds=_positive_int("MAX_DOMAIN_STATE_AGE_SECONDS", 90),
        )
