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
ANALYST_BRIEFING_SCHEMA_VERSION = 2
SPECIALIST_REPORT_SCHEMA_VERSION = 2
AGENT_MEMORY_SCHEMA_VERSION = 1
# Agent-artifact kinds the memory node can persist. Mirrors the agent-owned
# write surfaces from NOOA_HARNESS_ARCHITECTURE.md: observations, hypotheses,
# requests, briefings — plus 'fact'/'note' for durable analyst notes.
ValidMemoryKinds: tuple[str, ...] = (
    "observation", "hypothesis", "request", "briefing", "fact", "note",
)
StateStatus = Literal["healthy", "degraded", "invalid"]
ConfidenceLevel = Literal["low", "medium", "high"]
AnalystStatus = Literal["healthy", "degraded", "failed"]
ValidConfidence: tuple[str, ...] = ("low", "medium", "high")

# ---------------------------------------------------------------------------
# Typed sub-contracts for analyst-layer evidence, consensus, and disagreements.
#
# These are frozen dataclasses with to_dict / from_mapping round-trips so they
# compose cleanly inside SpecialistReport and AnalystBriefing. The LLM-produced
# JSON is still parsed through the existing from_llm_text / from_controller_text
# boundaries; these types tighten what the parser validates and what downstream
# consumers (Hermes, memory node) can rely on.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceEntry:
    """One piece of evidence cited by a specialist or controller.

    Required fields are ``path`` and ``interpretation`` — the envelope path
    the claim is sourced from and what the analyst concluded from it. ``value``
    is the raw value at that path (may be any JSON type). ``metric_name`` is an
    optional human-readable label for the metric (e.g. ``funding_rate``).
    """

    path: str
    interpretation: str
    value: Any | None = None
    metric_name: str | None = None

    def validate(self) -> None:
        if not self.path or not self.path.strip():
            raise ValueError("EvidenceEntry requires a non-empty path")
        if not self.interpretation or not self.interpretation.strip():
            raise ValueError("EvidenceEntry requires a non-empty interpretation")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "interpretation": self.interpretation,
        }
        if self.value is not None:
            out["value"] = self.value
        if self.metric_name is not None:
            out["metric_name"] = self.metric_name
        return out

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "EvidenceEntry":
        entry = cls(
            path=str(value.get("path", "")),
            interpretation=str(value.get("interpretation", "")),
            value=value.get("value"),
            metric_name=(
                str(value["metric_name"])
                if value.get("metric_name") is not None else None
            ),
        )
        entry.validate()
        return entry


@dataclass(frozen=True)
class Consensus:
    """Structured consensus from the controller.

    ``direction`` and ``confidence`` are the categorical baseline (same as v1).
    ``confidence_score`` is a numeric 0.0-1.0 the LLM produces directly —
    richer signal for downstream adaptive reasoning. ``timeframe`` is the
    implied horizon (e.g. ``intraday``, ``session``, ``swing``). ``magnitude``
    is the expected move intensity (e.g. ``marginal``, ``moderate``, ``strong``).
    """

    direction: str
    confidence: ConfidenceLevel
    confidence_score: float | None = None
    timeframe: str | None = None
    magnitude: str | None = None

    def validate(self) -> None:
        if self.confidence not in ValidConfidence:
            raise ValueError(f"invalid confidence: {self.confidence!r}")
        if self.confidence_score is not None and not 0.0 <= self.confidence_score <= 1.0:
            raise ValueError(
                f"confidence_score must be in [0.0, 1.0], got {self.confidence_score!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "direction": self.direction,
            "confidence": self.confidence,
        }
        if self.confidence_score is not None:
            out["confidence_score"] = self.confidence_score
        if self.timeframe is not None:
            out["timeframe"] = self.timeframe
        if self.magnitude is not None:
            out["magnitude"] = self.magnitude
        return out

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Consensus":
        confidence = str(value.get("confidence", "low"))
        if confidence not in ValidConfidence:
            confidence = "low"
        score = value.get("confidence_score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        consensus = cls(
            direction=str(value.get("direction", "unknown")),
            confidence=confidence,  # type: ignore[arg-type]
            confidence_score=score,
            timeframe=(
                str(value["timeframe"])
                if value.get("timeframe") is not None else None
            ),
            magnitude=(
                str(value["magnitude"])
                if value.get("magnitude") is not None else None
            ),
        )
        consensus.validate()
        return consensus


@dataclass(frozen=True)
class KeyEvidence:
    """One piece of cross-referenced evidence in the controller briefing.

    ``path`` is the canonical envelope path. ``claim`` is the human-readable
    assertion the controller makes about it. ``specialist`` names the source
    specialist if the evidence originated from a specialist report.
    """

    path: str
    claim: str
    value: Any | None = None
    run_id: str | None = None
    specialist: str | None = None

    def validate(self) -> None:
        if not self.path or not self.path.strip():
            raise ValueError("KeyEvidence requires a non-empty path")
        if not self.claim or not self.claim.strip():
            raise ValueError("KeyEvidence requires a non-empty claim")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "claim": self.claim,
        }
        if self.value is not None:
            out["value"] = self.value
        if self.run_id is not None:
            out["run_id"] = self.run_id
        if self.specialist is not None:
            out["specialist"] = self.specialist
        return out

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "KeyEvidence":
        entry = cls(
            path=str(value.get("path", "")),
            claim=str(value.get("claim", "")),
            value=value.get("value"),
            run_id=(
                str(value["run_id"])
                if value.get("run_id") is not None else None
            ),
            specialist=(
                str(value["specialist"])
                if value.get("specialist") is not None else None
            ),
        )
        entry.validate()
        return entry


@dataclass(frozen=True)
class Disagreement:
    """One structured disagreement between specialists.

    ``topic`` is the subject of disagreement. ``specialist_a`` and
    ``specialist_b`` name the disagreeing specialists. ``resolution`` is the
    controller's reconciliation (or ``unresolved``).
    """

    topic: str
    specialist_a: str | None = None
    specialist_b: str | None = None
    resolution: str | None = None

    def validate(self) -> None:
        if not self.topic or not self.topic.strip():
            raise ValueError("Disagreement requires a non-empty topic")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"topic": self.topic}
        if self.specialist_a is not None:
            out["specialist_a"] = self.specialist_a
        if self.specialist_b is not None:
            out["specialist_b"] = self.specialist_b
        if self.resolution is not None:
            out["resolution"] = self.resolution
        return out

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Disagreement":
        entry = cls(
            topic=str(value.get("topic", "")),
            specialist_a=(
                str(value["specialist_a"])
                if value.get("specialist_a") is not None else None
            ),
            specialist_b=(
                str(value["specialist_b"])
                if value.get("specialist_b") is not None else None
            ),
            resolution=(
                str(value["resolution"])
                if value.get("resolution") is not None else None
            ),
        )
        entry.validate()
        return entry


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


def _coerce_evidence_list(payload: dict[str, Any], key: str) -> tuple[EvidenceEntry, ...]:
    """Parse evidence list into typed EvidenceEntry objects.

    Each item must be an object with at least ``path`` and ``interpretation``.
    For backward compatibility with v1 evidence that only carried ``path``,
    a missing ``interpretation`` is substituted with an empty string —
    but the EvidenceEntry validator will reject it, so the suite's parse-error
    path surfaces this as a structured error rather than silently accepting
    incomplete evidence.
    """
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    out: list[EvidenceEntry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{key}[{i}] must be an object")
        if "path" not in item:
            raise ValueError(f"{key}[{i}] missing required field: path")
        try:
            out.append(EvidenceEntry.from_mapping(item))
        except ValueError as exc:
            raise ValueError(f"{key}[{i}]: {exc}") from exc
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

    Stored on the briefing so downstream consumers (Hermes, human reviewers)
    can see what evidence boundary the briefing was produced against, without
    re-fetching the full envelope. Preserves key market metrics so the briefing
    is self-contained for adaptive reasoning. ``null`` values are preserved
    (not zero-substituted) per the canonical null discipline.
    """
    coverage = envelope.get("coverage") or {}
    domain_status = (
        coverage.get("domain_status")
        if isinstance(coverage, dict) else None
    ) or {}
    canonical = envelope.get("canonical_state") or {}
    if not isinstance(canonical, dict):
        canonical = {}

    # Safe traversal helpers for nested envelope paths.
    def _path(d: Any, *keys: str) -> Any:
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    data_access = canonical.get("data-access") or {}
    calculations = canonical.get("calculations") or {}
    analysis = canonical.get("analysis") or {}

    # Key market metrics — null-preserving, never zero-substituted.
    ticker = _path(data_access, "evidence", "futures", "ticker_24h") or {}
    funding = _path(data_access, "evidence", "futures", "funding") or {}
    oi = _path(data_access, "evidence", "futures", "open_interest") or {}
    flow = _path(calculations, "calculations", "flow") or {}
    orderbook = _path(calculations, "calculations", "orderbook") or {}
    demand = _path(analysis, "analysis", "demand", "decomposition") or {}

    # CVD = buy_volume - sell_volume (deterministic, null-safe).
    spot_flow = _path(flow, "spot") or {}
    fut_flow = _path(flow, "futures") or {}
    spot_cvd: float | None = None
    futures_cvd: float | None = None
    try:
        if isinstance(spot_flow, dict) and "buy_volume" in spot_flow and "sell_volume" in spot_flow:
            spot_cvd = float(spot_flow["buy_volume"]) - float(spot_flow["sell_volume"])
    except (TypeError, ValueError):
        pass
    try:
        if isinstance(fut_flow, dict) and "buy_volume" in fut_flow and "sell_volume" in fut_flow:
            futures_cvd = float(fut_flow["buy_volume"]) - float(fut_flow["sell_volume"])
    except (TypeError, ValueError):
        pass

    return {
        "schema_version": envelope.get("schema_version"),
        "symbol": envelope.get("symbol"),
        "status": envelope.get("status"),
        "generated_at": envelope.get("generated_at"),
        "completed_at": envelope.get("completed_at"),
        "data_source": envelope.get("data_source"),
        "domain_status": domain_status,
        "error_count": len(envelope.get("errors") or []),
        # Key market metrics for downstream reasoning.
        "last_price": ticker.get("lastPrice") if isinstance(ticker, dict) else None,
        "volume_24h": ticker.get("volume") if isinstance(ticker, dict) else None,
        "high_24h": ticker.get("highPrice") if isinstance(ticker, dict) else None,
        "low_24h": ticker.get("lowPrice") if isinstance(ticker, dict) else None,
        "funding_rate": funding.get("fundingRate") if isinstance(funding, dict) else None,
        "mark_price": funding.get("markPrice") if isinstance(funding, dict) else None,
        "open_interest": oi.get("openInterest") if isinstance(oi, dict) else None,
        "spot_cvd": spot_cvd,
        "futures_cvd": futures_cvd,
        "spot_obi": _path(demand, "spot", "obi"),
        "futures_obi": _path(demand, "futures", "obi"),
        "fut_keystone_bid": _path(orderbook, "fut_keystone", "bid"),
        "fut_keystone_ask": _path(orderbook, "fut_keystone", "ask"),
        # Round-number bid anchors surfaced from wall_migration analysis.
        "bid_anchor_count": _path(analysis, "analysis", "wall_migration", "round_anchors", "count"),
        "mega_tier_pct": _path(analysis, "analysis", "wall_migration", "tiers", "mega", "pct"),
        "fut_microprice_skew_bps": _path(orderbook, "fut_microprice_skew_bps"),
    }


def _raw_preview(raw: str, limit: int = 240) -> str:
    """Return a short, JSON-safe preview of raw LLM output for parse errors."""
    if not isinstance(raw, str):
        raw = str(raw)
    cleaned = raw.strip().replace("\n", " ")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "..."
    return cleaned


def _extract_json_object(raw: str) -> dict | None:
    """Extract one JSON object embedded in model prose (best-effort).

    Reasoning-model gateways prefix answers with a ``thinking`` preamble and
    often fence the JSON in ```json ... ``` blocks. This finds the first ``{``
    and the final ``}`` and validates the slice. Returns ``None`` when no
    object parses — callers must then raise the structured parse error
    (never silently substitute).
    """
    start = raw.find("{")
    if start == -1:
        return None
    end = raw.rfind("}")
    if end <= start:
        return None
    try:
        value = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
    evidence: tuple[EvidenceEntry, ...]
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

        Strict JSON is preferred. When the raw text does not parse as JSON
        (reasoning models prefix answers with prose / ```json fences), the
        first embedded JSON object is extracted and marked on ``extra`` as
        ``extracted: True`` — never a silent substitution. Raises
        ``SpecialistReportParseError`` when no JSON object exists or required
        fields are missing. Callers MUST catch this and persist the
        structured error on the briefing — there is no silent fallback to
        neutral values.
        """
        preview = _raw_preview(raw)
        if not isinstance(raw, str) or not raw.strip():
            raise SpecialistReportParseError(
                name, run_id, "empty or non-string raw output", preview
            )
        extracted = False
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            payload = _extract_json_object(raw)
            if payload is None:
                raise SpecialistReportParseError(
                    name, run_id, f"invalid JSON: {exc.msg}", preview
                ) from exc
            extracted = True
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
        if extracted:
            extra["extracted"] = True
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
            "evidence": [e.to_dict() for e in self.evidence],
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
    consensus: Consensus
    disagreements: tuple[Disagreement, ...]
    key_evidence: tuple[KeyEvidence, ...]
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
        if not isinstance(self.consensus, Consensus):
            raise ValueError("consensus must be a Consensus instance")
        if not isinstance(self.specialist_reports, dict):
            raise ValueError("specialist_reports must be an object")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AnalystBriefing":
        """Round-trip from a dict payload (e.g. read back from Redis or Postgres).

        Handles both v1 (plain dict consensus/evidence) and v2 (typed) payloads:
        consensus/evidence/disagreements are run through their respective
        ``from_mapping`` constructors which accept plain dicts.
        """
        pinned = {
            "schema_version", "session_id", "run_id", "model_provider",
            "model_name", "generated_at", "narrative", "consensus",
            "disagreements", "key_evidence", "limitations",
            "uncertainty_sources", "parse_errors", "envelope_summary",
            "prompt_version", "completed_at", "status", "specialist_reports",
        }
        extra = {k: v for k, v in value.items() if k not in pinned}
        briefing = cls(
            schema_version=ANALYST_BRIEFING_SCHEMA_VERSION,
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]),
            model_provider=str(value["model_provider"]),
            model_name=str(value["model_name"]),
            generated_at=str(value["generated_at"]),
            narrative=str(value.get("narrative") or ""),
            consensus=Consensus.from_mapping(
                dict(value.get("consensus") or {"direction": "unknown", "confidence": "low"})
            ),
            disagreements=tuple(
                Disagreement.from_mapping(d)
                for d in (value.get("disagreements") or ())
                if isinstance(d, dict)
            ),
            key_evidence=tuple(
                KeyEvidence.from_mapping(d)
                for d in (value.get("key_evidence") or ())
                if isinstance(d, dict) and d.get("path")
            ),
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
        consensus = Consensus(direction="unknown", confidence="low")
        narrative = ""
        disagreements: tuple[Disagreement, ...] = ()
        key_evidence: tuple[KeyEvidence, ...] = ()
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
                    try:
                        consensus = Consensus.from_mapping(cs)
                    except ValueError:
                        consensus = Consensus(direction="unknown", confidence="low")
                try:
                    raw_disagreements = payload.get("disagreements") or []
                    if not isinstance(raw_disagreements, list):
                        raise ValueError("disagreements must be a list")
                    disagreements = tuple(
                        Disagreement.from_mapping(d)
                        for d in raw_disagreements
                        if isinstance(d, dict) and d.get("topic")
                    )
                    raw_key_evidence = payload.get("key_evidence") or []
                    if not isinstance(raw_key_evidence, list):
                        raise ValueError("key_evidence must be a list")
                    key_evidence = tuple(
                        KeyEvidence.from_mapping(d)
                        for d in raw_key_evidence
                        if isinstance(d, dict) and d.get("path")
                    )
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
            "consensus": self.consensus.to_dict(),
            "disagreements": [d.to_dict() for d in self.disagreements],
            "key_evidence": [e.to_dict() for e in self.key_evidence],
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


@dataclass(frozen=True)
class AgentMemory:
    """A durable, auditable analyst memory tied to one session (and run).

    The memory node's unit of state. Every memory is advisory — it is the
    analyst's own curated knowledge, never canonical market state. It may
    optionally reference the canonical ``run_id`` (or exact envelope paths in
    ``evidence_refs``) it was derived from, so recall stays auditable.

    Persistence mirrors ``AnalystBriefing``: Postgres is the durable ledger
    (``agent_memory`` table, schema-versioned), Redis is the live projection
    (``marketflow:agent:<SESSION_ID>:memory`` stream). ``forgotten=True`` is a
    tombstone — the row survives for audit but is excluded from recall.
    """

    session_id: str
    kind: str
    content: str
    memory_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str | None = None
    title: str | None = None
    importance: float = 5.0
    tags: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = field(default_factory=lambda: _utc_iso())
    updated_at: str | None = None
    forgotten: bool = False
    schema_version: int = AGENT_MEMORY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != AGENT_MEMORY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported agent memory schema version: {self.schema_version}"
            )
        if not self.session_id:
            raise ValueError("AgentMemory requires session_id")
        if self.kind not in ValidMemoryKinds:
            raise ValueError(
                f"invalid memory kind {self.kind!r}; expected one of {ValidMemoryKinds!r}"
            )
        if not self.content or not self.content.strip():
            raise ValueError("AgentMemory requires non-empty content")
        if not 0 <= self.importance <= 10:
            raise ValueError(f"importance must be in [0, 10], got {self.importance!r}")
        if self.run_id is not None and not str(self.run_id).strip():
            raise ValueError("run_id must be non-empty when given")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AgentMemory":
        """Round-trip from a dict payload (Redis or Postgres read-back)."""
        memory = cls(
            schema_version=int(
                value.get("schema_version", AGENT_MEMORY_SCHEMA_VERSION)
            ),
            session_id=str(value["session_id"]),
            kind=str(value["kind"]),
            content=str(value["content"]),
            memory_id=str(value.get("memory_id") or uuid.uuid4()),
            run_id=(
                str(value["run_id"])
                if value.get("run_id") is not None else None
            ),
            title=(
                str(value["title"])
                if value.get("title") is not None else None
            ),
            importance=float(value.get("importance", 5.0)),
            tags=tuple(str(t) for t in (value.get("tags") or ())),
            evidence_refs=tuple(
                str(e) for e in (value.get("evidence_refs") or ())
            ),
            created_at=str(value.get("created_at") or _utc_iso()),
            updated_at=(
                str(value["updated_at"])
                if value.get("updated_at") is not None else None
            ),
            forgotten=bool(value.get("forgotten", False)),
        )
        memory.validate()
        return memory

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "kind": self.kind,
            "content": self.content,
            "memory_id": self.memory_id,
            "run_id": self.run_id,
            "title": self.title,
            "importance": self.importance,
            "tags": list(self.tags),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "forgotten": self.forgotten,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))
