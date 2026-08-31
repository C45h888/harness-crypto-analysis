from __future__ import annotations

import os
from dataclasses import dataclass


# Canonical order-book depth. Single source of truth for every poller, calcs,
# analysis, and CLI path. Mirrors the legacy live scripts (which scanned the
# full Binance depth book up to ``limit=1000``) while staying a rational,
# centrally-configurable depth: ``DEPTH_LEVELS`` ovverrides at deploy time.
DEFAULT_DEPTH_LEVELS = 500


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def default_depth_levels() -> int:
    """Resolve the canonical order-book depth from env (no DB required).

    CLI/standalone analysis paths that fetch their own Binance book (and may
    run without a database) use this instead of ``Settings.from_redis_env()``
    so the order-book depth stays centralized on ``DEPTH_LEVELS`` everywhere.
    """
    return _positive_int("DEPTH_LEVELS", DEFAULT_DEPTH_LEVELS)


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    redis_url: str
    redis_key_prefix: str
    redis_stream_maxlen: int
    symbols: tuple[str, ...]
    poll_symbols: tuple[str, ...]
    poll_seconds: int
    flow_window_seconds: int
    depth_levels: int
    max_domain_state_age_seconds: int = 90
    wall_history_maxlen: int = 200

    @classmethod
    def from_env(cls) -> "Settings":
        """Full runtime settings requiring ``DATABASE_URL`` for the durable Postgres ledger.

        Use this for any surface that actually reads/writes Postgres (envelope
        persistence, analyst briefing, MemoryNode). Redis-only surfaces that
        never touch Postgres should use :meth:`from_redis_env` instead.
        """
        return cls._resolve(require_db=True)

    @classmethod
    def from_redis_env(cls) -> "Settings":
        """Redis-only runtime settings; ``DATABASE_URL`` is NOT required.

        This is the correct resolver for a mature runtime state where Redis
        is the operational source of truth and Postgres is the durable ledger
        only contacted when explicitly required. Redis-only surfaces
        (``--latest`` / ``--run-id`` / ``--refresh-derivatives`` / the poller)
        use this and run with Redis alone.

        ``database_url`` is ``None`` when unset. Any Postgres construction
        against it raises a clear bound-to-boundary error (see
        ``PostgresRuntimeStore``), so ``DATABASE_URL`` is only ever required
        by the Postgres system.
        """
        return cls._resolve(require_db=False)

    @classmethod
    def _resolve(cls, *, require_db: bool) -> "Settings":
        url = os.getenv("DATABASE_URL")
        if require_db and not url:
            raise ValueError(
                "DATABASE_URL is required for the Postgres ledger. "
                "Use Settings.from_redis_env() for Redis-only paths."
            )
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        symbols = tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip())
        if not symbols:
            raise ValueError("SYMBOLS must contain at least one symbol")
        # POLL_SYMBOLS: the subset the poller actively pulls. Separates
        # "what exists in the system" (SYMBOLS) from "what the poller
        # spends Binance weight on". Empty/unset -> full SYMBOLS set.
        # A live Redis control key overrides both at runtime (see poller.py).
        poll_env = tuple(s.strip().upper() for s in os.getenv("POLL_SYMBOLS", "").split(",") if s.strip())
        return cls(
            database_url=url,
            redis_url=redis_url,
            redis_key_prefix=os.getenv("REDIS_KEY_PREFIX", "marketflow"),
            redis_stream_maxlen=_positive_int("REDIS_STREAM_MAXLEN", 1200),
            wall_history_maxlen=_positive_int("WALL_HISTORY_MAXLEN", 200),
            symbols=symbols,
            poll_symbols=poll_env or symbols,
            poll_seconds=_positive_int("POLL_SECONDS", 5),
            flow_window_seconds=_positive_int("FLOW_WINDOW_SECONDS", 300),
            depth_levels=default_depth_levels(),
            max_domain_state_age_seconds=_positive_int("MAX_DOMAIN_STATE_AGE_SECONDS", 90),
        )
