from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


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


# ---------------------------------------------------------------------------
# Phase 2.4 — wall tier + scorecard configuration loaders.
# ---------------------------------------------------------------------------


def _parse_tier_config(raw: str | None) -> dict[str, float]:
    """Parse the ``WALL_TIER_CONFIG`` env var into a tier-config dict.

    Accepts JSON (``{"mega_usd": 250000, "large_usd": 50000}``) or the
    literal string ``"default"`` to reset to factory defaults. Returns
    the defaults when ``raw`` is unset/empty. Raises ``ValueError`` on
    malformed input.
    """
    defaults = {"mega_usd": 250_000.0, "large_usd": 50_000.0, "medium_usd": 10_000.0}
    if not raw or not raw.strip():
        return defaults
    if raw.strip().lower() == "default":
        return defaults
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"WALL_TIER_CONFIG must be JSON or 'default' (got: {raw!r})"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("WALL_TIER_CONFIG JSON must be an object")
    out = dict(defaults)
    for k, v in parsed.items():
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"WALL_TIER_CONFIG.{k} must be numeric") from exc
    return out


def _parse_scorecard_weights(raw: str | None) -> dict[str, float]:
    """Parse the ``WALL_SCORECARD_WEIGHTS`` env var.

    JSON object of ``{factor_name: weight}`` where factor names are
    the scorecard driver keys (``bid_ask_qty_ratio``,
    ``taker_buy_trend``, ``oi_change_trend``, ``top_long_drift``,
    ``net_buy_trend``). Unknown keys are ignored so an operator can
    leave the rest at defaults.
    """
    defaults = {
        "bid_ask_qty_ratio": 1.0,
        "taker_buy_trend": 1.0,
        "oi_change_trend": 1.0,
        "top_long_drift": 1.0,
        "net_buy_trend": 1.0,
    }
    if not raw or not raw.strip():
        return defaults
    if raw.strip().lower() == "default":
        return defaults
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"WALL_SCORECARD_WEIGHTS must be JSON or 'default' (got: {raw!r})"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("WALL_SCORECARD_WEIGHTS JSON must be an object")
    out = dict(defaults)
    for k, v in parsed.items():
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"WALL_SCORECARD_WEIGHTS.{k} must be numeric") from exc
    return out


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
    # Phase 2.4: tier + scorecard configuration (USD-notional buckets and
    # weight overrides for the wall/keystone-holds scorecard). Defaults
    # are sensible for SOL/ETH/BTC majors; per-instrument tuning via
    # env vars (``WALL_TIER_CONFIG``, ``WALL_SCORECARD_WEIGHTS``).
    wall_tier_config: dict[str, float] = field(default_factory=lambda: {
        "mega_usd": 250_000.0,
        "large_usd": 50_000.0,
        "medium_usd": 10_000.0,
    })
    wall_scorecard_weights: dict[str, float] = field(default_factory=lambda: {
        # Each weight scales the corresponding factor's raw contribution
        # (0 / 1 / 2 from the legacy ladder). Default weight 1.0
        # reproduces the legacy scorecard behavior exactly; operators
        # dial in relative importance per-factor without code changes.
        # The scorecard normalizes by the weighted ceiling so the
        # 0-10 ceiling is preserved regardless of weights.
        "bid_ask_qty_ratio": 1.0,
        "taker_buy_trend": 1.0,
        "oi_change_trend": 1.0,
        "top_long_drift": 1.0,
        "net_buy_trend": 1.0,
    })

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

        tier_config = _parse_tier_config(os.getenv("WALL_TIER_CONFIG"))
        scorecard_weights = _parse_scorecard_weights(os.getenv("WALL_SCORECARD_WEIGHTS"))

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
            wall_tier_config=tier_config,
            wall_scorecard_weights=scorecard_weights,
        )
