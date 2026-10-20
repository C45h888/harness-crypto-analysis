"""Typed, state-only emissions owned by the runtime telemetry kernel.

Telemetry events are observational receipts, not commands and not market
truth. Producers must emit a state snapshot/transition after the owning FSM has
made its decision. Raw evidence, narration, prompts, and credentials are not a
valid telemetry payload.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from market_service.runtime.contracts import SCHEMA_VERSION_REGISTRY

TELEMETRY_EVENT_SCHEMA_VERSION = SCHEMA_VERSION_REGISTRY["telemetry_event"]
MAX_STATE_BYTES = 128_000

CALCULATION_OWNERS: tuple[str, ...] = (
    "anchors", "delta", "density", "ladders", "large_print", "migration",
    "oi", "signals", "tape", "technicals", "tiers", "volume_profile",
)
ANALYSIS_OWNERS: tuple[str, ...] = (
    "demand", "liquidation", "oi_analysis", "regime", "stage",
    "wall_migration",
)

#: event_type -> the planes that may carry it.
_EVENT_PLANES: dict[str, frozenset[str]] = {
    "poller_status": frozenset({"ingestion"}),
    "capture_status": frozenset({"capture"}),
    "state_adjudicated": frozenset({"calculation", "analysis"}),
    "worker_state": frozenset({"calculation", "analysis"}),
    "governance_decision": frozenset({"calculation", "analysis"}),
    "agent_loop_transition": frozenset({"agentic"}),
    "agent_cadence": frozenset({"agentic"}),
    "agent_cycle": frozenset({"agentic"}),
    # Signal B (cadence-aware narration supervision): the streaming
    # transport's throttled heartbeat — counts and shapes only, never
    # prompts or outputs (see _FORBIDDEN_STATE_KEYS).
    "agent_narration_cadence": frozenset({"agentic"}),
}

#: owner -> the event types that owner may declare (the admission matrix).
#: An undeclared owner, or an event kind it does not own, is refused at
#: admission: the kernel validates provenance, it does not infer it.
_EVENT_OWNERS: dict[str, frozenset[str]] = {
    "poller": frozenset({"poller_status"}),
    "capture": frozenset({"capture_status"}),
    "cycle_controller": frozenset({
        "agent_loop_transition", "agent_cadence", "agent_cycle",
    }),
    # The streaming narration transport owns exactly one event: its own
    # cadence heartbeat. It never emits loop transitions or cycle records.
    "narration_transport": frozenset({"agent_narration_cadence"}),
    **{
        owner: frozenset({
            "state_adjudicated", "worker_state", "governance_decision",
        })
        for owner in (*CALCULATION_OWNERS, *ANALYSIS_OWNERS)
    },
}

#: owner -> the single plane its emissions are bound to.
_OWNER_PLANES: dict[str, str] = {
    "poller": "ingestion",
    "capture": "capture",
    "cycle_controller": "agentic",
    "narration_transport": "agentic",
    **{owner: "calculation" for owner in CALCULATION_OWNERS},
    **{owner: "analysis" for owner in ANALYSIS_OWNERS},
}
_FORBIDDEN_STATE_KEYS = frozenset({
    "api_key", "authorization", "credentials", "interpretation",
    "llm_output", "narrative", "password", "prompt", "raw_evidence",
    "secret", "system_prompt", "token", "tool_args", "user_prompt",
})


def _validate_state(value: Any, path: str = "state") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            normalized = key.strip().lower()
            if normalized in _FORBIDDEN_STATE_KEYS:
                raise ValueError(f"{path}.{key} is not permitted in pure-state telemetry")
            _validate_state(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_state(item, f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{path} contains unsupported value {type(value).__name__}")


def _event_identity(fields: Mapping[str, Any]) -> str:
    identity = {
        "schema_version": fields["schema_version"],
        "plane": fields["plane"],
        "owner": fields["owner"],
        "subject_type": fields["subject_type"],
        "subject_id": fields["subject_id"],
        "event_type": fields["event_type"],
        "source_sequence": fields["source_sequence"],
        "occurred_at_ms": fields["occurred_at_ms"],
        "source_ref": fields.get("source_ref"),
        "correlation_id": fields.get("correlation_id"),
        "parent_event_id": fields.get("parent_event_id"),
        "state": fields["state"],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TelemetryEvent:
    """One immutable, idempotent runtime-state emission."""

    event_id: str
    schema_version: int
    plane: str
    owner: str
    subject_type: str
    subject_id: str
    event_type: str
    source_sequence: int
    occurred_at_ms: int
    state: dict[str, Any]
    source_ref: str | None = None
    correlation_id: str | None = None
    parent_event_id: str | None = None

    @property
    def idempotency_key(self) -> str:
        """The stable dedupe identity a retried emission collapses onto."""
        return self.event_id

    @classmethod
    def create(
        cls,
        *,
        plane: str,
        owner: str,
        subject_type: str,
        subject_id: str,
        event_type: str,
        source_sequence: int,
        occurred_at_ms: int,
        state: Mapping[str, Any],
        source_ref: str | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> "TelemetryEvent":
        fields: dict[str, Any] = {
            "schema_version": TELEMETRY_EVENT_SCHEMA_VERSION,
            "plane": str(plane).strip().lower(),
            "owner": str(owner).strip().lower(),
            "subject_type": str(subject_type).strip().lower(),
            "subject_id": str(subject_id).strip(),
            "event_type": str(event_type).strip().lower(),
            "source_sequence": source_sequence,
            "occurred_at_ms": occurred_at_ms,
            "state": dict(state),
            "source_ref": source_ref,
            "correlation_id": correlation_id,
            "parent_event_id": parent_event_id,
        }
        _validate_fields(fields)
        return cls(event_id=_event_identity(fields), **fields)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TelemetryEvent":
        if not isinstance(value, Mapping):
            raise ValueError("telemetry event must be an object")
        event = cls.create(
            plane=value.get("plane", ""),
            owner=value.get("owner", ""),
            subject_type=value.get("subject_type", ""),
            subject_id=value.get("subject_id", ""),
            event_type=value.get("event_type", ""),
            source_sequence=value.get("source_sequence"),
            occurred_at_ms=value.get("occurred_at_ms"),
            state=value.get("state") or {},
            source_ref=value.get("source_ref"),
            correlation_id=value.get("correlation_id"),
            parent_event_id=value.get("parent_event_id"),
        )
        if str(value.get("event_id") or "") != event.event_id:
            raise ValueError("telemetry event_id does not match its state and provenance")
        if int(value.get("schema_version", 0)) != TELEMETRY_EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported telemetry event schema version: {value.get('schema_version')!r}"
            )
        return event

    def validate(self) -> None:
        _validate_fields(self.to_dict(include_id=False))
        if self.event_id != _event_identity(self.to_dict(include_id=False)):
            raise ValueError("telemetry event_id does not match its state and provenance")

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "plane": self.plane,
            "owner": self.owner,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "event_type": self.event_type,
            "source_sequence": self.source_sequence,
            "occurred_at_ms": self.occurred_at_ms,
            "state": dict(self.state),
            "source_ref": self.source_ref,
            "correlation_id": self.correlation_id,
            "parent_event_id": self.parent_event_id,
        }
        if include_id:
            result["event_id"] = self.event_id
        return result


def _validate_fields(fields: Mapping[str, Any]) -> None:
    if fields.get("schema_version") != TELEMETRY_EVENT_SCHEMA_VERSION:
        raise ValueError("telemetry event schema version is not current")
    plane = fields.get("plane")
    event_type = fields.get("event_type")
    if not isinstance(plane, str) or not plane:
        raise ValueError("telemetry event requires plane")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("telemetry event requires event_type")
    if plane not in _EVENT_PLANES.get(event_type, frozenset()):
        raise ValueError(f"event_type {event_type!r} is not allowed on plane {plane!r}")
    for key in ("owner", "subject_type", "subject_id"):
        if not isinstance(fields.get(key), str) or not fields[key].strip():
            raise ValueError(f"telemetry event requires {key}")
    owner = fields["owner"]
    if event_type not in _EVENT_OWNERS.get(owner, frozenset()):
        raise ValueError(
            f"owner {owner!r} is not declared for telemetry event {event_type!r}"
        )
    if plane != _OWNER_PLANES[owner]:
        raise ValueError(
            f"owner {owner!r} emits on plane {_OWNER_PLANES[owner]!r}, not {plane!r}"
        )
    for key in ("source_sequence", "occurred_at_ms"):
        value = fields.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"telemetry event {key} must be a non-negative integer")
    state = fields.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("telemetry event state must be an object")
    _validate_state(state)
    encoded = json.dumps(dict(state), sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError(f"telemetry event state exceeds {MAX_STATE_BYTES} bytes")
    for key in ("source_ref", "correlation_id", "parent_event_id"):
        value = fields.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"telemetry event {key} must be null or a non-empty string")


#: Terminal + interim outcomes for ``agent_narration_cadence`` heartbeats.
#: ``chunk`` is the throttled interim milestone; the rest are terminal.
NARRATION_CADENCE_OUTCOMES: tuple[str, ...] = (
    "started", "chunk", "complete",
    "stalled_no_bytes", "stalled_thinking", "timeout",
)


def narration_cadence_state(
    *,
    attempt_id: str,
    turn_ref: str,
    prompt_chars: int,
    deadline_ms: int,
    bytes_in: int,
    elapsed_ms: int,
    outcome: str,
    cause: str | None = None,
) -> dict[str, Any]:
    """Build a pure-state ``agent_narration_cadence`` payload (pure).

    Counts and shapes only — attempt identity, turn reference, prompt size,
    computed deadline, bytes observed, elapsed time, and the outcome. Never
    prompts, outputs, or credentials (rejected by ``_validate_state``).
    Raises ``ValueError`` on an unknown outcome or a negative counter.
    """
    if outcome not in NARRATION_CADENCE_OUTCOMES:
        raise ValueError(
            f"narration cadence outcome {outcome!r} not in "
            f"{list(NARRATION_CADENCE_OUTCOMES)}"
        )
    for key, value in (
        ("prompt_chars", prompt_chars),
        ("deadline_ms", deadline_ms),
        ("bytes_in", bytes_in),
        ("elapsed_ms", elapsed_ms),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"narration cadence {key} must be a non-negative integer"
            )
    if not str(attempt_id or "").strip():
        raise ValueError("narration cadence requires attempt_id")
    if not str(turn_ref or "").strip():
        raise ValueError("narration cadence requires turn_ref")
    state: dict[str, Any] = {
        "attempt_id": str(attempt_id),
        "turn_ref": str(turn_ref),
        "prompt_chars": prompt_chars,
        "deadline_ms": deadline_ms,
        "bytes_in": bytes_in,
        "elapsed_ms": elapsed_ms,
        "outcome": outcome,
    }
    if cause is not None:
        state["cause"] = str(cause)
    return state


__all__ = [
    "NARRATION_CADENCE_OUTCOMES",
    "TelemetryEvent",
    "narration_cadence_state",
]