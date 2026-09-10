"""Versioned contracts shared by data nodes, Redis, and durable storage.

These contracts deliberately contain transport-safe JSON only. Domain logic
stays in ``market_service`` and infrastructure details stay in the adapters.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

MARKET_STATE_SCHEMA_VERSION = 1
MARKET_RUN_SCHEMA_VERSION = 1
SPECIALIST_REPORT_SCHEMA_VERSION = 2
AGENT_MEMORY_SCHEMA_VERSION = 1
INFERENCE_ARTIFACT_SCHEMA_VERSION = 1
WAKE_ENVELOPE_SCHEMA_VERSION = 1
# Agent-artifact kinds the memory node can persist. Mirrors the agent-owned
# write surfaces from NOOA_HARNESS_ARCHITECTURE.md: observations, hypotheses,
# requests, briefings — plus 'fact'/'note' for durable analyst notes.
ValidMemoryKinds: tuple[str, ...] = (
    "observation", "hypothesis", "request", "briefing", "fact", "note",
)
# Valid wake trigger sources (who materialized the envelope).
ValidWakeSources: tuple[str, ...] = ("manual", "watcher", "hook")
# Valid trigger predicates (the deterministic wake conditions).
ValidWakePredicates: tuple[str, ...] = (
    "event_delta", "capture_recovery", "cold_start", "manual",
)
StateStatus = Literal["healthy", "degraded", "invalid"]
ConfidenceLevel = Literal["low", "medium", "high"]
ValidConfidence: tuple[str, ...] = ("low", "medium", "high")
# LLMs sometimes use "moderate" instead of "medium" — accept it as an alias.
# Hyphenated blends are coerced CONSERVATIVELY to the lower component
# (low-medium → low, medium-high → medium) so confidence is never inflated.
_CONFIDENCE_ALIASES: dict[str, str] = {
    "moderate": "medium",
    "med": "medium",
    "low-medium": "low",
    "low_medium": "low",
    "low/medium": "low",
    "medium-low": "low",
    "medium-high": "medium",
    "medium_high": "medium",
    "high-medium": "medium",
}


def normalize_confidence(value: Any) -> str | None:
    """Coerce an LLM-supplied confidence to the contract enum.

    Returns the canonical "low"|"medium"|"high" or None when unmappable.
    Lowercases, strips, maps _ → - , then applies _CONFIDENCE_ALIASES.
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("_", "-").replace("/", "-").replace(" ", "-")
    # collapse repeats like "low--medium"
    while "--" in key:
        key = key.replace("--", "-")
    if key in ValidConfidence:
        return key
    mapped = _CONFIDENCE_ALIASES.get(key)
    if mapped is not None and mapped in ValidConfidence:
        return mapped
    return None
# Inference-artifact status trichotomy (Pass-3 discipline, generalized):
# validated  — deterministic inputs passed every quality gate; narration may cite values
# provisional — fitted but diagnostics incomplete; narration must caveat, never signal
# insufficient — gates failed; interpretation is deterministically NULL (no LLM call)
InferenceStatus = Literal["validated", "provisional", "insufficient"]
ValidInferenceStatus: tuple[str, ...] = ("validated", "provisional", "insufficient")


@dataclass(frozen=True)
class WakeEnvelope:
    """Typed invocation envelope for the statistical inference engine.

    One envelope is a DETERMINISTIC ASSERTION that the inference engine
    should run a cycle: either synthesized by ``acquire_manual_wake`` (the
    CLI ``--force`` trigger) or constructed directly by a caller. The engine
    treats it as an assertion, not a command, and runs exactly one cycle
    per envelope. There is no worker, no stream transport, no trigger
    matrix — the CLI surfaces are the only producer.
    """

    symbol: str
    venue: str
    trigger_source: str
    predicates_fired: dict[str, Any]
    counter_snapshot: dict[str, Any]
    high_water: dict[str, Any]
    wake_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: _utc_iso())
    schema_version: int = WAKE_ENVELOPE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != WAKE_ENVELOPE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported wake envelope schema version: {self.schema_version}"
            )
        if not self.symbol or not self.venue:
            raise ValueError("WakeEnvelope requires symbol and venue")
        if self.trigger_source not in ValidWakeSources:
            raise ValueError(
                f"invalid trigger source {self.trigger_source!r}; "
                f"expected one of {ValidWakeSources!r}"
            )
        if not self.predicates_fired:
            raise ValueError("WakeEnvelope requires at least one fired predicate")
        for predicate in self.predicates_fired:
            if predicate not in ValidWakePredicates:
                raise ValueError(
                    f"invalid wake predicate {predicate!r}; "
                    f"expected one of {ValidWakePredicates!r}"
                )

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        venue: str,
        trigger_source: str,
        predicates_fired: dict[str, Any],
        counter_snapshot: dict[str, Any],
        high_water: dict[str, Any],
    ) -> "WakeEnvelope":
        created = cls(
            symbol=symbol.upper(),
            venue=venue,
            trigger_source=trigger_source,
            predicates_fired=dict(predicates_fired),
            counter_snapshot=dict(counter_snapshot),
            high_water=dict(high_water),
        )
        created.validate()
        return created

    def _replace_wake_id(self, wake_id: str) -> "WakeEnvelope":
        """Return a copy with the given ``wake_id``.

        The envelope has already been validated by ``create``; only the id and
        created-at stamps change.
        """
        return WakeEnvelope(
            wake_id=wake_id,
            symbol=self.symbol,
            venue=self.venue,
            trigger_source=self.trigger_source,
            predicates_fired=self.predicates_fired,
            counter_snapshot=self.counter_snapshot,
            high_water=self.high_water,
            created_at=self.created_at,
            schema_version=self.schema_version,
        )

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "WakeEnvelope":
        envelope = cls(
            wake_id=str(value["wake_id"]),
            symbol=str(value["symbol"]).upper(),
            venue=str(value["venue"]),
            trigger_source=str(value["trigger_source"]),
            predicates_fired=dict(value.get("predicates_fired") or {}),
            counter_snapshot=dict(value.get("counter_snapshot") or {}),
            high_water=dict(value.get("high_water") or {}),
            created_at=str(value.get("created_at") or _utc_iso()),
            schema_version=int(
                value.get("schema_version", WAKE_ENVELOPE_SCHEMA_VERSION)
            ),
        )
        envelope.validate()
        return envelope

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "wake_id": self.wake_id,
            "symbol": self.symbol,
            "venue": self.venue,
            "trigger_source": self.trigger_source,
            "predicates_fired": self.predicates_fired,
            "counter_snapshot": self.counter_snapshot,
            "high_water": self.high_water,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))


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


def _utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    """Return a transport-safe copy with non-finite floats coerced to None.

    Doctrine: contracts are "transport-safe JSON only". ``json.dumps`` writes
    non-finite floats (NaN / Infinity) as literal ``NaN``/``Infinity`` tokens,
    which are NOT valid JSON and are rejected by strict consumers (e.g. the
    Postgres ``json``/``jsonb`` column). A non-finite float means a division
    produced no computable value, so ``null`` ("source did not provide /
    could not compute") is the correct transport representation - not an
    invented zero.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


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
            "data": _json_safe(self.data),
            "run_id": self.run_id,
            "errors": list(self.errors),
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
    normalized = normalize_confidence(payload.get(key))
    if normalized is None:
        raise ValueError(
            f"confidence must be one of {ValidConfidence!r}, got {payload.get(key)!r}"
        )
    return normalized


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


@dataclass(frozen=True)
class InferenceArtifact:
    """Immutable output of one inference-engine cycle.

    The inference engine is an orchestrator object with AUTHORITY over the
    deterministic supporting modules: it reads price/microstructure data from
    Redis, validates the request via the bounded capability registry, triggers
    deterministic calculation + fitting, and then issues ONE bounded LLM call
    that narrates only the deterministic section it produced itself.

    Contract split inside the artifact:

    - ``deterministic_state`` — everything computed by Python, never by the
      LLM. Fits, calculations, coverage, input provenance. This section is
      the authority the interpretation cites.
    - ``interpretation`` — the LLM narration of ``deterministic_state``.
      Deterministically NULL when ``status == "insufficient"``: the hard gate
      guarantees no LLM call happens over data that failed the quality gates
      (null discipline — an empty interpretation means "not produced", never
      "nothing to say").
    - ``capability_log`` — the audit trail of every supporting-module dispatch
      the engine performed (module, scope, result status), so the artifact is
      self-documenting about how its deterministic state was produced.

    Persistence mirrors ``AnalystBriefing``: Postgres is the durable ledger
    (``inference_artifact`` table, schema-versioned), Redis is the live
    projection (``marketflow:latest:inference:<SYMBOL>`` + stream).
    """

    artifact_id: str
    symbol: str
    venue: str
    generated_at: str
    completed_at: str
    status: InferenceStatus
    window_minutes: int
    interval_seconds: int
    deterministic_state: dict[str, Any]
    capability_log: tuple[dict[str, Any], ...]
    input_hash: str
    model_version: str
    interpretation: dict[str, Any] | None = None
    session_id: str | None = None
    errors: tuple[dict[str, Any], ...] = ()
    # Pass C: paper-centered hypothesis validation. The combined formula
    # ΔP_k = α_i + c·OFI_k/AD_i^λ + (ν_i·OFI_k + ε_k) remains a DERIVED
    # statistical hypothesis (heteroskedastic ν·OFI), never a shortcut
    # single calculation. hypothesis_verdict is the deterministic
    # validation of the LLM's H0/H1 against the fits.
    hypothesis: dict[str, Any] | None = None
    hypothesis_verdict: str | None = None  # validated|invalidated|inconclusive
    verdict_reason: str | None = None
    calculations: dict[str, Any] | None = None  # {ofi_blocks, ad_blocks, derived_diagnostic}
    schema_version: int = INFERENCE_ARTIFACT_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        venue: str,
        generated_at: str,
        completed_at: str,
        status: InferenceStatus,
        window_minutes: int,
        interval_seconds: int,
        deterministic_state: dict[str, Any],
        capability_log: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        input_hash: str,
        model_version: str,
        interpretation: dict[str, Any] | None = None,
        session_id: str | None = None,
        errors: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        hypothesis: dict[str, Any] | None = None,
        hypothesis_verdict: str | None = None,
        verdict_reason: str | None = None,
        calculations: dict[str, Any] | None = None,
    ) -> "InferenceArtifact":
        created = cls(
            artifact_id=str(uuid.uuid4()),
            symbol=symbol.upper(),
            venue=venue,
            generated_at=generated_at,
            completed_at=completed_at,
            status=status,
            window_minutes=window_minutes,
            interval_seconds=interval_seconds,
            deterministic_state=deterministic_state,
            capability_log=tuple(capability_log),
            input_hash=input_hash,
            model_version=model_version,
            interpretation=interpretation,
            session_id=session_id,
            errors=tuple(errors),
            hypothesis=hypothesis,
            hypothesis_verdict=hypothesis_verdict,
            verdict_reason=verdict_reason,
            calculations=calculations,
        )
        # Construction-time enforcement: an insufficient artifact with a
        # non-NULL interpretation can never be created through the sanctioned
        # factory — the hard gate is structural, not advisory.
        created.validate()
        return created

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "InferenceArtifact":
        artifact = cls(
            artifact_id=str(value["artifact_id"]),
            symbol=str(value["symbol"]).upper(),
            venue=str(value["venue"]),
            generated_at=str(value["generated_at"]),
            completed_at=str(value["completed_at"]),
            status=value["status"],
            window_minutes=int(value["window_minutes"]),
            interval_seconds=int(value["interval_seconds"]),
            deterministic_state=dict(value.get("deterministic_state") or {}),
            capability_log=tuple(value.get("capability_log") or ()),
            input_hash=str(value["input_hash"]),
            model_version=str(value["model_version"]),
            interpretation=(
                dict(value["interpretation"])
                if value.get("interpretation") is not None else None
            ),
            session_id=(
                str(value["session_id"])
                if value.get("session_id") is not None else None
            ),
            errors=tuple(value.get("errors") or ()),
            hypothesis=dict(value["hypothesis"]) if value.get("hypothesis") is not None else None,
            hypothesis_verdict=value.get("hypothesis_verdict"),
            verdict_reason=value.get("verdict_reason"),
            calculations=dict(value["calculations"]) if value.get("calculations") is not None else None,
            schema_version=int(
                value.get("schema_version", INFERENCE_ARTIFACT_SCHEMA_VERSION)
            ),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if self.schema_version != INFERENCE_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported inference artifact schema version: {self.schema_version}"
            )
        if not self.artifact_id or not self.symbol or not self.venue:
            raise ValueError("artifact_id, symbol, and venue are required")
        if self.status not in ValidInferenceStatus:
            raise ValueError(f"invalid inference status: {self.status}")
        if self.window_minutes <= 0 or self.interval_seconds <= 0:
            raise ValueError("window_minutes and interval_seconds must be positive")
        if self.interpretation is not None and self.status == "insufficient":
            raise ValueError(
                "an insufficient artifact must carry a NULL interpretation "
                "(hard gate: no LLM narration over gate-failed data)"
            )
        if not isinstance(self.deterministic_state, dict):
            raise ValueError("deterministic_state must be an object")
        if self.hypothesis_verdict is not None and self.hypothesis_verdict not in ("validated", "invalidated", "inconclusive"):
            raise ValueError(f"invalid hypothesis_verdict: {self.hypothesis_verdict}")
        # Validator hardening (2026-09-10): interpretation confidence must be
        # the contract enum (aliases coerce, blends go conservative). Past
        # rows with "low-medium" read back as "low" via normalize.
        if self.interpretation is not None:
            conf = self.interpretation.get("confidence")
            if conf is not None and normalize_confidence(conf) is None:
                raise ValueError(
                    f"interpretation.confidence must be one of {ValidConfidence!r}, got {conf!r}"
                )
            evidence = self.interpretation.get("evidence")
            if isinstance(evidence, list):
                for i, entry in enumerate(evidence):
                    if not isinstance(entry, dict):
                        raise ValueError(f"interpretation.evidence[{i}] must be an object")
                    if not str(entry.get("path") or "").strip():
                        raise ValueError(f"interpretation.evidence[{i}] missing required field: path")
                    if not str(entry.get("interpretation") or "").strip():
                        raise ValueError(
                            f"interpretation.evidence[{i}] missing required field: interpretation"
                        )
        # Combined formula is derived hypothesis only — never a shortcut stored as deterministic prediction.
        if self.calculations and self.calculations.get("combined_prediction") is not None:
            raise ValueError("calculations.combined_prediction must not be stored as deterministic prediction; use derived_diagnostic")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "symbol": self.symbol,
            "venue": self.venue,
            "generated_at": self.generated_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "window_minutes": self.window_minutes,
            "interval_seconds": self.interval_seconds,
            "deterministic_state": self.deterministic_state,
            "capability_log": list(self.capability_log),
            "input_hash": self.input_hash,
            "model_version": self.model_version,
            "interpretation": self.interpretation,
            "session_id": self.session_id,
            "errors": list(self.errors),
            "hypothesis": self.hypothesis,
            "hypothesis_verdict": self.hypothesis_verdict,
            "verdict_reason": self.verdict_reason,
            "calculations": self.calculations,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))
