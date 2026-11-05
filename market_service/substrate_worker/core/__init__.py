"""Substrate core package — the shared transport of every substrate worker.

Fire.py is DELETED (absorbed, no shim): worker-owned fire logic lives in
``core/base.py`` (SubstrateBase.decide/_handle_rows/_fire/_fire_guarded and
friends — every worker inherits and may override decide pieces to own its
scheduling policy). Coordination primitives live in governance
(``governance/dedupe.py`` fail-closed dedupe + ``governance/gates.py``
authorize_fire/publish/pg_write). Shared performance helpers live in
``substrate_worker/evidence.py``; pure payload helpers in ``contracts.py``.

Semantic authorities (one file, one jurisdiction — no overlap):

  - ``base``       — the worker contract + worker-owned fire cycle:
                     class attrs, ``__init__`` resolution, ``probe``/
                     ``compute`` hooks, decide cascade, single-flight
                     dispatch with coalesce, compute+persist, horizon
                     folds, derivative/dependency attach, governance
                     gates, fail-closed dedupe call.
  - ``reader``     — read routing: consumer groups (raw + WS), blocking
                     XREADGROUP with NOGROUP-heal, status-transition
                     recovery, window rollover, high-water mark.
  - ``supervisor`` — lifecycle: start / run_forever / stop / tick heartbeat,
                     liveness + fired_count observability.

Boundary rules:
- a worker file imports exactly ONE calculation substrate;
- workers never touch Binance or the harness layer;
- workers own their scheduling policy; governance owns coordination legality.
"""

from __future__ import annotations

# Keep the module-level import surface the old ``core`` module had — tests
# patch ``market_service.substrate_worker.core.build_raw_window``.
from market_service.runtime.raw_window import build_raw_window  # noqa: F401

from market_service.governance.dedupe import DEDUPE_LUA as _DEDUPE_LUA
from market_service.substrate_worker.core.base import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_STALENESS_S,
    READ_BLOCK_MS,
    SUPERVISOR_MS,
    SubstrateBase,
)
from market_service.substrate_worker.contracts import SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core.reader import (
    ReaderMixin,
    _EVT_READ_LIMIT,
    _is_nogroup,
    is_recoverable_redis_exc,
)
from market_service.substrate_worker.core.supervisor import SupervisorMixin


class SubstrateWorkerCore(ReaderMixin, SupervisorMixin, SubstrateBase):
    """Composed worker — worker-owned fire + transport reads + lifecycle.

    ``probe`` / ``compute`` (and the class-hook contract) are inherited from
    :class:`SubstrateBase`; the worker file overrides exactly those two plus
    optionally its decide pieces. ``fire.py`` no longer exists.
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
