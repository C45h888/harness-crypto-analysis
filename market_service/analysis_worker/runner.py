"""Analysis worker runner — process entrypoint for the analysis plane.

Mirrors ``substrate_worker.runner``: instantiates the enabled analysis
workers (env-selected) and runs them as async tasks in ONE process; each
worker keeps its own consumer group and supervisor key on the Redis plane
(analysis keyspace), so per-domain isolation is preserved without
process-per-worker sprawl.

Env:
    ANALYSIS_WORKERS        comma list of analysis names, or "all"
    ANALYSIS_SYMBOLS        comma list of symbols (default MICROSTRUCTURE_SYMBOL
                            or SOLUSDT)
    ANALYSIS_COOLDOWN_S     global probe-cooldown override
    ANALYSIS_STALENESS_S    global staleness override
    DEPTH_LEVELS            canonical order-book depth (default resolver)
    LOG_LEVEL               (default INFO)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any

from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


def _split_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default).strip()
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _symbols() -> list[str]:
    explicit = os.getenv("ANALYSIS_SYMBOLS")
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
) -> list[Any]:
    """Instantiate the enabled analysis workers (registry-driven)."""
    selected = names or _split_env("ANALYSIS_WORKERS", "all")
    if "all" in selected:
        selected = sorted(ANALYSIS_WORKER_REGISTRY)
    cooldown_raw = os.getenv("ANALYSIS_COOLDOWN_S")
    staleness_raw = os.getenv("ANALYSIS_STALENESS_S")
    cooldown_s = int(cooldown_raw) if cooldown_raw else None
    staleness_s = int(staleness_raw) if staleness_raw else None
    depth = int(os.getenv("DEPTH_LEVELS") or 500)

    workers: list[Any] = []
    for name in selected:
        worker_cls = ANALYSIS_WORKER_REGISTRY.get(name.lower())
        if worker_cls is None:
            raise ValueError(
                f"unknown analysis worker {name!r}; registered: {sorted(ANALYSIS_WORKER_REGISTRY)}"
            )
        for symbol in symbols:
            workers.append(worker_cls(
                store,
                symbol=symbol,
                cooldown_s=cooldown_s,
                staleness_s=staleness_s,
                pg_store=pg_store,
                pg_strict=pg_strict,
            ))
    return workers


async def run_forever() -> int:
    settings_url = os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    store = RedisRuntimeStore(
        settings_url,
        os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    )
    workers = build_workers(store, symbols=_symbols())
    if not workers:
        log.error("no analysis workers selected — nothing to run")
        return 1
    log.info(
        "analysis workers starting: %s (symbols=%s)",
        [f"{w.SUBSTRATE_NAME}:{w.symbol}" for w in workers], _symbols(),
    )
    try:
        results = await asyncio.gather(
            *(worker.run_forever() for worker in workers),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        for failure in failures:
            log.error("analysis worker crashed: %r", failure)
        return 0 if not failures else 1
    finally:
        for worker in workers:
            try:
                await worker.stop()
            except Exception:
                log.exception("worker %s stop failed", worker.SUBSTRATE_NAME)
        await store.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run_forever())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())