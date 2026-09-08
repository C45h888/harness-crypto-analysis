"""SubstrateWorkerCore — the shared transport of every substrate worker.

One worker per substrate (spec: docs/SUBSTRATE_WORKER_SPEC.md). The core owns
ALL of the transport and fire orchestration and NONE of the market semantics:

* consumer group per (substrate, symbol) on the raw evidence stream
  (``XREADGROUP BLOCK``, ``noack=True`` — at-most-once like wake_worker;
  compute is idempotent, the next event re-triggers);
* L1 arrival gate, L3 time guards (cooldown + staleness heartbeat), L4
  liveness guards (cold start; recovery wired in Phase 2 with the WS plane);
* dedupe via a supervisor-key Lua script (same-condition collapse across
  redundant worker instances);
* supervisor heartbeat (TTL-bounded liveness key — observability, never
  control);
* persistence seam: atomic ``publish_substrate_state`` (Lua SET latest +
  XADD stream);
* dispatch: every fire runs as an ``asyncio`` task so the read loop stays
  responsive; recoverable Redis errors back off, never kill the loop.

The worker FILE (subclass) owns exactly two hooks and the substrate purity
rule: it imports exactly ONE calculation substrate and implements —

    SUBSTRATE_NAME              the registry name (== substrate module)
    probe(window, last, now)    L2 significance probe — deterministic,
                                thresholds imported from the substrate
    compute(evidence, depth)    the full substrate calculation (pure)

The fire decision: fire = (L1 AND L2) OR L3(staleness) OR L4(cold start),
then dedupe, then compute, then persist. Same stream entries + same last
state => same decision => same output (replay-safe).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from market_service.runtime.raw_window import build_raw_window
from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.substrate_worker.contracts import (
    ROLLOVER_PERIOD_MS,
    SUBSTRATE_STATE_SCHEMA_VERSION,
    CadenceProfile,
    SubstrateStatePayload,
    TriggerDecision,
)

log = logging.getLogger(__name__)

READ_BLOCK_MS = 1_000          # XREADGROUP blocking wait (ms)
TICK_RESET_MS = 5_000          # supervisor heartbeat cadence (ms)
SUPERVISOR_MS = 5_000          # supervisor heartbeat TTL (ms)
DEFAULT_COOLDOWN_S = 30        # min seconds between fires (per worker)
DEFAULT_STALENESS_S = 120      # max age of the latest projection before a
                               # staleness override fire
_EVT_READ_LIMIT = 500          # max stream entries per read burst


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_recoverable_redis_exc(exc: Exception) -> bool:
    """True for Redis connection-level errors the worker should back off on."""
    return type(exc).__name__ in (
        "ConnectionError", "ResponseError", "TimeoutError", "RedisError",
        "ConnectionClosedError", "ConnectionResetError", "OperationalError",
    )


# Same-condition collapse across worker instances (adapted from the wake
# worker's dedupe script): a fire is a duplicate when the consumed high-water
# has not advanced AND the trigger source is unchanged. Cooldown is NOT
# enforced here — that is the L3 gate in the loop; the script collapses only
# on condition identity.
_DEDUPE_LUA = r"""
-- deterministic substrate fire dedupe (condition-identity collapse)
-- KEYS[1] = supervisor key; ARGV[1] = candidate json; ARGV[2] = cooldown_ms;
-- ARGV[3] = high_water (consumed entry id or ''); ARGV[4] = trigger source
local current = redis.call("GET", KEYS[1])
if current then
    local ok, dec = pcall(cjson.decode, current)
    if ok and type(dec) == "table" then
        local last_water = tostring(dec.high_water or "")
        local last_source = tostring(dec.trigger_source or "")
        if tostring(ARGV[3]) == last_water and tostring(ARGV[4]) == last_source then
            return "0"
        end
    end
end
redis.call("SET", KEYS[1], ARGV[1])
return "1"
"""


def _store_fn(store: Any, name: str) -> Any | None:
    """Fetch an optional store method; None when the seam lacks it.

    The probe-purity test seam (``_NoStore``) raises on any store access
    beyond the namespace builders — an absent WS/derivative surface is a
    normal "no input" case, not an error, so swallow and return None.
    """
    try:
        return getattr(store, name, None)
    except Exception:
        return None


class SubstrateWorkerCore:
    """Runnable worker: bounded to the Redis plane, event-driven, async loop.

    Subclasses provide ``SUBSTRATE_NAME`` / ``probe`` / ``compute`` (see the
    module docstring); everything else is transport owned here.
    """

    SUBSTRATE_NAME: str = ""
    INPUT_STREAMS: tuple[str, ...] = ("raw",)
    # Per-worker cadence (Task 2): profile wins unless the operator exports
    # an explicit env override (runner passes None through in that case).
    CADENCE: CadenceProfile | None = None
    # Declared cross-worker data dependencies (signals worker only): names
    # from WORKER_REGISTRY loaded via read_substrate_latest at fire time and
    # injected into the compute evidence under "substrate_dependencies".
    # A stale/missing dependency turns the fire into insufficient_data.
    DEPENDENCIES: tuple[str, ...] = ()
    # Derivative-cache inputs (delta/technicals/oi workers): futures keys
    # merged from the derivative evidence cache into the window BEFORE the
    # probe runs, so probes stay pure (window in, decision out) while the
    # core owns the I/O. Missing/stale cache → dormant with a reason.
    DERIVATIVE_INPUTS: tuple[str, ...] = ()
    DERIVATIVE_FRESH_MS: int = 300_000

    def __init__(
        self,
        store: RedisRuntimeStore,
        *,
        symbol: str,
        window_minutes: int = 15,
        depth: int | None = None,
        cooldown_s: int | None = None,
        staleness_s: int | None = None,
        ws_venue: str | None = None,
        read_block_ms: int = READ_BLOCK_MS,
        tick_reset_ms: int = TICK_RESET_MS,
        supervisor_ms: int = SUPERVISOR_MS,
        dispatcher: Callable[[SubstrateStatePayload], Awaitable[dict[str, Any]]] | None = None,
        pg_store: Any | None = None,
        pg_strict: bool = True,
    ) -> None:
        if not self.SUBSTRATE_NAME:
            raise ValueError(
                f"{type(self).__name__} must define a non-empty SUBSTRATE_NAME"
            )
        if "raw" not in self.INPUT_STREAMS:
            raise ValueError(
                f"{type(self).__name__}: Phase 1 requires the raw input stream"
            )
        self.store = store
        self.symbol = symbol.upper()
        self.window_minutes = window_minutes
        self.depth = depth
        profile = type(self).CADENCE
        self.cooldown_s = (
            cooldown_s if cooldown_s is not None
            else (profile.cooldown_s if profile is not None else DEFAULT_COOLDOWN_S)
        )
        self.staleness_s = (
            staleness_s if staleness_s is not None
            else (profile.staleness_s if profile is not None else DEFAULT_STALENESS_S)
        )
        self.ws_venue = (
            ws_venue or os.getenv("MICROSTRUCTURE_VENUE") or "futures"
        ).lower()
        self.ws_input = bool(profile.ws_input) if profile is not None else False
        self.read_block_ms = read_block_ms
        self.tick_reset_ms = tick_reset_ms
        self.supervisor_ms = supervisor_ms
        self.dispatcher = dispatcher
        # Phase 3 PG-first ledger: when set, every fire inserts Postgres
        # BEFORE the Redis publish. Strict mode aborts the fire on PG
        # failure (no publish); lax mode logs and continues.
        self.pg_store = pg_store
        self.pg_strict = pg_strict

        self._stream = store.raw_stream(self.symbol)
        self._group = f"substrate:{self.SUBSTRATE_NAME}:{self.symbol}"
        self._consumer = f"substrate-{self.SUBSTRATE_NAME}-{os.getpid()}-{int(time.time())}"
        self._supervisor_key = store.substrate_supervisor_key(
            self.SUBSTRATE_NAME, self.symbol,
        )
        # WS input surface (Phase 2): a second consumer group per
        # (worker, symbol) on the shared microstructure event stream, plus
        # the status-transition stream tail (wake-worker "$" pattern).
        self._ws_group = f"substrate:{self.SUBSTRATE_NAME}:{self.symbol}:ws"
        self._ws_consumer = f"{self._consumer}-ws"
        self._ws_stream: str | None = None
        self._status_stream: str | None = None
        if self.ws_input and "microstructure" in self.INPUT_STREAMS:
            event_fn = _store_fn(store, "microstructure_event_stream")
            status_fn = _store_fn(store, "microstructure_status_stream")
            if callable(event_fn):
                self._ws_stream = event_fn(self.ws_venue, self.symbol)
            if callable(status_fn):
                self._status_stream = status_fn(self.ws_venue, self.symbol)

        self._redis = store.redis
        self._dedupe_lua_sha: str | None = None

        self._last_fire_ms: int | None = None
        self._last_error: str | None = None
        self._last_dormant_reason: str | None = None
        self._prev_newest_ms: int | None = None
        self._fired = 0
        self._ticks = 0
        self._liveness = False
        self._running = False
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Hooks (worker file implements)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        """L2 significance probe — deterministic; substrate thresholds only."""
        raise NotImplementedError

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        """Full substrate calculation over the evidence window (pure)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Redis plane setup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._ensure_group()
        await self._register_scripts()

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" in str(exc).upper():
                log.debug("substrate group %s already exists", self._group)
            else:
                raise
        if self._ws_stream is not None:
            try:
                await self._redis.xgroup_create(
                    self._ws_stream, self._ws_group, id="0", mkstream=True,
                )
            except Exception as exc:
                if "BUSYGROUP" in str(exc).upper():
                    log.debug("substrate ws group %s already exists", self._ws_group)
                else:
                    raise

    async def _register_scripts(self) -> None:
        if self._dedupe_lua_sha is not None:
            return
        try:
            self._dedupe_lua_sha = str(await self._redis.script_load(_DEDUPE_LUA))
        except Exception as exc:
            log.warning("substrate dedupe script registration failed: %s", exc)
            self._last_error = f"script_register: {exc}"
            self._dedupe_lua_sha = None

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> int:
        """Async supervisor loop — never exits except on fatal config error."""
        await self.start()
        last_tick = _now_ms()
        self._running = True
        while self._running:
            now = _now_ms()
            if now - last_tick >= self.tick_reset_ms:
                await self._tick(now)
                last_tick = now
            try:
                rows = await self._read_once()
                ws_rows, recovery = await self._read_ws_once()
                await self._handle_rows(rows, now, ws_rows=ws_rows, recovery=recovery)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_recoverable_redis_exc(exc):
                    log.warning("substrate %s loop backed off: %s", self.SUBSTRATE_NAME, exc)
                    self._last_error = f"loop: {exc}"
                    await asyncio.sleep(min(5.0, max(0.2, self.read_block_ms / 2_000)))
                else:
                    log.exception("substrate %s loop iteration failed", self.SUBSTRATE_NAME)
                    self._last_error = f"loop: {exc}"
                    await asyncio.sleep(1.0)
        return self._fired

    async def _read_once(self, block_ms: int | None = None) -> list[dict[str, Any]]:
        """Blocking read of ONE batch of the raw stream (new entries only)."""
        try:
            resp = await self._redis.xreadgroup(
                self._group, self._consumer,
                {self._stream: ">"},
                count=_EVT_READ_LIMIT,
                block=block_ms if block_ms is not None else self.read_block_ms,
                noack=True,
            )
        except Exception as exc:
            if is_recoverable_redis_exc(exc):
                self._last_error = f"read: {exc}"
                return []
            raise
        rows: list[dict[str, Any]] = []
        if not resp:
            return rows
        for _key, entries in resp:
            for entry_id, fields in entries or []:
                rows.append({
                    "id": entry_id.decode("utf-8", errors="replace")
                    if isinstance(entry_id, bytes) else str(entry_id),
                    "fields": fields if isinstance(fields, dict) else {},
                })
        return rows

    # ------------------------------------------------------------------
    # WS input surface (Phase 2): microstructure events + status recovery
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_ms(entry_id: str) -> int | None:
        """Event-time millis from a Redis stream entry id (``ms-seq``)."""
        try:
            return int(str(entry_id).split("-", 1)[0])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _status_transition_recovery(fields: dict[str, Any]) -> dict[str, Any] | None:
        """Detect a gap/reconnecting → running transition (L4 recovery).

        Mirrors the wake-worker ``_transition_from_state`` semantics: the
        capture node writes a status-transition entry on every state change.
        A transition INTO an established state from a degraded one is a
        recovery fire; anything else is routine.
        """
        to_state = fields.get("state") or fields.get("to_state")
        from_state = fields.get("from_state") or fields.get("from")
        raw_payload = fields.get("payload")
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8", errors="replace")
        if isinstance(raw_payload, str) and raw_payload:
            try:
                decoded = json.loads(raw_payload)
            except (ValueError, TypeError):
                decoded = None
            if isinstance(decoded, dict):
                to_state = to_state or decoded.get("state") or decoded.get("to_state")
                from_state = (
                    from_state or decoded.get("from_state") or decoded.get("from")
                )
        to_s = str(to_state).lower() if to_state is not None else ""
        from_s = str(from_state).lower() if from_state is not None else ""
        if to_s in ("running", "connected") and from_s in ("gap", "reconnecting"):
            return {"from_state": from_s, "to_state": to_s}
        return None

    async def _read_ws_once(
        self, block_ms: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read ONE batch of WS events + the status-transition tail.

        Returns ``(ws_rows, recovery)``. Workers without ``ws_input`` (or a
        store without the microstructure surface, e.g. test seams) return
        ``([], False)`` without touching Redis.
        """
        if self._ws_stream is None and self._status_stream is None:
            return [], False
        ws_rows: list[dict[str, Any]] = []
        recovery = False
        if self._ws_stream is not None:
            try:
                resp = await self._redis.xreadgroup(
                    self._ws_group, self._ws_consumer,
                    {self._ws_stream: ">"},
                    count=_EVT_READ_LIMIT,
                    block=block_ms if block_ms is not None else self.read_block_ms,
                    noack=True,
                )
            except Exception as exc:
                if is_recoverable_redis_exc(exc):
                    self._last_error = f"ws read: {exc}"
                    return [], False
                raise
            for _key, entries in resp or []:
                for entry_id, fields in entries or []:
                    ws_rows.append({
                        "id": entry_id.decode("utf-8", errors="replace")
                        if isinstance(entry_id, bytes) else str(entry_id),
                        "fields": fields if isinstance(fields, dict) else {},
                    })
        if self._status_stream is not None:
            try:
                resp = await self._redis.xread(
                    {self._status_stream: "$"},
                    count=_EVT_READ_LIMIT,
                    block=0,
                )
            except Exception as exc:
                if is_recoverable_redis_exc(exc):
                    self._last_error = f"ws status read: {exc}"
                    return ws_rows, False
                raise
            if isinstance(resp, (list, tuple)):
                for _key, entries in resp:
                    for entry in entries or []:
                        if isinstance(entry, (list, tuple)) and len(entry) == 2:
                            _eid, fields = entry
                        elif isinstance(entry, dict):
                            fields = entry.get("fields", entry)
                        else:
                            continue
                        if not isinstance(fields, dict):
                            continue
                        detail = self._status_transition_recovery(fields)
                        if detail is not None:
                            recovery = True
        return ws_rows, recovery

    # ------------------------------------------------------------------
    # Fire orchestration — fire = (L1 AND L2) OR L3(staleness) OR L4(cold)
    # ------------------------------------------------------------------

    async def _handle_rows(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        last_state = await self.store.read_substrate_latest(
            self.SUBSTRATE_NAME, self.symbol,
        )
        # Version-mismatch cold start: never probe against an incompatible
        # prior — treat it as absent (log + overwrite on the next fire).
        if last_state is not None:
            try:
                seen_version = int(last_state.get("schema_version") or 0)
            except (TypeError, ValueError):
                seen_version = 0
            if seen_version != SUBSTRATE_STATE_SCHEMA_VERSION:
                log.info(
                    "substrate %s schema mismatch (saw %r) — cold-starting",
                    self.SUBSTRATE_NAME, last_state.get("schema_version"),
                )
                last_state = None

        ws_rows = ws_rows or []
        arrival = bool(rows) or bool(ws_rows)

        # L4 — cold start: data arrived but no (usable) prior projection.
        if last_state is None:
            if not arrival:
                return  # nothing to compute from yet; heartbeat continues
            decision = TriggerDecision(fired=True, source="cold_start",
                                       predicates={"consumed_entries": len(rows) + len(ws_rows)})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L4 — capture recovery: a gap/reconnecting → running transition was
        # observed on the status stream. Hygiene fire: bypasses cooldown.
        if recovery:
            decision = TriggerDecision(fired=True, source="recovery",
                                       predicates={"transition": "gap/reconnecting->running"})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L3 — staleness override: the projection is older than the bound.
        # Hygiene fire: bypasses the cooldown gate (the Phase 1 bug).
        computed_at = last_state.get("computed_at_ms")
        age_ms = (now_ms - int(computed_at)) if isinstance(computed_at, (int, float)) else None
        staleness_due = (
            age_ms is not None and age_ms > self.staleness_s * 1_000
        )
        if staleness_due:
            decision = TriggerDecision(
                fired=True, source="staleness",
                predicates={"age_ms": age_ms, "staleness_s": self.staleness_s},
            )
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L3 — window rollovers: a period boundary crossed between the
        # previous and current batch's newest entries. Boundary fire:
        # bypasses the cooldown gate even when the probe is quiet.
        rollover = self._rollover_decision(rows or ws_rows)
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L1 — arrival gate: only build the (heavy) evidence window when new
        # data actually arrived on any surface.
        if not arrival:
            return

        window = await build_raw_window(self.store, self.symbol, self.window_minutes)

        # Derivative-cache inputs: merged BEFORE the probe so probes stay
        # pure (window in, decision out) while the core owns the I/O.
        # Missing/stale cache → dormant with a supervisor reason.
        deriv_reason = await self._attach_derivatives(window, now_ms)
        if deriv_reason is not None:
            self._last_dormant_reason = deriv_reason
            return

        # Declared cross-worker inputs: loaded BEFORE the probe so probes
        # stay pure (window in, decision out). A missing/stale dependency
        # keeps the worker dormant here; a hygiene fire that proceeds
        # without it computes to insufficient_data instead.
        dep_reason = await self._attach_dependencies(window, now_ms)
        if dep_reason is not None:
            self._last_dormant_reason = dep_reason
            return
        self._last_dormant_reason = None
        # Minimum-data gate: the probe may not fire on a thin window.
        profile = type(self).CADENCE
        min_depth = profile.min_book_depth if profile is not None else 1
        min_trades = profile.min_trade_count if profile is not None else 0
        if min_depth > 1 or min_trades > 0:
            fut_book = (window.get("futures") or {}).get("order_book") or {}
            book_depth = max(len(fut_book.get("bids") or []), len(fut_book.get("asks") or []))
            coverage = window.get("coverage") or {}
            trade_count = (
                ((coverage.get("spot_trades") or {}).get("trade_count") or 0)
                + ((coverage.get("futures_trades") or {}).get("trade_count") or 0)
            )
            if book_depth < min_depth or trade_count < min_trades:
                self._last_dormant_reason = (
                    f"insufficient_inputs: book_depth={book_depth} "
                    f"(min {min_depth}) trade_count={trade_count} (min {min_trades})"
                )
                return
        self._last_dormant_reason = None

        decision = self.probe(window, last_state, now_ms)
        if not decision.fired:
            return

        # Cooldown gates ONLY probe-source fires. Hygiene/boundary fires
        # (staleness/rollover/cold_start/recovery) return above and never
        # reach this gate.
        if self._last_fire_ms is not None:
            if now_ms - self._last_fire_ms < self.cooldown_s * 1_000:
                return

        await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows))

    def _rollover_decision(self, rows: list[dict[str, Any]]) -> TriggerDecision | None:
        """Fire when a declared period boundary crossed between batches.

        Compares ``ts // period`` of the previous vs current batch's newest
        stream entry ids. Advances the high-water mark on every batch with
        rows so each boundary fires exactly once.
        """
        newest_ms = self._entry_ms(rows[-1]["id"]) if rows else None
        previous_ms, self._prev_newest_ms = self._prev_newest_ms, newest_ms or self._prev_newest_ms
        if newest_ms is None or previous_ms is None:
            return None
        profile = type(self).CADENCE
        periods = profile.rollovers if profile is not None else ()
        crossed: dict[str, Any] = {}
        for period in periods:
            length = ROLLOVER_PERIOD_MS.get(period)
            if not length:
                continue
            from_bucket, to_bucket = previous_ms // length, newest_ms // length
            if to_bucket != from_bucket:
                crossed[period] = {
                    "period": period, "from_bucket": from_bucket, "to_bucket": to_bucket,
                }
        if not crossed:
            return None
        return TriggerDecision(fired=True, source="rollover", predicates=crossed)

    @staticmethod
    def _high_water(rows: list[dict[str, Any]]) -> str:
        """The newest consumed entry id — the dedupe/collapse water mark."""
        return rows[-1]["id"] if rows else ""

    async def _fire(
        self,
        decision: TriggerDecision,
        last_state: dict[str, Any] | None,
        now_ms: int,
        *,
        high_water: str,
    ) -> None:
        """Dedupe, then dispatch the compute+persist cycle as a task."""
        allowed = await self._dedupe(decision, high_water, now_ms)
        if not allowed:
            log.info("substrate %s fire deduped (source=%s)", self.SUBSTRATE_NAME, decision.source)
            return
        self._last_fire_ms = now_ms
        self._fired += 1
        log.info(
            "substrate %s FIRED symbol=%s source=%s predicates=%s",
            self.SUBSTRATE_NAME, self.symbol, decision.source, sorted(decision.predicates),
        )
        task = asyncio.create_task(self._fire_guarded(decision, last_state, now_ms))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fire_guarded(
        self,
        decision: TriggerDecision,
        last_state: dict[str, Any] | None,
        now_ms: int,
    ) -> None:
        """Compute the substrate and persist the state payload (never raises)."""
        try:
            evidence = await build_raw_window(self.store, self.symbol, self.window_minutes)
            # Best-effort derivative merge for compute (a hygiene fire may
            # proceed without cache; compute then reports insufficient_data).
            try:
                await self._attach_derivatives(evidence, now_ms)
            except Exception as exc:
                log.warning("substrate %s derivative attach failed: %s",
                            self.SUBSTRATE_NAME, exc)
            dependencies = await self._load_dependencies()
            if dependencies:
                evidence = {**evidence, "substrate_dependencies": dependencies}
            # Own previous output: the signals worker needs the snapshot it
            # persisted last cycle as `previous`. Namespaced, always present.
            evidence = {**evidence, "own_last_output": (
                (last_state or {}).get("output") or {}
            )}
            output = self.compute(evidence, self.depth if self.depth is not None else evidence.get("depth_levels") or 20)
            missing: list[str] = []
            status = "healthy"
            if not output:
                status = "insufficient_data"
                missing = ["substrate output empty"]
            payload = SubstrateStatePayload.create(
                substrate=self.SUBSTRATE_NAME,
                symbol=self.symbol,
                output=output,
                trigger=decision,
                freshness=self._freshness(evidence, high_water=None),
                observed_at_ms=evidence.get("observed_at_ms"),
                computed_at_ms=now_ms,
                missing_inputs=missing,
            )
            payload = (
                payload if status == "healthy"
                else self._with_status(payload, status, missing)
            )
            if self.dispatcher is not None:
                await self.dispatcher(payload)
            # Phase 3 PG-first: durable insert precedes the Redis publish.
            if self.pg_store is not None:
                try:
                    await self.pg_store.record_substrate_state(
                        self.symbol, self.SUBSTRATE_NAME, payload.to_dict(),
                    )
                except Exception as exc:
                    if self.pg_strict:
                        log.error("substrate %s PG insert failed (strict: fire aborted): %s",
                                    self.SUBSTRATE_NAME, exc)
                        self._last_error = f"pg: {exc}"
                        return
                    log.warning("substrate %s PG insert failed (lax: continuing): %s",
                                self.SUBSTRATE_NAME, exc)
                    self._last_error = f"pg (lax, continuing): {exc}"
            await self.store.publish_substrate_state(
                self.SUBSTRATE_NAME, self.symbol, payload.to_dict(),
            )
        except Exception as exc:
            log.exception("substrate %s fire failed", self.SUBSTRATE_NAME)
            self._last_error = f"fire: {exc}"

    async def _attach_derivatives(
        self, window: dict[str, Any], now_ms: int,
    ) -> str | None:
        """Merge declared derivative-cache keys into the window futures.

        Returns a dormant reason when the cache is missing/stale, else None.
        Workers without ``DERIVATIVE_INPUTS`` are untouched (no store read).
        """
        wanted = type(self).DERIVATIVE_INPUTS
        if not wanted:
            return None
        reader = _store_fn(self.store, "read_derivative_evidence")
        deriv = await reader(self.symbol) if callable(reader) else None
        observed = (deriv or {}).get("observed_at_ms")
        age_ms = now_ms - int(observed) if isinstance(observed, (int, float)) else None
        fresh_ms = type(self).DERIVATIVE_FRESH_MS
        if (
            not isinstance(deriv, dict)
            or age_ms is None or age_ms < 0 or age_ms > fresh_ms
        ):
            return f"derivative_cache_missing_or_stale: inputs={list(wanted)}"
        deriv_fut = deriv.get("futures") or {}
        merged = dict(window.get("futures") or {})
        missing = [k for k in wanted if deriv_fut.get(k) is None]
        if missing:
            return f"derivative_inputs_missing: {missing}"
        for key in wanted:
            merged[key] = deriv_fut[key]
        window["futures"] = merged
        return None

    async def _attach_dependencies(
        self, window: dict[str, Any], now_ms: int,
    ) -> str | None:
        """Merge declared cross-worker latests into the window.

        Returns a dormant reason when a dependency is missing/stale.
        Staleness is measured against this worker's own staleness bound —
        a dependency older than that cannot inform a fresh fire.
        """
        wanted = type(self).DEPENDENCIES
        if not wanted:
            return None
        loaded = await self._load_dependencies()
        missing = [n for n in wanted
                   if not isinstance(loaded.get(n), dict)
                   or loaded[n].get("available") is False]
        if missing:
            return f"dependency_missing: {missing}"
        stale = []
        for name in wanted:
            computed = loaded[name].get("computed_at_ms")
            age = now_ms - int(computed) if isinstance(computed, (int, float)) else None
            if age is None or age < 0 or age > self.staleness_s * 1_000:
                stale.append(name)
        if stale:
            return f"dependency_stale: {stale}"
        window["substrate_dependencies"] = loaded
        return None

    async def _load_dependencies(self) -> dict[str, Any]:
        """Load declared cross-worker inputs (signals worker only).

        Each ``DEPENDENCIES`` name is read via ``read_substrate_latest``;
        the result maps name → latest payload (or ``{"available": False}``
        when missing — the worker reports insufficient_data, never zeros).
        """
        loaded: dict[str, Any] = {}
        for name in type(self).DEPENDENCIES:
            try:
                latest = await self.store.read_substrate_latest(name, self.symbol)
            except Exception as exc:
                log.warning("substrate %s dependency %s read failed: %s",
                            self.SUBSTRATE_NAME, name, exc)
                latest = None
            loaded[name] = latest if isinstance(latest, dict) else {"available": False}
        return loaded

    @staticmethod
    def _with_status(
        payload: SubstrateStatePayload, status: str, missing: list[str],
    ) -> SubstrateStatePayload:
        return SubstrateStatePayload(
            schema_version=payload.schema_version,
            substrate=payload.substrate,
            symbol=payload.symbol,
            status=status,
            observed_at_ms=payload.observed_at_ms,
            computed_at_ms=payload.computed_at_ms,
            trigger=payload.trigger,
            freshness=payload.freshness,
            output=payload.output,
            missing_inputs=tuple(missing),
            provenance=payload.provenance,
        )

    def _freshness(self, evidence: dict[str, Any], *, high_water: str | None) -> dict[str, Any]:
        coverage = evidence.get("coverage") or {}
        return {
            "window_minutes": self.window_minutes,
            "input_fingerprint": {
                "observed_at_ms": evidence.get("observed_at_ms"),
                "stream_staleness_ms": coverage.get("stream_staleness_ms"),
                "spot_trade_count": (coverage.get("spot_trades") or {}).get("trade_count"),
                "futures_trade_count": (coverage.get("futures_trades") or {}).get("trade_count"),
                "high_water": high_water,
            },
        }

    # ------------------------------------------------------------------
    # Dedupe + supervisor
    # ------------------------------------------------------------------

    async def _dedupe(self, decision: TriggerDecision, high_water: str, now_ms: int) -> bool:
        """Collapse identical fire conditions via the supervisor Lua script."""
        candidate = json.dumps(
            {
                "high_water": high_water,
                "trigger_source": decision.source,
                "fired_stamp_ms": now_ms,
                "now_ms": now_ms,
                "predicate_keys": sorted(decision.predicates),
            },
            separators=(",", ":"),
        )
        try:
            if self._dedupe_lua_sha is None:
                await self._register_scripts()
            if self._dedupe_lua_sha is None:
                return True  # script unavailable — fire anyway (availability > strict dedupe)
            raw = await self._redis.evalsha(
                self._dedupe_lua_sha, 1, self._supervisor_key,
                candidate, str(self.cooldown_s * 1_000),
                str(high_water), str(decision.source), str(now_ms),
            )
        except Exception as exc:
            log.warning("substrate dedupe eval failed (fire anyway): %s", exc)
            return True
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return raw == "1"

    async def _tick(self, now_ms: int) -> None:
        """Supervisor heartbeat — proves liveness in the deployment."""
        try:
            payload = {
                "state": "running",
                "substrate": self.SUBSTRATE_NAME,
                "symbol": self.symbol,
                "pid": os.getpid(),
                "last_fire_ms": self._last_fire_ms,
                "fired": self._fired,
                "last_error": self._last_error,
                "dormant_reason": self._last_dormant_reason,
                "ws_input": self._ws_stream is not None,
                "last_tick_ms": now_ms,
            }
            await self._redis.setex(
                self._supervisor_key, self.supervisor_ms // 1000 + 2,
                json.dumps(payload, default=str, separators=(",", ":")),
            )
            self._ticks += 1
            self._liveness = True
        except Exception as exc:
            if is_recoverable_redis_exc(exc):
                self._liveness = False
                log.warning("substrate supervisor tick failed: %s", exc)
            else:
                raise

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    @property
    def liveness(self) -> bool:
        return self._liveness

    @property
    def fired_count(self) -> int:
        return self._fired