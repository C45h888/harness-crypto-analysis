"""Analysis container — the analysis worker plane as ONE service.

Mirror of the calculation container (``substrate_worker.dain_container``):
runs every enabled analysis worker as a long-lived task (each task is the
data-driven ``SubstrateWorkerCore.run_forever`` — blocking XREADGROUP waits,
no timer), with a tiny HTTP control plane on ``ANALYSIS_CONTROL_PORT``
(default 8042): GET /health, GET /status, POST /invoke — the ONLY sanctioned
way for a foreign process (the NOOA container) to request a fire-tick.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any

from aiohttp import web

from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

DEFAULT_CONTROL_PORT = 8042


def _settings() -> dict[str, Any]:
    return {
        "redis_url": os.getenv("REDIS_URL") or "redis://localhost:6379/0",
        "prefix": os.getenv("REDIS_KEY_PREFIX") or "marketflow",
        "maxlen": int(os.getenv("REDIS_STREAM_MAXLEN") or 10_000),
    }


async def build_workers(store: RedisRuntimeStore) -> list[Any]:
    from market_service.analysis_worker.runner import _symbols, build_workers
    from market_service.substrate_worker.runner import (
        pg_store_from_env,
        pg_strict_from_env,
    )

    return build_workers(
        store, symbols=_symbols(),
        pg_store=pg_store_from_env(), pg_strict=pg_strict_from_env(),
    )


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


class AnalysisHttpApp:
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
        from market_service.analysis_worker.tools import read_state

        return web.json_response(await read_state(
            self.store, _request.query.get("symbol", "SOLUSDT")))

    async def _invoke(self, request: web.Request) -> web.Response:
        """Request one bounded fire-tick per named analysis worker.

        The ONLY sanctioned way for a process outside ``analysis_worker/``
        to fire a worker — same invoke-authority rule as the calc container.
        """
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            return web.json_response({"error": "symbol is required"}, status=400)
        analyses = body.get("analyses")
        if analyses is not None:
            if not isinstance(analyses, (list, tuple)):
                return web.json_response({"error": "analyses must be a list"}, status=400)
            analyses = [str(n).strip().lower() for n in analyses if str(n).strip()]
            unknown = [n for n in analyses if n not in ANALYSIS_WORKER_REGISTRY]
            if unknown:
                return web.json_response(
                    {"error": f"unknown analysis worker(s): {sorted(unknown)}",
                     "registered": sorted(ANALYSIS_WORKER_REGISTRY)},
                    status=400)
        from market_service.analysis_worker.tools import invoke_many

        result = await invoke_many(self.store, symbol, analyses or None)
        return web.json_response(result)


async def run_container() -> int:
    s = _settings()
    store = RedisRuntimeStore(s["redis_url"], s["prefix"], s["maxlen"])
    workers = await build_workers(store)
    if not workers:
        log.error("no analysis workers selected — nothing to run")
        await store.close()
        return 1
    http = AnalysisHttpApp(store, workers)
    port = int(os.getenv("ANALYSIS_CONTROL_PORT") or DEFAULT_CONTROL_PORT)
    runner = web.AppRunner(http.app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)  # noqa: S104 — compose boundary
    await site.start()
    log.info("analysis control plane on 0.0.0.0:%s (%d workers)", port, len(workers))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analysis container")
    parser.add_argument("--once", action="store_true", help="reserved (not used)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run_container())


if __name__ == "__main__":
    raise SystemExit(main())