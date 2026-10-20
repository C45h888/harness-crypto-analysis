"""Pure reducers for the runtime telemetry read model."""

from __future__ import annotations

from typing import Any

from market_service.telemetry_kernel.contracts import TelemetryEvent


def event_order(event: TelemetryEvent) -> tuple[int, int, str]:
    """A deterministic per-subject order; wall time is never an authority alone."""
    return (event.occurred_at_ms, event.source_sequence, event.event_id)


def reduce_projection(
    previous: dict[str, Any] | None, event: TelemetryEvent,
) -> dict[str, Any]:
    """Apply one accepted event without allowing late arrivals to rewind state.

    The owner sequence and occurrence time define a stable ordering. The event
    stream retains every event; this materialized view retains only the newest
    accepted state for its subject.
    """
    event.validate()
    current = dict(previous or {})
    current_order = (
        int(current.get("last_event_at_ms", -1)),
        int(current.get("source_sequence", -1)),
        str(current.get("last_event_id", "")),
    )
    if previous is not None and event_order(event) <= current_order:
        return current
    return {
        "schema_version": event.schema_version,
        "plane": event.plane,
        "owner": event.owner,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "event_count": int(current.get("event_count", 0)) + 1,
        "last_event_id": event.event_id,
        "last_event_at_ms": event.occurred_at_ms,
        "source_sequence": event.source_sequence,
        "event_type": event.event_type,
        "source_ref": event.source_ref,
        "correlation_id": event.correlation_id,
        "parent_event_id": event.parent_event_id,
        "state": dict(event.state),
    }


def rebuild_projection(events: list[TelemetryEvent]) -> dict[str, Any] | None:
    """Replay a durable event sequence into one deterministic latest view."""
    unique = {event.event_id: event for event in events}
    ordered = sorted(unique.values(), key=event_order)
    if not ordered:
        return None
    projection: dict[str, Any] | None = None
    for event in ordered:
        projection = reduce_projection(projection, event)
    assert projection is not None
    projection["event_count"] = len(ordered)
    return projection
