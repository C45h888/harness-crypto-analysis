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

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from market_service.microstructure import fitting
from market_service.runtime.contracts import WAKE_ENVELOPE_SCHEMA_VERSION, WakeEnvelope
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


# ---------------------------------------------------------------------------
# Wake plane — deterministic trigger evaluation + durable wake stream
#
# The engine is event-driven, never lazily polled. A trigger evaluation is a
# PURE function over (current Redis counters, last artifact high-water, wake
# config). When a predicate fires, the host materializes a typed
# ``WakeEnvelope`` and XADDs it to the durable wake stream. The engine drains
# pending envelopes, coalesces them into ONE cycle, dedupes by wake_id, and
# re-validates the counters against live Redis before doing expensive work
# (two-phase wake: envelope asserts, engine verifies).
# ---------------------------------------------------------------------------

# Default: how many new best-quote events justify a fresh inference cycle.
DEFAULT_EVENT_DELTA_THRESHOLD = 1_800  # ~one 30-min block at ~1 event/sec

# Runtime hygiene: minimum seconds between engine cycles regardless of wakes.
DEFAULT_CYCLE_COOLDOWN_S = 60

WAKE_SCHEMA_VERSION = WAKE_ENVELOPE_SCHEMA_VERSION


@dataclass(frozen=True)
class WakeConfig:
    """Frozen trigger thresholds for one engine host."""

    event_delta_threshold: int = DEFAULT_EVENT_DELTA_THRESHOLD
    cooldown_seconds: int = DEFAULT_CYCLE_COOLDOWN_S


@dataclass(frozen=True)
class CounterSnapshot:
    """Point-in-time Redis counter state for trigger evaluation."""

    event_stream_len: int
    capture_state: str | None
    last_artifact_events_total: int | None
    last_artifact_capture_state: str | None
    last_artifact_completed_at_ms: int | None


def evaluate_triggers(
    snapshot: CounterSnapshot,
    config: WakeConfig,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Pure trigger evaluation: which wake predicates fire, if any.

    Returns ``{"fired": bool, "predicates": {name: detail}, "snapshot": {...}}``
    — identical inputs always yield identical outputs (testable without Redis).

    Predicates:
    - ``event_delta``: ≥ threshold new events since the last artifact's
      recorded ``events_total`` high-water mark. Cold start (no prior
      artifact) with an established capture fires unconditionally — there is
      nothing to compare against and the capture tape is usable.
    - ``capture_recovery``: capture transitioned from a degraded state
      (gap/reconnecting) to running — fit-ability changed, re-infer.
    """
    predicates: dict[str, Any] = {}

    established = snapshot.capture_state in _ESTABLISHED_CAPTURE_STATES
    prior_total = snapshot.last_artifact_events_total

    if established and prior_total is None:
        # Cold start: usable capture tape, no prior artifact to compare.
        predicates["cold_start"] = {
            "capture_state": snapshot.capture_state,
            "event_stream_len": snapshot.event_stream_len,
        }
    elif established and prior_total is not None:
        delta = snapshot.event_stream_len - prior_total
        if delta >= config.event_delta_threshold:
            predicates["event_delta"] = {
                "new_events": delta,
                "threshold": config.event_delta_threshold,
                "high_water": prior_total,
                "event_stream_len": snapshot.event_stream_len,
            }

    degraded_then_running = (
        snapshot.last_artifact_capture_state in ("gap", "reconnecting")
        and snapshot.capture_state == "running"
    )
    if degraded_then_running:
        predicates["capture_recovery"] = {
            "from": snapshot.last_artifact_capture_state,
            "to": snapshot.capture_state,
        }

    return {
        "fired": bool(predicates),
        "predicates": predicates,
        "snapshot": {
            "event_stream_len": snapshot.event_stream_len,
            "capture_state": snapshot.capture_state,
            "last_artifact_events_total": prior_total,
            "last_artifact_capture_state": snapshot.last_artifact_capture_state,
            "last_artifact_completed_at_ms": snapshot.last_artifact_completed_at_ms,
            "now_ms": now_ms,
        },
    }


def wake_dedupe_id(
    symbol: str, venue: str, predicates: dict[str, Any], high_water: dict[str, Any],
) -> str:
    """Deterministic wake_id: identical conditions collapse to one wake."""
    payload = {
        "symbol": symbol.upper(), "venue": venue,
        "predicates": predicates, "high_water": high_water,
        "schema_version": WAKE_SCHEMA_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "wake-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def build_wake_envelope(
    *,
    symbol: str,
    venue: str,
    trigger_source: str,
    evaluation: dict[str, Any],
) -> WakeEnvelope:
    """Materialize one typed wake envelope from a trigger evaluation."""
    return WakeEnvelope.create(
        symbol=symbol,
        venue=venue,
        trigger_source=trigger_source,
        predicates_fired=evaluation["predicates"],
        counter_snapshot=evaluation["snapshot"],
        high_water={
            "events_total": evaluation["snapshot"].get("last_artifact_events_total"),
            "completed_at_ms": evaluation["snapshot"].get("last_artifact_completed_at_ms"),
        },
    )


async def publish_wake(
    store: RedisRuntimeStore, envelope: WakeEnvelope, *, maxlen: int | None = None,
) -> str:
    """XADD one wake envelope to the durable wake stream (never pub/sub)."""
    key = store.inference_wake_stream(envelope.symbol, envelope.venue)
    return str(await store.redis.xadd(
        key,
        {"payload": envelope.to_json(), "wake_id": envelope.wake_id,
         "trigger_source": envelope.trigger_source},
        maxlen=maxlen or store.stream_maxlen, approximate=True,
    ))


def coalesce_wakes(
    envelopes: list[WakeEnvelope], *,
    cooldown_seconds: int, now_ms: int,
    last_cycle_completed_at_ms: int | None,
) -> tuple[WakeEnvelope | None, dict[str, Any]]:
    """Drain + merge pending wakes into ONE cycle decision.

    Returns ``(merged_envelope_or_None, decision_meta)``. Merging keeps the
    union of predicates; the meta records which wake_ids were consumed and
    why the batch was accepted or deferred.

    Deferral rules (never silently dropped — recorded on the meta):
    - cooldown: last cycle completed less than ``cooldown_seconds`` ago.
    - empty: no pending wakes at all.
    """
    consumed_ids = [w.wake_id for w in envelopes]
    meta: dict[str, Any] = {
        "pending_wake_count": len(envelopes),
        "consumed_wake_ids": consumed_ids,
        "cooldown_seconds": cooldown_seconds,
    }
    if not envelopes:
        meta["decision"] = "no_pending_wakes"
        return None, meta
    if (last_cycle_completed_at_ms is not None
            and (now_ms - last_cycle_completed_at_ms) < cooldown_seconds * 1_000):
        meta["decision"] = "cooldown"
        meta["last_cycle_completed_at_ms"] = last_cycle_completed_at_ms
        return None, meta

    predicates: dict[str, Any] = {}
    sources: set[str] = set()
    for envelope in envelopes:
        sources.add(envelope.trigger_source)
        for name, detail in envelope.predicates_fired.items():
            if name not in predicates:
                predicates[name] = detail
            elif isinstance(detail, dict) and isinstance(predicates[name], dict):
                # Keep the strongest detail for repeated predicates (e.g. the
                # largest event_delta seen across the batch).
                if "new_events" in detail and "new_events" in predicates[name]:
                    if detail["new_events"] > predicates[name]["new_events"]:
                        predicates[name] = detail
                else:
                    predicates[name] = detail
            else:
                predicates[name] = detail
    merged = WakeEnvelope.create(
        symbol=envelopes[-1].symbol,
        venue=envelopes[-1].venue,
        trigger_source="watcher" if len(sources) > 1 else next(iter(sources)),
        predicates_fired=predicates,
        counter_snapshot=envelopes[-1].counter_snapshot,
        high_water=envelopes[-1].high_water,
    )
    meta["decision"] = "fire"
    meta["merged_sources"] = sorted(sources)
    return merged, meta


async def read_pending_wakes(
    store: RedisRuntimeStore, symbol: str, venue: str, *, count: int = 50,
) -> list[WakeEnvelope]:
    """Read pending wake envelopes from the durable wake stream."""
    key = store.inference_wake_stream(symbol, venue)
    rows = await store.redis.xrevrange(key, count=count)
    envelopes: list[WakeEnvelope] = []
    for _entry_id, fields in rows or []:
        raw = (fields or {}).get("payload")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            continue
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, dict):
            continue
        try:
            envelopes.append(WakeEnvelope.from_mapping(decoded))
        except (KeyError, ValueError, TypeError):
            continue
    return envelopes


def revalidate_wake(
    envelope: WakeEnvelope, snapshot: CounterSnapshot,
) -> tuple[bool, dict[str, Any]]:
    """Two-phase wake: verify the envelope's assertion against LIVE counters.

    Called at dispatch, after drain. A wake asserting ``event_delta`` must
    still hold (the event stream has not been consumed by another cycle); a
    ``capture_recovery``/``cold_start`` wake holds if capture is established.
    Returns (holds, detail) — a stale wake is dropped WITHOUT running a cycle
    and the drop is recorded.
    """
    detail: dict[str, Any] = {"wake_id": envelope.wake_id}
    if snapshot.capture_state not in _ESTABLISHED_CAPTURE_STATES:
        detail["reason"] = "capture_not_established"
        detail["capture_state"] = snapshot.capture_state
        return False, detail
    if "event_delta" in envelope.predicates_fired:
        asserted = envelope.predicates_fired["event_delta"]
        prior_total = asserted.get("high_water")
        if prior_total is None:
            detail["reason"] = "event_delta_missing_high_water"
            return False, detail
        delta = snapshot.event_stream_len - prior_total
        detail["revalidated_delta"] = delta
        detail["asserted_delta"] = asserted.get("new_events")
        if delta < DEFAULT_EVENT_DELTA_THRESHOLD // 2:
            # Another cycle already consumed most of this delta.
            detail["reason"] = "event_delta_consumed"
            return False, detail
    detail["reason"] = "holds"
    return True, detail


# ---------------------------------------------------------------------------
# Tool base (Pass B2) — the agent's commandable calculation surface.
#
# Tools are named, scope-validated registry entries the LLM can COMMAND in
# its narration call (it never recomputes; it dispatches). Every dispatch is
# deterministic, produces a capability_log audit entry, and returns
# JSON-transportable output. Two families:
#
#   T1 micro   — the Pass-3 paper-derived microstructure stack
#   T2 market  — the canonical pipeline's calculation groups + ledger reads
#
# Tool dispatch may run at most ONE round per narration cycle (spec §4);
# results are cited evidence, never memory.
# ---------------------------------------------------------------------------

TOOL_NAMES: dict[str, str] = {
    # T1 — microstructure (paper stack)
    "micro.capture_status": "redis.read_capture_status",
    "micro.events": "redis.read_events",
    "micro.ofi_intervals": "redis.read_intervals",
    "micro.replay": "fitting.replay",
    "micro.fit_beta": "fitting.assemble_evidence",
    "micro.evidence": "redis.read_evidence",
    # T2 — market correlation (canonical pipeline seams)
    "market.envelope": "market.read_envelope",
    "market.group": "market.run_group",
    "market.derivatives": "market.read_derivatives",
    "market.keystone_history": "market.read_keystone_history",
    "market.wall_history": "market.read_wall_history",
}

# Registry additions for the T2 read tools (T1 already registered in Pass A).
_MARKET_TOOLS: dict[str, Capability] = {
    name: Capability(
        name=name,
        description=description,
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    )
    for name, description in {
        "redis.read_intervals": "Read completed OFI interval rows from the capture ledger.",
        "redis.read_evidence": "Read the latest immutable MicrostructureEvidence projection.",
        "market.read_envelope": "Read the latest collated MarketRunEnvelope for the symbol.",
        "market.run_group": "Run one calculation-model group (wall/flow/structure/positioning) fresh from the raw Redis window; never persists.",
        "market.read_derivatives": "Read the cached derivative evidence (funding, OI, cross-asset).",
        "market.read_keystone_history": "Read the bounded keystone cross-cycle ledger.",
        "market.read_wall_history": "Read the bounded wall cross-cycle ledger.",
    }.items()
}
CAPABILITIES.update(_MARKET_TOOLS)


def _bounded(values: list[Any], cap: int) -> list[Any]:
    """Hard output bound for tool payloads (context-budget discipline)."""
    return values[:cap]


async def dispatch_read_intervals(
    store: RedisRuntimeStore, symbol: str, venue: str, *, count: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: micro.ofi_intervals — completed OFI interval rows."""
    cap = CAPABILITIES["redis.read_intervals"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        rows = await store.read_microstructure_intervals(
            venue, symbol.upper(), count=count,
        )
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_evidence(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: micro.evidence — latest immutable evidence object."""
    cap = CAPABILITIES["redis.read_evidence"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        evidence = await store.read_microstructure_evidence(venue, symbol.upper())
        return evidence, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_envelope(
    store: RedisRuntimeStore, symbol: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.envelope — latest collated canonical envelope."""
    cap = CAPABILITIES["market.read_envelope"]
    scope = {"symbol": symbol.upper()}
    try:
        cap.validate_scope(symbol, "spot")
        envelope = await store.read_latest_run(symbol.upper())
        return (
            envelope.to_dict() if envelope is not None else None,
            capability_log_entry(cap.name, scope, "ok"),
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_derivatives(
    store: RedisRuntimeStore, symbol: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.derivatives — cached funding/OI/cross-asset evidence."""
    cap = CAPABILITIES["market.read_derivatives"]
    scope = {"symbol": symbol.upper()}
    try:
        cap.validate_scope(symbol, "spot")
        payload = await store.read_derivative_evidence(symbol.upper())
        return payload, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_keystone_history(
    store: RedisRuntimeStore, symbol: str, *, count: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: market.keystone_history — bounded cross-cycle keystone series."""
    cap = CAPABILITIES["market.read_keystone_history"]
    scope = {"symbol": symbol.upper(), "count": count}
    try:
        cap.validate_scope(symbol, "spot")
        rows = await store.read_keystone_history(symbol.upper(), count=count)
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_wall_history(
    store: RedisRuntimeStore, symbol: str, *, count: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: market.wall_history — bounded cross-cycle wall series."""
    cap = CAPABILITIES["market.read_wall_history"]
    scope = {"symbol": symbol.upper(), "count": count}
    try:
        cap.validate_scope(symbol, "spot")
        rows = await store.read_wall_history(symbol.upper(), count=count)
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_market_group(
    symbol: str, group: str, *, window_minutes: int = 15,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.group — run one calculation-model group fresh from Redis.

    Uses the pipeline's own GROUP_MAP seam (same code path as the harness
    ``--wall/--flow/--structure/--positioning`` commands): reads the raw
    Redis window, resolves section dependencies, runs the deterministic
    calculators + analysis. NEVER persists — the canonical envelope path
    stays owned by the outer CLI.
    """
    from market_service.config import Settings
    from market_service.nooa_harness import pipeline as pipeline_mod

    cap = CAPABILITIES["market.run_group"]
    scope = {"symbol": symbol.upper(), "group": group, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, "spot")
        if group not in pipeline_mod.GROUP_MAP:
            raise CapabilityDenied(
                f"unknown group {group!r}; allowed: {sorted(pipeline_mod.GROUP_MAP)}"
            )
        settings = Settings.from_redis_env()
        redis = RedisRuntimeStore(
            settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
        )
        try:
            evidence = await pipeline_mod.read_raw_window(
                redis, symbol.upper(), window_minutes,
            )
            calc_sections, analysis_sections = pipeline_mod.sections_for_groups((group,))
            calculations = pipeline_mod.run_calculations(
                evidence,
                depth=evidence.get("depth_levels") or 20,
                window=window_minutes,
                sections=calc_sections,
            )
            analysis = pipeline_mod.run_analysis(
                evidence, calculations, sections=analysis_sections,
            )
            result = {
                "group": group,
                "window_minutes": window_minutes,
                "calculations": calculations,
                "analysis": analysis,
            }
            return result, capability_log_entry(cap.name, scope, "ok")
        finally:
            await redis.close()
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def execute_tool(
    store: RedisRuntimeStore, name: str, args: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Execute one tool call by public name with scope validation + audit.

    This is the single entry point the narration loop uses for the LLM's
    ``tool_calls``. Unknown tool names and out-of-scope dispatches return a
    structured ``denied`` result — never an exception to the caller.
    """
    if name not in TOOL_NAMES:
        return None, capability_log_entry(
            "tool.unknown", {"name": name}, "denied",
            detail=f"unknown tool; allowed: {sorted(TOOL_NAMES)}",
        )
    symbol = str(args.get("symbol", "")).upper()
    venue = str(args.get("venue", "spot"))
    if name == "micro.capture_status":
        return await dispatch_read_capture_status(store, symbol, venue)
    if name == "micro.events":
        return await dispatch_read_events(
            store, symbol, venue, count=int(args.get("count") or 200),
        )
    if name == "micro.ofi_intervals":
        return await dispatch_read_intervals(
            store, symbol, venue, count=int(args.get("count") or 200),
        )
    if name == "micro.replay":
        payloads = list(args.get("event_payloads") or [])
        return dispatch_replay(
            payloads, symbol=symbol, venue=venue,
            interval_ms=int(args.get("interval_ms") or 10_000),
        )
    if name == "micro.fit_beta":
        return await _tool_fit_beta(store, symbol, venue, args)
    if name == "micro.evidence":
        return await dispatch_read_evidence(store, symbol, venue)
    if name == "market.envelope":
        return await dispatch_read_envelope(store, symbol)
    if name == "market.group":
        return await dispatch_market_group(
            symbol, str(args.get("group") or "flow"),
            window_minutes=int(args.get("window_minutes") or 15),
        )
    if name == "market.derivatives":
        return await dispatch_read_derivatives(store, symbol)
    if name == "market.keystone_history":
        return await dispatch_read_keystone_history(
            store, symbol, count=int(args.get("count") or 100),
        )
    if name == "market.wall_history":
        return await dispatch_read_wall_history(
            store, symbol, count=int(args.get("count") or 100),
        )
    return None, capability_log_entry(
        "tool.unrouted", {"name": name}, "denied", detail="no dispatch path",
    )


async def _tool_fit_beta(
    store: RedisRuntimeStore, symbol: str, venue: str, args: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: micro.fit_beta — replay + fit over the windowed event ledger.

    Reads events, replays deterministically, assembles evidence (primary β +
    sensitivity + depth-scaling-over-prior-evidence). Window anchoring is the
    last captured event (reproducible), mirroring the CLI fit path.
    """
    from decimal import Decimal

    from market_service.microstructure import fitting as fitting_mod

    cap = CAPABILITIES["fitting.assemble_evidence"]
    scope = {
        "symbol": symbol.upper(), "venue": venue,
        "interval_seconds": int(args.get("interval_seconds") or 10),
        "window_minutes": int(args.get("window_minutes") or 30),
    }
    try:
        cap.validate_scope(symbol, venue)
        interval_s = int(args.get("interval_seconds") or 10)
        window_m = int(args.get("window_minutes") or 30)
        tick = Decimal(str(args.get("tick_size") or "0.01"))

        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fitting_mod.replay_events_from_payloads(payloads)
        if len(events) < 2:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "insufficient", "reason": "fewer than 2 events"},
            )
        window_ms = window_m * 60_000
        end_ts = events[-1].current.exchange_ts_ms
        windowed = [
            e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms
        ]
        intervals = fitting_mod.replay_intervals(windowed, interval_ms=interval_s * 1_000)
        if intervals and intervals[-1].end_ts_ms > end_ts:
            intervals = intervals[:-1]
        fit_config = {
            "symbol": symbol, "venue": venue, "tick_size": str(tick),
            "interval_seconds": interval_s, "window_minutes": window_m,
        }
        evidence_id = f"ev-{fitting_mod.input_hash(intervals, fit_config)[:16]}"
        evidence = fitting_mod.assemble_evidence(
            intervals, symbol=symbol, venue=venue, tick_size=tick,
            interval_seconds=interval_s,
            evidence_id=evidence_id,
            generated_at_ms=end_ts,
            events=windowed,
            coverage={
                "events_total": len(events),
                "events_in_window": len(windowed),
                "events_dropped_on_decode": dropped,
                "intervals_closed": len(intervals),
            },
        )
        return evidence.to_dict(), capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": evidence.status, "intervals": len(intervals)},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


__all__ = [
    "CAPABILITIES",
    "TOOL_NAMES",
    "Capability",
    "CapabilityDenied",
    "CounterSnapshot",
    "WakeConfig",
    "build_wake_envelope",
    "capability_log_entry",
    "coalesce_wakes",
    "dispatch_assemble_evidence",
    "dispatch_read_capture_status",
    "dispatch_read_events",
    "dispatch_replay",
    "evaluate_triggers",
    "execute_tool",
    "gate_interpretation",
    "publish_wake",
    "read_pending_wakes",
    "resolve_inference_status",
    "revalidate_wake",
    "wake_dedupe_id",
]


# ---------------------------------------------------------------------------
# Wake dispatcher — the seam that CLOSES the loop (Slice 1)
#
# The wake worker owns the trigger plane and hands the engine a typed
# ``WakeEnvelope``. This dispatcher is the async callable the worker invokes
# on a fire. Slice 2 replaces the placeholder body with the full engine
# cycle (gather -> gate -> narrate -> persist); today it records the wake
# so the loop is observable end-to-end without an LLM.
# ---------------------------------------------------------------------------


async def default_wake_dispatcher(
    envelope: WakeEnvelope,
    *,
    store: RedisRuntimeStore | None = None,
) -> dict[str, Any]:
    """Minimal deterministic wake handler (no LLM): record + return.

    Called on every firing wake. Persists a structured record of the wake
    (its predicates, high-water, and the deterministic wake_id) to the
    inference stream so a human or an operator can audit what fired and
    when — this is the informational journal, NOT load-bearing control flow
    (the retired ``publish_wake``/``read_pending_wakes`` transport is gone).
    Slice 2 swaps the body for the real engine cycle.
    """
    record = {
        "schema_version": envelope.schema_version,
        "wake_id": envelope.wake_id,
        "symbol": envelope.symbol,
        "venue": envelope.venue,
        "trigger_source": envelope.trigger_source,
        "predicates_fired": envelope.predicates_fired,
        "high_water": envelope.high_water,
        "created_at": envelope.created_at,
    }
    out = {"dispatched": True, "record": record}
    if store is not None:
        try:
            await store.publish_wake_record(record)
            out["journal"] = "ok"
        except Exception as exc:
            out["journal"] = f"{type(exc).__name__}: {exc}"
    return out
