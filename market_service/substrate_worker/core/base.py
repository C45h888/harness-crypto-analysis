"""Base substrate contract — what a worker IS, without transport.

SEMANTIC JURISDICTION: this file owns the SUBSTRATE contract that every
worker file implements:

* class attributes (``SUBSTRATE_NAME``, ``INPUT_STREAMS``, ``CADENCE``,
  ``DEPENDENCIES``, ``DERIVATIVE_INPUTS``, ``DERIVATIVE_FRESH_MS``);
* the ``__init__`` that resolves per-worker cadence/profile defaults and
  builds the Redis keyspace/group/consumer names;
* the two pure hooks the worker file overrides (``probe``/``compute``);
* ``_store_fn`` — the probe-purity test seam helper.

It does NOT own transport reads (``reader``), fire decisions (``fire``), or
the supervisor heartbeat (``supervisor``).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.substrate_worker.contracts import (
    CadenceProfile,
    SubstrateStatePayload,
    TriggerDecision,
)

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_S = 30
DEFAULT_STALENESS_S = 120
READ_BLOCK_MS = 1_000
SUPERVISOR_MS = 5_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _store_fn(store: Any, name: str) -> Any | None:
    """Fetch an optional store method; None when the seam lacks it."""
    try:
        return getattr(store, name, None)
    except Exception:
        return None


class SubstrateBase:
    """Substrate contract: class attrs, resolution, hooks. No transport."""

    SUBSTRATE_NAME: str = ""
    INPUT_STREAMS: tuple[str, ...] = ("raw",)
    CADENCE: CadenceProfile | None = None
    DEPENDENCIES: tuple[str, ...] = ()
    DERIVATIVE_INPUTS: tuple[str, ...] = ()
    DERIVATIVE_FRESH_MS: int = 300_000

    def __init__(
        self,
        store: RedisRuntimeStore,
        *,
        symbol: str,
        window_minutes: int = 15,
        depth: int | None = None,
        cooldown_s: int | None = None,
        staleness_s: int | None = None,
        ws_venue: str | None = None,
        read_block_ms: int = READ_BLOCK_MS,
        supervisor_ms: int = SUPERVISOR_MS,
        dispatcher: Any | None = None,
        pg_store: Any | None = None,
        pg_strict: bool = True,
    ) -> None:
        if not self.SUBSTRATE_NAME:
            raise ValueError(
                f"{type(self).__name__} must define a non-empty SUBSTRATE_NAME"
            )
        if "raw" not in self.INPUT_STREAMS:
            raise ValueError(
                f"{type(self).__name__}: Phase 1 requires the raw input stream"
            )
        self.store = store
        self.symbol = symbol.upper()
        self.window_minutes = window_minutes
        self.depth = depth
        profile = type(self).CADENCE
        self.cooldown_s = (
            cooldown_s if cooldown_s is not None
            else (profile.cooldown_s if profile is not None else DEFAULT_COOLDOWN_S)
        )
        self.staleness_s = (
            staleness_s if staleness_s is not None
            else (profile.staleness_s if profile is not None else DEFAULT_STALENESS_S)
        )
        self.ws_venue = (
            ws_venue or os.getenv("MICROSTRUCTURE_VENUE") or "futures"
        ).lower()
        self.ws_input = bool(profile.ws_input) if profile is not None else False
        self.read_block_ms = read_block_ms
        self.supervisor_ms = supervisor_ms
        self.dispatcher = dispatcher
        self.pg_store = pg_store
        self.pg_strict = pg_strict

        self._stream = store.raw_stream(self.symbol)
        self._group = f"substrate:{self.SUBSTRATE_NAME}:{self.symbol}"
        self._consumer = f"substrate-{self.SUBSTRATE_NAME}-{os.getpid()}-{int(time.time())}"
        self._supervisor_key = store.substrate_supervisor_key(
            self.SUBSTRATE_NAME, self.symbol,
        )
        # WS input surface (Phase 2): a second consumer group per
        # (worker, symbol) on the shared microstructure event stream, plus
        # the status-transition stream tail (wake-worker "$" pattern).
        self._ws_group = f"substrate:{self.SUBSTRATE_NAME}:{self.symbol}:ws"
        self._ws_consumer = f"{self._consumer}-ws"
        self._ws_stream: str | None = None
        self._status_stream: str | None = None
        if self.ws_input and "microstructure" in self.INPUT_STREAMS:
            event_fn = _store_fn(store, "microstructure_event_stream")
            status_fn = _store_fn(store, "microstructure_status_stream")
            if callable(event_fn):
                self._ws_stream = event_fn(self.ws_venue, self.symbol)
            if callable(status_fn):
                self._status_stream = status_fn(self.ws_venue, self.symbol)

        self._redis = store.redis
        self._dedupe_lua_sha: str | None = None
        self._last_fire_ms: int | None = None
        self._last_error: str | None = None
        self._last_dormant_reason: str | None = None
        self._ws_err_log_ms: int = 0
        self._prev_newest_ms: int | None = None
        self._fired = 0
        self._ticks = 0
        self._liveness = False
        self._running = False
        self._tasks: set[Any] = set()

    # ---- hooks (worker file implements) ----

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        """L2 significance probe — deterministic; substrate thresholds only."""
        raise NotImplementedError

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        """Full substrate calculation over the evidence window (pure)."""
        raise NotImplementedError