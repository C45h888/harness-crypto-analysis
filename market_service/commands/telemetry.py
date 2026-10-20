"""Operator read surface for the governed telemetry kernel.

Read-only, bounded, and deliberately NOT a tool: the agent plane gets no
arbitrary telemetry-store access, because telemetry is an observation surface
whose readers must not be able to reshape or re-derive canonical state.

Two verbs:

``status``   — sink health per owner, ledger availability, and the durable
               cursor of the projection consumer. A surface that has never
               reported is ``null`` with ``available: false`` — the read path
               never invents a healthy default.
``read``     — one subject's accepted event history (PostgreSQL replay source,
               Redis stream fallback) plus its materialized latest projection,
               and an explicit ``--rebuild`` that restores the projection from
               the durable ledger.

Every command is plane/owner/subject scoped: there is no unbounded scan.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from market_service.config import Settings
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.telemetry_kernel import (
    TELEMETRY_EVENT_SCHEMA_VERSION,
    TelemetryKernel,
    TelemetryStore,
)
from market_service.telemetry_kernel.contracts import (
    ANALYSIS_OWNERS,
    CALCULATION_OWNERS,
)

#: Owners whose health the status verb reports. Kernel health is local and
#: non-recursive; the rest are the emitting planes.
HEALTH_OWNERS: tuple[str, ...] = (
    "kernel", "poller", "capture", "cycle_controller",
)
#: Every owner the admission matrix knows — the bounded read scope.
KNOWN_OWNERS: tuple[str, ...] = (
    "poller", "capture", "cycle_controller",
    *CALCULATION_OWNERS, *ANALYSIS_OWNERS,
)
MAX_EVENTS = 500


class _Pool:
    """Open the runtime stores for one bounded read, and close them again."""

    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.redis = RedisRuntimeStore(
            self.settings.redis_url,
            self.settings.redis_key_prefix,
            self.settings.redis_stream_maxlen,
        )
        self.postgres: PostgresRuntimeStore | None = None
        if self.settings.database_url:
            self.postgres = PostgresRuntimeStore(self.settings.database_url)

    async def __aenter__(self) -> tuple[TelemetryStore, TelemetryKernel]:
        if self.postgres is not None:
            await self.postgres.connect()
        store = TelemetryStore(self.redis, self.postgres)
        return store, TelemetryKernel(store, retries=1)

    async def __aexit__(self, *exc_info: object) -> None:
        if self.postgres is not None:
            await self.postgres.close()
        await self.redis.close()


async def telemetry_status() -> dict[str, Any]:
    """Sink health + ledger availability. Absent surfaces report as absent."""
    async with _Pool() as (store, _kernel):
        health: dict[str, Any] = {}
        for owner in HEALTH_OWNERS:
            try:
                health[owner] = await store.read_health(owner)
            except Exception as exc:  # noqa: BLE001 — a read failure is data
                health[owner] = {"available": False,
                                 "error": type(exc).__name__}
        try:
            cursor = await store.read_cursor("projection")
        except Exception:  # noqa: BLE001
            cursor = None
        return {
            "available": any(value is not None for value in health.values()),
            "schema_version": TELEMETRY_EVENT_SCHEMA_VERSION,
            "namespace": store.prefix,
            "ledger": "postgres" if store.postgres_store is not None else "absent",
            "health": health,
            "projection_cursor": cursor,
            "notes": [
                "health is written by the emitting process; null means that"
                " owner has never reported on this Redis",
                "the PostgreSQL ledger is the durable replay source; Redis"
                " holds bounded streams and latest projections",
            ],
        }


async def telemetry_read(
    *,
    plane: str,
    owner: str,
    subject_type: str = "symbol",
    subject_id: str,
    limit: int = 50,
    rebuild: bool = False,
) -> dict[str, Any]:
    """One subject's accepted event history + its materialized projection."""
    bounded_limit = max(1, min(int(limit), MAX_EVENTS))
    async with _Pool() as (store, kernel):
        events: list[dict[str, Any]] = []
        source = "postgres" if store.postgres_store is not None else "redis"
        try:
            events = await store.read_events(
                plane, owner, subject_type, subject_id, limit=bounded_limit,
            )
        except Exception as exc:  # noqa: BLE001 — fall back, and say so
            events = []
            source = f"{source}_error:{type(exc).__name__}"
        projection = None
        if rebuild:
            projection = await kernel.rebuild(
                plane, owner, subject_type, subject_id, limit=bounded_limit,
            )
        if projection is None:
            try:
                projection = await store.read_projection(
                    plane, owner, subject_type, subject_id,
                )
            except Exception:  # noqa: BLE001
                projection = None
        return {
            "plane": plane,
            "owner": owner,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "source": source,
            "count": len(events),
            "events": events[-bounded_limit:],
            "projection": projection,
            "available": projection is not None or bool(events),
        }


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def status_main() -> int:
    payload = asyncio.run(telemetry_status())
    _print(payload)
    return 0 if payload.get("available") else 1


def read_main(
    *,
    plane: str,
    owner: str,
    subject_id: str,
    subject_type: str = "symbol",
    limit: int = 50,
    rebuild: bool = False,
) -> int:
    payload = asyncio.run(telemetry_read(
        plane=plane, owner=owner, subject_type=subject_type,
        subject_id=subject_id, limit=limit, rebuild=rebuild,
    ))
    _print(payload)
    return 0 if payload.get("available") else 1


if __name__ == "__main__":
    raise SystemExit(status_main())
