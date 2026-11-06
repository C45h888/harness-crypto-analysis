"""Signals substrate worker — deterministic state-change signals.

Bounded to exactly ONE substrate module: ``calculations.substrates.signals``.
This worker IS the signal producer: it builds the market snapshot from the
tape worker's latest projection + the raw snapshot's open interest, runs
``deterministic_signals(snapshot, previous)``, and persists both the signals
and the snapshot (so the next cycle has its ``previous``).

The one declared cross-worker data dependency (decided): ``DEPENDENCIES =
("tape",)``. The core loads ``tape:latest`` at fire time into the window /
evidence under ``substrate_dependencies``; a stale/missing dependency keeps
the probe dormant and turns a hygiene fire into insufficient_data. No worker
imports another worker — the dependency travels via class attribute + store.

Probe: ``deterministic_signals(new_snapshot, previous_snapshot)`` non-empty
→ fire, with the tripped signals as predicates.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.signals import deterministic_signals
from market_service.substrate_worker.contracts import ArtifactBundle, CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core import SubstrateWorkerCore


def _snapshot_from(window: dict[str, Any]) -> dict[str, Any] | None:
    """Build the signal snapshot from tape:latest + raw open interest.

    Returns None when the tape dependency is unusable — the caller reports
    dormant/insufficient, never a fabricated snapshot.
    """
    deps = window.get("substrate_dependencies") or {}
    tape = deps.get("tape") or {}
    if tape.get("available") is False:
        return None
    tape_out = tape.get("output") or {}
    spot_flow = tape_out.get("spot_flow") or {}
    fut_flow = tape_out.get("futures_flow") or {}
    if not spot_flow and not fut_flow:
        return None
    oi_raw = (window.get("futures") or {}).get("open_interest") or {}
    oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
    # Binance REST returns open_interest as a JSON STRING (the value is a
    # Decimal in source-of-truth but the wire shape is "7923049.05"); the
    # raw stream preserves that shape. The signals substrate does
    # arithmetic on the value (current_oi - previous_oi), so a string here
    # becomes TypeError on every fire after the cold_start. Coerce at the
    # snapshot boundary — every downstream substrate sees a consistent
    # numeric type. A failed parse drops the field (null discipline: never
    # fabricate an OI value).
    if isinstance(oi_value, str):
        try:
            oi_value = float(oi_value)
        except (TypeError, ValueError):
            oi_value = None
    return {
        "spot_buy_share": spot_flow.get("buy_share"),
        "futures_buy_share": fut_flow.get("buy_share"),
        "spot_obi_top_n": spot_flow.get("obi"),
        "open_interest": oi_value,
    }


class SignalsWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "signals"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=15, staleness_s=120)
    DEPENDENCIES = ("tape",)

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: any deterministic rule trips on the tape-derived snapshot; tape-gated. Cooldown 15s probe-only (hygiene bypasses);
    # staleness 120s.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """SignalsWorker: any deterministic rule trips on the tape-derived snapshot; tape-gated."""
        last_state = await self._read_own_state()
        if last_state is not None:
            try:
                seen_version = int(last_state.get("schema_version") or 0)
            except (TypeError, ValueError):
                seen_version = 0
            if seen_version != SUBSTRATE_STATE_SCHEMA_VERSION:
                last_state = None

        ws_rows = ws_rows or []
        arrival = bool(rows)

        # L4 — cold start: data arrived but no usable prior projection.
        if last_state is None:
            if not arrival:
                return
            decision = TriggerDecision(fired=True, source="cold_start",
                                       predicates={"consumed_entries": len(rows)})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L4 — capture recovery (gap/reconnecting -> running) bypasses cooldown.
        if recovery:
            decision = TriggerDecision(fired=True, source="recovery",
                                       predicates={"transition": "gap/reconnecting->running"})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L3 — staleness override (hygiene, bypasses cooldown).
        computed_at = last_state.get("computed_at_ms")
        age_ms = (now_ms - int(computed_at)) if isinstance(computed_at, (int, float)) else None
        if age_ms is not None and age_ms > self.staleness_s * 1_000:
            decision = TriggerDecision(
                fired=True, source="staleness",
                predicates={"age_ms": age_ms, "staleness_s": self.staleness_s},
            )
            await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L3 — window rollover.
        rollover = self._rollover_decision(rows)
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water(rows))
            return

        # L1 — arrival gate: no new entries, no window build, no probe.
        if not arrival:
            return

        # Stage-one screen (ns.read discipline): the probe inputs for
        # this worker are point-in-time (latest snapshot + last state),
        # so run the SAME probe on latest-only state first. Quiet screen
        # skips the heavy window materialization entirely; a firing screen
        # proceeds to the full window + confirming probe below. Zero miss
        # risk: screen and probe read identical inputs here.
        latest = await self.store.read("raw.latest", ids={"symbol": self.symbol})
        if latest is not None and not await self.screen(latest, last_state, now_ms):
            self._last_dormant_reason = "screen: latest state shows no likely flip"
            return

        window = await self._cached_evidence(now_ms)
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
        await self._fire(decision, last_state, now_ms, high_water=self._high_water(rows),
                         evidence=window)
    async def screen(
        self, latest: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> bool:
        """Stage-one: signals probe over tape:latest + latest OI (exact).

        The snapshot is a pure function of the tape dependency projection
        plus the latest raw OI — the window adds nothing. One extra GET
        (the tape projection) replaces the full window materialization.
        """
        deps = await self._load_dependencies()
        window = {
            "futures": (latest.get("futures") or {}),
            "spot": (latest.get("spot") or {}),
            "substrate_dependencies": deps,
        }
        return self.probe(window, last_state, now_ms).fired

    # ------------------------------------------------------------------
    # L2 — significance probe (any rule trips)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
        seed: dict[str, Any] | None = None,
    ) -> TriggerDecision:
        snapshot = _snapshot_from(window)
        if snapshot is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "tape_dependency_unusable"})
        previous = ((last_state or {}).get("output") or {}).get("snapshot")
        signals = deterministic_signals(snapshot, previous)
        if not signals:
            return TriggerDecision(fired=False, source="probe", predicates={})
        predicates = {
            s.get("signal_type", f"signal_{i}"): s.get("evidence", {})
            for i, s in enumerate(signals) if isinstance(s, dict)
        }
        return TriggerDecision(fired=True, source="probe", predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — signals substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int,
                artifacts: ArtifactBundle | None = None) -> dict[str, Any]:
        snapshot = _snapshot_from(evidence)
        if snapshot is None:
            # Null discipline: without the tape dependency there is no
            # snapshot to evaluate — insufficient, never empty signals.
            return {}
        previous = (evidence.get("own_last_output") or {}).get("snapshot")
        return {
            "signals": deterministic_signals(snapshot, previous),
            "snapshot": snapshot,
        }
