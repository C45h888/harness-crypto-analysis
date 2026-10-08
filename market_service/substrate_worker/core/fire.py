"""Fire authority — dedupe, dispatch, compute, persist.

SEMANTIC JURISDICTION: this file owns EVERYTHING that happens once a worker
decides to fire:

* the decision cascade in ``_handle_rows`` (cold start, recovery, staleness,
  rollover, L1 arrival → probe → cooldown);
* dedupe via the supervisor Lua script (same-condition collapse);
* the compute+persist cycle (derivative/dependency attach, freshness,
  status, PG-first, Redis publish);
* fire dispatch as asyncio tasks so the read loop stays responsive.

It does NOT own: how rows are read (``reader.py``), the supervisor heartbeat
(``supervisor.py``), or the worker hooks (the subclass file).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from market_service.runtime.horizons import (
    fold_snapshot, fold_state_summary, fold_trades, horizon_minutes,
)
from market_service.runtime.raw_window import (
    build_raw_window,
    build_ws_surface,
    overlay_ws_into_window,
)
from market_service.substrate_worker.core.base import SubstrateBase, _store_fn
from market_service.substrate_worker.contracts import (
    SUBSTRATE_STATE_SCHEMA_VERSION,
    TriggerDecision,
    SubstrateStatePayload,
)
from market_service.runtime.horizons import restore_fold_state

log = logging.getLogger(__name__)

# Process-global evidence-window cache: every worker for the same symbol builds
# the SAME raw+WS window, but doing so ~1.4s per worker per arrival on a
# single asyncio event loop starved the other workers' supervisor heartbeats
# (their ``_tick`` was delayed past the ~7s TTL). Share one build per symbol
# across all workers so the heavy window is computed once per TTL slice.
# Keyed by (symbol, window_minutes).
_EVIDENCE_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_EVIDENCE_CACHE_TTL_S = 2.5


class FireMixin(SubstrateBase):
    """Fire authority: decision cascade → dedupe → compute → persist."""

    # Bounded durable-ledger insert (seconds): a hung PG must abort the
    # fire within this window, never stall the worker plane silently.
    _PG_INSERT_TIMEOUT_S = 10.0

    async def _handle_rows(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        last_state = await self._read_own_state()
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

        # Phase H1 — fold new raw rows into the per-horizon accumulators
        # BEFORE any decision. The fold is the cadence-layer horizon
        # identification: every entry the consumer group delivers updates
        # the IST-aligned segment rings (monotonic-id dedupe inside), so
        # probes and fires read always-fresh horizon state with zero
        # history re-scans.
        if self.horizons:
            self._fold_rows(rows)

        # L4 — cold start: data arrived but no (usable) prior projection.
        if last_state is None:
            if not arrival:
                return  # nothing to compute from yet; heartbeat continues
            if self.horizons and not self._horizons_backfilled:
                await self._backfill_horizons()
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

        # L3 — per-horizon staleness (Phase H1): a declared horizon's fold
        # that has not seen fresh evidence for its OWN bound fires with a
        # horizon-tagged predicate. Uses trade-time (fold last_trade_ms),
        # so a silent tape trips the bound even when stream entries arrive.
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

        # L3 — window rollovers: a period boundary crossed between the
        # previous and current batch's newest entries.
        rollover = self._rollover_decision(rows or ws_rows)
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L1 — arrival gate: only build the (heavy) evidence window when new
        # data actually arrived on any surface.
        if not arrival:
            return

        # Probe path uses the cached evidence window (shared with the compute
        # of the same fire) — see ``_cached_evidence``.
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
        # Cooldown gates ONLY probe-source fires.
        if self._last_fire_ms is not None:
            if now_ms - self._last_fire_ms < self.cooldown_s * 1_000:
                return
        await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows or ws_rows))

    async def _fire(
        self,
        decision: TriggerDecision,
        last_state: dict[str, Any] | None,
        now_ms: int,
        *,
        high_water: str,
        rows: list[dict[str, Any]] | None = None,
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
        task = asyncio.create_task(self._fire_guarded(decision, last_state, now_ms, rows=rows))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _backfill_horizons(self) -> None:
        """Cold-start horizon rebuild (Phase H1 §2.5).

        PG-FIRST, raw-merge: the durable ledger's newest v2 payload carries
        the last persisted fold summaries; restoring them seeds segment
        state AND the trade-id high-waters. The raw retention fold then
        runs on TOP unchanged — the monotonic-id dedupe folds every
        already-counted trade as zero, so the merge is free and the only
        work done is folding the ledger-to-now delta. PG absent, stale,
        schema-mismatched, or fold-less → raw-only rebuild (pre-B shape).
        One bounded read of the widened raw retention either way; never
        per fire.
        """
        source = "raw"
        try:
            if await self._restore_horizons_from_pg():
                source = "pg"
        except Exception as exc:
            log.warning("substrate %s horizon PG restore failed: %s",
                        self.SUBSTRATE_NAME, exc)
            self._last_error = f"horizon_pg_restore: {exc}"
        try:
            max_minutes = max(horizon_minutes(hz) for hz in self.horizons)
            window = await build_raw_window(self.store, self.symbol, max_minutes)
            for hz in self.horizons:
                state = self._horizon_folds[hz]
                for venue in ("spot", "futures"):
                    trades = ((window.get(venue) or {}).get("trades_normalized")) or []
                    fold_trades(state, trades, venue)
            self._horizons_backfilled = True
            self._horizons_rebuilt_from = source
            log.info(
                "substrate %s horizon backfill complete (source=%s): horizons=%s segments=%s",
                self.SUBSTRATE_NAME, source, list(self.horizons),
                {hz: len(self._horizon_folds[hz]["segments"]) for hz in self.horizons},
            )
        except Exception as exc:
            # Never fail a cold-start fire on backfill; fold state resumes
            # incrementally and the next cold_start/staleness boundary
            # retries the rebuild.
            log.warning("substrate %s horizon backfill failed: %s", self.SUBSTRATE_NAME, exc)
            self._last_error = f"horizon_backfill: {exc}"

    async def _restore_horizons_from_pg(self) -> bool:
        """Seed fold state from the durable ledger's last v2 payload.

        Uses the EXISTING ``read_substrate_history`` reader (limit=1). A
        payload qualifies when it is the current schema version AND its
        horizons block carries restorable fold summaries. Returns True
        only when at least one horizon actually restored — a partial
        restore falls back to raw-only for the missing horizons.
        """
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
        return restored_any

    def _fold_rows(self, rows: list[dict[str, Any]]) -> None:
        """Fold newly arrived raw stream rows into the horizon folds.

        Runs on EVERY read tick for horizon workers (cheap: monotonic-id
        dedupe means each trade is folded exactly once per process). Each
        row's payload is the slimmed raw snapshot — the fold reads only
        ``trades_normalized``.
        """
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
        """Evidence assembly seam — the raw evidence window (calculation plane).

        Analysis-plane workers override: their evidence is the composed
        dependency-substrate latests (attached by ``_attach_dependencies``)
        plus the derivative cache — never a raw REST window.

        For WS-io workers (delta, large_print) the base fuses the poller REST
        window with the high-frequency microstructure WS surface so compute
        aggregates poller + WS as one combined input. WS is best-effort: an
        absent WS surface leaves the REST window intact (pre-WS behavior).

        Phase H1 — horizon workers: when ``HORIZONS`` is declared, each
        horizon gets its OWN deduped evidence window (built from the
        widened, time-trimmed raw retention) attached at
        ``evidence["horizons"][hz]``. Builds share the process-global
        evidence cache (keyed by symbol+window), so a 4h rebuild happens
        at most once per cache slice across all workers of the symbol.
        """
        evidence = await build_raw_window(self.store, self.symbol, self.window_minutes)
        if self.horizons:
            horizon_windows: dict[str, dict[str, Any]] = {}
            for hz in self.horizons:
                minutes = horizon_minutes(hz)
                if minutes == int(self.window_minutes):
                    horizon_windows[hz] = evidence
                    continue
                key = (self.symbol, minutes, False)
                cached = _EVIDENCE_CACHE.get(key)
                now = time.monotonic()
                if cached is not None and (now - cached[0]) < _EVIDENCE_CACHE_TTL_S:
                    horizon_windows[hz] = cached[1]
                    continue
                built = await build_raw_window(self.store, self.symbol, minutes)
                _EVIDENCE_CACHE[key] = (time.monotonic(), built)
                horizon_windows[hz] = built
            evidence = {**evidence, "horizons": horizon_windows}
        if self.ws_input and "microstructure" in self.INPUT_STREAMS:
            try:
                ws = await build_ws_surface(
                    self.store, self.symbol, self.ws_venue,
                    window_minutes=self.window_minutes,
                )
            except Exception as exc:
                log.warning("substrate %s ws surface build failed: %s",
                            self.SUBSTRATE_NAME, exc)
            else:
                evidence = overlay_ws_into_window(evidence, ws)
        return evidence

    async def _cached_evidence(self, now_ms: int) -> dict[str, Any]:
        """Cached evidence window — one heavy build per (symbol, window) per
        TTL slice, shared across all workers in this process. Building the
        ~1.4s raw+WS window per worker per arrival starved the shared event
        loop and let supervisor heartbeats expire; this thins it to one build
        per slice. ``_EVIDENCE_CACHE_TTL_S`` bounds reuse."""
        key = (self.symbol, int(self.window_minutes), self.ws_input and "microstructure" in self.INPUT_STREAMS)
        cached = _EVIDENCE_CACHE.get(key)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < _EVIDENCE_CACHE_TTL_S:
            return cached[1]
        built = await self._build_evidence(now_ms)
        _EVIDENCE_CACHE[key] = (time.monotonic(), built)
        return built

    async def _fire_guarded(
        self,
        decision: TriggerDecision,
        last_state: dict[str, Any] | None,
        now_ms: int,
        *,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        """Compute the substrate and persist the state payload (never raises)."""
        try:
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
            evidence = {**evidence, "own_last_output": (
                (last_state or {}).get("output") or {}
            )}
            output = self.compute(evidence, self.depth if self.depth is not None else evidence.get("depth_levels") or 20)
            missing: list[str] = []
            status = "healthy"
            # Phase H1 — extract per-horizon outputs the worker computed
            # (output["horizons"]) into the payload's horizons block, next
            # to the persisted fold summaries. Base ``output`` stays the
            # base-horizon verdict for every existing reader.
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
                    # Retention honesty: a horizon window the stream cannot
                    # cover is DEGRADED evidence — named, never silent.
                    hz_cov = ((evidence.get("horizons") or {}).get(hz) or {}).get("coverage") or {}
                    ret = hz_cov.get("retention") or {}
                    if ret.get("horizon_degraded"):
                        missing.append(
                            f"horizon_degraded: {hz} oldest_entry_ms={ret.get('oldest_entry_ms')}"
                        )
            # Precise provenance: when the derivative cache was missing/stale
            # at fire time, name THAT (not a generic "output empty") so the
            # projection tells the operator which cache surface degraded.
            if deriv_reason is not None:
                missing.append(deriv_reason)
            if not output:
                status = "insufficient_data"
                missing.append("substrate output empty")
            elif missing:
                # Output computed, but some declared inputs were absent —
                # partially degraded, never silently healthy.
                status = "degraded"
            payload = SubstrateStatePayload.create(
                substrate=self.SUBSTRATE_NAME,
                symbol=self.symbol,
                output=output,
                trigger=decision,
                freshness=self._freshness(evidence, high_water=None),
                observed_at_ms=evidence.get("observed_at_ms"),
                computed_at_ms=now_ms,
                missing_inputs=missing,
                horizons=horizons_block or None,
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
                    # Shared durable ledger, plane-namespaced: analysis
                    # workers record under "analysis:<name>" so calc-plane
                    # and analysis-plane names can never collide.
                    ledger_name = (
                        f"analysis:{self.SUBSTRATE_NAME}"
                        if self._plane == "analysis" else self.SUBSTRATE_NAME
                    )
                    # Bounded insert: a hung PG (dead daemon, network drop)
                    # must fail FAST — an unbounded await here stalls every
                    # dispatched fire task, freezes all projections, and
                    # turns the staleness heartbeats into a fire-storm that
                    # never lands. Abort loudly inside the strict window.
                    await asyncio.wait_for(
                        self.pg_store.record_substrate_state(
                            self.symbol, ledger_name, payload.to_dict(),
                        ),
                        timeout=self._PG_INSERT_TIMEOUT_S,
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
            await self._publish_own_state(payload.to_dict())
        except Exception as exc:
            log.exception("substrate %s fire failed", self.SUBSTRATE_NAME)
            self._last_error = f"fire: {exc}"

    async def _attach_derivatives(
        self, window: dict[str, Any], now_ms: int,
    ) -> str | None:
        """Merge declared derivative-cache keys into the window futures."""
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
        """Merge declared cross-worker latests into the window."""
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
            horizons=payload.horizons,
        )

    def _freshness(self, evidence: dict[str, Any], *, high_water: str | None) -> dict[str, Any]:
        coverage = evidence.get("coverage") or {}
        out = {
            "window_minutes": self.window_minutes,
            "input_fingerprint": {
                "observed_at_ms": evidence.get("observed_at_ms"),
                "stream_staleness_ms": coverage.get("stream_staleness_ms"),
                "spot_trade_count": (coverage.get("spot_trades") or {}).get("trade_count"),
                "futures_trade_count": (coverage.get("futures_trades") or {}).get("trade_count"),
                "high_water": high_water,
            },
        }
        if self.horizons:
            out["horizons_rebuilt_from"] = self._horizons_rebuilt_from
            out["horizon_segments"] = {
                hz: len(self._horizon_folds[hz]["segments"]) for hz in self.horizons
            }
        return out

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
                return True  # script unavailable — fire anyway
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