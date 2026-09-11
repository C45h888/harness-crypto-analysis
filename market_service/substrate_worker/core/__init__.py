"""Substrate core package — the shared transport of every substrate worker.

Decomposition pass (move-don't-rewrite, following ``nooa_harness/engine``):
the monolith ``core.py`` is now a package of single-purpose authorities.
Every public + test-pinned name continues to be importable from
``market_service.substrate_worker.core`` — the composed ``SubstrateWorkerCore``
below, plus the constants / helpers that tests import directly. Callers do
not need to change: ``from market_service.substrate_worker.core import
SubstrateWorkerCore`` still works exactly as before.

Semantic authorities (one file, one jurisdiction — no overlap):

  - ``base``       — the substrate contract: class attrs, ``__init__``
                     resolution, the ``probe``/``compute`` hooks. No transport.
  - ``reader``     — read routing: consumer groups (raw + WS), blocking
                     XREADGROUP with NOGROUP-heal, status-transition
                     recovery, window rollover, high-water mark.
  - ``fire``       — the fire decision cascade (cold start / recovery /
                     staleness / rollover / arrival → probe → cooldown),
                     dedupe Lua, compute+persist cycle (derivative &
                     dependency attach, freshness, status, PG-first).
  - ``supervisor`` — lifecycle: start / run_forever / stop / tick heartbeat,
                     liveness + fired_count observability.

Beautiful: the old ``core.py`` held 959 lines of ALL of these blended
together; ``SubstrateWorkerCore`` below is 30 lines of composition.

Boundary rules preserved from the monolith:
- a worker file imports exactly ONE calculation substrate;
- workers never touch Binance or the harness layer;
- the core owns transport + fire orchestration, never the market semantics.
"""

from __future__ import annotations

# Keep the module-level import surface the old ``core`` module had — tests
# patch ``market_service.substrate_worker.core.build_raw_window``.
from market_service.runtime.raw_window import build_raw_window  # noqa: F401

from market_service.substrate_worker.core.base import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_STALENESS_S,
    READ_BLOCK_MS,
    SUPERVISOR_MS,
    SubstrateBase,
)
from market_service.substrate_worker.contracts import SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core.fire import FireMixin
from market_service.substrate_worker.core.reader import (
    ReaderMixin,
    _DEDUPE_LUA,
    _EVT_READ_LIMIT,
    _is_nogroup,
    is_recoverable_redis_exc,
)
from market_service.substrate_worker.core.supervisor import SupervisorMixin


class SubstrateWorkerCore(ReaderMixin, FireMixin, SupervisorMixin, SubstrateBase):
    """Composed worker — transport + fire + lifecycle, no market semantics.

    ``probe`` / ``compute`` (and the class-hook contract) are inherited from
    :class:`SubstrateBase`; the worker file overrides exactly those two.
    """

    pass


__all__ = [
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_STALENESS_S",
    "READ_BLOCK_MS",
    "SUPERVISOR_MS",
    "SUBSTRATE_STATE_SCHEMA_VERSION",
    "SubstrateBase",
    "SubstrateWorkerCore",
    "_DEDUPE_LUA",
    "_EVT_READ_LIMIT",
    "_is_nogroup",
    "is_recoverable_redis_exc",
]