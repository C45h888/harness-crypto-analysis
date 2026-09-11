"""Supervisor authority — lifecycle + heartbeat of a substrate worker.

SEMANTIC JURISDICTION: this file owns the RUNNING of a worker, not the
meaning:

* ``start`` — ensure consumer groups + register scripts (setup once);
* ``run_forever`` — the data-driven async supervisor loop (blocking reads
  are the only cadence; no timer task); recoverable Redis errors back off;
* ``stop`` — cancel in-flight fire tasks and exit cleanly;
* ``_tick`` — the supervisor heartbeat (TTL-bounded liveness key,
  observability only, never control);
* ``liveness`` / ``fired_count`` — observability accessors.

It does NOT own: read routing (``reader.py``), fire decisions (``fire.py``),
or the worker's market semantics (the subclass file).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from market_service.substrate_worker.core.base import SubstrateBase
from market_service.substrate_worker.core.reader import is_recoverable_redis_exc

log = logging.getLogger(__name__)

_READ_BLOCK_MS = 1_000          # XREADGROUP blocking wait (ms)
_SUPERVISOR_MS = 5_000          # supervisor heartbeat TTL (ms)


def _now_ms() -> int:
    return int(time.time() * 1000)


class SupervisorMixin(SubstrateBase):
    """Lifecycle + heartbeat authority (no market semantics)."""

    async def start(self) -> None:
        """One-time setup: create groups + register Lua scripts."""
        await self._ensure_group()
        await self._register_scripts()

    async def run_forever(self) -> int:
        """Async supervisor loop — data-driven, never timer-driven.

        The blocking XREADGROUP reads are the only cadence. On every
        completed read cycle (fired or quiet) the supervisor heartbeat is
        refreshed; ``now_ms`` is read only as an input to age/boundary
        predicates, never to drive work.
        """
        await self.start()
        self._running = True
        while self._running:
            try:
                rows = await self._read_once()
                ws_rows, recovery = await self._read_ws_once()
                now = _now_ms()
                await self._handle_rows(rows, now, ws_rows=ws_rows, recovery=recovery)
                await self._tick(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_recoverable_redis_exc(exc):
                    log.warning("substrate %s loop backed off: %s", self.SUBSTRATE_NAME, exc)
                    self._last_error = f"loop: {exc}"
                    await asyncio.sleep(min(5.0, max(0.2, self.read_block_ms / 2_000)))
                else:
                    log.exception("substrate %s loop iteration failed", self.SUBSTRATE_NAME)
                    self._last_error = f"loop: {exc}"
                    await asyncio.sleep(1.0)
        return self._fired

    async def _tick(self, now_ms: int) -> None:
        """Supervisor heartbeat — proves liveness in the deployment."""
        try:
            payload = {
                "state": "running",
                "substrate": self.SUBSTRATE_NAME,
                "symbol": self.symbol,
                "pid": os.getpid(),
                "last_fire_ms": self._last_fire_ms,
                "fired": self._fired,
                "last_error": self._last_error,
                "dormant_reason": self._last_dormant_reason,
                "ws_input": self._ws_stream is not None,
                "last_tick_ms": now_ms,
            }
            await self._redis.setex(
                self._supervisor_key, self.supervisor_ms // 1000 + 2,
                json.dumps(payload, default=str, separators=(",", ":")),
            )
            self._ticks += 1
            self._liveness = True
        except Exception as exc:
            if is_recoverable_redis_exc(exc):
                self._liveness = False
                log.warning("substrate %s supervisor tick failed: %s",
                            self.SUBSTRATE_NAME, exc)
            else:
                raise

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    @property
    def liveness(self) -> bool:
        return self._liveness

    @property
    def fired_count(self) -> int:
        return self._fired