"""Technicals substrate worker — EMA/ATR/trend time-series math.

Bounded to exactly ONE substrate: ``calculations.substrates.technicals``.
(Tiered large flow + seller aggression are the large_print worker's — the
legacy ``technical`` module split along that seam.) Watches the 5m kline
series; fires on a new bar close or an EMA-position flip; computes the
technicals half of the composition technical section.

Klines arrive ONLY via the derivative evidence cache (cache-only — the
worker never touches Binance). The core merges ``klines`` into the window
before the probe; a missing/stale cache keeps the worker dormant.

Probe: new 5m bar close (kline count grew vs the persisted ``kline_count``)
OR ``ema_position`` flip vs last state (price vs ema21).
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.technicals import (
    atr_pct_from_klines,
    ema_position,
    ema_series,
    trend_drift,
    trend_slope,
)
from market_service.runtime.horizons import fold_candles
from market_service.substrate_worker.contracts import ArtifactBundle, CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core import SubstrateWorkerCore

EMA_SHORT_KEY = "ema21"  # the watched EMA leg (price vs ema21)
# Phase A — the horizon-fold leg. The fold rings cap at 14 (1h) / 18 (4h)
# segments, so only the SHORT EMA leg can resolve there; the horizon
# predicates watch ema9 while the base path keeps ema21 on klines.
EMA_HORIZON_LEG = "ema9"


def _closes(klines: Any) -> list[float]:
    out: list[float] = []
    for row in klines or []:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            try:
                out.append(float(row[4]))
            except (TypeError, ValueError):
                continue
    return out


class TechnicalsWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "technicals"
    INPUT_STREAMS = ("raw",)
    # Phase A rollout — fold-CANDLE horizons: per-horizon EMA/trend/ATR
    # are computed from fold segments (each segment becomes a kline-shaped
    # row) — no extra derivative fetch. Base 5m EMA stays on klines.
    HORIZONS = ("1h", "4h")
    # Fold-native: compute/probe read only fold_candles(), never
    # evidence["horizons"] — skip the raw 1h/4h materializations (vector 2).
    RAW_HORIZON_EVIDENCE = False
    CADENCE = CadenceProfile(
        cooldown_s=60, staleness_s=300, rollovers=("bar_5m",),
        horizons=("1h", "4h"),
        horizon_staleness_s=(("1h", 600), ("4h", 4 * 3600)),
    )
    DERIVATIVE_INPUTS = ("klines",)
    # Short ATR window for fold candles: the 1h ring holds ≤14 segments,
    # so the kline default (14) would rarely resolve; period 3 resolves
    # from 4 candles and stays honest on partial rings.
    FOLD_ATR_PERIOD = 3

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: new 5m bar or EMA flip, base plus fold-candle 1h/4h; klines-gated. Cooldown 60s probe-only (hygiene bypasses);
    # staleness 300s. 1h staleness at 10m; 4h at 4h; bar_5m rollover.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """TechnicalsWorker: new 5m bar or EMA flip, base plus fold-candle 1h/4h; klines-gated."""
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

        if self.horizons:
            self._fold_rows(rows)

        # L4 — cold start: data arrived but no usable prior projection.
        if last_state is None:
            if not arrival:
                return
            if self.horizons and not self._horizons_backfilled:
                await self._backfill_horizons()
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
        # L3 — per-horizon staleness on fold trade-time.
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
                                         high_water=self._high_water(rows))
                        return


        # L3 — window rollover.
        rollover = self._rollover_decision(rows)
        if rollover is not None:
            await self._fire(rollover, last_state, now_ms, high_water=self._high_water(rows))
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
    # ------------------------------------------------------------------
    # L2 — significance probe (new bar / EMA-position flip)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
        seed: dict[str, Any] | None = None,
    ) -> TriggerDecision:
        klines = (window.get("futures") or {}).get("klines") or []
        closes = _closes(klines)
        if not closes:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_klines"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}

        prev_count = prev_output.get("kline_count")
        if isinstance(prev_count, int) and len(klines) > prev_count:
            predicates["new_bar"] = {"from_count": prev_count, "to_count": len(klines)}

        current_pos = ema_position(closes).get(EMA_SHORT_KEY)
        prev_pos = (prev_output.get("ema_position") or {}).get(EMA_SHORT_KEY)
        if current_pos is not None and prev_pos is not None and current_pos != prev_pos:
            predicates["ema_position_flip"] = {
                "leg": EMA_SHORT_KEY, "from": prev_pos, "to": current_pos,
            }

        # Phase A — fold-candle horizon predicates: EMA-position flips on
        # the horizon frames, vs the last fired horizon output.
        prev_horizons = (last_state or {}).get("horizons") or {}
        for hz in self.horizons:
            candles = fold_candles(self._horizon_folds[hz], include_partial=True)
            hz_closes = _closes(candles)
            if not hz_closes:
                continue
            hz_pos = ema_position(hz_closes).get(EMA_HORIZON_LEG)
            prev_hz_out = (prev_horizons.get(hz) or {}).get("output") or {}
            prev_hz_pos = (prev_hz_out.get("ema_position") or {}).get(EMA_HORIZON_LEG)
            if hz_pos is not None and prev_hz_pos is not None and hz_pos != prev_hz_pos:
                predicates[f"{hz}:ema_position_flip"] = {
                    "horizon": hz, "leg": EMA_HORIZON_LEG,
                    "from": prev_hz_pos, "to": hz_pos,
                }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — technicals substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int,
                artifacts: ArtifactBundle | None = None) -> dict[str, Any]:
        klines = (evidence.get("futures") or {}).get("klines") or []
        closes = _closes(klines)
        if not closes:
            # Null discipline: no klines means no time-series math.
            return {}
        # Mirrors composition._technical_builder (emas + source marker).
        out = {
            "emas": ema_series(closes),
            "emas_source": "derivatives.klines_5m",
            "atr_pct": atr_pct_from_klines(klines, period=14),
            "trend_slope": trend_slope(closes),
            "trend_drift": trend_drift(closes),
            "ema_position": ema_position(closes),
            "kline_count": len(klines),
        }
        # Phase A — fold-candle horizon outputs: per-horizon EMA/trend
        # over the fold ring (segments as candles). Same substrate
        # functions, kline-shaped input — the math stays horizon-agnostic.
        hz_outputs: dict[str, Any] = {}
        for hz in self.horizons:
            candles = fold_candles(self._horizon_folds[hz], include_partial=True)
            if not candles:
                continue
            hz_closes = _closes(candles)
            hz_outputs[hz] = {
                "ema_position": ema_position(hz_closes),
                "trend_slope": trend_slope(hz_closes),
                "trend_drift": trend_drift(hz_closes),
                "atr_pct": atr_pct_from_klines(candles, period=self.FOLD_ATR_PERIOD),
                "candle_count": len(candles),
                "source": "fold_candles",
            }
        if hz_outputs:
            out["horizons"] = hz_outputs
        return out
