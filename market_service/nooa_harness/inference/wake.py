"""Wake plane — deterministic trigger evaluation + durable wake stream.

The engine is event-driven, never lazily polled. A trigger evaluation is a
PURE function over (current Redis counters, last artifact high-water, wake
config). When a predicate fires, the host materializes a typed
``WakeEnvelope`` and XADDs it to the durable wake stream. The engine drains
pending envelopes, coalesces them into ONE cycle, dedupes by wake_id, and
re-validates the counters against live Redis before doing expensive work
(two-phase wake: envelope asserts, engine verifies).

Moved verbatim from the inference.py monolith (decomposition Phase 0).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from market_service.runtime.contracts import WAKE_ENVELOPE_SCHEMA_VERSION, WakeEnvelope
from market_service.runtime.redis_store import RedisRuntimeStore

from .gate import _ESTABLISHED_CAPTURE_STATES

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
