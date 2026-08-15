"""Versioned contracts shared by data nodes, Redis, and durable storage.

These contracts deliberately contain transport-safe JSON only. Domain logic
stays in ``market_service`` and infrastructure details stay in the adapters.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

MARKET_STATE_SCHEMA_VERSION = 1
MARKET_RUN_SCHEMA_VERSION = 1
ANALYST_BRIEFING_SCHEMA_VERSION = 1
SPECIALIST_REPORT_SCHEMA_VERSION = 1
StateStatus = Literal["healthy", "degraded", "invalid"]
RuntimePhase = Literal[
    "REQUESTED", "INITIALIZING", "COLLECTING", "CALCULATING", "ANALYZING",
    "COLLATING", "PUBLISHED", "DEGRADED", "INVALID", "STALE", "FAILED",
]
ConfidenceLevel = Literal["low", "medium", "high"]
AnalystStatus = Literal["healthy", "degraded", "failed"]
ValidConfidence: tuple[str, ...] = ("low", "medium", "high")


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


# ---------------------------------------------------------------------------
# Analyst-layer contracts.
#
# These reuse the same discipline as MarketRunEnvelope / MarketStateEnvelope:
# frozen dataclass, schema-versioned, JSON-serializable, validated on
# construction. They live next to the canonical envelopes so the analyst
# layer shares one contract surface with the runtime that produces the
# evidence it reasons over.
#
# NOOA / model output is unstructured text. ``SpecialistReport.from_llm_text``
# and ``AnalystBriefing.from_controller_text`` are the only sanctioned
# boundary that turns LLM text into typed state — anything that reaches the
# Redis agent stream or the durable ``analyst_briefing`` table has already
# crossed it. Parse failures are surfaced as ``SpecialistReportParseError``
# carrying (specialist_name, run_id, error, raw_preview) so the suite can
# record them on the briefing without silently substituting neutral values.
# ---------------------------------------------------------------------------


class SpecialistReportParseError(ValueError):
    """Raised when an LLM-produced specialist JSON fails to parse or validate.

    Carries the specialist name, the canonical run_id, and a short preview of
    the raw text so the suite can persist a structured parse_errors entry
    on the briefing without inventing neutral values.
    """

    def __init__(self, specialist: str, run_id: str, error: str, raw_preview: str):
        super().__init__(f"{specialist} parse failed for run_id={run_id}: {error}")
        self.specialist = specialist
        self.run_id = run_id
        self.error = error
        self.raw_preview = raw_preview


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing or non-string required field: {key}")
    return value


def _require_confidence(payload: dict[str, Any], key: str = "confidence") -> str:
    value = payload.get(key)
    if value not in ValidConfidence:
        raise ValueError(
            f"confidence must be one of {ValidConfidence!r}, got {value!r}"
        )
    return str(value)


def _coerce_evidence_list(payload: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{key}[{i}] must be an object")
        if "path" not in item:
            raise ValueError(f"{key}[{i}] missing required field: path")
        out.append(dict(item))
    return tuple(out)


def _coerce_string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise ValueError(f"{key}[{i}] must be a string")
        out.append(item)
    return tuple(out)


def _coerce_list_of_objects(payload: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{key}[{i}] must be an object")
        out.append(dict(item))
    return tuple(out)


def _envelope_summary(envelope: dict[str, Any]) -> dict[str, Any]:
    """Compact, bounded projection of the canonical envelope for the briefing.

    Stored on the briefing so the human reviewer can see what evidence
    boundary the briefing was produced against, without re-fetching the
    full envelope. ``null`` values are preserved (not zero-substituted) per
    the canonical null discipline.
    """
    coverage = envelope.get("coverage") or {}
    domain_status = (
        coverage.get("domain_status")
        if isinstance(coverage, dict) else None
    ) or {}
    return {
        "schema_version": envelope.get("schema_version"),
        "symbol": envelope.get("symbol"),
        "status": envelope.get("status"),
        "generated_at": envelope.get("generated_at"),
        "completed_at": envelope.get("completed_at"),
        "data_source": envelope.get("data_source"),
        "domain_status": domain_status,
        "error_count": len(envelope.get("errors") or []),
    }


def _raw_preview(raw: str, limit: int = 240) -> str:
    """Return a short, JSON-safe preview of raw LLM output for parse errors."""
    if not isinstance(raw, str):
        raw = str(raw)
    cleaned = raw.strip().replace("\n", " ")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "..."
    return cleaned


@dataclass(frozen=True)
class SpecialistReport:
    """Validated output of one specialist agent for one canonical run.

    ``from_llm_text`` is the ONLY sanctioned way to construct one. Direct
    construction in production code is a smell — it means the LLM text was
    not routed through the parser.
    """

    name: str
    run_id: str
    summary: str
    evidence: tuple[dict[str, Any], ...]
    confidence: ConfidenceLevel
    limitations: tuple[str, ...]
    extra: dict[str, Any]
    schema_version: int = SPECIALIST_REPORT_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != SPECIALIST_REPORT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported specialist report schema version: {self.schema_version}"
            )
        if not self.name or not self.run_id:
            raise ValueError("SpecialistReport requires name and run_id")
        if self.confidence not in ValidConfidence:
            raise ValueError(f"invalid confidence: {self.confidence!r}")

    @classmethod
    def from_llm_text(
        cls, name: str, run_id: str, raw: str
    ) -> "SpecialistReport":
        """Parse and validate one specialist's JSON output.

        Raises ``SpecialistReportParseError`` on malformed JSON or missing
        required fields. Callers MUST catch this and persist the structured
        error on the briefing — there is no silent fallback to neutral
        values.
        """
        preview = _raw_preview(raw)
        if not isinstance(raw, str) or not raw.strip():
            raise SpecialistReportParseError(
                name, run_id, "empty or non-string raw output", preview
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SpecialistReportParseError(
                name, run_id, f"invalid JSON: {exc.msg}", preview
            ) from exc
        if not isinstance(payload, dict):
            raise SpecialistReportParseError(
                name, run_id, "top-level JSON must be an object", preview
            )

        try:
            summary = _require_str(payload, "summary")
            evidence = _coerce_evidence_list(payload, "evidence")
            confidence = _require_confidence(payload)
            limitations = _coerce_string_list(payload, "limitations")
        except ValueError as exc:
            raise SpecialistReportParseError(
                name, run_id, str(exc), preview
            ) from exc

        # Reserved keys are pinned by the schema; everything else is
        # preserved on ``extra`` (e.g. null_fields, cannot_establish,
        # missing_data) so specialist-specific fields are not silently
        # dropped.
        reserved = {"summary", "evidence", "confidence", "limitations"}
        extra = {k: v for k, v in payload.items() if k not in reserved}
        report = cls(
            name=name,
            run_id=run_id,
            summary=summary,
            evidence=evidence,
            confidence=confidence,  # type: ignore[arg-type]
            limitations=limitations,
            extra=extra,
        )
        report.validate()
        return report

    def to_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "name": self.name,
            "run_id": self.run_id,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "limitations": list(self.limitations),
        }
        if self.extra:
            out.update(self.extra)
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))


@dataclass(frozen=True)
class AnalystBriefing:
    """Validated, auditable analyst briefing tied to one immutable run_id.

    Persisted once per (session_id, run_id) into Redis and the durable
    ``analyst_briefing`` table. The briefing always carries the canonical
    run_id it was produced from — there is no analyst artifact without a
    source envelope.
    """

    session_id: str
    run_id: str
    schema_version: int
    model_provider: str
    model_name: str
    generated_at: str
    narrative: str
    consensus: dict[str, Any]
    disagreements: tuple[dict[str, Any], ...]
    key_evidence: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]
    uncertainty_sources: tuple[str, ...]
    parse_errors: tuple[dict[str, Any], ...]
    envelope_summary: dict[str, Any]
    extra: dict[str, Any]
    prompt_version: str = "nooa-harness-v1"
    completed_at: str | None = None
    status: AnalystStatus = "healthy"
    specialist_reports: dict[str, dict[str, Any] | None] = field(default_factory=dict)

    def validate(self) -> None:
        if self.schema_version != ANALYST_BRIEFING_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported analyst briefing schema version: {self.schema_version}"
            )
        if not self.session_id or not self.run_id:
            raise ValueError("AnalystBriefing requires session_id and run_id")
        if not self.model_provider or not self.model_name:
            raise ValueError("AnalystBriefing requires model_provider and model_name")
        if not self.prompt_version:
            raise ValueError("AnalystBriefing requires prompt_version")
        if self.status not in ("healthy", "degraded", "failed"):
            raise ValueError(f"invalid analyst status: {self.status}")
        if not isinstance(self.consensus, dict):
            raise ValueError("consensus must be an object")
        if "direction" not in self.consensus:
            raise ValueError("consensus.direction is required")
        if not isinstance(self.specialist_reports, dict):
            raise ValueError("specialist_reports must be an object")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AnalystBriefing":
        """Round-trip from a dict payload (e.g. read back from Redis or Postgres)."""
        pinned = {
            "schema_version", "session_id", "run_id", "model_provider",
            "model_name", "generated_at", "narrative", "consensus",
            "disagreements", "key_evidence", "limitations",
            "uncertainty_sources", "parse_errors", "envelope_summary",
            "prompt_version", "completed_at", "status", "specialist_reports",
        }
        extra = {k: v for k, v in value.items() if k not in pinned}
        briefing = cls(
            schema_version=int(value.get("schema_version", ANALYST_BRIEFING_SCHEMA_VERSION)),
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]),
            model_provider=str(value["model_provider"]),
            model_name=str(value["model_name"]),
            generated_at=str(value["generated_at"]),
            narrative=str(value.get("narrative") or ""),
            consensus=dict(value.get("consensus") or {}),
            disagreements=tuple(dict(d) for d in (value.get("disagreements") or ())),
            key_evidence=tuple(dict(d) for d in (value.get("key_evidence") or ())),
            limitations=tuple(str(s) for s in (value.get("limitations") or ())),
            uncertainty_sources=tuple(str(s) for s in (value.get("uncertainty_sources") or ())),
            parse_errors=tuple(dict(d) for d in (value.get("parse_errors") or ())),
            envelope_summary=dict(value.get("envelope_summary") or {}),
            extra=extra,
            prompt_version=str(value.get("prompt_version") or "nooa-harness-v1"),
            completed_at=(
                str(value["completed_at"])
                if value.get("completed_at") is not None else None
            ),
            status=value.get("status", "healthy"),
            specialist_reports={
                str(name): (dict(report) if isinstance(report, dict) else None)
                for name, report in dict(value.get("specialist_reports") or {}).items()
            },
        )
        briefing.validate()
        return briefing

    @classmethod
    def from_controller_text(
        cls,
        *,
        session_id: str,
        run_id: str,
        model_provider: str,
        model_name: str,
        generated_at: str,
        raw: str,
        envelope: dict[str, Any],
        parse_errors: tuple[dict[str, Any], ...] = (),
        prompt_version: str = "nooa-harness-v1",
        completed_at: str | None = None,
        status: AnalystStatus = "healthy",
        specialist_reports: dict[str, dict[str, Any] | None] | None = None,
    ) -> "AnalystBriefing":
        """Parse the controller's JSON output and assemble the briefing.

        On parse failure a minimal briefing is still produced — the raw text
        is preserved on ``extra.raw_narrative`` so the human reviewer can
        still see what the model emitted, and the structured parse error is
        recorded on ``parse_errors``. The briefing is never silently dropped.
        """
        envelope_summary = _envelope_summary(envelope)
        preview = _raw_preview(raw)
        consensus: dict[str, Any] = {"direction": "unknown", "confidence": "low"}
        narrative = ""
        disagreements: tuple[dict[str, Any], ...] = ()
        key_evidence: tuple[dict[str, Any], ...] = ()
        limitations: tuple[str, ...] = ()
        uncertainty_sources: tuple[str, ...] = ()
        extra: dict[str, Any] = {}
        next_parse_errors = parse_errors

        if isinstance(raw, str) and raw.strip():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
                extra["raw_narrative"] = raw
                next_parse_errors = parse_errors + (
                    {"stage": "controller", "error": "invalid JSON", "preview": preview},
                )
            if isinstance(payload, dict):
                narrative = str(payload.get("narrative") or "")
                cs = payload.get("consensus")
                if isinstance(cs, dict):
                    consensus = {
                        "direction": str(cs.get("direction", "unknown")),
                        "confidence": str(cs.get("confidence", "low")),
                    }
                    if consensus["confidence"] not in ValidConfidence:
                        consensus["confidence"] = "low"
                try:
                    disagreements = _coerce_list_of_objects(payload, "disagreements")
                    key_evidence = _coerce_list_of_objects(payload, "key_evidence")
                    limitations = _coerce_string_list(payload, "limitations")
                    uncertainty_sources = _coerce_string_list(payload, "uncertainty_sources")
                except ValueError as exc:
                    next_parse_errors = next_parse_errors + (
                        {"stage": "controller", "error": str(exc), "preview": preview},
                    )
                # Preserve any controller fields we did not pin above.
                pinned = {
                    "narrative", "consensus", "disagreements",
                    "key_evidence", "limitations", "uncertainty_sources",
                }
                for k, v in payload.items():
                    if k not in pinned:
                        extra[k] = v
        else:
            next_parse_errors = parse_errors + (
                {"stage": "controller", "error": "empty raw output", "preview": preview},
            )
            extra["raw_narrative"] = raw

        if not narrative and "raw_narrative" in extra:
            narrative = (
                "[controller JSON could not be parsed; raw text preserved on "
                "extra.raw_narrative; see parse_errors]"
            )

        briefing = cls(
            session_id=session_id,
            run_id=run_id,
            schema_version=ANALYST_BRIEFING_SCHEMA_VERSION,
            model_provider=model_provider,
            model_name=model_name,
            generated_at=generated_at,
            narrative=narrative,
            consensus=consensus,
            disagreements=disagreements,
            key_evidence=key_evidence,
            limitations=limitations,
            uncertainty_sources=uncertainty_sources,
            parse_errors=next_parse_errors,
            envelope_summary=envelope_summary,
            extra=extra,
            prompt_version=prompt_version,
            completed_at=completed_at,
            status=("degraded" if status == "healthy" and next_parse_errors else status),
            specialist_reports=dict(specialist_reports or {}),
        )
        briefing.validate()
        return briefing

    def to_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "generated_at": self.generated_at,
            "narrative": self.narrative,
            "consensus": self.consensus,
            "disagreements": list(self.disagreements),
            "key_evidence": list(self.key_evidence),
            "limitations": list(self.limitations),
            "uncertainty_sources": list(self.uncertainty_sources),
            "parse_errors": list(self.parse_errors),
            "envelope_summary": self.envelope_summary,
            "prompt_version": self.prompt_version,
            "completed_at": self.completed_at,
            "status": self.status,
            "specialist_reports": self.specialist_reports,
        }
        if self.extra:
            out.update(self.extra)
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))
