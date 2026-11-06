"""Tape substrate worker — taker-tape flow, bucketed CVD, turnover.

Bounded to exactly ONE substrate: ``calculations.substrates.tape``. Watches
the taker tape on both venues; fires on market-semantic tape boundaries;
computes the flow/bucketed_cvd/correlation/turnover sections the composition
root runs (``composition.run_calculations``); aggregates into the
``tape:latest`` projection.

Probe (deterministic, documented bands only):

  buy_share_cross — a venue's taker buy_share crossed the 0.55/0.45 demand
                    bands (the ``demand_verdict``/``auction_verdict``
                    convention shared by the interpretation plane).
  cvd_surge       — the latest futures CVD bucket's |delta| exceeds
                    ``TAPE_CVD_MULTIPLE`` (env-tunable, default 3.0) times
                    the prior buckets' mean absolute delta.

Compute emits ONLY tape-substrate outputs (spot/futures flow summaries, both
bucketed CVD series, the CVD correlation, turnover share, microprice skew).
"""

from __future__ import annotations

import os
from typing import Any

from market_service.calculations.substrates.tape import (
    bucketed_cvd,
    cvd_series_corr,
    microprice_skew_bps,
    spot_turnover_share,
    summarize,
)
from market_service.runtime.horizons import fold_venue_totals
from market_service.substrate_worker.contracts import ArtifactBundle, CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core import SubstrateWorkerCore

# Probe bands — the demand/auction verdict convention used across the
# interpretation plane (buy_share >= 0.55 demand-dominated,
# <= 0.45 supply-dominated). Not a second threshold table: the same bands
# the demand adapter reads from flow output.
BUY_SHARE_BANDS = (0.55, 0.45)
CVD_MULTIPLE_DEFAULT = 3.0  # |latest Δ| vs prior-bucket mean |Δ|


def _cvd_multiple() -> float:
    try:
        return float(os.getenv("TAPE_CVD_MULTIPLE") or CVD_MULTIPLE_DEFAULT)
    except (TypeError, ValueError):
        return CVD_MULTIPLE_DEFAULT


def _pairs(levels: Any, depth: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in (levels or [])[:depth]:
        try:
            out.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _buy_share(trades: Any) -> float | None:
    buy = sell = 0.0
    n = 0
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        try:
            qty = float(t["qty"])
        except (KeyError, TypeError, ValueError):
            continue
        n += 1
        if t.get("is_buyer_maker"):
            sell += qty
        else:
            buy += qty
    if n == 0 or buy + sell <= 0:
        return None
    return buy / (buy + sell)


def _window_s(evidence: dict[str, Any]) -> int:
    try:
        return max(60, int(evidence.get("fetch_window_ms") or 900_000) // 1_000)
    except (TypeError, ValueError):
        return 900


class TapeWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "tape"
    INPUT_STREAMS = ("raw", "microstructure")
    # Phase A rollout — fold-NATIVE horizons: per-horizon CVD/buy-share
    # are computed from the cadence layer's fold aggregates (no extra
    # evidence windows). Probe predicates are horizon-tagged.
    HORIZONS = ("1h", "4h")
    # Fold-native: compute/probe read only self._horizon_folds, never
    # evidence["horizons"] — skip the raw 1h/4h materializations (vector 2).
    RAW_HORIZON_EVIDENCE = False
    CADENCE = CadenceProfile(
        cooldown_s=60, staleness_s=180, ws_input=True,
        horizons=("1h", "4h"),
        horizon_staleness_s=(("1h", 600), ("4h", 4 * 3600)),
    )

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: buy-share band cross or CVD surge, base plus fold-native 1h/4h. Cooldown 60s probe-only (hygiene bypasses);
    # staleness 180s. 1h staleness at 10m; 4h at 4h.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """TapeWorker: buy-share band cross or CVD surge, base plus fold-native 1h/4h."""
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

        if self.horizons:
            self._fold_rows(rows)

        # L4 — cold start: data arrived but no usable prior projection.
        if last_state is None:
            if not arrival:
                return
            if self.horizons and not self._horizons_backfilled:
                await self._backfill_horizons()
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
                                         high_water=self._high_water((rows or ws_rows)))
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
        await self._fire(decision, last_state, now_ms, high_water=self._high_water((rows or ws_rows)),
                         evidence=window)
    # ------------------------------------------------------------------
    # L2 — significance probe (demand bands + CVD surge)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
        seed: dict[str, Any] | None = None,
    ) -> TriggerDecision:
        spot_trades = (window.get("spot") or {}).get("trades_normalized") or []
        fut_trades = (window.get("futures") or {}).get("trades_normalized") or []
        if not spot_trades and not fut_trades:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_trades"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}

        # buy_share band crossing per venue (absolute share frame).
        for venue, trades in (("spot", spot_trades), ("futures", fut_trades)):
            share = _buy_share(trades)
            prev = (prev_output.get(f"{venue}_flow") or {}).get("buy_share")
            if share is None or prev is None:
                continue
            try:
                prev_f = float(prev)
            except (TypeError, ValueError):
                continue
            hi, lo = BUY_SHARE_BANDS
            crossed = (
                (prev_f < hi <= share) or (prev_f >= hi > share)
                or (prev_f > lo >= share) or (prev_f <= lo < share)
            )
            if crossed:
                predicates[f"buy_share_cross:{venue}"] = {
                    "venue": venue, "from": prev_f, "to": share,
                    "bands": [hi, lo],
                }

        # Phase A — fold-native horizon predicates: per-horizon, per-venue
        # buy-share band crossings between THIS fold's trailing-horizon
        # totals and the PREVIOUS fire's persisted fold totals. The same
        # 0.55/0.45 bands, on the horizon frame — no second threshold table.
        prev_horizons = (last_state or {}).get("horizons") or {}
        for hz in self.horizons:
            cur_totals = fold_venue_totals(self._horizon_folds[hz])
            prev_fold = (prev_horizons.get(hz) or {}).get("fold") or {}
            prev_totals = fold_venue_totals({"segments": prev_fold.get("segments") or []})
            for venue in ("spot", "futures"):
                cur = cur_totals.get(venue) or {}
                prev = prev_totals.get(venue) or {}
                cur_notional = float(cur.get("notional") or 0.0)
                prev_notional = float(prev.get("notional") or 0.0)
                if cur_notional <= 0 or prev_notional <= 0:
                    continue
                share = float(cur.get("buy_notional") or 0.0) / cur_notional
                prev_share = float(prev.get("buy_notional") or 0.0) / prev_notional
                hi, lo = BUY_SHARE_BANDS
                crossed = (
                    (prev_share < hi <= share) or (prev_share >= hi > share)
                    or (prev_share > lo >= share) or (prev_share <= lo < share)
                )
                if crossed:
                    predicates[f"{hz}:buy_share_cross:{venue}"] = {
                        "horizon": hz, "venue": venue,
                        "from": round(prev_share, 6), "to": round(share, 6),
                        "bands": [hi, lo],
                    }

        # CVD surge on the futures tape vs the prior buckets' scale.
        window_s = _window_s(window)
        current = bucketed_cvd(fut_trades, window_s=window_s)
        prev_buckets = prev_output.get("futures_bucketed_cvd") or []
        prev_deltas = [
            abs(float(b.get("delta") or 0.0)) for b in prev_buckets
            if isinstance(b, dict)
        ]
        if current and prev_deltas:
            baseline = sum(prev_deltas) / len(prev_deltas)
            latest = abs(float(current[-1].get("delta") or 0.0))
            multiple = _cvd_multiple()
            if baseline > 0 and latest > multiple * baseline:
                predicates["cvd_surge"] = {
                    "latest_abs_delta": latest, "baseline_mean_abs": baseline,
                    "multiple": multiple,
                }

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — tape substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int,
                artifacts: ArtifactBundle | None = None) -> dict[str, Any]:
        spot = evidence.get("spot") or {}
        fut = evidence.get("futures") or {}
        spot_trades = spot.get("trades_normalized") or []
        fut_trades = fut.get("trades_normalized") or []
        spot_book = spot.get("order_book") or {}
        fut_book = fut.get("order_book") or {}
        window_s = _window_s(evidence)

        spot_flow = summarize(spot_trades, spot_book, depth_levels=depth)
        futures_flow = summarize(fut_trades, fut_book, depth_levels=depth)
        spot_buckets = bucketed_cvd(spot_trades, window_s=window_s)
        futures_buckets = bucketed_cvd(fut_trades, window_s=window_s)
        spot_notional = (spot_flow.get("buy_notional_usd") or 0.0) + (
            spot_flow.get("sell_notional_usd") or 0.0)
        fut_notional = (futures_flow.get("buy_notional_usd") or 0.0) + (
            futures_flow.get("sell_notional_usd") or 0.0)
        # Adapter-level marshalling (mirrors composition.run_calculations):
        # the skew field is named fut_* but computed over the SPOT book.
        spot_bids = _pairs(spot_book.get("bids"), depth)
        spot_asks = _pairs(spot_book.get("asks"), depth)
        out = {
            "spot_flow": spot_flow,
            "futures_flow": futures_flow,
            "spot_bucketed_cvd": spot_buckets,
            "futures_bucketed_cvd": futures_buckets,
            "cvd_series_corr": cvd_series_corr(spot_buckets, futures_buckets,
                                               window_s=window_s),
            "spot_turnover_share": spot_turnover_share(spot_notional, fut_notional),
            "fut_microprice_skew_bps": microprice_skew_bps(spot_bids, spot_asks),
        }
        # Phase A — fold-native horizon outputs: per-horizon per-venue
        # CVD proxy (buy_notional − sell_notional over the trailing
        # horizon) + buy_share, straight from the fold aggregates. The
        # core lifts "horizons" into the payload's horizons block.
        hz_outputs: dict[str, Any] = {}
        for hz in self.horizons:
            totals = fold_venue_totals(self._horizon_folds[hz])
            if not totals:
                continue
            hz_outputs[hz] = {
                "cvd_by_venue": {
                    venue: round(float(v.get("buy_notional") or 0.0)
                                 - float(v.get("sell_notional") or 0.0), 6)
                    for venue, v in sorted(totals.items())
                },
                "buy_share": {
                    venue: round(float(v.get("buy_notional") or 0.0)
                                 / float(v["notional"]), 6)
                    for venue, v in sorted(totals.items())
                    if float(v.get("notional") or 0.0) > 0
                },
                "notional_by_venue": {
                    venue: round(float(v.get("notional") or 0.0), 6)
                    for venue, v in sorted(totals.items())
                },
            }
        if hz_outputs:
            out["horizons"] = hz_outputs
        return out
