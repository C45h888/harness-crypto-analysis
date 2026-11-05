"""Positioning (OI) substrate worker — walls + contract composition.

Bounded to exactly ONE substrate: ``calculations.substrates.positioning``.
Watches open interest against the seller-wall grid; fires when OI moves
>= 1% vs last state; computes the oi section the composition root runs
(``composition._adapt_oi``).

OI history + long/short ratios arrive ONLY via the derivative evidence
cache (cache-only — the worker never touches Binance). The core merges
``oi_history``/``top_ls``/``global_ls`` into the window before the probe;
a missing/stale cache keeps the worker dormant.

Probe: ``|ΔOI| >= 1%`` vs last state's ``raw_open_interest`` (provenance:
the deterministic_signals ``open_interest_shift`` 0.01 threshold).

The ``_oi_series`` / ``_oi_value_series`` / ``_ls_last_pct`` extractors are
adapter-level marshalling copied from the composition oi adapter (same
class as the anchors tick/step heuristic): they read the raw derivative
bar shapes into the substrate's list inputs.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.positioning import (
    find_walls,
    oi_implied_value,
    oi_inflow_outflow,
    oi_weighted_contracts,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision, SUBSTRATE_STATE_SCHEMA_VERSION
from market_service.substrate_worker.core import SubstrateWorkerCore

# OI move threshold — the deterministic_signals open_interest_shift rule
# (abs(current - previous) / previous >= 0.01).
OI_MOVE_PCT = 1.0


def _pairs(levels: Any, depth: int) -> list[list[float]]:
    out: list[list[float]] = []
    for row in (levels or [])[:depth]:
        try:
            out.append([float(row[0]), float(row[1])])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _mid(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


def _oi_series(oi_hist: Any) -> list[float]:
    """Adapter marshalling (copied from the composition oi adapter)."""
    out: list[float] = []
    for row in oi_hist or []:
        if not isinstance(row, dict):
            continue
        v = (row.get("sumOpenInterest") or row.get("sum_open_interest")
             or row.get("openInterest") or row.get("open_interest"))
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _oi_value_series(oi_hist: Any) -> list[float]:
    """Adapter marshalling (copied from the composition oi adapter)."""
    out: list[float] = []
    for row in oi_hist or []:
        if not isinstance(row, dict):
            continue
        v = row.get("sumOpenInterestValue") or row.get("sum_open_interest_value")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _ls_last_pct(series: Any) -> float | None:
    """Adapter marshalling (copied from the composition oi adapter)."""
    if not series:
        return None
    last = series[-1] if isinstance(series[-1], dict) else None
    if not last:
        return None
    for k in ("longAccount", "long_account"):
        if k in last and last[k] is not None:
            try:
                return float(last[k])
            except (TypeError, ValueError):
                continue
    return None


class OiWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "oi"
    INPUT_STREAMS = ("raw",)
    CADENCE = CadenceProfile(cooldown_s=60, staleness_s=300)
    DERIVATIVE_INPUTS = ("oi_history", "top_ls", "global_ls")

    # ------------------------------------------------------------------
    # Worker-owned scheduling — WHEN this substrate may compute.
    #
    # Absorbed from the deleted core/fire.py: this file (not shared code)
    # owns the cascade order and gates below. Shared primitives (reads,
    # evidence cache, governance gates, fail-closed dedupe, publish) live
    # in core/base.py, governance/, substrate_worker/evidence.py.
    # Policy: OI move >= 1% vs last state; derivative-cache-gated. Cooldown 60s probe-only (hygiene bypasses);
    # staleness 300s.
    # ------------------------------------------------------------------

    async def decide(
        self,
        rows: list[dict[str, Any]],
        now_ms: int,
        *,
        ws_rows: list[dict[str, Any]] | None = None,
        recovery: bool = False,
    ) -> None:
        """OiWorker: OI move >= 1% vs last state; derivative-cache-gated."""
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
    # L2 — significance probe (|ΔOI| >= 1%)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        fut = window.get("futures") or {}
        oi_raw = fut.get("open_interest") or {}
        oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
        # Binance REST returns open_interest as a JSON STRING ("7923049.05");
        # the raw stream preserves that shape. Coerce here so the arithmetic
        # below never sees a string (null discipline on parse failure).
        if isinstance(oi_value, str):
            try:
                oi_value = float(oi_value)
            except (TypeError, ValueError):
                oi_value = None
        current = oi_value if isinstance(oi_value, (int, float)) else None
        if current is None:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_open_interest"})
        prev = ((last_state or {}).get("output") or {}).get("raw_open_interest")
        # Same string→float coercion as current OI (the stored projection
        # may carry a stale string from before the boundary fix).
        if isinstance(prev, str):
            try:
                prev = float(prev)
            except (TypeError, ValueError):
                prev = None
        prev_f = prev if isinstance(prev, (int, float)) else None
        if not prev_f:
            return TriggerDecision(fired=False, source="probe", predicates={})
        move_pct = abs(current - prev_f) / abs(prev_f) * 100.0
        if move_pct < OI_MOVE_PCT:
            return TriggerDecision(fired=False, source="probe", predicates={})
        return TriggerDecision(
            fired=True, source="probe",
            predicates={"oi_move": {"from": prev_f, "to": current,
                                    "move_pct": round(move_pct, 4),
                                    "threshold_pct": OI_MOVE_PCT}})

    # ------------------------------------------------------------------
    # Compute — positioning substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut = evidence.get("futures") or {}
        fut_book = fut.get("order_book") or {}
        asks = _pairs(fut_book.get("asks"), depth)
        bids = _pairs(fut_book.get("bids"), depth)
        last_price = _mid(bids, asks)
        oi_raw = fut.get("open_interest") or {}
        oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
        # String-to-number coercion for the same Binance shape as the probe.
        if isinstance(oi_value, str):
            try:
                oi_value = float(oi_value)
            except (TypeError, ValueError):
                oi_value = None
        oi_float = oi_value if isinstance(oi_value, (int, float)) else None
        oi_hist = fut.get("oi_history") or []
        if (last_price is None or last_price <= 0) and oi_float is None and not oi_hist:
            # Null discipline: no price, no OI, no history — nothing to say.
            return {}
        # Mirrors composition._adapt_oi section for section.
        walls = (find_walls(asks, float(last_price), 0.005, 0.05)
                 if last_price is not None and last_price > 0 else [])
        oi_series = _oi_series(oi_hist)
        oi_value_series = _oi_value_series(oi_hist)
        weighted = (
            oi_weighted_contracts(oi_float, _ls_last_pct(fut.get("top_ls")),
                                  _ls_last_pct(fut.get("global_ls")))
            if oi_float is not None
            else {"oi": None, "top_long_contracts": None,
                  "global_long_contracts": None}
        )
        implied_rows = [
            {"bucket": i * 300_000, "oi": v, "oi_value": nv}
            for i, (v, nv) in enumerate(zip(oi_series, oi_value_series))
        ]
        return {
            "walls": walls,
            "weighted_contracts": weighted,
            "inflow_outflow": oi_inflow_outflow(oi_series),
            "implied_value": oi_implied_value(implied_rows),
            "raw_open_interest": oi_float,
            "bars_available": len(oi_series),
        }
