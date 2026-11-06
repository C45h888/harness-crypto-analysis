"""Base substrate contract — what a worker IS, without transport.

SEMANTIC JURISDICTION: this file owns the SUBSTRATE contract that every
worker file implements:

* class attributes (``SUBSTRATE_NAME``, ``INPUT_STREAMS``, ``CADENCE``,
  ``DEPENDENCIES``, ``DERIVATIVE_INPUTS``, ``DERIVATIVE_FRESH_MS``);
* the ``__init__`` that resolves per-worker cadence/profile defaults and
  builds the Redis keyspace/group/consumer names;
* the two pure hooks the worker file overrides (``probe``/``compute``);
* ``_store_fn`` — the probe-purity test seam helper.

It does NOT own transport reads (``reader``), fire decisions (``fire``), or
the supervisor heartbeat (``supervisor``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from market_service.runtime.horizons import (
    HORIZON_PERIOD_MS,
    fold_snapshot,
    fold_state_summary,
    fold_trades,
    horizon_minutes,
    new_fold_state,
    restore_fold_state,
)
from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.substrate_worker.contracts import (
    SUBSTRATE_STATE_SCHEMA_VERSION,
    ArtifactBundle,
    CadenceProfile,
    SubstrateStatePayload,
    TriggerDecision,
    build_freshness,
    ledger_substrate_name,
    with_status as _contracts_with_status,
)

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_S = 30
DEFAULT_STALENESS_S = 120
READ_BLOCK_MS = 1_000
# Heartbeat TTL. The heartbeat refreshes once per completed read cycle, but
# a busy loop (big fire tasks, GC, PG pressure) can delay the refresh past a
# tight TTL — observed as /health flapping degraded on healthy workers under
# fire pressure. 12s tick = 14s TTL absorbs multi-second loop stalls without
# masking a genuinely dead task (a dead loop never refreshes at all).
SUPERVISOR_MS = 12_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _store_fn(store: Any, name: str) -> Any | None:
    """Fetch an optional store method; None when the seam lacks it."""
    try:
        return getattr(store, name, None)
    except Exception:
        return None


class SubstrateBase:
    """Substrate contract: class attrs, resolution, hooks. No transport."""

    SUBSTRATE_NAME: str = ""
    INPUT_STREAMS: tuple[str, ...] = ("raw",)
    CADENCE: CadenceProfile | None = None
    DEPENDENCIES: tuple[str, ...] = ()
    DERIVATIVE_INPUTS: tuple[str, ...] = ()
    DERIVATIVE_FRESH_MS: int = 300_000

    # Plane identity. "substrate" = the calculation plane (wakes on raw
    # REST evidence). "analysis" = the analysis plane — it binds a
    # substrate state stream ("substrate:<name>" in INPUT_STREAMS: the
    # calculation plane's own publishes ARE the deterministic trigger)
    # and projects into the analysis: keyspace. The core machinery (fire
    # cascade, reader, supervisor) is shared byte-for-byte; the plane
    # only redirects WHICH stream/keys the worker touches.
    PLANE: str = "substrate"
    # Consumer-group prefix (groups are namespaced per stream; this keeps
    # analysis groups identifiable on the substrate state streams).
    GROUP_PREFIX: str = "substrate"
    # Phase H1 — declared time horizons (docs/HORIZON_RETENTION_SPEC.md).
    # () = legacy single-window behavior (byte-for-byte unchanged). A
    # worker declaring e.g. ("15m", "1h", "4h") gets per-horizon fold
    # state maintained by the cadence layer, per-horizon evidence windows
    # attached to compute input, and horizon-tagged rollover fires.
    HORIZONS: tuple[str, ...] = ()
    # Fold-native horizons need NO raw horizon evidence: the worker computes
    # its per-horizon outputs from the bounded in-memory folds (tape CVD /
    # technicals fold-candles), which never touch evidence["horizons"].
    # True = _build_evidence also materializes one raw window per declared
    # horizon (migration runs hz keystone migration on them; density runs
    # hz trade intensity on them). False = base window only.
    RAW_HORIZON_EVIDENCE: bool = True

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
        supervisor_ms: int = SUPERVISOR_MS,
        dispatcher: Any | None = None,
        governor: Any | None = None,
        pg_store: Any | None = None,
        pg_strict: bool = True,
        telemetry: Any | None = None,
    ) -> None:
        if not self.SUBSTRATE_NAME:
            raise ValueError(
                f"{type(self).__name__} must define a non-empty SUBSTRATE_NAME"
            )
        if "raw" not in self.INPUT_STREAMS and not any(
            isinstance(s, str) and s.startswith("substrate:")
            for s in self.INPUT_STREAMS
        ):
            raise ValueError(
                f"{type(self).__name__}: INPUT_STREAMS must bind 'raw' "
                f"(calculation plane) or a 'substrate:<name>' state stream "
                f"(analysis plane)"
            )
        self.store = store
        self._plane = getattr(type(self), "PLANE", "substrate")
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
        self.supervisor_ms = supervisor_ms
        self.dispatcher = dispatcher
        # Plane governance gate (docs/PLANE_GOVERNANCE_SPEC.md). Duck-typed:
        # an object exposing async ``authorize_fire`` / ``authorize_publish`` /
        # ``authorize_pg_write``. ``None`` = ungoverned legacy behaviour
        # (byte-identical to before this seam existed). The core never imports
        # governance — the plane's governor is injected at construction.
        self.governor = governor
        # PG is horizon persistence, not a latest sink: only workers that
        # restore folds from PG (non-empty HORIZONS) keep a live pg_store.
        # The other eight workers run PG-free — no insert, no strict abort,
        # no breaker — so PG semantics stay pure to the workers that need it.
        self.pg_store = pg_store if type(self).HORIZONS else None
        self.pg_strict = pg_strict
        # Observational mirror only: the plane FSM remains the legality owner.
        self.telemetry = telemetry

        self._stream = self._resolve_input_stream(store)
        self._group = (
            f"{getattr(type(self), 'GROUP_PREFIX', 'substrate')}"
            f":{self.SUBSTRATE_NAME}:{self.symbol}"
        )
        self._consumer = f"substrate-{self.SUBSTRATE_NAME}-{os.getpid()}-{int(time.time())}"
        if self._plane == "analysis":
            self._supervisor_key = store.analysis_supervisor_key(
                self.SUBSTRATE_NAME, self.symbol,
            )
        else:
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
        # Phase H1 — per-horizon fold state (in-memory; summaries persist in
        # the payload and rebuild via backfill/PG on cold start).
        for hz in type(self).HORIZONS:
            if hz not in HORIZON_PERIOD_MS:
                raise ValueError(
                    f"{type(self).__name__}: unknown horizon {hz!r}; "
                    f"expected one of {sorted(HORIZON_PERIOD_MS)}"
                )
        self.horizons: tuple[str, ...] = tuple(type(self).HORIZONS)
        self._horizon_folds: dict[str, dict[str, Any]] = {
            hz: new_fold_state(hz) for hz in self.horizons
        }
        self._horizons_backfilled = False
        self._horizons_rebuilt_from: str | None = None
        self._last_fire_ms: int | None = None
        self._last_error: str | None = None
        self._last_dormant_reason: str | None = None
        self._ws_err_log_ms: int = 0
        self._prev_newest_ms: int | None = None
        self._fired = 0
        self._ticks = 0
        self._liveness = False
        self._running = False
        self._tasks: set[Any] = set()
        # Worker-owned single-flight: one compute at a time per worker;
        # a second decision while busy coalesces into exactly one rerun.
        self._fire_lock: asyncio.Semaphore = asyncio.Semaphore(1)
        self._pending: tuple[Any, Any, int, str, dict[str, Any] | None, Any] | None = None
        # Governance-owned dedupe slot (fail-closed); the Lua lives in
        # market_service/governance/dedupe.py, this dict is the worker's slot.
        self._dedupe_state: dict[str, Any] = {}
        self._PG_INSERT_TIMEOUT_S = 10.0

    # ---- hooks (worker file implements) ----

    def _resolve_input_stream(self, store: RedisRuntimeStore) -> str:
        """Bind the worker's blocking read to its trigger stream.

        "raw" (calculation plane) binds the poller's raw evidence stream.
        "substrate:<name>" (analysis plane) binds that substrate's STATE
        stream — the calculation plane's own publishes, which ARE the
        deterministic trigger. First substrate: entry is the primary
        blocking wake; secondary dependencies ride the staleness gate.
        """
        streams = self.INPUT_STREAMS
        if "raw" in streams:
            return store.raw_stream(self.symbol)
        for s in streams:
            if isinstance(s, str) and s.startswith("substrate:"):
                return store.substrate_stream(s.split(":", 1)[1], self.symbol)
        raise ValueError(
            f"{type(self).__name__}: no bindable trigger stream in {streams}"
        )

    async def _read_own_state(self) -> dict[str, Any] | None:
        """Read this worker's last persisted projection (plane-routed)."""
        if self._plane == "analysis":
            return await self.store.read(f"analysis.{self.SUBSTRATE_NAME.lower()}", ids={"symbol": self.symbol})
        return await self.store.read(f"substrate.{self.SUBSTRATE_NAME.lower()}", ids={"symbol": self.symbol})

    async def _publish_own_state(self, payload: dict[str, Any]) -> str:
        """Publish this worker's projection (plane-routed; atomic SET+XADD)."""
        if self._plane == "analysis":
            return await self.store.publish(f"analysis.{self.SUBSTRATE_NAME.lower()}", payload, ids={"symbol": self.symbol})
        return await self.store.publish(f"substrate.{self.SUBSTRATE_NAME.lower()}", payload, ids={"symbol": self.symbol})

    async def _emit_telemetry_state(
        self, event_type: str, state: dict[str, Any], *,
        source_sequence: int, occurred_at_ms: int, source_ref: str | None = None,
    ) -> None:
        """Mirror a governed state outcome without changing worker behavior."""
        if self.telemetry is None:
            return
        try:
            await self.telemetry.emit_state(
                plane="analysis" if self._plane == "analysis" else "calculation",
                owner=self.SUBSTRATE_NAME,
                subject_type="symbol",
                subject_id=self.symbol,
                event_type=event_type,
                source_sequence=max(0, int(source_sequence)),
                occurred_at_ms=max(0, int(occurred_at_ms)),
                state=state,
                source_ref=source_ref,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never gates a fire
            log.warning("telemetry emit failed for %s/%s: %s",
                        self.SUBSTRATE_NAME, self.symbol, exc)

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        """L2 significance probe — deterministic; substrate thresholds only."""
        raise NotImplementedError

    async def screen(
        self, latest: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> bool:
        """Stage-one gate: cheap latest-only check authorizing the heavy read.

        Default True (history workers: probes accumulate window history, so
        no latest-only predicate is a safe necessary condition). Point-in-time
        workers override with the SAME probe over latest-only state (exact —
        screen quiet implies full-probe quiet, zero miss risk).
        """
        return True

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        """Full substrate calculation over the evidence window (pure)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Worker-owned fire cycle (absorbed from deleted core/fire.py).
    # Every worker inherits this; each worker file may override decide()
    # pieces to own its scheduling policy. Governance owns legality
    # (authorize_fire/publish/pg_write) and the fail-closed dedupe slot.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """Default scheduling cascade — workers override this to own WHEN.

        The fallback below is the generic cascade (cold start / recovery /
        staleness / rollover / arrival → probe → cooldown). Every
        calculation-plane worker file overrides decide() with its own
        explicit cascade so the scheduling policy lives in the worker that
        is woken, not in shared code. Analysis-plane workers without an
        override inherit this default.
        """
        last_state = await self._read_own_state()
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

        if self.horizons:
            self._fold_rows(rows)

        # L4 — cold start.
        if last_state is None:
            if not arrival:
                return
            if self.horizons and not self._horizons_backfilled:
                await self._backfill_horizons()
            decision = TriggerDecision(fired=True, source="cold_start",
                                       predicates={"consumed_entries": len(rows) + len(ws_rows)})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L4 — capture recovery.
        if recovery:
            decision = TriggerDecision(fired=True, source="recovery",
                                       predicates={"transition": "gap/reconnecting->running"})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L3 — staleness override (hygiene, bypasses cooldown).
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

        # L3 — per-horizon staleness (trade-time fold last_trade_ms).
        if self.horizons:
            profile_hz = type(self).CADENCE
            for hz in self.horizons:
                bound_s = profile_hz.staleness_for(hz) if profile_hz is not None else self.staleness_s
                segs = self._horizon_folds[hz]["segments"]
                newest_fold_ms = max((s.get("last_trade_ms") or 0) for s in segs) if segs else None
                if newest_fold_ms:
                    hz_age_ms = now_ms - newest_fold_ms
                    if hz_age_ms > bound_s * 1_000:
                        decision = TriggerDecision(
                            fired=True, source="staleness",
                            predicates={"horizon": hz, "age_ms": hz_age_ms,
                                        "staleness_s": bound_s},
                        )
                        await self._fire(decision, last_state, now_ms,
                                         high_water=self._high_water(rows or ws_rows))
                        return

        # L3 — window rollovers.
        rollover = self._rollover_decision(rows or ws_rows)
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L1 — arrival gate: only build the heavy window when data arrived.
        if not arrival:
            return

        # Probe path uses the shared cached evidence (one build per TTL slice).
        window = await self._cached_evidence(now_ms)
        deriv_reason = await self._attach_derivatives(window, now_ms)
        if deriv_reason is not None:
            self._last_dormant_reason = deriv_reason
            return
        dep_reason = await self._attach_dependencies(window, now_ms)
        if dep_reason is not None:
            self._last_dormant_reason = dep_reason
            return
        self._last_dormant_reason = None
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
        # Cooldown gates ONLY probe-source fires.
        if self._last_fire_ms is not None:
            if now_ms - self._last_fire_ms < self.cooldown_s * 1_000:
                return
        # Evidence-once: thread the probe snapshot into compute.
        await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows),
                         evidence=window)

    async def _handle_rows(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """Compat entry — dispatches to the worker-owned decide().

        The supervisor loop, runner --once, and analysis tools call this;
        the worker file's decide() is what actually gates the compute.
        """
        await self.decide(rows, now_ms, ws_rows=ws_rows, recovery=recovery)

    async def _fire(
        self,
        decision: TriggerDecision,
        last_state: dict[str, Any] | None,
        now_ms: int,
        *,
        high_water: str,
        rows: list[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
        artifacts: ArtifactBundle | None = None,
    ) -> None:
        """Governance FIRE gate → fail-closed dedupe → single-flight dispatch.

        ORDER IS THE CONTRACT: breaker, governance, then dedupe, then the
        cooldown stamp and fired counter. A denial burns nothing.
        """
        # Breaker first: a PG-dependent fire into a dead sink stands down
        # before governance, dedupe, slots, or compute burn anything.
        if self.pg_store is not None and await self._pg_circuit_skip(now_ms):
            self._last_dormant_reason = "pg_circuit_open"
            return
        if not await self._govern("fire"):
            return
        allowed = await self._dedupe(decision, high_water, now_ms)
        if not allowed:
            log.info("substrate %s fire deduped/suppressed (source=%s)",
                     self.SUBSTRATE_NAME, decision.source)
            return
        self._last_fire_ms = now_ms
        self._fired += 1
        log.info(
            "substrate %s FIRED symbol=%s source=%s predicates=%s",
            self.SUBSTRATE_NAME, self.symbol, decision.source, sorted(decision.predicates),
        )
        # Single-flight with coalesce: a second decision while compute is in
        # flight collapses into exactly one rerun — never N tasks.
        if self._fire_lock.locked():
            self._pending = (decision, last_state, now_ms, high_water, evidence, artifacts)
            return
        task = asyncio.create_task(
            self._fire_guarded(decision, last_state, now_ms, rows=rows,
                              evidence=evidence, artifacts=artifacts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _pg_circuit_skip(self, now_ms: int) -> bool:
        """Duck-typed breaker consult — ungoverned workers never skip."""
        gov = getattr(self, "governor", None)
        consult = getattr(gov, "pg_circuit_skip", None)
        if not callable(consult):
            return False
        try:
            return bool(consult(int(now_ms)))
        except Exception:
            return False

    def _pg_record(self, ok: bool, now_ms: int) -> None:
        """Feed a PG round-trip outcome to the breaker (never raises)."""
        gov = getattr(self, "governor", None)
        if gov is None:
            return
        try:
            if ok:
                success = getattr(gov, "pg_record_success", None)
                if callable(success):
                    success()
            else:
                failure = getattr(gov, "pg_record_failure", None)
                if callable(failure):
                    failure(int(now_ms))
        except Exception:
            pass

    @asynccontextmanager
    async def _compute_slot(self, reason: str):
        """FSM-governed compute slot (vector 3): one plane-wide bound.

        The governor's fair semaphore serializes heavy sections across all
        workers of the plane — cold-start backfills and fire computes queue
        in batches of the FSM's number. Ungoverned seams (bare test workers
        without a governor) run unbounded, byte-identical to before.
        """
        gov = getattr(self, "governor", None)
        acquire = getattr(gov, "acquire_compute_slot", None)
        release = getattr(gov, "release_compute_slot", None)
        if not (callable(acquire) and callable(release)):
            yield
            return
        await acquire(symbol=self.symbol, substrate=self.SUBSTRATE_NAME, reason=reason)  # type: ignore[misc]
        try:
            yield
        finally:
            await release(symbol=self.symbol, substrate=self.SUBSTRATE_NAME)  # type: ignore[misc]

    async def _backfill_horizons(self) -> None:
        source = "raw"
        try:
            if await self._restore_horizons_from_pg():
                source = "pg"
        except Exception as exc:
            log.warning("substrate %s horizon PG restore failed: %s",
                        self.SUBSTRATE_NAME, exc)
            self._last_error = f"horizon_pg_restore: {exc}"
        try:
            # Shared single-flight horizon build (vector 1): all horizon
            # workers cold-starting together coalesce on one widened
            # materialization per (symbol, minutes) instead of each paying
            # a full unshared 4h read. The PG-restored folds make the merge
            # free (monotonic-id dedupe folds only the ledger-to-now delta).
            from market_service.substrate_worker import evidence as _ev
            max_minutes = max(horizon_minutes(hz) for hz in self.horizons)
            # The widened build + fold runs under the governed slot: all
            # horizon workers cold-starting together queue here in batches.
            async with self._compute_slot("backfill"):
                window = await _ev.horizon_window(self.store, self.symbol, max_minutes)
                for hz in self.horizons:
                    state = self._horizon_folds[hz]
                    for venue in ("spot", "futures"):
                        trades = ((window.get(venue) or {}).get("trades_normalized")) or []
                        fold_trades(state, trades, venue)
            self._horizons_backfilled = True
            self._horizons_rebuilt_from = source
        except Exception as exc:
            log.warning("substrate %s horizon backfill failed: %s", self.SUBSTRATE_NAME, exc)
            self._last_error = f"horizon_backfill: {exc}"

    async def _restore_horizons_from_pg(self) -> bool:
        if self.pg_store is None:
            return False
        reader = getattr(self.pg_store, "read_substrate_history", None)
        if not callable(reader):
            return False
        ledger_name = (
            f"analysis:{self.SUBSTRATE_NAME}"
            if self._plane == "analysis" else self.SUBSTRATE_NAME
        )
        try:
            rows = await reader(self.symbol, ledger_name, 1)
        except Exception as exc:
            log.warning("substrate %s ledger read failed: %s",
                        self.SUBSTRATE_NAME, exc)
            # A dead PG at cold start trips the breaker through the restore
            # path, not through twelve doomed computes. Healthy-empty (no
            # rows below) records nothing — absence of history is not failure.
            self._pg_record(False, int(time.time() * 1000))
            return False
        if not rows or not isinstance(rows[0], dict):
            return False
        payload = rows[0].get("payload")
        if not isinstance(payload, dict):
            return False
        try:
            if int(payload.get("schema_version") or 0) != SUBSTRATE_STATE_SCHEMA_VERSION:
                return False
        except (TypeError, ValueError):
            return False
        block = payload.get("horizons") or {}
        if not isinstance(block, dict):
            return False
        restored_any = False
        for hz in self.horizons:
            summary = (block.get(hz) or {}).get("fold") \
                if isinstance(block.get(hz), dict) else None
            if not isinstance(summary, dict):
                continue
            try:
                self._horizon_folds[hz] = restore_fold_state(summary)
                restored_any = True
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("substrate %s fold restore %s failed: %s",
                            self.SUBSTRATE_NAME, hz, exc)
        if restored_any:
            self._pg_record(True, int(time.time() * 1000))
        return restored_any

    def _fold_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        for row in rows:
            fields = row.get("fields") or {}
            raw = fields.get("payload")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                snapshot = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(snapshot, dict):
                continue
            for hz in self.horizons:
                state = self._horizon_folds[hz]
                for venue in ("spot", "futures"):
                    fold_snapshot(state, snapshot, venue)

    async def _build_evidence(self, now_ms: int, *, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from market_service.substrate_worker import evidence as _ev
        evidence = await _ev.cached_window(
            self.store, self.symbol, self.window_minutes,
            ws=bool(self.ws_input and "microstructure" in self.INPUT_STREAMS),
            ws_venue=self.ws_venue,
        )
        if self.horizons and type(self).RAW_HORIZON_EVIDENCE:
            horizon_windows: dict[str, dict[str, Any]] = {}
            for hz in self.horizons:
                minutes = horizon_minutes(hz)
                if minutes == int(self.window_minutes):
                    horizon_windows[hz] = evidence
                    continue
                horizon_windows[hz] = await _ev.horizon_window(self.store, self.symbol, minutes)
            evidence = {**evidence, "horizons": horizon_windows}
        return evidence

    async def _cached_evidence(self, now_ms: int) -> dict[str, Any]:
        from market_service.substrate_worker import evidence as _ev
        if self.horizons:
            return await self._build_evidence(now_ms)
        return await _ev.cached_window(
            self.store, self.symbol, self.window_minutes,
            ws=bool(self.ws_input and "microstructure" in self.INPUT_STREAMS),
            ws_venue=self.ws_venue,
        )

    async def _fire_guarded(
        self,
        decision: TriggerDecision,
        last_state: dict[str, Any] | None,
        now_ms: int,
        *,
        rows: list[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
        artifacts: ArtifactBundle | None = None,
    ) -> None:
        """Compute the substrate and persist the state payload (never raises).

        Evidence-once: the probe snapshot is threaded through — derivatives
        and dependencies attached for the probe are reused, not refetched.
        Artifact-once: the probe's derivations ride the bundle — compute
        reuses them after verifying the binding instead of recomputing.
        Hygiene fires (evidence=None) build once here. The whole cycle runs
        under the FSM-governed compute slot (vector 3): at most the plane's
        number of workers hold evidence + compute + persist at once.
        """
        async with self._fire_lock, self._compute_slot("compute"):
            try:
                try:
                    if evidence is None:
                        evidence = await self._cached_evidence(now_ms)
                        deriv_reason: str | None = None
                        try:
                            deriv_reason = await self._attach_derivatives(evidence, now_ms)
                        except Exception as exc:
                            deriv_reason = f"derivative attach error: {exc!r}"
                            log.warning("substrate %s derivative attach failed: %s",
                                        self.SUBSTRATE_NAME, exc)
                        dependencies = await self._load_dependencies()
                        if dependencies:
                            evidence = {**evidence, "substrate_dependencies": dependencies}
                    else:
                        # Probe path already attached; surface the dormant
                        # reason if one was recorded, else healthy.
                        deriv_reason = None
                    evidence = {**evidence, "own_last_output": (
                        (last_state or {}).get("output") or {}
                    )}
                    output = self.compute(evidence, self.depth if self.depth is not None else evidence.get("depth_levels") or 20,
                                              artifacts=artifacts)
                    missing: list[str] = []
                    status = "healthy"
                    horizons_block: dict[str, Any] = {}
                    if self.horizons:
                        per_hz = output.get("horizons")
                        per_hz = dict(per_hz) if isinstance(per_hz, dict) else {}
                        if "horizons" in output:
                            output = {k: v for k, v in output.items() if k != "horizons"}
                        for hz in self.horizons:
                            horizons_block[hz] = {
                                "output": per_hz.get(hz) or {},
                                "fold": fold_state_summary(self._horizon_folds[hz]),
                            }
                            hz_cov = ((evidence.get("horizons") or {}).get(hz) or {}).get("coverage") or {}
                            ret = hz_cov.get("retention") or {}
                            if ret.get("horizon_degraded"):
                                missing.append(
                                    f"horizon_degraded: {hz} oldest_entry_ms={ret.get('oldest_entry_ms')}"
                                )
                    if deriv_reason is not None:
                        missing.append(deriv_reason)
                    if not output:
                        status = "insufficient_data"
                        missing.append("substrate output empty")
                    elif missing:
                        status = "degraded"
                    payload = SubstrateStatePayload.create(
                        substrate=self.SUBSTRATE_NAME,
                        symbol=self.symbol,
                        output=output,
                        trigger=decision,
                        freshness=build_freshness(
                            evidence, window_minutes=self.window_minutes,
                            high_water=None, horizons=self.horizons,
                            horizon_folds=self._horizon_folds,
                            horizons_rebuilt_from=self._horizons_rebuilt_from,
                        ),
                        observed_at_ms=evidence.get("observed_at_ms"),
                        computed_at_ms=now_ms,
                        missing_inputs=missing,
                        horizons=horizons_block or None,
                    )
                    payload = (
                        payload if status == "healthy"
                        else _contracts_with_status(payload, status, missing)
                    )
                    if self.dispatcher is not None:
                        await self.dispatcher(payload)
                    if not await self._govern("publish", payload.to_dict(),
                                              record_allow=(self.pg_store is None)):
                        return
                    if self.pg_store is not None:
                        if not await self._govern("pg_write", payload.to_dict(),
                                                  record_allow=True):
                            return
                        try:
                            ledger_name = ledger_substrate_name(
                                self._plane, self.SUBSTRATE_NAME)
                            await asyncio.wait_for(
                                self.pg_store.record_substrate_state(
                                    self.symbol, ledger_name, payload.to_dict(),
                                ),
                                timeout=self._PG_INSERT_TIMEOUT_S,
                            )
                        except Exception as exc:
                            self._pg_record(False, now_ms)
                            if self.pg_strict:
                                log.error("substrate %s PG insert failed (strict: fire aborted): %s",
                                            self.SUBSTRATE_NAME, exc)
                                self._last_error = f"pg: {exc}"
                                # PG-aborted fires re-arm the cooldown: failure
                                # quiets the worker instead of shouting into
                                # a dead sink every tick until the breaker
                                # counts it open. Only PG workers reach here
                                # (decoupled at construction).
                                self._last_fire_ms = now_ms
                                self._last_dormant_reason = "pg_insert_failed"
                                return
                            log.warning("substrate %s PG insert failed (lax: continuing): %s",
                                        self.SUBSTRATE_NAME, exc)
                            self._last_error = f"pg (lax, continuing): {exc}"
                        else:
                            self._pg_record(True, now_ms)
                    stream_id = await self._publish_own_state(payload.to_dict())
                    await self._emit_telemetry_state(
                        "state_adjudicated", payload.to_dict(),
                        source_sequence=int(payload.computed_at_ms),
                        occurred_at_ms=int(payload.computed_at_ms),
                        source_ref=str(stream_id),
                    )
                except Exception as exc:
                    log.exception("substrate %s fire failed", self.SUBSTRATE_NAME)
                    self._last_error = f"fire: {exc}"
                    await self._emit_telemetry_state(
                        "worker_state",
                        {"status": "failed", "failure_code": type(exc).__name__},
                        source_sequence=max(0, int(now_ms)),
                        occurred_at_ms=max(0, int(now_ms)),
                    )
            finally:
                pending, self._pending = self._pending, None
                if pending is not None:
                    p_decision, p_last, p_now, p_water, p_ev, p_art = pending
                    task = asyncio.create_task(
                        self._fire_guarded(p_decision, p_last, p_now, evidence=p_ev,
                                           artifacts=p_art))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def _govern(
        self, gate: str, payload: dict[str, Any] | None = None,
        *, record_allow: bool = False,
    ) -> bool:
        if self.governor is None:
            return True
        evaluate = getattr(self.governor, "evaluate", None)
        record = getattr(self.governor, "record", None)
        try:
            if callable(evaluate) and callable(record):
                from market_service.governance.contracts import GovernanceGate
                binding = tuple(dict.fromkeys(
                    (*self.INPUT_STREAMS,
                     *type(self).DEPENDENCIES,
                     *type(self).DERIVATIVE_INPUTS),
                ))
                observation, verdict = await evaluate(
                    GovernanceGate(gate),
                    substrate=self.SUBSTRATE_NAME,
                    symbol=self.symbol,
                    payload=payload,
                    inputs=binding,
                )
                if not verdict.allowed or record_allow:
                    await record(
                        GovernanceGate(gate),
                        substrate=self.SUBSTRATE_NAME,
                        symbol=self.symbol,
                        observation=observation,
                        verdict=verdict,
                    )
                    decision_ms = int(
                        (payload or {}).get("computed_at_ms")
                        or self._last_fire_ms or time.time() * 1000
                    )
                    await self._emit_telemetry_state(
                        "governance_decision",
                        {
                            "gate": gate,
                            "allowed": bool(verdict.allowed),
                            "rule": verdict.rule,
                            "reason": verdict.reason[:500],
                            "status": getattr(observation, "status", None),
                            "schema_version": getattr(observation, "schema_version", None),
                        },
                        source_sequence=max(0, decision_ms),
                        occurred_at_ms=max(0, decision_ms),
                        source_ref=(
                            f"{self.SUBSTRATE_NAME}:{self.symbol}:{gate}:{decision_ms}"
                        ),
                    )
            else:
                method = getattr(self.governor, f"authorize_{gate}", None)
                if not callable(method):
                    log.error("governor has no evaluate/record or authorize_%s; "
                              "refusing gate", gate)
                    self._last_error = f"governance: no authorize_{gate}"
                    return False
                verdict = await method(
                    substrate=self.SUBSTRATE_NAME,
                    symbol=self.symbol,
                    payload=payload,
                    inputs=tuple(dict.fromkeys(
                        (*self.INPUT_STREAMS,
                         *type(self).DEPENDENCIES,
                         *type(self).DERIVATIVE_INPUTS),
                    )),
                )
        except Exception as exc:
            log.error("governance %s gate raised (failing closed): %s", gate, exc)
            self._last_error = f"governance: {gate}: {exc!r}"
            return False
        if not verdict.allowed:
            self._last_dormant_reason = f"governance:{verdict.rule}:{verdict.reason}"
            log.warning(
                "substrate %s denied at %s: [%s] %s",
                self.SUBSTRATE_NAME, gate, verdict.rule, verdict.reason,
            )
            return False
        return True

    async def _attach_derivatives(
        self, window: dict[str, Any], now_ms: int,
    ) -> str | None:
        wanted = type(self).DERIVATIVE_INPUTS
        if not wanted:
            return None
        deriv = await self.store.read("derivatives.latest", ids={"symbol": self.symbol})
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
        loaded: dict[str, Any] = {}
        for name in type(self).DEPENDENCIES:
            try:
                latest = await self.store.read(f"substrate.{name.lower()}", ids={"symbol": self.symbol})
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
        return _contracts_with_status(payload, status, missing)

    def _freshness(self, evidence: dict[str, Any], *, high_water: str | None) -> dict[str, Any]:
        return build_freshness(
            evidence, window_minutes=self.window_minutes,
            high_water=high_water, horizons=self.horizons,
            horizon_folds=self._horizon_folds,
            horizons_rebuilt_from=self._horizons_rebuilt_from,
        )

    async def _dedupe(self, decision: TriggerDecision, high_water: str, now_ms: int) -> bool:
        """Governance-owned fail-closed dedupe (was fire.py::_dedupe)."""
        from market_service.governance import dedupe as _gov_dedupe
        return await _gov_dedupe.evaluate(
            self._redis, self._supervisor_key, decision, high_water, now_ms,
            cooldown_ms=self.cooldown_s * 1_000, state=self._dedupe_state,
        )