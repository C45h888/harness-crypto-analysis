"""Governed telemetry emission and deterministic replay orchestration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from market_service.telemetry_kernel.contracts import TelemetryEvent
from market_service.telemetry_kernel.projections import rebuild_projection, reduce_projection
from market_service.telemetry_kernel.store import TelemetryStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmissionResult:
    event_id: str
    durable: bool
    projected: bool
    duplicate: bool = False
    pg_attempts: int = 0
    errors: tuple[str, ...] = ()


def kernel_for_runtime(
    redis_store: Any,
    postgres_store: Any | None = None,
    *,
    retries: int = 3,
) -> "TelemetryKernel | None":
    """Construct an emitter at a real runtime boundary, not in pure tests.

    Redis-shaped test doubles that intentionally omit an operational Redis
    client remain uninstrumented; production callers pass their existing
    runtime stores, so the kernel opens no duplicate connections. The atomic
    publish is a Lua script, so the client must expose ``eval`` — a double
    without it is reported as uninstrumented rather than as sink failure.
    """
    redis_client = getattr(redis_store, "redis", None)
    if redis_client is None or not callable(getattr(redis_client, "eval", None)):
        return None
    try:
        store = TelemetryStore(redis_store, postgres_store)
    except Exception:
        return None
    return TelemetryKernel(store, retries=retries)


class TelemetryKernel:
    """Out-of-band mirror for events already adjudicated by runtime FSMs.

    ``emit`` never changes the caller's governance result. PG is the durable
    replay source when configured; Redis is a bounded operational projection.
    Sink errors are returned and counted rather than raised into the governed
    calculation, analysis, or agentic state transition.
    """

    def __init__(
        self,
        store: TelemetryStore,
        *,
        retries: int = 3,
        retry_delay_s: float = 0.02,
    ) -> None:
        self.store = store
        self.retries = max(1, int(retries))
        self.retry_delay_s = max(0.0, float(retry_delay_s))
        self._emissions = 0
        self._duplicates = 0
        self._persist_failures = 0
        self._projection_failures = 0
        self._durability_unavailable = 0
        self._validation_failures = 0
        self._last_error: str | None = None

    async def emit_state(self, **fields: Any) -> EmissionResult:
        """Build and emit one state event; contract errors never escape to owners."""
        try:
            event = TelemetryEvent.create(**fields)
        except Exception as exc:  # noqa: BLE001 — telemetry cannot gate domain state
            self._validation_failures += 1
            self._last_error = f"validation:{type(exc).__name__}:{exc}"
            return EmissionResult(
                event_id="", durable=False, projected=False,
                errors=(self._last_error,),
            )
        return await self.emit(event)

    async def emit(self, event: TelemetryEvent) -> EmissionResult:
        """Persist and project one validated, idempotent state emission."""
        event.validate()
        self._emissions += 1
        errors: list[str] = []
        pg_attempts = 0
        durable = False
        for attempt in range(1, self.retries + 1):
            pg_attempts = attempt
            try:
                # False means the event already exists; it is still durable.
                persisted = await self.store.persist(event)
                durable = persisted is not None
                if not durable:
                    errors.append("postgres:not_configured")
                    self._durability_unavailable += 1
                break
            except Exception as exc:  # noqa: BLE001 — telemetry never gates domain state
                errors.append(f"postgres:{type(exc).__name__}:{exc}")
                if attempt < self.retries and self.retry_delay_s:
                    await asyncio.sleep(self.retry_delay_s * attempt)
        if not durable and any(error.startswith("postgres:") and error != "postgres:not_configured" for error in errors):
            self._persist_failures += 1
            self._last_error = errors[-1]

        projected = False
        duplicate = False
        previous = None
        try:
            previous = await self.store.read_projection(
                event.plane, event.owner, event.subject_type, event.subject_id,
            )
        except Exception as exc:  # still try the atomic stream/latest writer
            errors.append(f"redis_read:{type(exc).__name__}:{exc}")
        projection = reduce_projection(previous, event)
        try:
            publish_result = await self.store.publish(event, projection)
            projected = True
            duplicate = publish_result == "duplicate"
            if duplicate:
                self._duplicates += 1
        except Exception as exc:  # noqa: BLE001 — telemetry never gates domain state
            errors.append(f"redis_write:{type(exc).__name__}:{exc}")
            self._projection_failures += 1
            self._last_error = errors[-1]
            log.warning("telemetry projection failed for %s: %s", event.event_id, exc)

        result = EmissionResult(
            event_id=event.event_id,
            durable=durable,
            projected=projected,
            duplicate=duplicate,
            pg_attempts=pg_attempts,
            errors=tuple(errors),
        )
        if errors:
            # Degraded delivery is SURFACED, never silently treated as success.
            await self.publish_health(owner=event.owner)
        return result

    async def publish_health(self, *, owner: str = "kernel") -> dict[str, Any]:
        """Persist sink health outside the event path (never recursive)."""
        health = self.health()
        writer = getattr(self.store, "write_health", None)
        if callable(writer):
            try:
                await writer(owner, health)
            except Exception as exc:  # noqa: BLE001 — health cannot gate the owner
                self._last_error = f"health:{type(exc).__name__}:{exc}"
        return health

    async def record_lifecycle(self, state: str, **detail: Any) -> dict[str, Any]:
        """Record kernel start/replay/degraded/recovered as LOCAL health.

        Deliberately not an ``agent_*``/``*_status`` event: a lifecycle
        emission that itself failed would re-enter this path forever.
        """
        payload = {
            **self.health(),
            "lifecycle": str(state),
            "detail": {k: v for k, v in detail.items() if isinstance(
                v, (str, int, float, bool, type(None)))},
        }
        writer = getattr(self.store, "write_health", None)
        if callable(writer):
            try:
                await writer("kernel", payload)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"lifecycle:{type(exc).__name__}:{exc}"
        return payload

    async def rebuild(
        self, plane: str, owner: str, subject_type: str, subject_id: str,
        *, limit: int = 10_000,
    ) -> dict[str, Any] | None:
        """Rebuild one Redis read model from the durable PG event history."""
        events = await self.store.read_events(
            plane, owner, subject_type, subject_id, limit=limit,
        )
        valid = [TelemetryEvent.from_mapping(row) for row in events]
        projection = rebuild_projection(valid)
        if projection is not None:
            await self.store.replace_projection(projection)
        cursor_writer = getattr(self.store, "write_cursor", None)
        if callable(cursor_writer) and projection is not None:
            try:
                await cursor_writer("projection", {
                    "plane": plane, "owner": owner,
                    "subject_type": subject_type, "subject_id": subject_id,
                    "schema_version": projection.get("schema_version"),
                    "cursor_event_id": projection.get("last_event_id"),
                    "cursor_occurred_at_ms": projection.get("last_event_at_ms"),
                    "replayed_events": len(valid),
                })
            except Exception as exc:  # noqa: BLE001 — cursor is advisory
                self._last_error = f"cursor:{type(exc).__name__}:{exc}"
        return projection

    def health(self) -> dict[str, Any]:
        """Local sink health; deliberately not emitted back into this kernel."""
        return {
            "status": (
                "degraded" if (
                    self._persist_failures or self._projection_failures
                    or self._durability_unavailable
                    or self._validation_failures
                ) else "healthy"
            ),
            "emissions": self._emissions,
            "duplicates": self._duplicates,
            "persist_failures": self._persist_failures,
            "projection_failures": self._projection_failures,
            "durability_unavailable": self._durability_unavailable,
            "validation_failures": self._validation_failures,
            "last_error": self._last_error,
        }
