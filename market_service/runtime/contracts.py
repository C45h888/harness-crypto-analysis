"""Versioned contracts shared by data nodes, Redis, and durable storage.

These contracts deliberately contain transport-safe JSON only. Domain logic
stays in ``market_service`` and infrastructure details stay in the adapters.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

MARKET_STATE_SCHEMA_VERSION = 1
MARKET_RUN_SCHEMA_VERSION = 1
StateStatus = Literal["healthy", "degraded", "invalid"]
RuntimePhase = Literal[
    "REQUESTED", "INITIALIZING", "COLLECTING", "CALCULATING", "ANALYZING",
    "COLLATING", "PUBLISHED", "DEGRADED", "INVALID", "STALE", "FAILED",
]


def _utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class MarketStateEnvelope:
    """A latest-state projection published by a domain data node."""

    symbol: str
    source: str
    observed_at: str
    produced_at: str
    status: StateStatus
    data: dict[str, Any]
    run_id: str | None = None
    errors: tuple[dict[str, Any], ...] = ()
    coverage_seconds: int | None = None
    schema_version: int = MARKET_STATE_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "MarketStateEnvelope":
        envelope = cls(
            symbol=str(value["symbol"]).upper(),
            source=str(value["source"]),
            observed_at=str(value["observed_at"]),
            produced_at=str(value.get("produced_at") or _utc_iso()),
            status=value.get("status", "degraded"),
            data=dict(value.get("data") or {}),
            run_id=str(value["run_id"]) if value.get("run_id") else None,
            errors=tuple(value.get("errors") or ()),
            coverage_seconds=value.get("coverage_seconds"),
            schema_version=int(value.get("schema_version", MARKET_STATE_SCHEMA_VERSION)),
        )
        if envelope.schema_version != MARKET_STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported market state schema version: {envelope.schema_version}")
        if envelope.status not in ("healthy", "degraded", "invalid"):
            raise ValueError(f"invalid market state status: {envelope.status}")
        return envelope

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "source": self.source,
            "observed_at": self.observed_at,
            "produced_at": self.produced_at,
            "status": self.status,
            "coverage_seconds": self.coverage_seconds,
            "data": self.data,
            "run_id": self.run_id,
            "errors": list(self.errors),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))


@dataclass(frozen=True)
class MarketEvent:
    """An append-only telemetry or signal event."""

    event_type: str
    symbol: str
    payload: dict[str, Any]
    event_id: str | None = None
    occurred_at: str | None = None
    schema_version: int = MARKET_STATE_SCHEMA_VERSION

    def to_fields(self) -> dict[str, str]:
        return {
            "schema_version": str(self.schema_version),
            "event_type": self.event_type,
            "symbol": self.symbol.upper(),
            "occurred_at": self.occurred_at or _utc_iso(),
            "payload": json.dumps(self.payload, default=str, separators=(",", ":")),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "MarketEvent":
        payload_raw = fields.get("payload", "{}")
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except json.JSONDecodeError:
            payload = {}
        return cls(
            event_type=str(fields.get("event_type", "")),
            symbol=str(fields.get("symbol", "")).upper(),
            payload=payload,
            occurred_at=fields.get("occurred_at"),
        )


@dataclass(frozen=True)
class RefreshCommand:
    """A bounded request for a data node to refresh one domain and symbol."""

    domain: str
    symbol: str
    requested_by: str = "runtime"
    command_id: str | None = None
    parameters: dict[str, Any] | None = None

    def to_fields(self) -> dict[str, str]:
        return {
            "command_id": self.command_id or "",
            "domain": self.domain,
            "symbol": self.symbol.upper(),
            "requested_by": self.requested_by,
            "parameters": json.dumps(self.parameters or {}, separators=(",", ":")),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "RefreshCommand":
        parameters_raw = fields.get("parameters", "{}")
        try:
            parameters = json.loads(parameters_raw) if parameters_raw else {}
        except json.JSONDecodeError:
            parameters = {}
        cmd_id = fields.get("command_id") or None
        return cls(
            domain=str(fields.get("domain", "")),
            symbol=str(fields.get("symbol", "")).upper(),
            requested_by=str(fields.get("requested_by", "runtime")),
            command_id=cmd_id,
            parameters=parameters or None,
        )


@dataclass(frozen=True)
class HarnessRunRequest:
    """A bounded request for the orchestrator to run one complete symbol cycle."""

    symbol: str
    requested_by: str = "harness"
    request_id: str | None = None
    parameters: dict[str, Any] | None = None

    def to_fields(self) -> dict[str, str]:
        return {
            "request_id": self.request_id or str(uuid.uuid4()),
            "symbol": self.symbol.upper(),
            "requested_by": self.requested_by,
            "parameters": json.dumps(self.parameters or {}, separators=(",", ":")),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "HarnessRunRequest":
        raw = fields.get("parameters", "{}")
        try:
            parameters = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parameters = {}
        return cls(
            symbol=str(fields.get("symbol", "")).upper(),
            requested_by=str(fields.get("requested_by", "harness")),
            request_id=fields.get("request_id") or None,
            parameters=parameters or None,
        )


@dataclass(frozen=True)
class RuntimeRunState:
    """Operational lifecycle record for one orchestrated symbol refresh."""

    run_id: str
    symbol: str
    phase: RuntimePhase
    started_at: str
    updated_at: str
    domains: dict[str, str]
    publication: str = "pending"
    errors: tuple[dict[str, Any], ...] = ()
    schema_version: int = MARKET_STATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "symbol": self.symbol.upper(),
            "phase": self.phase,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "domains": self.domains,
            "publication": self.publication,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class MarketRunEnvelope:
    """Immutable, transport-safe result of one canonical live collation run."""

    run_id: str
    symbol: str
    generated_at: str
    completed_at: str
    status: StateStatus
    data_source: str
    coverage: dict[str, Any]
    canonical_state: dict[str, Any]
    domain_outputs: dict[str, Any]
    errors: tuple[dict[str, Any], ...] = ()
    source_metadata: dict[str, Any] | None = None
    schema_version: int = MARKET_RUN_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        generated_at: str,
        completed_at: str,
        status: StateStatus,
        data_source: str,
        coverage: dict[str, Any],
        canonical_state: dict[str, Any],
        domain_outputs: dict[str, Any],
        errors: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        source_metadata: dict[str, Any] | None = None,
    ) -> "MarketRunEnvelope":
        return cls(
            run_id=str(uuid.uuid4()),
            symbol=symbol.upper(),
            generated_at=generated_at,
            completed_at=completed_at,
            status=status,
            data_source=data_source,
            coverage=coverage,
            canonical_state=canonical_state,
            domain_outputs=domain_outputs,
            errors=tuple(errors),
            source_metadata=source_metadata or {},
        )

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "MarketRunEnvelope":
        envelope = cls(
            run_id=str(value["run_id"]),
            symbol=str(value["symbol"]).upper(),
            generated_at=str(value["generated_at"]),
            completed_at=str(value["completed_at"]),
            status=value["status"],
            data_source=str(value["data_source"]),
            coverage=dict(value.get("coverage") or {}),
            canonical_state=dict(value.get("canonical_state") or {}),
            domain_outputs=dict(value.get("domain_outputs") or {}),
            errors=tuple(value.get("errors") or ()),
            source_metadata=dict(value.get("source_metadata") or {}),
            schema_version=int(value.get("schema_version", MARKET_RUN_SCHEMA_VERSION)),
        )
        envelope.validate()
        return envelope

    def validate(self) -> None:
        if self.schema_version != MARKET_RUN_SCHEMA_VERSION:
            raise ValueError(f"unsupported market run schema version: {self.schema_version}")
        if not self.run_id or not self.symbol or not self.data_source:
            raise ValueError("run_id, symbol, and data_source are required")
        if self.status not in ("healthy", "degraded", "invalid"):
            raise ValueError(f"invalid market run status: {self.status}")
        if not isinstance(self.canonical_state, dict) or not isinstance(self.domain_outputs, dict):
            raise ValueError("canonical_state and domain_outputs must be objects")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "symbol": self.symbol,
            "generated_at": self.generated_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "data_source": self.data_source,
            "coverage": self.coverage,
            "canonical_state": self.canonical_state,
            "domain_outputs": self.domain_outputs,
            "errors": list(self.errors),
            "source_metadata": self.source_metadata or {},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))
