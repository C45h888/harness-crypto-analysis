"""Substrate worker runner — process entrypoint for the worker plane.

Instantiates the enabled substrate workers (env-selected) and runs them as
async tasks in ONE process; each worker keeps its own consumer group and
supervisor key on the Redis plane, so per-substrate isolation is preserved
without process-per-substrate sprawl.

Usage:
    python -m market_service.substrate_worker.runner [SYMBOL] [--once]

Env:
    SUBSTRATE_WORKERS       comma list of substrate names, or "all"
                            (default: all registered workers)
    SUBSTRATE_SYMBOLS       comma list of symbols (default: MICROSTRUCTURE_SYMBOL
                            or SOLUSDT; Phase 3 fans out per symbol)
    SUBSTRATE_WINDOW_MINUTES  evidence window per fire (default 15)
    SUBSTRATE_COOLDOWN_S    min seconds between fires per worker (default 30)
    SUBSTRATE_STALENESS_S   max projection age before a staleness override
                            fire (default 120)
    DEPTH_LEVELS            canonical order-book depth (default resolver)
    LOG_LEVEL               (default INFO)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any

from market_service.config import default_depth_levels
from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.substrate_worker import WORKER_REGISTRY, SubstrateWorkerCore

log = logging.getLogger(__name__)


def _split_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default).strip()
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def _symbols() -> list[str]:
    explicit = os.getenv("SUBSTRATE_SYMBOLS")
    if explicit:
        return [s.strip().upper() for s in explicit.split(",") if s.strip()]
    return [(os.getenv("MICROSTRUCTURE_SYMBOL") or "SOLUSDT").upper()]


def build_workers(
    store: RedisRuntimeStore,
    *,
    symbols: list[str],
    names: list[str] | None = None,
    pg_store: Any | None = None,
    pg_strict: bool = True,
) -> list[SubstrateWorkerCore]:
    """Instantiate the enabled workers (registry-driven, env-tunable)."""
    selected = names or _split_env("SUBSTRATE_WORKERS", "all")
    if "ALL" in selected:
        selected = sorted(WORKER_REGISTRY)
    window_minutes = int(os.getenv("SUBSTRATE_WINDOW_MINUTES") or 15)
    # Global overrides: explicit env wins; unset means each worker's
    # CADENCE profile decides (None falls through to the profile).
    _cooldown_raw = os.getenv("SUBSTRATE_COOLDOWN_S")
    _staleness_raw = os.getenv("SUBSTRATE_STALENESS_S")
    cooldown_s = int(_cooldown_raw) if _cooldown_raw else None
    staleness_s = int(_staleness_raw) if _staleness_raw else None
    depth = int(os.getenv("DEPTH_LEVELS") or default_depth_levels())

    workers: list[SubstrateWorkerCore] = []
    for name in selected:
        worker_cls = WORKER_REGISTRY.get(name.lower())
        if worker_cls is None:
            raise ValueError(
                f"unknown substrate worker {name!r}; registered: {sorted(WORKER_REGISTRY)}"
            )
        for symbol in symbols:
            workers.append(worker_cls(
                store,
                symbol=symbol,
                window_minutes=window_minutes,
                depth=depth,
                cooldown_s=cooldown_s,
                staleness_s=staleness_s,
                pg_store=pg_store,
                pg_strict=pg_strict,
            ))
    return workers


def _pg_store() -> Any | None:
    """Build the Phase 3 durable ledger handle (None when unset).

    ``DATABASE_URL`` lives only at this boundary: Redis-only operation
    stays possible by simply not exporting it.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return None
    from market_service.runtime.postgres_store import PostgresRuntimeStore
    return PostgresRuntimeStore(database_url)


def _pg_strict() -> bool:
    """PG strict mode (default on): PG failure aborts the fire."""
    return (os.getenv("SUBSTRATE_PG_STRICT") or "1").strip() != "0"


async def run_forever() -> int:
    """Run all enabled workers until cancelled."""
    settings_url = os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    store = RedisRuntimeStore(
        settings_url,
        os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    )
    workers = build_workers(store, symbols=_symbols(),
                            pg_store=(pg := _pg_store()), pg_strict=_pg_strict())
    if not workers:
        log.error("no substrate workers selected — nothing to run")
        return 1
    log.info(
        "substrate workers starting: %s (symbols=%s)",
        [f"{w.SUBSTRATE_NAME}:{w.symbol}" for w in workers], _symbols(),
    )
    try:
        results = await asyncio.gather(
            *(worker.run_forever() for worker in workers),
            return_exceptions=True,
        )
        fired = sum(r for r in results if isinstance(r, int))
        failures = [r for r in results if isinstance(r, BaseException)]
        for failure in failures:
            log.error("worker crashed: %r", failure)
        return 0 if not failures else 1
    finally:
        for worker in workers:
            try:
                await worker.stop()
            except Exception:
                log.exception("worker %s stop failed", worker.SUBSTRATE_NAME)
        await store.close()
        if pg is not None:
            try:
                await pg.close()
            except Exception:
                log.exception("pg store close failed")


async def run_once() -> int:
    """One bounded tick per worker (smoke test / cold-start bootstrap)."""
    settings_url = os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    store = RedisRuntimeStore(
        settings_url,
        os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    )
    workers = build_workers(store, symbols=_symbols())
    try:
        for worker in workers:
            await worker.start()
            await worker._tick(_now_ms_safe())
            await worker._read_once(block_ms=10)
            await worker._handle_rows(await worker._read_once(block_ms=10), _now_ms_safe())
            log.info("substrate %s once-tick complete (fired=%d)",
                     worker.SUBSTRATE_NAME, worker.fired_count)
        return 0
    finally:
        for worker in workers:
            await worker.stop()
        await store.close()


def _now_ms_safe() -> int:
    import time
    return int(time.time() * 1000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Substrate worker plane")
    parser.add_argument("symbol", nargs="?", default=None,
                        help="symbol override (single-symbol mode)")
    parser.add_argument("--once", action="store_true",
                        help="run a single bounded tick per worker and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.symbol:
        os.environ["SUBSTRATE_SYMBOLS"] = args.symbol.upper()

    if args.once:
        return asyncio.run(run_once())
    try:
        return asyncio.run(run_forever())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())