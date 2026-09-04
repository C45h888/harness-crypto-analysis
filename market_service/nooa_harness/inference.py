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

This module is openai-free (the client is injected via
``backends.build_llm``): it imports only runtime contracts and the
deterministic microstructure stack, so contract tests never pay the OpenAI
SDK import cost.
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


# Frozen initial scope — extended for SOL-USDT perps (per/user request).
# Microstructure capture remains spot-only (event-level tape), but
# calculation modules (market.group/read) are venue-agnostic via raw poller
# which fetches spot + USD-M perps. This lets inference validate SOL perps
# statistically even when micro evidence is spot-derived.
_INITIAL_SYMBOLS = frozenset({"BTCUSDT", "SOLUSDT", "ETHUSDT"})
_INITIAL_VENUES = frozenset({"spot", "perps", "perp", "usdm", "futures"})

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
    # T1 — microstructure (paper stack) — split AD/OFI per Pass C
    "micro.capture_status": "redis.read_capture_status",
    "micro.events": "redis.read_events",
    "micro.ofi_intervals": "redis.read_intervals",
    "micro.replay": "fitting.replay",
    "micro.fit_beta": "fitting.assemble_evidence",
    "micro.evidence": "redis.read_evidence",
    # T1 split: AD/OFI separate tools, final fit is hypothesis validation
    "calc.ofi.intervals": "calc.ofi_intervals",
    "calc.depth.average": "calc.ad_average",
    "calc.observation.build": "calc.observation_build",
    "calc.fit.price_impact": "calc.fit_price_impact",
    "calc.fit.depth_scaling": "calc.fit_depth_scaling",
    "calc.derived_diagnostic": "calc.derived_diagnostic",
    "calc.price.delta": "calc.derived_diagnostic",
    "memory.recall_paper": "memory.recall_paper",
    # T2 — market correlation (canonical pipeline seams)
    "market.read": "market.read",
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
        "market.read": "Read the latest collated market run from Redis. Modes: snapshot (bounded headline view, default), inventory (section keys + snapshot), full (raw payload deep-dive).",
        "market.run_group": "Run one calculation-model group (wall/flow/structure/positioning) fresh from the raw Redis window; never persists.",
        "market.read_derivatives": "Read the cached derivative evidence (funding, OI, cross-asset).",
        "market.read_keystone_history": "Read the bounded keystone cross-cycle ledger.",
        "market.read_wall_history": "Read the bounded wall cross-cycle ledger.",
        "calc.ofi_intervals": "Deterministic OFI per interval: sum e_n in [t_{k-1},t_k) — clock-bound, no AD. Paper Cont eq OFI_k.",
        "calc.ad_average": "Deterministic AD per block: event-average (qB+qA)/2 — separate from OFI, needs tick_size. Paper AD_i.",
        "calc.observation_build": "Join OFI intervals + AD blocks + mid → PriceImpactObservation[] (ΔP ticks vs OFI), quality filtered.",
        "calc.fit_price_impact": "OLS ΔP_k = α + β·OFI_k (HC0 SE) — returns PriceImpactFit, status trichotomy. Takes observations, not raw intervals.",
        "calc.fit_depth_scaling": "Log-log ln β = ln c - λ ln AD across blocks — needs ≥3 distinct AD_i, derived diagnostic only.",
        "calc.derived_diagnostic": "NUMERIC derived ΔP (alias: calc.price.delta): pass ofi (else latest interval OFI) → route A ΔP=α+β·OFI with 95% band + route B depth-scaled when c/λ exist. Refuses on insufficient fits. Heteroskedastic ν·OFI — diagnostic, not prediction.",
        "memory.recall_paper": "Recall Cont-Kukanov-Stoikov paper facts from real MemoryNode (kind=fact, paper-kb session) — not prompt.",
    }.items()
}
CAPABILITIES.update(_MARKET_TOOLS)


# Phase map for the staged inference cycle (engine drives P1→P5).
# Credit is by TOOL FAMILY actually executed, not by the phase the model
# declares — robust to mislabeled turns. P4 (explanation) needs no tools;
# it is validated through summary/evidence quality at finalization.
TOOL_PHASE: dict[str, str] = {
    # P1 — OFI / tape quality
    "micro.capture_status": "P1",
    "micro.events": "P1",
    "micro.ofi_intervals": "P1",
    "micro.replay": "P1",
    "calc.ofi.intervals": "P1",
    # P2 — AD / observations / fits
    "micro.fit_beta": "P2",
    "micro.evidence": "P2",
    "calc.depth.average": "P2",
    "calc.observation.build": "P2",
    "calc.fit.price_impact": "P2",
    "calc.fit.depth_scaling": "P2",
    # P3 — market correlation (Redis plane)
    "market.read": "P3",
    "market.group": "P3",
    "market.derivatives": "P3",
    "market.keystone_history": "P3",
    "market.wall_history": "P3",
    # P5 — paper grounding + derived ΔP
    "memory.recall_paper": "P5",
    "calc.derived_diagnostic": "P5",
    "calc.price.delta": "P5",
}
_PHASE_ORDER = ("P1", "P2", "P3", "P4", "P5")
_REQUIRED_PHASES = ("P1", "P2", "P3", "P5")


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


async def dispatch_market_read(
    store: RedisRuntimeStore, symbol: str, *, mode: str = "snapshot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.read — latest collated market run, raw from Redis.

    Post-envelope deviation: no dataclass round trip. One GET, one
    json.loads, one schema-version guard (read_paths), then a bounded
    projection. ``mode`` selects the agent-facing shape:

      snapshot  — headline scalars + CVD multi-window sign series (default;
                  small by construction, the primary inference view)
      inventory — section key inventory + the snapshot
      full      — the raw collated payload (explicit deep-dive; the
                  engine's 40k tool-result gate is the bound)

    A schema-version mismatch is a structured ``error`` payload, never a
    coercion — the writer is on a different contract and must escalate.
    """
    from market_service.runtime import read_paths

    cap = CAPABILITIES["market.read"]
    scope = {"symbol": symbol.upper(), "mode": mode}
    try:
        cap.validate_scope(symbol, "spot")
        if mode not in ("snapshot", "inventory", "full"):
            raise CapabilityDenied(f"unknown market.read mode: {mode!r}")
        payload = await read_paths.read_collated(store, symbol.upper())
        if payload is None:
            return None, capability_log_entry(
                cap.name, scope, "ok", detail={"status": "no_run_persisted"},
            )
        if mode == "full":
            result: dict[str, Any] = payload
        elif mode == "inventory":
            result = read_paths.market_inventory(payload)
        else:
            result = read_paths.market_snapshot(payload)
        # NaN-safe at the tool seam: the stored payload passed the write
        # gate, but projections traverse live pipeline dicts that may hold
        # raw float('nan') (pipeline flow math). The engine's json.loads
        # round trip would choke on a bare NaN token.
        result = read_paths.json_safe(result)
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"schema_version": payload.get("schema_version")},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except ValueError as exc:
        # Schema guard breach — stale writer on a different contract.
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


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


# ------------------------------------------------------------------
# Pass C split: AD/OFI separate tools + derived diagnostic
# The final formula ΔP = α + c·OFI/AD^λ + (ν·OFI+ε) remains DERIVED
# hypothesis (heteroskedastic ν·OFI), never shortcut calculation.
# ------------------------------------------------------------------

async def dispatch_calc_ofi_intervals(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_ms: int = 10_000, window_minutes: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: calc.ofi.intervals — deterministic OFI per interval (no AD)."""
    from decimal import Decimal
    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.ofi_intervals"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_ms": interval_ms, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_ms)
        # Return OFI-only projection (paper Cont OFI_k), AD stripped for split discipline
        projected = [{"start_ts_ms": i.start_ts_ms, "end_ts_ms": i.end_ts_ms, "ofi": str(i.ofi), "event_count": i.event_count, "quality": i.quality} for i in intervals]
        return _bounded(projected, 200), capability_log_entry(cap.name, scope, "ok", detail={"intervals": len(intervals), "dropped": dropped, "note": "AD excluded — use calc.depth.average separately"})
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_ad_average(
    store: RedisRuntimeStore, symbol: str, venue: str, *, window_minutes: int = 30,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.depth.average — AD per block, separate from OFI (paper AD_i)."""
    from decimal import Decimal
    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.ad_average"]
    scope = {"symbol": symbol.upper(), "venue": venue, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=10_000)
        # AD per block via DepthAverager semantics: mean (qB+qA)/2
        ads = [str(i.average_depth) if i.average_depth is not None else None for i in intervals]
        valid_ads = [a for a in ads if a is not None]
        mean_ad = str(sum(Decimal(a) for a in valid_ads) / len(valid_ads)) if valid_ads else None
        result = {"window_minutes": window_minutes, "n_intervals": len(intervals), "ad_per_interval": _bounded(ads, 200), "mean_ad": mean_ad, "depth_estimator": fm.DEPTH_ESTIMATOR, "note": "OFI excluded — use calc.ofi.intervals separately; ν·OFI heteroskedastic"}
        return result, capability_log_entry(cap.name, scope, "ok", detail={"mean_ad": mean_ad, "n": len(valid_ads)})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_observation_build(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_seconds: int = 10, window_minutes: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: calc.observation.build — join OFI+AD+ΔP → observations (ΔP ticks vs OFI)."""
    from decimal import Decimal
    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.observation_build"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_seconds": interval_seconds, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, dropped = fm.replay_events_from_payloads(payloads)
        window_ms = window_minutes * 60_000
        end_ts = events[-1].current.exchange_ts_ms if events else 0
        windowed = [e for e in events if e.current.exchange_ts_ms >= end_ts - window_ms] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_seconds*1000)
        observations, excluded = fm.build_observations(intervals, tick_size=Decimal("0.01"))
        proj = [{"ofi": str(o.ofi), "delta_ticks": str(o.delta_ticks), "average_depth": str(o.average_depth) if o.average_depth else None, "quality": o.quality} for o in observations[:50]]
        return proj, capability_log_entry(cap.name, scope, "ok", detail={"n_observations": len(observations), "excluded": excluded, "note": "ΔP = α+β·OFI observations ready for calc.fit.price_impact"})
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_fit_price_impact(
    store: RedisRuntimeStore, symbol: str, venue: str, *, interval_seconds: int = 10, window_minutes: int = 30,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.fit.price_impact — OLS ΔP=α+β·OFI (HC0), takes split observations."""
    from decimal import Decimal
    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.fit_price_impact"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_seconds": interval_seconds}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, _ = fm.replay_events_from_payloads(payloads)
        windowed = [e for e in events if e.current.exchange_ts_ms >= (events[-1].current.exchange_ts_ms - window_minutes*60_000)] if events else []
        intervals = fm.replay_intervals(windowed, interval_ms=interval_seconds*1000)
        fit, _ = fm.fit_price_impact(intervals, symbol=symbol, venue=venue, tick_size=Decimal("0.01"), interval_seconds=interval_seconds)
        return fit.to_dict(), capability_log_entry(cap.name, scope, "ok", detail={"beta": str(fit.beta), "status": fit.status})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

async def dispatch_calc_fit_depth_scaling(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.fit.depth_scaling — lnβ = ln c - λ ln AD, needs ≥3 blocks. Derived diagnostic."""
    from market_service.microstructure import fitting as fm
    cap = CAPABILITIES["calc.fit_depth_scaling"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        # For split discipline, we can only fit depth scaling from history — single window gives n_blocks=1 → insufficient by design
        payloads = await store.read_microstructure_events(venue, symbol.upper())
        events, _ = fm.replay_events_from_payloads(payloads)
        intervals = fm.replay_intervals(events, interval_ms=10_000)
        fit, _ = fm.fit_price_impact(intervals, symbol=symbol, venue=venue, tick_size=fm.DECIMAL("0.01") if hasattr(fm,"DECIMAL") else __import__("decimal").Decimal("0.01"), interval_seconds=10) if intervals else (None,None)
        if fit is None:
            return None, capability_log_entry(cap.name, scope, "ok", detail={"status": "insufficient", "reason": "no intervals"})
        depth_fit = fm.fit_depth_scaling([fit], symbol=symbol, venue=venue)
        return depth_fit.to_dict(), capability_log_entry(cap.name, scope, "ok", detail={"status": depth_fit.status, "n_blocks": depth_fit.n_blocks, "note": "derived, not prediction"})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

def _depth_fit_from_dict(data: dict[str, Any]) -> Any | None:
    """Reconstruct a DepthScalingFit from a persisted dict (PG history or fresh evidence)."""
    try:
        from decimal import Decimal

        from market_service.microstructure.contracts import DepthScalingFit

        def _dec(key: str) -> Decimal | None:
            value = data.get(key)
            return Decimal(str(value)) if value is not None else None

        return DepthScalingFit(
            fit_id=str(data.get("fit_id") or "hist-depth-unknown"),
            symbol=str(data.get("symbol") or "").upper(),
            venue=str(data.get("venue") or ""),
            c=_dec("c"),
            lambda_=_dec("lambda"),
            stderr_lambda=_dec("stderr_lambda"),
            n_blocks=int(data.get("n_blocks") or 0),
            r2=_dec("r2"),
            fit_ids=tuple(str(f) for f in (data.get("fit_ids") or ())),
            depth_estimator=str(data.get("depth_estimator") or ""),
            model_version=str(data.get("model_version") or ""),
            status=str(data.get("status") or "insufficient"),
        )
    except (ValueError, TypeError, ArithmeticError, KeyError):
        return None


async def dispatch_calc_derived_diagnostic(
    store: RedisRuntimeStore, symbol: str, venue: str,
    *, interval_seconds: int = 10, window_minutes: int = 30,
    ofi: Any | None = None, postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: calc.price.delta (alias calc.derived_diagnostic) — NUMERIC derived ΔP.

    P5 derivation endpoint: an OFI scenario value (or the latest closed
    interval's OFI by default) is run through the FITTED models via
    ``fitting.derive_price_delta`` — route A direct plus route B
    depth-scaled when c/λ identify. Refuses (result None, never zero) on
    gate-failed fits, empty tapes, or unparseable inputs.
    """
    from decimal import Decimal

    from market_service.microstructure import fitting as fm

    cap = CAPABILITIES["calc.derived_diagnostic"]
    scope = {"symbol": symbol.upper(), "venue": venue,
              "interval_seconds": interval_seconds,
              "window_minutes": window_minutes, "ofi": ofi}
    try:
        cap.validate_scope(symbol, venue)
        evidence_dict, _fit_log = await _tool_fit_beta(
            store, symbol, venue,
            {"interval_seconds": interval_seconds,
             "window_minutes": window_minutes, "tick_size": "0.01"},
            postgres=postgres,
        )
        if not isinstance(evidence_dict, dict):
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "no evidence window"},
            )
        price_fit = _price_fit_from_dict(evidence_dict.get("price_impact_fit") or {})
        if price_fit is None:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": "unparseable price fit"},
            )
        dsf_dict = evidence_dict.get("depth_scaling_fit")
        depth_fit = (_depth_fit_from_dict(dsf_dict)
                     if isinstance(dsf_dict, dict) else None)
        if ofi is not None:
            try:
                ofi_dec = Decimal(str(ofi))
            except Exception:
                return None, capability_log_entry(
                    cap.name, scope, "ok",
                    detail={"status": "refused",
                            "reason": f"unparseable ofi scenario: {ofi!r}"},
                )
            ofi_source = "scenario_arg"
        else:
            payloads = await store.read_microstructure_events(venue, symbol.upper())
            events, _dropped = fm.replay_events_from_payloads(payloads)
            intervals = fm.replay_intervals(events, interval_ms=interval_seconds * 1_000)
            if not intervals:
                return None, capability_log_entry(
                    cap.name, scope, "ok",
                    detail={"status": "refused",
                            "reason": "no closed intervals for default OFI"},
                )
            ofi_dec = intervals[-1].ofi
            ofi_source = "latest_interval"
        tick_size = Decimal(str(evidence_dict.get("tick_size") or "0.01"))
        try:
            derived = fm.derive_price_delta(
                price_fit, ofi=ofi_dec, tick_size=tick_size,
                depth_fit=depth_fit, average_depth=price_fit.mean_ad,
            )
        except ValueError as vex:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "refused", "reason": str(vex),
                        "fit_id": price_fit.fit_id},
            )
        route_b = derived.get("route_b_depth_scaled") or {}
        result = {
            "formula": "ΔP_k = α_i + c·OFI_k/AD_i^λ + (ν_i·OFI_k + ε_k)",
            "ofi": str(ofi_dec),
            "ofi_source": ofi_source,
            **derived,
            "units": {"price_unit": "ticks", "tick_size": str(tick_size)},
            "heteroskedasticity": {
                "flag": derived.get("heteroskedasticity_flag"),
                "warning": ("ν·OFI term: error variance grows with |OFI| — "
                              "bands widen on large flow; diagnostic, never a point prediction"),
            },
            "status": "derived_ok",
            "paper": "Cont 1011.6402 §3",
        }
        route_a = derived.get("route_a_direct") or {}
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": "derived_ok",
                    "delta_ticks": route_a.get("delta_ticks"),
                    "route_b": route_b.get("status")},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}",
        )

async def dispatch_memory_recall_paper(
    symbol: str, venue: str, *, query: str = "Cont OFI AD beta",
    memory: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: memory.recall_paper — recall paper facts through the ENGINE's own
    MemoryNode (two-plane boundary pass, 2026-09-04).

    The node and the paper-KB session are injected; this dispatcher never
    constructs stores and never re-reads the environment. The paper session
    UUID comes from the single source of truth ``memory.paper_kb_session_id``
    shared with scripts/seed_paper_kb.py. With no memory node (Redis-only
    deployment) the result is an explicit null payload — never a fabricated
    recall and never a second connection pool.
    """
    from market_service.nooa_harness.memory import paper_kb_session_id

    cap = CAPABILITIES["memory.recall_paper"]
    scope = {"symbol": symbol.upper(), "venue": venue, "query": query}
    try:
        cap.validate_scope(symbol, venue)
        if memory is None:
            return [], capability_log_entry(
                cap.name, scope, "ok",
                detail={"facts": 0, "reason": "memory_node_not_configured"},
            )
        mems = await memory.recall(paper_kb_session_id(), query=query, limit=8)
        projected = [{"content": m.content[:600], "tags": list(m.tags),
                      "importance": m.importance} for m in mems]
        return projected, capability_log_entry(
            cap.name, scope, "ok",
            detail={"facts": len(projected), "query": query,
                    "session": "paper-kb (injected MemoryNode)"},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_market_group(
    store: RedisRuntimeStore, settings: Any, symbol: str, group: str,
    *, window_minutes: int = 15,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.group — run one calculation-model group through the
    INFERENCE PLANE seam (two-plane boundary pass, 2026-09-04).

    Routes to ``pipeline_inference.run_inference_group`` with the engine's
    own injected store + settings. This dispatcher no longer imports the
    interpretation plane's pipeline module, no longer opens a second Redis
    pool, and no longer runs the math with lossy defaults (depth=20, no
    tier config, no wall history). Group semantics live in
    ``bedrock.GROUP_MAP`` — one source of truth for both planes.
    """
    from market_service.nooa_harness import pipeline_inference

    cap = CAPABILITIES["market.run_group"]
    scope = {"symbol": symbol.upper(), "group": group, "window_minutes": window_minutes}
    try:
        cap.validate_scope(symbol, "spot")
        if group not in pipeline_inference.bedrock.GROUP_MAP:
            raise CapabilityDenied(
                f"unknown group {group!r}; allowed: {sorted(pipeline_inference.bedrock.GROUP_MAP)}"
            )
        result = await pipeline_inference.run_inference_group(
            store, settings, symbol, group, window_minutes=window_minutes,
        )
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"seam": "pipeline_inference", "depth_levels": settings.depth_levels},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def execute_tool(
    store: RedisRuntimeStore, name: str, args: dict[str, Any],
    *, postgres: Any | None = None, memory: Any | None = None,
    settings: Any | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Execute one tool call by public name with scope validation + audit.

    This is the single entry point the narration loop uses for the LLM's
    ``tool_calls``. Unknown tool names and out-of-scope dispatches return a
    structured ``denied`` result — never an exception to the caller.

    Injected dependencies (two-plane boundary pass, 2026-09-04):
    - ``store``    — the engine's own Redis connection (shared, never rebuilt)
    - ``postgres`` — the engine's durable store, for fit tools' prior cycles
    - ``memory``   — the engine's MemoryNode, for memory.recall_paper
    - ``settings`` — the operator Settings, for market.group (depth/tiers/
                     weights). ``None`` is tolerated by every other tool.
    """
    canonical = _normalize_tool_name(name)
    if canonical is None:
        attempted = {
            "name": name,
            "arg_keys": sorted(args.keys()) if isinstance(args, dict) else None,
        }
        return None, capability_log_entry(
            "tool.unknown", {"name": name}, "denied",
            detail={"attempted": attempted, "allowed": sorted(TOOL_NAMES)},
        )
    name = canonical
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
        return await _tool_fit_beta(store, symbol, venue, args, postgres=postgres)
    if name == "micro.evidence":
        return await dispatch_read_evidence(store, symbol, venue)
    if name == "market.read":
        return await dispatch_market_read(
            store, symbol, mode=str(args.get("mode") or "snapshot"),
        )
    if name == "market.group":
        if settings is None:
            return None, capability_log_entry(
                "market.run_group", {"name": name}, "denied",
                detail="settings_not_injected; market.group needs the engine's operator config",
            )
        return await dispatch_market_group(
            store, settings, symbol, str(args.get("group") or "flow"),
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
    if name == "calc.ofi.intervals":
        return await dispatch_calc_ofi_intervals(store, symbol, venue, interval_ms=int(args.get("interval_ms") or args.get("interval_seconds", 10)*1000 if "interval_seconds" in args else 10_000), window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.depth.average":
        return await dispatch_calc_ad_average(store, symbol, venue, window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.observation.build":
        return await dispatch_calc_observation_build(store, symbol, venue, interval_seconds=int(args.get("interval_seconds") or 10), window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.fit.price_impact":
        return await dispatch_calc_fit_price_impact(store, symbol, venue, interval_seconds=int(args.get("interval_seconds") or 10), window_minutes=int(args.get("window_minutes") or 30))
    if name == "calc.fit.depth_scaling":
        return await dispatch_calc_fit_depth_scaling(store, symbol, venue)
    if name in ("calc.derived_diagnostic", "calc.price.delta"):
        return await dispatch_calc_derived_diagnostic(
            store, symbol, venue,
            interval_seconds=int(args.get("interval_seconds") or 10),
            window_minutes=int(args.get("window_minutes") or 30),
            ofi=args.get("ofi"), postgres=postgres,
        )
    if name == "memory.recall_paper":
        return await dispatch_memory_recall_paper(
            symbol, venue, query=str(args.get("query") or "Cont OFI AD beta"),
            memory=memory,
        )
    return None, capability_log_entry(
        "tool.unrouted", {"name": name}, "denied", detail="no dispatch path",
    )


# Alias table for LLM-supplied tool names — lookup is by fully-normalized
# form (every "_" treated as "."), so exact keys, all-underscore forms,
# and MIXED forms (``calc.ofi_intervals``) all resolve. Plus explicit
# truncations. Built once from TOOL_NAMES so new tools inherit it.
def _norm_tool_key(value: str) -> str:
    return value.replace("_", ".")


_TOOL_ALIASES: dict[str, str] = {
    _norm_tool_key(_alias_key): _alias_key for _alias_key in TOOL_NAMES
}
_TOOL_ALIASES.update({
    "micro.ofi": "micro.ofi_intervals",
    "micro.fit": "micro.fit_beta",
    "micro.status": "micro.capture_status",
    "micro.capture": "micro.capture_status",
    "calc.ofi": "calc.ofi.intervals",
    "calc.ad": "calc.depth.average",
    "calc.depth": "calc.depth.average",
    "calc.observations": "calc.observation.build",
    "calc.observation": "calc.observation.build",
    "calc.derived": "calc.derived_diagnostic",
    "market.history": "market.keystone_history",
})


def _normalize_tool_name(name: Any) -> str | None:
    """Resolve an LLM-supplied tool name to its canonical registry key.

    Accepts the exact key plus separator variants (``calc.ofi_intervals`` /
    ``calc.ofi.intervals``) and a small explicit alias map for truncated
    names. Returns None when nothing matches — the caller denies with the
    attempted payload attached for debuggability.
    """
    if not isinstance(name, str):
        return None
    cleaned = name.strip().lower()
    normalized = _norm_tool_key(cleaned)
    if normalized in _TOOL_ALIASES:
        return _TOOL_ALIASES[normalized]
    return None


def _price_fit_from_dict(data: dict[str, Any]) -> Any | None:
    """Reconstruct a PriceImpactFit from a persisted PG-history dict.

    History rows were serialized with ``default=str`` so every Decimal
    arrives as a string; anything unparseable yields None (skipped, never
    fabricated).
    """
    try:
        from decimal import Decimal

        from market_service.microstructure.contracts import PriceImpactFit

        def _dec(key: str) -> Decimal | None:
            value = data.get(key)
            return Decimal(str(value)) if value is not None else None

        beta = _dec("beta")
        if beta is None:
            return None
        return PriceImpactFit(
            fit_id=str(data.get("fit_id") or "hist-unknown"),
            symbol=str(data.get("symbol") or "").upper(),
            venue=str(data.get("venue") or ""),
            window_start_ms=int(data.get("window_start_ms") or 0),
            window_end_ms=int(data.get("window_end_ms") or 0),
            interval_seconds=int(data.get("interval_seconds") or 0),
            alpha=_dec("alpha") or Decimal(0),
            beta=beta,
            stderr_beta=_dec("stderr_beta"),
            robust_se_method=str(data.get("robust_se_method") or "HC0"),
            n_observations=int(data.get("n_observations") or 0),
            excluded_observations=int(data.get("excluded_observations") or 0),
            r2=_dec("r2"),
            residual_std=_dec("residual_std"),
            heteroskedasticity_flag=bool(data.get("heteroskedasticity_flag", False)),
            mean_ad=_dec("mean_ad"),
            price_unit=str(data.get("price_unit") or "ticks"),
            tick_size=_dec("tick_size") or Decimal("0.01"),
            input_hash=str(data.get("input_hash") or ""),
            model_version=str(data.get("model_version") or ""),
            sensitivity=bool(data.get("sensitivity", False)),
            status=str(data.get("status") or "insufficient"),
        )
    except (ValueError, TypeError, ArithmeticError, KeyError):
        return None


async def _load_prior_block_fits(
    postgres: Any | None, symbol: str, venue: str,
    *, interval_seconds: int, limit: int = 8,
) -> list[Any]:
    """Load prior-cycle price-impact fits so depth scaling is identified.

    Without history every cycle fits depth scaling from a single block
    (n_blocks=1 → insufficient by design). Priors come from the durable PG
    ledger — same symbol/venue/interval, validated-or-provisional,
    non-sensitivity, distinct fit_ids. Empty on any failure (fit degrades
    to single-block, never fabricates).
    """
    if postgres is None:
        return []
    try:
        rows = await postgres.read_recent_inference_artifacts(
            symbol, venue=venue, limit=limit,
        )
    except Exception:
        return []
    fits: list[Any] = []
    seen: set[str] = set()
    for row in rows or []:
        state = (row or {}).get("deterministic_state") or {}
        micro = state.get("microstructure_evidence") or {}
        fit_dict = micro.get("price_impact_fit")
        if not isinstance(fit_dict, dict):
            continue
        fit = _price_fit_from_dict(fit_dict)
        if fit is None or fit.fit_id in seen:
            continue
        if fit.venue != venue or fit.interval_seconds != interval_seconds:
            continue
        if fit.sensitivity or fit.status not in ("validated", "provisional"):
            continue
        seen.add(fit.fit_id)
        fits.append(fit)
    return fits


async def _tool_fit_beta(
    store: RedisRuntimeStore, symbol: str, venue: str, args: dict[str, Any],
    *, postgres: Any | None = None,
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
        if not intervals:
            return None, capability_log_entry(cap.name, scope, "ok", detail={"status": "insufficient", "reason": "no closed intervals", "events": len(windowed)})
        fit_config = {
            "symbol": symbol, "venue": venue, "tick_size": str(tick),
            "interval_seconds": interval_s, "window_minutes": window_m,
        }
        evidence_id = f"ev-{fitting_mod.input_hash(intervals, fit_config)[:16]}"
        prior_fits = await _load_prior_block_fits(
            postgres, symbol, venue, interval_seconds=interval_s,
        )
        evidence = fitting_mod.assemble_evidence(
            intervals, symbol=symbol, venue=venue, tick_size=tick,
            interval_seconds=interval_s,
            evidence_id=evidence_id,
            generated_at_ms=end_ts,
            events=windowed,
            prior_block_fits=prior_fits,
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
    "TOOL_PHASE",
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
