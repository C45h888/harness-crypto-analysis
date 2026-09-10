"""Calculation container — the substrate worker plane as one service.

Runs every enabled substrate worker as a long-lived task (each task is the
data-driven ``SubstrateWorkerCore.run_forever``: blocking XREADGROUP waits,
no timer). A tiny HTTP control plane (``aiohttp``, localhost-bound) lets the
harness / NOOA containers and operators invoke workers and read state over
the SAME deterministic code path the CLI tools use (``substrate_worker.tools``)
— Redis is the shared bus, this process is the computation host.

Endpoints (port ``CALC_CONTROL_PORT``, default 8041):

  GET  /health     exit-0/1 semantics over HTTP: every selected worker's
                   supervisor heartbeat key must exist.
  GET  /status     per-(worker, symbol) {substrate, symbol, fired, status,
                   age_ms, last_error} from supervisor keys + latest
                   projections (same data as ``--substrate-read``).
  POST /invoke     {"substrates": ["tape", ...]} → one deterministic
                   fire-tick per named worker (same path as --invoke).

No timer loops live in this process; wake-condition tuning happens at
real-time test time against the deterministic dispatch order in the core.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any

from aiohttp import web

from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.substrate_worker import tools as substrate_tools

log = logging.getLogger(__name__)

DEFAULT_CONTROL_PORT = 8041


def _settings() -> dict[str, Any]:
    return {
        "redis_url": os.getenv("REDIS_URL") or "redis://localhost:6379/0",
        "prefix": os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        "maxlen": int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    }


async def build_workers(store: RedisRuntimeStore) -> list[Any]:
    from market_service.substrate_worker.runner import _symbols, build_workers

    return build_workers(store, symbols=_symbols())


async def health(store: RedisRuntimeStore, workers: list[Any]) -> bool:
    missing: list[str] = []
    for w in workers:
        try:
            alive = await store.redis.get(w._supervisor_key)
        except Exception:  # noqa: BLE001 — healthcheck must never crash
            alive = None
        if alive is None:
            missing.append(f"{w.SUBSTRATE_NAME}:{w.symbol}")
    return not missing


class CalcHttpApp:
    def __init__(self, store: RedisRuntimeStore, workers: list[Any]) -> None:
        self.store = store
        self.workers = workers
        self.app = web.Application()
        self.app.router.add_get("/health", self._health)
        self.app.router.add_get("/status", self._status)
        self.app.router.add_post("/invoke", self._invoke)

    async def _health(self, _request: web.Request) -> web.Response:
        ok = await health(self.store, self.workers)
        return web.json_response(
            {"status": "ok" if ok else "degraded",
             "workers": [f"{w.SUBSTRATE_NAME}:{w.symbol}" for w in self.workers]},
            status=200 if ok else 503,
        )

    async def _status(self, _request: web.Request) -> web.Response:
        return web.json_response(
            await substrate_tools.read_state(self.store, _request.query.get("symbol", "SOLUSDT")),
        )

    async def _invoke(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        substrates = body.get("substrates")
        result = await substrate_tools.invoke_many(
            self.store, body.get("symbol", "SOLUSDT"), substrates or None)
        return web.json_response(result)


async def run_container() -> int:
    s = _settings()
    store = RedisRuntimeStore(s["redis_url"], s["prefix"], s["maxlen"])
    workers = await build_workers(store)
    if not workers:
        log.error("no substrate workers selected — nothing to run")
        await store.close()
        return 1
    http = CalcHttpApp(store, workers)
    port = int(os.getenv("CALC_CONTROL_PORT") or DEFAULT_CONTROL_PORT)
    runner = web.AppRunner(http.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log.info("calculation control plane on 127.0.0.1:%s (%d workers)", port, len(workers))
    try:
        results = await asyncio.gather(
            *(w.run_forever() for w in workers), return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        return 0 if not failures else 1
    finally:
        await runner.cleanup()
        for w in workers:
            try:
                await w.stop()
            except Exception:
                log.exception("worker %s stop failed", w.SUBSTRATE_NAME)
        await store.close()


async def _once() -> int:
    """One bounded tick per worker (smoke / cold-start bootstrap)."""
    from market_service.substrate_worker.runner import run_once

    return await run_once()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calculation container")
    parser.add_argument("--once", action="store_true", help="one bounded smoke tick")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.once:
        return asyncio.run(_once())
    return asyncio.run(run_container())


if __name__ == "__main__":
    raise SystemExit(main())