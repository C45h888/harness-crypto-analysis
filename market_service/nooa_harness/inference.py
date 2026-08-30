"""Inference-engine mechanics: hard status gate + bounded capability registry.

This module is the mechanical foundation of the agent role shift: the OO
agent moves from passive interpretation layer to statistical inference
engine. Two disciplines make that safe:

1. HARD STATUS GATE — the trichotomy (validated / provisional /
   insufficient) is resolved DETERMINISTICALLY from the inputs, before any
   LLM call is even considered. ``insufficient`` forces a NULL
   interpretation: the engine persists the refusal as durable state and
   spends zero tokens narrating gate-failed data. Null discipline — an
   empty interpretation means "not produced", never "nothing to say".

2. CAPABILITY REGISTRY — the engine's authority over supporting modules is
   exercised ONLY through named, scope-validated capabilities. Every
   dispatch produces an audit log entry that lands in the artifact's
   ``capability_log``, so each artifact is self-documenting about how its
   deterministic state was produced. Out-of-scope requests are denied
   before any module runs.

This module is nooa-free: it imports only runtime contracts and the
deterministic microstructure stack, so contract tests never pay the
litellm import cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from market_service.microstructure import fitting
from market_service.runtime.redis_store import RedisRuntimeStore

# ---------------------------------------------------------------------------
# Hard status gate
# ---------------------------------------------------------------------------

# Capture states that count as "established" — the capture produced a usable
# tape. Any other state (starting, stopped, None) refuses inference.
_ESTABLISHED_CAPTURE_STATES = frozenset({"running", "gap", "reconnecting", "connected"})


class GateInputs:
    """Named input bundle for the hard gate (plain object, no validation)."""


def resolve_inference_status(
    *,
    n_observations: int,
    min_observations: int,
    fit_status: str | None,
    capture_state: str | None,
    events_in_window: int,
    sequence_gaps: int = 0,
) -> tuple[str, tuple[str, ...]]:
    """Deterministically resolve the artifact status trichotomy.

    Returns ``(status, reasons)`` where reasons is the ordered tuple of gate
    failures/warnings that drove the decision. Identical inputs always yield
    identical outputs — this is a pure function, safe to re-run on replay.

    Rules (evaluated in order):
    - insufficient: fewer usable observations than the minimum; the
      underlying fit is insufficient; capture never established; or fewer
      than 2 events in the window.
    - provisional: the fit is provisional; sequence gaps occurred during
      capture; or observation count is below twice the minimum.
    - validated: none of the above.
    """
    reasons: list[str] = []

    if n_observations < min_observations:
        reasons.append(
            f"observations {n_observations} < minimum {min_observations}"
        )
    if fit_status == "insufficient":
        reasons.append("underlying price-impact fit is insufficient")
    if capture_state not in _ESTABLISHED_CAPTURE_STATES:
        reasons.append(f"capture state {capture_state!r} is not established")
    if events_in_window < 2:
        reasons.append(f"only {events_in_window} events in window")
    if reasons:
        return "insufficient", tuple(reasons)

    provisional_reasons: list[str] = []
    if fit_status == "provisional":
        provisional_reasons.append("underlying fit is provisional")
    if sequence_gaps > 0:
        provisional_reasons.append(f"{sequence_gaps} sequence gap(s) during capture")
    if n_observations < 2 * min_observations:
        provisional_reasons.append(
            f"observations {n_observations} < 2x minimum {2 * min_observations}"
        )
    if provisional_reasons:
        return "provisional", tuple(provisional_reasons)

    return "validated", ()


def gate_interpretation(status: str, interpretation: dict[str, Any] | None) -> dict[str, Any] | None:
    """Enforce the NULL-interpretation discipline at the constructor boundary.

    An ``insufficient`` artifact can never carry an interpretation — the hard
    gate guarantees no LLM call happened over gate-failed data. Passing one
    through here neutralizes it instead of raising, so engine code paths can
    be written uniformly.
    """
    if status == "insufficient":
        return None
    return interpretation


# ---------------------------------------------------------------------------
# Capability registry — the engine's bounded authority over supporting modules
# ---------------------------------------------------------------------------


class CapabilityDenied(ValueError):
    """A capability dispatch was refused by scope or quality validation."""


@dataclass(frozen=True)
class Capability:
    """One named, scope-bounded supporting-module dispatch surface."""

    name: str
    description: str
    allowed_symbols: frozenset[str]
    allowed_venues: frozenset[str]

    def validate_scope(self, symbol: str, venue: str) -> None:
        if symbol.upper() not in self.allowed_symbols:
            raise CapabilityDenied(
                f"capability {self.name!r} denied: symbol {symbol} outside "
                f"bounded scope {sorted(self.allowed_symbols)}"
            )
        if venue not in self.allowed_venues:
            raise CapabilityDenied(
                f"capability {self.name!r} denied: venue {venue} outside "
                f"bounded scope {sorted(self.allowed_venues)}"
            )


# Frozen initial scope — mirrors the Pass-3 bounded request validator.
_INITIAL_SYMBOLS = frozenset({"BTCUSDT"})
_INITIAL_VENUES = frozenset({"spot"})

CAPABILITIES: dict[str, Capability] = {
    "redis.read_capture_status": Capability(
        name="redis.read_capture_status",
        description="Read the isolated microstructure capture status object.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
    "redis.read_events": Capability(
        name="redis.read_events",
        description="Read best-quote transition events from the capture ledger.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
    "fitting.replay": Capability(
        name="fitting.replay",
        description="Deterministically replay events into OFI intervals.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
    "fitting.assemble_evidence": Capability(
        name="fitting.assemble_evidence",
        description="Run the deterministic beta/c/lambda fitter over replayed intervals.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
}


def capability_log_entry(
    name: str, scope: dict[str, Any], result: str, *, detail: Any = None,
) -> dict[str, Any]:
    """One audit-trail row for the artifact's ``capability_log``."""
    entry: dict[str, Any] = {"capability": name, "scope": scope, "result": result}
    if detail is not None:
        entry["detail"] = detail
    return entry


async def dispatch_read_capture_status(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Capability: redis.read_capture_status."""
    cap = CAPABILITIES["redis.read_capture_status"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        status = await store.read_microstructure_status(venue, symbol.upper())
        return status, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_events(
    store: RedisRuntimeStore, symbol: str, venue: str, *, count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Capability: redis.read_events."""
    cap = CAPABILITIES["redis.read_events"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(
            venue, symbol.upper(), count=count,
        )
        return payloads, capability_log_entry(
            cap.name, scope, "ok", detail={"entries": len(payloads)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


def dispatch_replay(
    event_payloads: list[dict[str, Any]], *, symbol: str, venue: str, interval_ms: int,
) -> tuple[list[Any], list[Any], int, dict[str, Any]]:
    """Capability: fitting.replay — pure, synchronous.

    Returns (events, intervals, dropped_count, log_entry).
    """
    cap = CAPABILITIES["fitting.replay"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_ms": interval_ms}
    try:
        cap.validate_scope(symbol, venue)
        events, dropped = fitting.replay_events_from_payloads(event_payloads)
        intervals = fitting.replay_intervals(events, interval_ms=interval_ms)
        return events, intervals, dropped, capability_log_entry(
            cap.name, scope, "ok",
            detail={"events": len(events), "intervals": len(intervals), "dropped": dropped},
        )
    except CapabilityDenied as exc:
        return [], [], 0, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


def dispatch_assemble_evidence(
    intervals: list[Any], *, symbol: str, venue: str, tick_size: Any,
    interval_seconds: int, evidence_id: str, generated_at_ms: int,
    events: list[Any] | None = None, coverage: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Capability: fitting.assemble_evidence — pure, synchronous.

    Returns (evidence_or_None, log_entry). A CapabilityDenied scope refusal
    yields (None, denied-entry) without touching the fitter.
    """
    cap = CAPABILITIES["fitting.assemble_evidence"]
    scope = {"symbol": symbol.upper(), "venue": venue,
             "interval_seconds": interval_seconds}
    try:
        cap.validate_scope(symbol, venue)
        evidence = fitting.assemble_evidence(
            intervals, symbol=symbol, venue=venue, tick_size=tick_size,
            interval_seconds=interval_seconds, evidence_id=evidence_id,
            generated_at_ms=generated_at_ms, events=events, coverage=coverage,
        )
        return evidence, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


__all__ = [
    "CAPABILITIES",
    "Capability",
    "CapabilityDenied",
    "capability_log_entry",
    "dispatch_assemble_evidence",
    "dispatch_read_capture_status",
    "dispatch_read_events",
    "dispatch_replay",
    "gate_interpretation",
    "resolve_inference_status",
]
