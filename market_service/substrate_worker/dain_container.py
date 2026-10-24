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
  POST /invoke     {"symbol": "SOLUSDT", "substrates": ["tape", ...]} → one
                   deterministic fire-tick per named worker, with the durable
                   Postgres ledger threaded through. Bad symbol or unknown
                   substrate → 400, so a caller can tell a bad request from a
                   worker that simply declined to fire (200, ``fired: 0``).
  GET  /governance?symbol=&substrate=  the calculation governance FSM's own
                   decision ledger (latest + denials + count) — the plane's
                   legality audit trail (docs/PLANE_GOVERNANCE_SPEC.md).

**This endpoint is the only sanctioned way for a process outside
``market_service/substrate_worker/`` to request a fire-tick.** Worker identity
is ``(substrate, symbol)`` and never the process, so a foreign process that
builds its own ``SubstrateWorkerCore`` would overwrite this container's
supervisor heartbeat (and with it the fire-dedupe high-water state, which
shares that key) and consume raw-stream entries from its consumer group with
``noack=True`` — unrecoverably. Invoking here instead runs the same
``substrate_tools`` code path inside the workers' own process, so the
inference plane keeps full authority to fire a calculation whenever it judges
one necessary, with no collision.

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
from market_service.telemetry_kernel import kernel_for_runtime
from market_service.substrate_worker import WORKER_REGISTRY
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
    from market_service.substrate_worker.runner import (
        _symbols,
        build_workers,
        governance_from_env,
        pg_store_from_env,
        pg_strict_from_env,
    )

    pg = pg_store_from_env()
    return build_workers(
        store, symbols=_symbols(),
        pg_store=pg, pg_strict=pg_strict_from_env(),
        governor=governance_from_env(store, pg),
        telemetry=kernel_for_runtime(store, pg),
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


class CalcHttpApp:
    def __init__(self, store: RedisRuntimeStore, workers: list[Any], *,
                 pg_store: Any | None = None, pg_strict: bool = True) -> None:
        self.store = store
        self.workers = workers
        self.pg_store = pg_store
        self.pg_strict = pg_strict
        self.telemetry = workers[0].telemetry if workers else None
        self.app = web.Application()
        self.app.router.add_get("/health", self._health)
        self.app.router.add_get("/status", self._status)
        self.app.router.add_get("/telemetry", self._telemetry_status)
        self.app.router.add_get("/governance", self._governance)
        self.app.router.add_post("/invoke", self._invoke)
        self.app.router.add_post("/dispatch-ticket", self._dispatch_ticket)

    async def _governance(self, request: web.Request) -> web.Response:
        """The calculation plane's governance observability surface.

        Reads the FSM's own decision ledger from Redis (latest + recent
        decisions + denials + liveness). The FSM is the sole legality
        authority, so its decisions — allowed AND denied — are the plane's
        audit trail.
        """
        plane = "calculation"
        symbol = request.query.get("symbol", "SOLUSDT").upper()
        substrate = request.query.get("substrate", "").lower()
        latest = await self.store.read("governance.latest", ids={"plane": plane, "symbol": symbol, "name": substrate} if substrate else {"plane": plane, "symbol": symbol})
        history = await self.store.read("governance.stream", ids={"plane": plane, "symbol": symbol}, count=50)
        denials = await self.store.read("governance.denials", ids={"plane": plane, "symbol": symbol}, count=20)
        alive = await self.store.governance_alive(plane, symbol)
        return web.json_response({
            "plane": plane,
            "symbol": symbol,
            "alive": alive,
            "available": latest is not None,
            "latest": latest,
            "denials": denials,
            "decisions": len(history),
        })

    async def _health(self, request: web.Request) -> web.Response:
        ok = await health(self.store, self.workers)
        symbol = request.query.get("symbol", "SOLUSDT").upper()
        return web.json_response(
            {"status": "ok" if ok else "degraded",
             "workers": [f"{w.SUBSTRATE_NAME}:{w.symbol}" for w in self.workers],
             "governance": {
                 "plane": "calculation",
                 "alive": await self.store.governance_alive("calculation", symbol),
             },
             "telemetry": self.telemetry.health() if self.telemetry else {
                 "status": "unavailable"}},
            status=200 if ok else 503,
        )

    async def _status(self, _request: web.Request) -> web.Response:
        from market_service.substrate_worker.healthcheck import check_starvation
        return web.json_response({
            **await substrate_tools.read_state(
                self.store, _request.query.get("symbol", "SOLUSDT")),
            "starved": await check_starvation(self.store, self.workers),
            "telemetry": self.telemetry.health() if self.telemetry else {
                "status": "unavailable"},
        })

    async def _telemetry_status(self, _request: web.Request) -> web.Response:
        return web.json_response(
            self.telemetry.health() if self.telemetry else {"status": "unavailable"})

    async def _dispatch_ticket(self, request: web.Request) -> web.Response:
        """Dry-run the dispatch guard: due-check + batch plan, no compute."""
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            return web.json_response({"error": "symbol is required"}, status=400)
        substrates = body.get("substrates")
        if substrates is not None:
            if not isinstance(substrates, (list, tuple)):
                return web.json_response({"error": "substrates must be a list"}, status=400)
            substrates = [str(n).strip().lower() for n in substrates if str(n).strip()]
            unknown = [n for n in substrates if n not in WORKER_REGISTRY]
            if unknown:
                return web.json_response(
                    {"error": f"unknown substrate worker(s): {sorted(unknown)}",
                     "registered": sorted(WORKER_REGISTRY)}, status=400)
        try:
            from market_service.governance import build_calculation_governor
            governor = build_calculation_governor(self.store)
            ticket = await governor.authorize_dispatch(
                symbol=symbol, requested=substrates or None, dry_run=True)
        except Exception as exc:  # noqa: BLE001 — dry-run never 500s on guard build
            log.warning("dispatch-ticket guard unavailable (%s); failing open", exc)
            return web.json_response({
                "symbol": symbol, "plane": "calculation",
                "allowed": substrates or sorted(WORKER_REGISTRY),
                "rule": "dispatch-observation-unavailable",
            })
        return web.json_response(ticket.to_dict())

    async def _invoke(self, request: web.Request) -> web.Response:
        """Request one bounded fire-tick per named worker.

        The ONLY sanctioned way for a process outside ``substrate_worker/`` to
        fire a worker: it runs here, in the workers' own process, against
        their own consumer groups and supervisor keys. A caller that builds
        its own ``SubstrateWorkerCore`` instead would clobber this container's
        heartbeat, wipe its fire-dedupe state, and steal its stream entries.

        Bad requests are 4xx so the caller can tell "I asked wrongly" from
        "the worker declined to fire" (a 200 with ``fired: 0``).
        """
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)

        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            return web.json_response(
                {"error": "symbol is required"}, status=400)

        substrates = body.get("substrates")
        if substrates is not None:
            if not isinstance(substrates, (list, tuple)):
                return web.json_response(
                    {"error": "substrates must be a list"}, status=400)
            substrates = [str(name).strip().lower() for name in substrates if str(name).strip()]
            unknown = [name for name in substrates if name not in WORKER_REGISTRY]
            if unknown:
                return web.json_response(
                    {"error": f"unknown substrate worker(s): {sorted(unknown)}",
                     "registered": sorted(WORKER_REGISTRY)},
                    status=400)

        result = await self._invoke_guarded(symbol, substrates)
        return web.json_response(result)

    async def _invoke_guarded(
        self, symbol: str, substrates: list[str] | None,
    ) -> dict[str, Any]:
        """Guarded invoke: FSM ticket first, compute only due workers."""
        try:
            from market_service.governance import build_calculation_governor
            governor = build_calculation_governor(self.store)
            ticket = await governor.authorize_dispatch(
                symbol=symbol, requested=substrates or None)
        except Exception as exc:  # noqa: BLE001 — guard never bricks invokes
            log.warning("dispatch guard unavailable (%s); failing open", exc)
            result = await substrate_tools.invoke_many(
                self.store, symbol, substrates or None,
                pg_store=self.pg_store, pg_strict=self.pg_strict,
                telemetry=self.telemetry)
            result["dispatch_ticket"] = {
                "plane": "calculation", "symbol": symbol,
                "rule": "dispatch-observation-unavailable",
            }
            return result
        if ticket.deferred or not ticket.allowed:
            return {
                "symbol": symbol, "invoked": 0, "fired": 0, "reports": [],
                "dispatch_ticket": ticket.to_dict(),
            }
        try:
            result = await substrate_tools.invoke_many(
                self.store, symbol, list(ticket.allowed),
                pg_store=self.pg_store, pg_strict=self.pg_strict,
                telemetry=self.telemetry)
        finally:
            try:
                await governor.release_dispatch_lease(symbol=symbol)
            except Exception:
                pass
        result["dispatch_ticket"] = ticket.to_dict()
        return result


async def run_container() -> int:
    from market_service.substrate_worker.runner import (
        pg_store_from_env,
        pg_strict_from_env,
    )

    s = _settings()
    store = RedisRuntimeStore(s["redis_url"], s["prefix"], s["maxlen"])
    workers = await build_workers(store)
    if not workers:
        log.error("no substrate workers selected — nothing to run")
        await store.close()
        return 1
    # The durable ledger rides the control plane too: an HTTP-triggered fire
    # writes Postgres exactly like the worker loop's own fires.
    pg_store = pg_store_from_env()
    http = CalcHttpApp(store, workers,
                       pg_store=pg_store, pg_strict=pg_strict_from_env())
    port = int(os.getenv("CALC_CONTROL_PORT") or DEFAULT_CONTROL_PORT)
    runner = web.AppRunner(http.app)
    await runner.setup()
    # Bound to every interface: the compose network is the boundary, and the
    # port is published to host loopback only. Container-loopback would make
    # the plane unreachable from the NOOA container that depends on it.
    site = web.TCPSite(runner, "0.0.0.0", port)  # noqa: S104 — see above
    await site.start()
    _prov = {"GIT_SHA": "unknown", "GIT_DIRTY": "unknown", "BUILD_UTC": "unknown"}
    try:
        with open("/app/BUILD_PROVENANCE", encoding="utf-8") as _fh:
            for _raw in _fh.read().splitlines():
                _k, _, _v = _raw.partition("=")
                if _k.strip() in _prov:
                    _prov[_k.strip()] = _v.strip()
    except OSError:
        pass
    log.info("provenance plane=calculation sha=%s dirty=%s built=%s",
             _prov["GIT_SHA"], _prov["GIT_DIRTY"], _prov["BUILD_UTC"])
    log.info("calculation control plane on 0.0.0.0:%s (%d workers)", port, len(workers))
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
        if pg_store is not None:
            try:
                await pg_store.close()
            except Exception:
                log.exception("pg store close failed")


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