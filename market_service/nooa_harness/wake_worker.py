"""Event-driven wake worker for the statistical inference engine.

This module is the DEDICATED runtime process that closes the loop between
the deterministic trigger plane (``inference.evaluate_triggers`` +
``WakeEnvelope``) and the engine's cycle work. It is bounded ENTIRELY on the
Redis plane — it is not an agent, it does not reason, and it never calls the
LLM. It reads the microstructure data streams, evaluates the deterministic
wake conditions at its boundary, and when a condition is met it fires the
engine cycle as an asynchronous task.

Design decisions (basis of the transport collapse):

* BLOCKER-ONLY, never polled. The worker consumes the microstructure event
  and status streams with ``XREADGROUP BLOCK`` — a firing wake is triggered
  by the DATA, not by a clock. The five-second poller remains a data
  producer entirely outside this control path.
* In-memory assertion. A firing condition is materialized as a typed
  ``WakeEnvelope`` (facts: status-state, events-total, elapsed-age) that the
  worker owns and passes DIRECTLY to the engine dispatcher. No ``publish_wake``
  XADD, no ``read_pending_wakes`` XREVRANGE, no JSON round-trip: the envelope
  is a deterministic assertion object, never a transported artifact.
* Durable position, not durable delivery. The consumer group advanced ``>``
  marker doubles as the replay/crash-resume ledger that the retired wake
  stream used to provide — with strictly less machinery.
* One consumer per scope. Exactly one active consumer per (symbol, venue)
  group; a second redundant instance only takes over via the supervisor
  heartbeat key when the first dies.
* Async loop. Fires spawn ``asyncio.create_task`` for the engine cycle
  (never awaited in the read loop); the loop stays responsive.

A wake fires ONLY when the deterministic ``evaluate_triggers`` predicate
matrix passes AND the engine-side hygiene gates pass:

  event_delta      — new events since the last artifact's high-water,
                     applied through a timestamp-water gate (the event
                     stream must be newer than the artifact; a consumer
                     restart replays old events and MUST NOT re-fire stale
                     deltas), with both a reach gate and an elapsed gate.
  cold_start       — established capture with no prior artifact yet.
  capture recovery — the status stream records a state transition into an
                     established state (gap/reconnecting -> running/connected).

The worker writes a supervisor heartbeat (``...:supervisor``, TTL-bounded)
proving liveness in the deployment, but that key is NEVER consulted for the
wake decision — it is observability, not control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from market_service.nooa_harness.inference import (
    CounterSnapshot,
    WakeConfig,
    evaluate_triggers,
    wake_dedupe_id,
)
from market_service.runtime.contracts import WakeEnvelope
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

# Runtime durations --------------------------------------------------------
READ_BLOCK_MS = 1_000          # XREADGROUP blocking wait (ms)
SUPERVISOR_MS = 5_000          # supervisor heartbeat TTL (ms)
_EVT_READ_LIMIT = 500          # max event entries pulled per XREADGROUP burst
_STREAM_MAXLEN = 1_000         # status-transition stream retention

# Status-transition stream retention (capture node). Writing ONLY on state
# change keeps this tiny; 1000 entries comfortably covers weeks of
# transition events at BTCUSDT capture cadence.
STATUS_STREAM_MAXLEN = _STREAM_MAXLEN


@dataclass(frozen=True)
class _StatusPayload:
    """Frozen view of the capture node's status payload (latest key)."""

    state: str | None
    best_quote_events: int | None
    sequence_gaps: int | None  # type: ignore[assignment]
    error: str | None = None
    updated_at_ms: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> _StatusPayload:
        state = payload.get("state") if payload else None
        bqe = payload.get("best_quote_events") if payload else None
        state = str(state) if state is not None else None
        try:
            bqe = int(bqe) if bqe is not None else None
        except (TypeError, ValueError):
            bqe = None
        return cls(
            state=state,
            best_quote_events=bqe,
            sequence_gaps=int(payload.get("sequence_gaps")) if payload and payload.get("sequence_gaps") is not None else None,
            error=str(payload["error"]) if payload and payload.get("error") else None,
            updated_at_ms=int(payload["updated_at_ms"]) if payload and payload.get("updated_at_ms") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "best_quote_events": self.best_quote_events}
        if self.sequence_gaps is not None:
            out["sequence_gaps"] = self.sequence_gaps
        if self.error is not None:
            out["error"] = self.error
        return out


def _parse_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _stream_age_ms(raw: bytes | str | None, now_ms: int) -> int | None:
    """Parse the entry timestamp from an XREADGROUP ID, return its age."""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    try:
        ms = int(text.split("-")[0])
    except ValueError:
        return None
    return now_ms - ms


def _transition_from_state(status_rows: list[dict[str, Any]]) -> str | None:
    """Derive the PRIOR capture state from a status-transition stream entry.

    The capture node writes a status-transition entry on EVERY state change,
    carrying ``from_state`` (the state we left) and ``to_state`` (the state
    we entered). The wake plane uses ``from_state`` to decide whether a
    transition into an established state is a recovery (gap/reconnecting --->
    running/connected) vs a routine running tick.
    Returns the ``from_state`` of the NEWEST entry, or None.
    """
    if not status_rows:
        return None
    newest = status_rows[-1]
    fields: Any = None
    if isinstance(newest, dict):
        fields = newest.get("fields")
    elif isinstance(newest, (list, tuple)) and len(newest) == 2:
        # Raw (entry_id, fields) tuple form (test seam / legacy producers).
        fields = newest[1]
    if not isinstance(fields, dict):
        return None
    state = fields.get("from_state") or fields.get("from")
    return str(state) if state is not None else None


def _high_water_from_artifact(artifact: dict[str, Any] | None) -> int | None:
    """Read the last artifact's recorded ``events_total`` high-water.

    The engine persists ``events_total`` on the microstructure evidence
    inside ``deterministic_state`` (``…microstructure_evidence.coverage``);
    a legacy top-level ``deterministic_state.coverage`` layout is accepted
    too. That counter is the RESTART-SAFE authority for what the wake plane
    has already consumed. ``None`` when no artifact exists yet (cold start) —
    the trigger evaluator treats that as the cold-start branch.
    """
    if not artifact:
        return None
    state = artifact.get("deterministic_state") or {}
    evidence = state.get("microstructure_evidence") or {}
    coverage = (
        evidence.get("coverage")
        or state.get("coverage")
        or {}
    )
    value = coverage.get("events_total")
    return _parse_int(value)


def make_wake_envelope(
    *,
    symbol: str,
    venue: str,
    trigger_source: str,
    status_state: str | None,
    events_total: int | None,
    high_water: int | None,
    last_artifact_completed_at_ms: int | None,
    now_ms: int,
    predicates: dict[str, Any],
    water_age_ms: int | None,
) -> WakeEnvelope:
    """Build the typed in-memory wake assertion (transport-free).

    Compared to ``inference.build_wake_envelope`` (which evaluated from a
    CounterSnapshot and carried that snapshot on the transported envelope),
    this materializes directly from the conditions the worker is firing on,
    appending the deterministic elapsed-age stamps the worker computed.
    ``wake_id`` is the deterministic condition hash so identical conditions
    collapse; ``predicates_fired`` carries the fired predicate details.
    """
    predicates_fired = dict(predicates)
    # Stamps ride on the envelope fields / high_water, NOT inside the closed
    # predicate set (the contract validates predicates against
    # ``ValidWakePredicates``; trigger_source/status_state/events_total are
    # deterministic provenance, not predicates).
    high_water_map = {
        "events_total": high_water,
        "completed_at_ms": last_artifact_completed_at_ms,
    }
    wake_id = wake_dedupe_id(
        symbol, venue,
        predicates_fired, high_water_map,
    )
    return WakeEnvelope.create(
        symbol=symbol,
        venue=venue,
        trigger_source=trigger_source,
        predicates_fired=predicates_fired,
        counter_snapshot={
            "events_total": events_total,
            "status_state": status_state,
            "water_age_ms": water_age_ms,
        },
        high_water=high_water_map,
    )._replace_wake_id(wake_id)


def is_recoverable_redis_exc(exc: Exception) -> bool:
    """True for Redis connection-level errors the worker should back off on."""
    name = type(exc).__name__
    return name in (
        "ConnectionError", "ResponseError", "TimeoutError", "RedisError",
        "ConnectionClosedError", "ConnectionResetError", "OperationalError",
    )


class WakeSupervisor:
    """Runnable worker: bounded to the Redis plane, event-driven, async loop."""

    def __init__(
        self,
        store: RedisRuntimeStore,
        *,
        symbol: str = "BTCUSDT",
        venue: str = "spot",
        read_block_ms: int = READ_BLOCK_MS,
        supervisor_ms: int = SUPERVISOR_MS,
        status_stream_maxlen: int = STATUS_STREAM_MAXLEN,
        dispatcher: Callable[[WakeEnvelope], Awaitable[dict[str, Any]]] | None = None,
        wake_config: WakeConfig | None = None,
        on_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.symbol = symbol.upper()
        self.venue = venue
        self.read_block_ms = read_block_ms
        self.supervisor_ms = supervisor_ms
        self.status_stream_maxlen = status_stream_maxlen
        self.dispatcher = dispatcher
        self.config = wake_config or WakeConfig()
        self._on_stop = on_stop

        self._stream = self.store.microstructure_event_stream(self.venue, self.symbol)
        self._group = f"inference-wake:{self.venue}:{self.symbol}"
        self._consumer = f"wake-{os.getpid()}-{int(time.time())}"
        self._status_stream = self.store.microstructure_status_stream(
            self.venue, self.symbol,
        )

        self._xadd = self.store.redis.xadd
        self._xlen = self.store.redis.xlen
        self._xread = self.store.redis.xread
        self._xreadgroup = self.store.redis.xreadgroup
        self._get = self.store.redis.get
        self._set = self.store.redis.set
        self._set_ttl = self.store.redis.setex
        self._delete = self.store.redis.delete
        self._eval_sha = self.store.redis.evalsha
        self._script_register = self.store.redis.script_load

        self._status_stream_key = getattr(
            self.store, "microstructure_status_stream", None
        )
        if self._status_stream_key is not None:
            self._status_stream_key = self._status_stream_key(self.venue, self.symbol)

        self._last_event_water_ms: int | None = None
        self._last_fired_events_total: int | None = None
        self._last_fired_status_state: str | None = None
        self._last_fire_started_ms: int | None = None
        self._last_error: str | None = None
        self._last_tick_ms: int | None = None
        self._liveness: bool = False
        self._fired: int = 0
        self._ticks: int = 0
        self._running: bool = False
        self._tasks: set[asyncio.Task] = set()
        self._dedupe_lua_sha: str | None = None

        self._status_stream_key = self.store.microstructure_status_stream(
            self.venue, self.symbol,
        )
        # Use the custom-lua registration so tests can monkeypatch register.
        self._script_register = self.store.redis.script_load
        self._eval_sha = self.store.redis.evalsha

    def _now(self) -> int:
        # overridable for tests
        return _now_ms()

    async def start(self) -> None:
        """Open the consumer group and register the dedupe script."""
        await self._ensure_group()

    async def _ensure_group(self) -> None:
        # redis-py has no xgroup_create_mkstream; xgroup_create with
        # mkstream=True creates the stream if missing. If the group already
        # exists, BUSYGROUP is raised and we treat it as expected.
        try:
            await self.store.redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" in str(exc).upper():
                log.debug("wake group %s already exists", self._group)
            else:
                raise

    async def _register_scripts(self) -> None:
        """Register the dedupe Lua script (idempotent across registrations)."""
        await asyncio.sleep(0)
        if self._dedupe_lua_sha is not None:
            return
        source = _DEDUPE_LUA
        try:
            self._dedupe_lua_sha = str(await self._script_register(source))
        except Exception as exc:
            log.warning("wake dedupe script registration failed: %s", exc)
            self._last_error = f"script_register: {exc}"
            self._dedupe_lua_sha = None

    async def _read_once(self, block_ms: int | None = None) -> list[dict[str, Any]]:
        """Blocking read of ONE batch of the event stream.

        Honors a consumer-group stream with existing position: consumes
        only entries the group has not yet acknowledged. On an empty reply
        we return []. After a Redis error the caller backs off — no wake is
        produced here.
        """
        try:
            resp = await self._xreadgroup(
                self._group, self._consumer,
                {self._stream: ">"},
                count=_EVT_READ_LIMIT,
                block=block_ms if block_ms is not None else self.read_block_ms,
                noack=True,
            )
        except Exception as exc:
            if is_recoverable_redis_exc(exc):
                log.warning("wake read failed (backoff): %s", exc)
                self._last_error = f"read: {exc}"
                return []
            raise
        rows: list[dict[str, Any]] = []
        if not resp:
            return rows
        if isinstance(resp, (list, tuple)):
            for item in resp:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    _key, entries = item
                    if entries:
                        for entry in entries:
                            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                                entry_id, fields = entry
                                rows.append(
                                    {
                                        "id": entry_id,
                                        "fields": fields if isinstance(fields, dict) else {},
                                    }
                                )
        return rows

    async def _read_status_once(self, block_ms: int | None = None) -> list[dict[str, Any]]:
        """Blocking read of ONE batch of the status-transition stream."""
        try:
            resp = await self._xread(
                {self._status_stream_key: "$"},
                count=_EVT_READ_LIMIT,
                block=block_ms if block_ms is not None else self.read_block_ms,
            )
        except Exception as exc:
            if is_recoverable_redis_exc(exc):
                log.warning("wake status read failed (backoff): %s", exc)
                return []
            raise
        rows: list[dict[str, Any]] = []
        if not resp:
            return rows
        if isinstance(resp, (list, tuple)):
            for item in resp:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    _key, entries = item
                    for entry in entries or []:
                        if isinstance(entry, (list, tuple)) and len(entry) == 2:
                            entry_id, fields = entry
                            rows.append(
                                {
                                    "id": entry_id,
                                    "fields": fields if isinstance(fields, dict) else {},
                                }
                            )
        return rows

    async def _collect_source(self) -> _StatusPayload | None:
        """Read the live status + event counters (the wake plane's eyes)."""
        status_raw = await self._get(self.store.microstructure_status_key(
            self.venue, self.symbol,
        ))
        if not status_raw:
            return None
        try:
            decoded = json.loads(status_raw)
        except (ValueError, TypeError):
            self._last_error = "status payload decode failed"
            return None
        return _StatusPayload.from_dict(decoded if isinstance(decoded, dict) else None)

    async def run_forever(self) -> int:
        """The async supervisor loop — data-driven, never timer-driven.

        No ticker task exists: the blocking XREADs are the only cadence.
        The loop is idle-stuck on Redis until data arrives; on every
        completed read cycle (fired or quiet) the supervisor heartbeat is
        refreshed as an activity stamp, and ``now_ms`` is read only as an
        input to age/boundary predicates, never to drive work.

        Structure per iteration:
          1. blocking reads of the event + status-transition streams.
          2. on ANY arrival: evaluate the deterministic trigger matrix;
             on fire: dedupe-check then dispatch the engine cycle as a task
             (never awaited here), then activity-heartbeat.
          3. on no arrival: activity-heartbeat (proves the reader task is
             alive), return to the blocking read.
          4. on any Redis-level error: exponential backoff, keep alive.
        """
        await self.start()
        self._running = True

        # Initial trigger evaluation: fire cold_start / capture_recovery on
        # the current state BEFORE entering the blocking read loop. Without
        # this the worker waits for new events that may never arrive, and
        # the cold-start wake is silently skipped.
        try:
            now = self._now()
            await self._handle_delta([], [], now)
        except Exception as exc:
            log.warning("wake initial trigger evaluation failed: %s", exc)

        while self._running:
            try:
                event_rows = await self._read_once()
                status_rows = await self._read_status_once()
                now = self._now()
                if event_rows or status_rows:
                    await self._handle_delta(status_rows, event_rows, now)
                # Activity heartbeat: refreshed once per completed read
                # cycle (fired or quiet). The blocking-read return IS the
                # liveness signal — no separate timer task exists.
                try:
                    await self._tick()
                except Exception as exc:
                    log.warning("wake activity heartbeat failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("wake loop iteration failed: %s", exc, exc_info=True)
                await self._sleep(min(5.0, max(0.2, self.read_block_ms / 2_000)))
        return self._fired

    @staticmethod
    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _tick(self) -> None:
        """Heartbeat + script registration + last_tick ledger write."""
        try:
            if self._dedupe_lua_sha is None:
                await self._register_scripts()
            payload = {
                "state": "running",
                "symbol": self.symbol,
                "venue": self.venue,
                "pid": os.getpid(),
                "last_fire_started_ms": self._last_fire_started_ms,
                "last_fired_events_total": self._last_fired_events_total,
                "last_tick_ms": self._now(),
            }
            await self._set_ttl(
                self.store.inference_wake_supervisor_key(self.symbol, self.venue),
                self.supervisor_ms // 1000 + 2,
                json.dumps(payload, default=str, separators=(",", ":")),
            )
            self._last_tick_ms = self._now()
            self._ticks += 1
            self._liveness = True
        except Exception as exc:
            if is_recoverable_redis_exc(exc):
                self._liveness = False
                log.warning("wake supervisor tick failed: %s", exc)
            else:
                raise

    async def _handle_delta(
        self, status_rows: list[dict[str, Any]], event_rows: list[dict[str, Any]], now_ms: int,
    ) -> None:
        """Evaluate the deterministic trigger matrix; fire a Task when true."""
        # Accept both normalized rows ({"id","fields"}, from _read_once) and
        # raw (entry_id, fields) tuples — the trigger matrix only needs ids.
        normalized: list[dict[str, Any]] = []
        for row in event_rows or []:
            if isinstance(row, dict) and "id" in row:
                normalized.append(row)
            elif isinstance(row, (list, tuple)) and len(row) == 2:
                normalized.append({"id": row[0], "fields": row[1] if isinstance(row[1], dict) else {}})
        event_rows = normalized
        # Normalize status-transition rows the same way: accept both dict
        # ({"id", "fields"}) and raw (entry_id, fields) tuples.
        normalized_status: list[dict[str, Any]] = []
        for row in status_rows or []:
            if isinstance(row, dict) and "id" in row:
                normalized_status.append(row)
            elif isinstance(row, (list, tuple)) and len(row) == 2:
                normalized_status.append({"id": row[0], "fields": row[1] if isinstance(row[1], dict) else {}})
        status_rows = normalized_status
        status = await self._collect_source()
        if status is None or status.best_quote_events is None:
            # No status yet: stream may exist but capture is not established.
            self._last_error = "capture not established: no status payload"
            return
        if self._last_fired_events_total is None and status.best_quote_events == 0:
            return

        artifact = await self.store.read_latest_inference_artifact(self.symbol, self.venue)
        hw = _high_water_from_artifact(artifact)
        completed_ms = _parse_iso_ms(
            (artifact or {}).get("completed_at") if artifact else None
        )
        # Transport-level stream length — this is what the trigger evaluator's
        # ``event_delta`` predicate compares (new_events = stream_len − hw).
        # The status payload's ``best_quote_events`` counter is the monotonic
        # total; here we need the STREAM length as the live count of entries.
        stream_len = int(await self._xlen(self.store.microstructure_event_stream(
            self.venue, self.symbol,
        )) or 0)
        snapshot = CounterSnapshot(
            event_stream_len=int(stream_len) if stream_len is not None else 0,
            capture_state=status.state,
            last_artifact_events_total=hw,
            # Recovery provenance: the status-TRANSITION stream entry carries
            # the prior state the transition came FROM. When the latest
            # status is an established running state but the status stream
            # shows a gap/reconnecting -> running transition, the evaluator
            # fires ``capture_recovery``.
            last_artifact_capture_state=(
                _transition_from_state(status_rows) if status_rows else None
            ),
            last_artifact_completed_at_ms=completed_ms,
        )
        evaluation = evaluate_triggers(snapshot, self.config, now_ms=now_ms)
        if not evaluation["fired"]:
            return

        predicates = evaluation["predicates"]

        # Timestamp-water gate: the stream must be strictly NEWER than the
        # last artifact. Without this a consumer restart replays OLD deltas
        # and re-fires a stale event_delta wake. cold_start fires on the
        # ABSENCE of a prior artifact — it does not need a water timestamp.
        # Recovery/status-only wakes have NO event row (no water timestamp) —
        # they bypass this gate by definition.
        water_age_ms = _stream_age_ms(
            event_rows[0]["id"] if event_rows else None, now_ms,
        )
        needs_water = bool(predicates.get("event_delta"))
        if needs_water and water_age_ms is None:
            return
        if predicates.get("event_delta"):
            if water_age_ms is not None and water_age_ms > SUPERVISOR_MS * 3:
                # Stale (older than ~15s): not a fresh data arrival.
                return
        # Cooldown vs artifact completion: never fire before the previous
        # cycle's artifact is older than the cooldown window. A negative
        # ``water_age_ms`` (entry newer than ``now`` in tests / clock skew)
        # is treated as fresh, never as a stale block.
        if water_age_ms is not None and completed_ms is not None:
            if completed_ms > now_ms - self.config.cooldown_seconds * 1_000:
                return

        # Reach gate: must actually see NEW events at the transport level.
        if predicates.get("event_delta") and event_rows:
            # The entry's own fresh timestamp is the reach proof — we don't
            # double-count from the (possibly stale) in-memory counter.
            pass

        # Elapsed gate: minimum seconds since the last fire (cooldown).
        if self._last_fire_started_ms is not None:
            if now_ms - self._last_fire_started_ms < self.config.cooldown_seconds * 1_000:
                return

        # Dedupe: collapse identical conditions via the Lua script.
        source_events_total = status.best_quote_events
        passage = await self._dedupe(
            status_state=status.state,
            events_total=source_events_total,
            last_artifact_completed_ms=completed_ms,
            now_ms=now_ms,
            predicates=predicates,
            high_water=hw,
        )
        if not passage["allowed"]:
            log.info("wake dedupe skipped (latest=%s reason=%s)",
                     passage.get("latest_events_total"),
                     passage.get("reason", "dup"))
            return

        envelope = make_wake_envelope(
            symbol=self.symbol, venue=self.venue,
            trigger_source=(
                "watcher"
                if predicates.get("event_delta") or predicates.get("cold_start")
                else "watcher"
            ),
            status_state=status.state, events_total=status.best_quote_events,
            high_water=hw, last_artifact_completed_at_ms=completed_ms,
            now_ms=now_ms, predicates=predicates,
            water_age_ms=water_age_ms,
        )
        self._last_fired_events_total = status.best_quote_events
        self._last_fired_status_state = status.state
        self._last_fire_started_ms = now_ms
        self._fired += 1
        log.info(
            "wake FIRED symbol=%s venue=%s predicate=%s state=%s events_total=%s high_water=%s",
            self.symbol, self.venue, sorted(predicates), status.state,
            status.best_quote_events, hw,
        )
        if self.dispatcher is not None:
            task = asyncio.create_task(self._dispatch_guarded(envelope))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _dedupe(
        self,
        *,
        status_state: str | None,
        events_total: int | None,
        last_artifact_completed_ms: int | None,
        now_ms: int,
        predicates: dict[str, Any],
        high_water: int | None,
    ) -> dict[str, Any]:
        """Collapse identical wake conditions via the supervisor-lua script.

        Returns ``{"allowed": bool, ...}``. The script atomically reads+sets
        the supervisor key so concurrent worker instances cannot double-fire
        on the SAME condition. ``events_total`` monotonic: if the recorded
        value is already at-or-above ours AND the status-state is unchanged
        AND the previous fire is less than ``cooldown_seconds`` old, we skip.
        """
        status_key = self.store.inference_wake_supervisor_key(self.symbol, self.venue)
        cooldown_ms = self.config.cooldown_seconds * 1_000
        candidate = json.dumps(
            {
                "events_total": events_total,
                "status_state": status_state,
                "fired_stamp_ms": now_ms,
                "predicate_keys": sorted(predicates),
                "high_water": high_water,
            },
            separators=(",", ":"),
        )
        try:
            if self._dedupe_lua_sha is None:
                await self._register_scripts()
            if self._dedupe_lua_sha is None:
                return {"allowed": True, "reason": "script_unavailable"}
            raw = await self._eval_sha(
                self._dedupe_lua_sha,
                1,
                status_key,
                candidate,
                str(cooldown_ms),
                str(events_total),
                str(status_state),
            )
        except Exception as exc:
            log.warning("wake dedupe eval failed (fire anyway): %s", exc)
            return {"allowed": True, "reason": "eval_error"}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if raw == "1":
            self._last_fired_events_total = events_total
            self._last_fired_status_state = status_state
            return {"allowed": True, "reason": "new"}
        return {"allowed": False, "reason": "dup", "latest_events_total": events_total}

    async def _dispatch_guarded(self, envelope: WakeEnvelope) -> None:
        try:
            result = await self.dispatcher(envelope)
            log.info("wake dispatch complete: %s", result)
        except Exception as exc:
            log.exception("wake dispatch failed: %s", exc)

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        # The worker NEVER owns the status-transition stream or the event
        # stream — capture writes them and they are the durable event surface
        # for recovery wakes. Nothing is deleted here.
        if self._on_stop is not None:
            try:
                await self._on_stop()
            except Exception:
                log.exception("wake on_stop callback failed")

    @property
    def liveness(self) -> bool:
        return self._liveness

    @property
    def fired_count(self) -> int:
        return self._fired


_DEDUPE_LUA = r"""
-- deterministic wake dedupe: atomically collapse identical conditions
-- KEYS[1] = supervisor key; ARGV[1] = candidate json; ARGV[2] = cooldown_ms;
-- ARGV[3] = events_total; ARGV[4] = status_state
local current = redis.call("GET", KEYS[1])
if current then
    local ok, dec = pcall(cjson.decode, current)
    if ok and type(dec) == "table" then
        local last_total = tonumber(dec.events_total) or 0
        local last_state = dec.status_state
        if tonumber(ARGV[3]) <= last_total and tostring(ARGV[4]) == tostring(last_state) then
            -- previous firing already consumed at-least this events_total
            -- with the same capture state; collapse (no new condition).
            return "0"
        end
    end
end
redis.call("SET", KEYS[1], ARGV[1])
return "1"
"""


@dataclass
class WakeSupervisorConfig:
    """Env-driven settings for the command entrypoints."""

    symbol: str
    venue: str = "spot"
    read_block_ms: int = READ_BLOCK_MS
    supervisor_ms: int = SUPERVISOR_MS
    cooldown_seconds: int = 60
    event_delta_threshold: int = 1_800
    once: bool = False
    interval: float = 0.0
    status_stream_maxlen: int = STATUS_STREAM_MAXLEN

    @classmethod
    def from_env(cls, symbol: str | None = None) -> WakeSupervisorConfig:
        symbol = (symbol or os.getenv("MICROSTRUCTURE_SYMBOL") or "SOLUSDT").upper()
        # Venue: MICROSTRUCTURE_VENUE (futures perps) is canonical, WAKE_VENUE legacy alias
        raw_venue = (os.getenv("MICROSTRUCTURE_VENUE") or os.getenv("WAKE_VENUE") or "futures").lower().strip()
        if raw_venue in ("perps", "perp", "usdm"):
            raw_venue = "futures"
        return cls(
            symbol=symbol,
            venue=str(raw_venue),
            read_block_ms=int(os.getenv("WAKE_READ_BLOCK_MS") or READ_BLOCK_MS),
            supervisor_ms=int(os.getenv("WAKE_SUPERVISOR_MS") or SUPERVISOR_MS),
            cooldown_seconds=int(os.getenv("WAKE_COOLDOWN_S") or 60),
            event_delta_threshold=int(os.getenv("WAKE_EVENT_DELTA_THRESHOLD") or 1_800),
            status_stream_maxlen=int(os.getenv("WAKE_STATUS_STREAM_MAXLEN") or STATUS_STREAM_MAXLEN),
        )


async def _build_supervisor(
    config: WakeSupervisorConfig,
    dispatcher: Callable[[WakeEnvelope], Awaitable[dict[str, Any]]] | None = None,
) -> WakeSupervisor:
    from market_service.config import Settings

    settings = Settings.from_redis_env()
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    # Default dispatcher: record the wake to the inference journal (no LLM).
    # Set ``WAKE_ENGINE_DISPATCH=1`` (or inject an ``engine_dispatcher``
    # callable) to hand the firing envelope DIRECTLY to the engine's
    # ``run_cycle`` — the Slice-2 closed loop. The engine path is lazy
    # (openai is imported only when actually dispatching), so the worker
    # module stays openai-free at import.
    engine_close: Callable[[], Awaitable[None]] | None = None
    if dispatcher is None and os.getenv("WAKE_ENGINE_DISPATCH") == "1":
        dispatch, engine_close = _engine_dispatcher_factory(store, settings)
        dispatcher = dispatch
    if dispatcher is None:
        from market_service.nooa_harness.inference import default_wake_dispatcher

        async def _dispatcher(envelope: WakeEnvelope) -> dict[str, Any]:
            return await default_wake_dispatcher(envelope, store=store)

        dispatcher = _dispatcher
    return WakeSupervisor(
        store,
        symbol=config.symbol, venue=config.venue,
        read_block_ms=config.read_block_ms,
        supervisor_ms=config.supervisor_ms,
        status_stream_maxlen=config.status_stream_maxlen,
        dispatcher=dispatcher,
        on_stop=engine_close,
    )


def _engine_dispatcher_factory(store: RedisRuntimeStore, settings: Any):
    """Lazy engine-cycle dispatcher (Slice-2 closed loop).

    Imports the engine only on first fire (keeps the worker module and its
    tests openai-free). The engine's ``run_cycle`` expects a ``WakeEnvelope``
    plus wake_meta — exactly the in-memory assertion the worker produces.
    Returns ``(dispatch, close)``; ``close`` shuts the lazily-built engine's
    own stores down when the worker stops.
    """
    from market_service.nooa_harness.inference_runner import _build_engine

    engine_holder = {}

    async def _dispatch(envelope: WakeEnvelope) -> dict[str, Any]:
        if "engine" not in engine_holder:
            engine_holder["engine"] = await _build_engine(
                settings, symbol=envelope.symbol, venue=envelope.venue,
            )
        engine = engine_holder["engine"]
        artifact, cycle_meta = await engine.run_cycle(
            envelope, {"decision": "fire", "source": "event_driven",
                       "consumed_wake_ids": [envelope.wake_id]},
        )
        return {
            "artifact_id": artifact.artifact_id,
            "status": artifact.status,
            "cycle": cycle_meta,
        }

    async def _close() -> None:
        engine = engine_holder.pop("engine", None)
        if engine is not None:
            try:
                await engine.close()
            except Exception:
                log.exception("engine dispatcher close failed")

    return _dispatch, _close


async def run_until_stopped(config: WakeSupervisorConfig) -> int:
    """Async supervisor entrypoint (used by CLI)."""
    supervisor = await _build_supervisor(config)
    try:
        return await supervisor.run_forever()
    except KeyboardInterrupt:
        await supervisor.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m market_service.nooa_harness.wake_worker``.

    ``--once`` runs a single bounded tick instead of the async loop
    (useful for smoke tests / a first cold-start fire).
    """
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Event-driven inference wake worker")
    parser.add_argument("symbol", nargs="?", default=None)
    parser.add_argument("--once", action="store_true", help="run a single bounded tick")
    parser.add_argument("--read-block-ms", type=int, default=None)
    parser.add_argument("--interval", type=float, default=0.0,
                        help="extra seconds to run for (0 = forever)")
    args = parser.parse_args(argv)

    config = WakeSupervisorConfig.from_env(args.symbol)
    if args.read_block_ms is not None:
        config.read_block_ms = args.read_block_ms
    if args.interval:
        config.interval = args.interval
    config.once = args.once

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config.once:
        return asyncio.run(_once(config))
    return asyncio.run(run_until_stopped(config))


async def _once(config: WakeSupervisorConfig) -> int:
    supervisor = await _build_supervisor(config)
    await supervisor.start()
    try:
        await supervisor._tick()
        await supervisor._read_once(block_ms=10)
        await supervisor._read_status_once(block_ms=10)
    finally:
        await supervisor.stop()
    return supervisor.fired_count


if __name__ == "__main__":
    raise SystemExit(main())