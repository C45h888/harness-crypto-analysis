"""Explicit contract validation for canonical analysis/calculation adapters.

Moved from ``market_service.nodes.contracts``. Used by the harness-owned
pipeline to validate input shapes before calling pure math/analysis functions.

Each canonical function in ``market_service.analysis`` and
``market_service.calculations`` documents an input shape. Adapters MUST
build that exact shape before calling the function. When the shape cannot
be built, the adapter MUST raise :class:`ContractViolation` rather than
silently swallowing the failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContractViolation(Exception):
    """Raised when an adapter cannot produce the shape a canonical function requires."""

    def __init__(self, function: str, reason: str, details: dict[str, Any] | None = None):
        self.function = function
        self.reason = reason
        self.details = details or {}
        super().__init__(f"{function}: {reason}")


def as_contract_error(exc: BaseException, function: str) -> ContractViolation:
    if isinstance(exc, ContractViolation):
        return exc
    return ContractViolation(function=function, reason=f"{type(exc).__name__}: {exc}", details={})


def strict_call(function_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ContractViolation:
        raise
    except Exception as exc:
        raise as_contract_error(exc, function_name) from exc


def require_key(value: Any, *keys: str, function: str, where: str = "") -> Any:
    cursor: Any = value
    path = where or "input"
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            raise ContractViolation(
                function=function,
                reason=f"missing required key '{'.'.join(keys[: keys.index(key) + 1])}' in {path}",
                details={"path": path, "missing_key": key, "seen_type": type(value).__name__},
            )
        cursor = cursor[key]
    return cursor


def require_list_of_pairs(
    levels: Any, *, function: str, where: str, max_items: int | None = None,
) -> list[list[float]]:
    if levels is None:
        return []
    if not isinstance(levels, Iterable):
        raise ContractViolation(
            function=function,
            reason=f"{where} must be iterable of [price, qty] pairs",
            details={"where": where, "got_type": type(levels).__name__},
        )
    out: list[list[float]] = []
    for i, row in enumerate(levels):
        if max_items is not None and i >= max_items:
            break
        if isinstance(row, dict):
            price = row.get("price") or row.get("p")
            qty = row.get("qty") or row.get("q")
            if price is None or qty is None:
                continue
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, qty = row[0], row[1]
        else:
            continue
        try:
            out.append([float(price), float(qty)])
        except (TypeError, ValueError):
            continue
    return out


def require_float(
    value: Any, *, function: str, where: str, default: float | None = None,
) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            function=function,
            reason=f"{where} must be a float-compatible number",
            details={"where": where, "got_type": type(value).__name__},
        ) from exc


def contract_error_entry(violation: ContractViolation) -> dict[str, Any]:
    return {
        "function": violation.function,
        "error": violation.reason,
        "details": violation.details,
    }


# ---------------------------------------------------------------------------
# Specialized group envelopes — the interpretation-plane read interface.
#
# One typed envelope per calculation-model group (wall / flow / structure /
# positioning). The harness reads the raw evidence window DIRECTLY from the
# Redis store, runs only that group's calculation + analysis sections, and
# emits a GroupEnvelope sized to fit an LLM context BY CONSTRUCTION (arrays
# are capped at emission with explicit markers — never the silent collapse
# the monolithic envelope suffered under bounded_envelope_view).
#
# The canonical MarketRunEnvelope stays the persisted audit record in
# Postgres; GroupEnvelopes are the read/interpretation interface. When
# derived from a canonical envelope (canonical_projection source) they carry
# its run_id so every specialist claim stays audit-linked to one run.
# ---------------------------------------------------------------------------

GROUP_ENVELOPE_SCHEMA_VERSION = 1
GROUP_KINDS: tuple[str, ...] = ("wall", "flow", "structure", "positioning")


class GroupEnvelopeError(ValueError):
    """Raised on an invalid group kind or schema version."""


@dataclass(frozen=True)
class GroupEnvelope:
    """One specialized read envelope for a single calculation-model group.

    ``kind`` selects the group (GROUP_MAP in calculations.composition).
    ``calculations`` / ``analysis`` carry ONLY that group's sections.
    ``evidence_headlines`` is the compact shared evidence context (snake_case
    scalars read from the evidence — price, funding, OI — never raw arrays).
    ``source`` is ``group_cycle`` (built directly from the Redis raw stream)
    or ``canonical_projection`` (derived from a persisted MarketRunEnvelope,
    in which case ``run_id`` is the canonical run's id).
    """

    kind: str
    symbol: str
    status: str
    generated_at: str
    run_id: str
    window_minutes: int
    coverage: dict[str, Any]
    calculations: dict[str, Any]
    analysis: dict[str, Any]
    evidence_headlines: dict[str, Any] = field(default_factory=dict)
    errors: tuple[dict[str, Any], ...] = ()
    source: str = "canonical_projection"
    # Substrate attribution for the sections in this envelope: section id →
    # owning substrate(s) (from calculations.composition.SUBSTRATE_GRAPH). Lets the
    # interpretation plane explain WHICH calculation substrate produced each
    # section. Additive-only; older readers ignore it (schema_version 1).
    substrate_provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: int = GROUP_ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Fail fast: an invalid GroupEnvelope must never exist, even briefly.
        self.validate()

    def validate(self) -> None:
        if self.schema_version != GROUP_ENVELOPE_SCHEMA_VERSION:
            raise GroupEnvelopeError(
                f"unsupported group envelope schema version: {self.schema_version}"
            )
        if self.kind not in GROUP_KINDS:
            raise GroupEnvelopeError(
                f"invalid group kind {self.kind!r}; expected one of {GROUP_KINDS!r}"
            )
        if not self.run_id:
            raise GroupEnvelopeError("GroupEnvelope requires run_id")
        if not self.symbol:
            raise GroupEnvelopeError("GroupEnvelope requires symbol")
        if self.status not in ("healthy", "degraded", "invalid"):
            raise GroupEnvelopeError(f"invalid group envelope status: {self.status!r}")
        if self.source not in ("group_cycle", "canonical_projection"):
            raise GroupEnvelopeError(f"invalid group envelope source: {self.source!r}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "symbol": self.symbol,
            "status": self.status,
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "window_minutes": self.window_minutes,
            "coverage": self.coverage,
            "calculations": self.calculations,
            "analysis": self.analysis,
            "evidence_headlines": self.evidence_headlines,
            "errors": list(self.errors),
            "source": self.source,
            "substrate_provenance": dict(self.substrate_provenance),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, separators=(",", ":"))

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "GroupEnvelope":
        envelope = cls(
            kind=str(value.get("kind", "")),
            symbol=str(value.get("symbol", "")).upper(),
            status=str(value.get("status", "degraded")),
            generated_at=str(value.get("generated_at") or _utc_iso()),
            run_id=str(value.get("run_id") or ""),
            window_minutes=int(value.get("window_minutes") or 15),
            coverage=dict(value.get("coverage") or {}),
            calculations=dict(value.get("calculations") or {}),
            analysis=dict(value.get("analysis") or {}),
            evidence_headlines=dict(value.get("evidence_headlines") or {}),
            errors=tuple(value.get("errors") or ()),
            source=str(value.get("source") or "canonical_projection"),
            substrate_provenance=dict(value.get("substrate_provenance") or {}),
            schema_version=int(value.get("schema_version") or GROUP_ENVELOPE_SCHEMA_VERSION),
        )
        envelope.validate()
        return envelope