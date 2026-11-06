"""Delta substrate worker — the signed -2..+2 DELTA variable.

Bounded to exactly ONE substrate: ``calculations.substrates.delta``. Watches
the futures book + taker buy/sell alignment; fires when the DELTA state band
flips or the taker-buy share crosses the neutral band; computes the delta
section the composition root runs (``composition.run_analysis`` delta
adapter).

Taker buy/sell arrives via the derivative evidence cache (cache-only — the
worker never touches Binance). The core merges ``taker_buy_sell`` into the
window before the probe runs, so the probe stays pure; a missing/stale cache
keeps the worker dormant with a supervisor reason instead of firing.

Probe (deterministic, imported substrate semantics):

  delta_state_flip — ``delta_state(new) != delta_state(last)`` (±0.25/±1.0
                     boundaries owned by the substrate).
  tbr_band_cross   — latest ``tbr_last_pct`` crossed the 50±5 neutral band
                     (sides -1/0/+1 vs [45, 55] differ between last and new).
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.delta import (
    multi_timeframe_delta,
)
from market_service.substrate_worker.contracts import ArtifactBundle, CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION, artifact_binding
from market_service.substrate_worker.core import SubstrateWorkerCore

# TBR neutral band: 50% taker-buy is neutral flow; ±5 marks the
# composition delta adapter's alignment-significance edge.
TBR_BAND = (45.0, 55.0)


def _pairs(levels: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in levels or ():
        try:
            out.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _mid(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


def _tbr_side(tbr: float | None) -> int | None:
    if tbr is None:
        return None
    try:
        v = float(tbr)
    except (TypeError, ValueError):
        return None
    lo, hi = TBR_BAND
    if v < lo:
        return -1
    if v > hi:
        return 1
    return 0


def _delta_of(window: dict[str, Any]) -> dict[str, Any] | None:
    """Pure DELTA evaluation over a window (probe + compute share it).

    Combines the fused poller+WS book with the WS delta series, computing
    the multi-timeframe (5m/15m/4h) DELTA aggregation when WS evidence is
    present and falling back to the single live book when it is not.
    """
    fut = window.get("futures") or {}
    book = fut.get("order_book") or {}
    ws_deltas = fut.get("ws_deltas") or (window.get("ws") or {}).get("deltas") or []
    mid = _mid(_pairs(book.get("bids")), _pairs(book.get("asks")))
    if mid is None or mid <= 0:
        return None
    tbr_series = fut.get("taker_buy_sell")
    try:
        payload = multi_timeframe_delta(
            book, tbr_series, mid,
            half_range=0.75, band_step=0.10,
            ws_deltas=ws_deltas or None,
        )
    except (ValueError, TypeError):
        return None
    return payload


class DeltaWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "delta"
    INPUT_STREAMS = ("raw", "microstructure")
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=180, ws_input=True)
    DERIVATIVE_INPUTS = ("taker_buy_sell",)

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: delta-state flip, TBR band cross, or MTF flip; taker_buy_sell-gated. Cooldown 60s probe-only (hygiene bypasses);
    # staleness 180s.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """DeltaWorker: delta-state flip, TBR band cross, or MTF flip; taker_buy_sell-gated."""
        last_state = await self._read_own_state()
        if last_state is not None:
            try:
                seen_version = int(last_state.get("schema_version") or 0)
            except (TypeError, ValueError):
                seen_version = 0
            if seen_version != SUBSTRATE_STATE_SCHEMA_VERSION:
                last_state = None

        ws_rows = ws_rows or []
        arrival = bool(rows) or bool(ws_rows)

        # L4 — cold start: data arrived but no usable prior projection.
        if last_state is None:
            if not arrival:
                return
            decision = TriggerDecision(fired=True, source="cold_start",
                                       predicates={"consumed_entries": len((rows or ws_rows))})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water((rows or ws_rows)))
            return

        # L4 — capture recovery (gap/reconnecting -> running) bypasses cooldown.
        if recovery:
            decision = TriggerDecision(fired=True, source="recovery",
                                       predicates={"transition": "gap/reconnecting->running"})
            await self._fire(decision, last_state, now_ms, high_water=self._high_water((rows or ws_rows)))
            return

        # L3 — staleness override (hygiene, bypasses cooldown).
        computed_at = last_state.get("computed_at_ms")
        age_ms = (now_ms - int(computed_at)) if isinstance(computed_at, (int, float)) else None
        if age_ms is not None and age_ms > self.staleness_s * 1_000:
            decision = TriggerDecision(
                fired=True, source="staleness",
                predicates={"age_ms": age_ms, "staleness_s": self.staleness_s},
            )
            await self._fire(decision, last_state, now_ms, high_water=self._high_water((rows or ws_rows)))
            return

        # L3 — window rollover.
        rollover = self._rollover_decision((rows or ws_rows))
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water((rows or ws_rows)))
            return

        # L1 — arrival gate: no new entries, no window build, no probe.
        if not arrival:
            return

        window = await self._cached_evidence(now_ms)
        deriv_reason = await self._attach_derivatives(window, now_ms)
        if deriv_reason is not None:
            self._last_dormant_reason = deriv_reason
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

        # Artifact-once: the heavy derivation runs exactly once per tick.
        # decide evaluates _delta_of a single time; the probe consumes it as
        # seed and compute reuses it from the bundle after verifying the
        # build-nonce binding. At worst (no nonce) both recompute as before.
        current = _delta_of(window)
        bundle = (
            ArtifactBundle(
                producer=self.SUBSTRATE_NAME,
                binding=artifact_binding(
                    self.symbol, "delta",
                    int(window.get("_artifact_nonce") or 0)),
                values={"delta": current},
            )
            if current is not None else None
        )
        decision = self.probe(window, last_state, now_ms, seed=current)
        if not decision.fired:
            return
        # Cooldown gates ONLY probe-source fires.
        if self._last_fire_ms is not None:
            if now_ms - self._last_fire_ms < self.cooldown_s * 1_000:
                return
        # Evidence-once: thread the probe snapshot into compute.
        await self._fire(decision, last_state, now_ms, high_water=self._high_water((rows or ws_rows)),
                         evidence=window, artifacts=bundle)
    # ------------------------------------------------------------------
    # L2 — significance probe (state-band flip / TBR band cross)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
        seed: dict[str, Any] | None = None,
    ) -> TriggerDecision:
        # Seed is the decide-stage _delta_of result on this same window:
        # reuse it instead of evaluating a second time. Anything else
        # (None, foreign shape) evaluates fresh — purity preserved.
        current = seed if seed else _delta_of(window)
        if current is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_book_mid"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}

        prev_state = prev_output.get("state")
        if prev_state is not None and current["state"] != prev_state:
            predicates["delta_state_flip"] = {
                "from": prev_state, "to": current["state"],
                "delta": current["delta"],
            }

        prev_side = _tbr_side(prev_output.get("tbr_last_pct"))
        cur_side = _tbr_side(current.get("tbr_last_pct"))
        if prev_side is not None and cur_side is not None and prev_side != cur_side:
            predicates["tbr_band_cross"] = {
                "from": prev_output.get("tbr_last_pct"), "to": current.get("tbr_last_pct"),
                "band": list(TBR_BAND),
            }

        # Multi-timeframe: ANY horizon state flip is a fire condition.
        cur_windows = current.get("deltas") or {}
        prev_windows = prev_output.get("deltas") or {}
        for h, d in cur_windows.items():
            if not isinstance(d, dict):
                continue
            p = prev_windows.get(h)
            p_state = (p or {}).get("state") if isinstance(p, dict) else None
            if p_state is not None and p_state != d.get("state"):
                predicates[f"delta_state_flip:{h}min"] = {
                    "from": p_state, "to": d.get("state"),
                    "delta": d.get("delta"),
                }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)


    # ------------------------------------------------------------------
    # Compute — delta substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int,
                artifacts: ArtifactBundle | None = None) -> dict[str, Any]:
        # Artifact-once: reuse the probe-stage derivation when its binding
        # proves it came from this same window build. No nonce (hand-built
        # evidence, foreign bundle) recomputes — never a stale reuse.
        current: dict[str, Any] | None = None
        nonce = evidence.get("_artifact_nonce")
        if artifacts is not None and nonce:
            try:
                if artifacts.usable_for(
                    self.SUBSTRATE_NAME,
                    artifact_binding(self.symbol, "delta", int(nonce)),
                ):
                    maybe = artifacts.values.get("delta")
                    current = maybe if isinstance(maybe, dict) and maybe else None
            except Exception:
                current = None
        if current is None:
            current = _delta_of(evidence)
        if current is None:
            # Null discipline: without a real mid the DELTA math cannot run.
            return {}
        out = {
            "delta": current["delta"],
            "delta_raw": current["delta_raw"],
            "wall_imbalance": current["wall_imbalance"],
            "flow_alignment": current["flow_alignment"],
            "tbr_last_pct": current["tbr_last_pct"],
            "tbr_3avg_pct": current["tbr_3avg_pct"],
            "n_bands": current["n_bands"],
            "bands": current["bands"],
            "range": current["range"],
            "state": current["state"],
        }
        # Multi-timeframe aggregate: 5m / 15m / 4h signed DELTA + per-window
        # state. Sources poller book fused with the WS microstructure series.
        if isinstance(current.get("deltas"), dict):
            out["deltas"] = current["deltas"]
        else:
            out["deltas"] = {
                5: {"delta": current["delta"], "state": current["state"], "window_min": 5},
                15: {"delta": current["delta"], "state": current["state"], "window_min": 15},
                240: {"delta": current["delta"], "state": current["state"], "window_min": 240},
            }
        return out
