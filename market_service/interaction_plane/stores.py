"""Store construction + venue — one place opens Redis/Postgres.

``reads.py`` / ``control.py`` use these helpers; the CLI never touches
a store directly.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_VENUE = "futures"


def venue() -> str:
    """Runtime venue the whole system agrees on (capture writes it)."""
    return (os.getenv("MICROSTRUCTURE_VENUE") or DEFAULT_VENUE).lower().strip()


def settings_redis() -> Any:
    from market_service.config import Settings

    return Settings.from_redis_env()


def open_redis(settings: Any) -> Any:
    from market_service.runtime.redis_store import RedisRuntimeStore

    return RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )


def open_postgres(settings: Any) -> Any | None:
    if not getattr(settings, "database_url", None):
        return None
    from market_service.runtime.postgres_store import PostgresRuntimeStore

    return PostgresRuntimeStore(settings.database_url)


__all__ = ["DEFAULT_VENUE", "open_postgres", "open_redis", "settings_redis", "venue"]
