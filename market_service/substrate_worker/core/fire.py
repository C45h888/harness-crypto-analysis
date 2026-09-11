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
from typing import Any

from market_service.runtime.raw_window import build_raw_window
from market_service.substrate_worker.core.base import SubstrateBase, _store_fn
from market_service.substrate_worker.contracts import (
    SUBSTRATE_STATE_SCHEMA_VERSION,
    TriggerDecision,
    SubstrateStatePayload,
)

log = logging.getLogger(__name__)


class FireMixin(SubstrateBase):
    """Fire authority: decision cascade → dedupe → compute → persist."""

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
        # previous and current batch's newest entries.
        rollover = self._rollover_decision(rows or ws_rows)
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water(rows or ws_rows))
            return

        # L1 — arrival gate: only build the (heavy) evidence window when new
        # data actually arrived on any surface.
        if not arrival:
            return

        window = await build_raw_window(self.store, self.symbol, self.window_minutes)
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
            try:
                await self._attach_derivatives(evidence, now_ms)
            except Exception as exc:
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