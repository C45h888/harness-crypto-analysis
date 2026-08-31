"""Harness-owned pipeline — reads raw evidence from Redis, runs math + analysis, collates.

Replaces the four-node pipeline (data-access → calculations → analysis → collator).
The 5-second poller continuously feeds the raw stream; the harness calls
``run_cycle()`` on its own schedule with a configurable time window.

The output of ``run_cycle`` is one plain dict payload persisted to the
same Redis keys the harness already reads from. The read plane
(``runtime.read_paths``) consumes the same JSON — the frozen envelope
dataclass was retired 2026-08-31; the payload SHAPE is the contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from market_service.calculations.flow import (
    bucketed_cvd,
    cvd_series_corr,
    microprice_skew_bps,
    spot_turnover_share,
    summarize,
)
from market_service.calculations.orderbook import (
    absorption_ladder,
    ask_wall_ladder,
    find_keystone,
    hourly_keystone_migration,
    keystone_bid_stack,
    keystone_trade_intensity,
    significant_levels,
    top_density_windows,
    zone_buy_sell,
    zone_ratio_grid,
)
from market_service.calculations.signals import deterministic_signals
from market_service.calculations.technical import (
    ema_series,
    seller_aggression_classify,
    tiered_large_flow,
)
from market_service.calculations.delta import delta_variable, delta_state as _delta_state_fn

from market_service.clients.binance import Binance
from market_service.calculations.volume_profile import build_volume_profile, volume_profile_summary
from market_service.config import default_depth_levels
from market_service.analysis.auction import (
    auction_verdict,
    flow_persistence,
    initiated_flow,
    microprice,
)
from market_service.analysis.demand import decompose_demand, demand_verdict, macro_climate
from market_service.analysis.oi import (
    find_walls,
    oi_implied_value,
    oi_inflow_outflow,
    oi_weighted_contracts,
    wall_break_assessment,
)
from market_service.analysis.path_absorption import (
    fuel_ratio as path_fuel_ratio,
    simulated_ascent,
    simulated_descent,
)
from market_service.analysis.regime import regime_verdict
from market_service.analysis.wall_migration import (
    bid_tier_balance,
    compute_bid_tiers,
    compute_round_anchors,
    densest_clusters,
    fuel_ratio as wall_fuel_ratio,
    keystone_holds_scorecard,
    keystone_wall_balance,
    level_absorption,
    mega_at_keystone,
    wall_delta,
    wall_trap_assessment,
)
from market_service.config import Settings
from market_service.runtime.contracts import (
    MARKET_RUN_SCHEMA_VERSION,
    _json_safe,
)
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from . import contracts as C
from .contracts import GroupEnvelope

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window config
# ---------------------------------------------------------------------------

WINDOW_MINUTES_MAP: dict[str, int] = {
    "15m": 15,
    "1h": 60,
    "4h": 240,
}


# ---------------------------------------------------------------------------
# Calculation-model groups — segregated command surface (Pass 3 pivot)
#
# The monolithic run-cycle stays as the canonical persisted envelope path
# (--analyze). The calculation-model commands (--wall / --flow /
# --structure / --positioning) run ONLY the sections each analytical
# domain needs — no envelope, no persistence, focused output.
#
# GROUP_MAP is the single source of truth for which calculation and
# analysis sections belong to each domain group. The section names here
# are exactly the keys produced by run_calculations / run_analysis.
# ---------------------------------------------------------------------------

GROUP_MAP: dict[str, dict[str, tuple[str, ...]]] = {
    "wall": {
        "calculations": ("orderbook",),
        "analysis": ("wall_migration", "path_absorption", "oi"),
    },
    "flow": {
        "calculations": ("flow", "bucketed_cvd", "correlation", "technical"),
        "analysis": ("demand", "auction", "delta"),
    },
    "structure": {
        "calculations": ("volume_profile", "technical"),
        "analysis": ("regime", "stage"),
    },
    "positioning": {
        "calculations": (),
        "analysis": ("oi",),
    },
}

# Calculation-section dependencies: a requested section pulls its
# prerequisites in automatically (turnover reads flow output; signals
# reads flow output + OI). Kept separate from GROUP_MAP so the group
# definitions stay domain-pure while dependency resolution stays generic.
_CALC_SECTION_DEPS: dict[str, tuple[str, ...]] = {
    "turnover": ("flow",),
    "signals": ("flow",),
}

# Analysis adapters that consume the calculations layer need the
# corresponding calculation sections present (regime reads calc flow;
# wall_migration reads calc orderbook). Auto-included on request.
_ANALYSIS_CALC_DEPS: dict[str, tuple[str, ...]] = {
    "regime": ("flow",),
    "wall_migration": ("orderbook",),
}


def resolve_calc_sections(sections: frozenset[str] | None) -> frozenset[str] | None:
    """Expand a requested calculation-section set with its prerequisites.

    ``None`` means "all sections" (the canonical full-cycle behavior) and
    passes through untouched. The expansion is transitive so a dependency
    of a dependency is also pulled in.
    """
    if sections is None:
        return None
    resolved = set(sections)
    frontier = list(resolved)
    while frontier:
        sec = frontier.pop()
        for dep in _CALC_SECTION_DEPS.get(sec, ()):
            if dep not in resolved:
                resolved.add(dep)
                frontier.append(dep)
    return frozenset(resolved)


def resolve_analysis_sections(
    sections: frozenset[str] | None,
    calc_sections: frozenset[str] | None,
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    """Expand analysis sections and merge their calculation prerequisites.

    Returns (analysis_sections, calc_sections). An analysis adapter that
    consumes the calculations layer auto-includes the calculation sections
    it reads (regime→flow, wall_migration→orderbook). ``None`` analysis
    sections means "all" and passes through.
    """
    if sections is None:
        return None, calc_sections
    calc = set(calc_sections) if calc_sections is not None else None
    for sec in sections:
        for dep in _ANALYSIS_CALC_DEPS.get(sec, ()):
            if calc is None:
                break  # all calculations already requested
            calc.add(dep)
    return sections, (frozenset(calc) if calc is not None else None)


def sections_for_groups(groups: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    """Flatten requested group names into (calc_sections, analysis_sections).

    Unknown group names raise ValueError so the CLI surfaces a clean error
    instead of silently returning empty output.
    """
    calc: set[str] = set()
    anal: set[str] = set()
    for g in groups:
        spec = GROUP_MAP.get(g)
        if spec is None:
            raise ValueError(f"unknown calculation group: {g!r}")
        calc.update(spec["calculations"])
        anal.update(spec["analysis"])
    return frozenset(calc), frozenset(anal)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Step 1 — read raw evidence from Redis stream
# ---------------------------------------------------------------------------

async def read_raw_window(
    redis: RedisRuntimeStore,
    symbol: str,
    window_minutes: int,
) -> dict[str, Any]:
    """Read the latest raw evidence snapshot and accumulate trades within the window.

    The poller writes a full snapshot every ``poll_seconds``. We read the latest
    snapshot for order book / funding / OI / tickers (point-in-time), and use the
    stream to accumulate trades across the window. Because each snapshot's
    ``trades_normalized`` is a rolling window (``flow_window_seconds`` ≫ poll
    interval), overlapping snapshots repeat the same trade ids; we **dedupe by
    trade ``id``** (stable Binance aggregate id) so volumes/CVD are not inflated
    by redeclaring each trade once per snapshot it appears in.
    """
    latest = await redis.read_raw_latest(symbol)
    if latest is None:
        return {
            "observed_at": _utc_iso(),
            "observed_at_ms": int(time.time() * 1000),
            "fetch_window_ms": window_minutes * 60_000,
            "depth_levels": default_depth_levels(),
            "errors": [{"endpoint": "all", "error": "no raw evidence in Redis"}],
            "coverage": {
                "requested_window_seconds": window_minutes * 60,
                "snapshots_used": 0,
                "latest_observed_at_ms": None,
                "stream_staleness_ms": None,
                "spot_trades": {"trade_count": 0, "raw_trade_count": 0,
                                "duplicates_removed": 0, "first_trade_ms": None,
                                "last_trade_ms": None, "span_seconds": None},
                "futures_trades": {"trade_count": 0, "raw_trade_count": 0,
                                   "duplicates_removed": 0, "first_trade_ms": None,
                                   "last_trade_ms": None, "span_seconds": None},
            },
            "spot": {"ticker_24h": None, "order_book": {}, "trades_raw": [], "trades_normalized": []},
            "futures": {"ticker_24h": None, "order_book": {}, "trades_raw": [], "trades_normalized": [],
                        "funding": {}, "open_interest": {}},
        }

    since_ms = int(time.time() * 1000) - window_minutes * 60_000
    snapshots = await redis.read_raw_window(symbol, since_ms)

    def _dedupe(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            if tid is not None:
                try:
                    key = int(tid)
                except (TypeError, ValueError):
                    continue
                if key in seen:
                    continue
                seen.add(key)
            out.append(t)
        return out

    # Accumulate trades across all snapshots in the window (already oldest-first).
    spot_raw: list[dict[str, Any]] = []
    fut_raw: list[dict[str, Any]] = []
    for snap in snapshots:
        spot_raw.extend(snap.get("spot", {}).get("trades_normalized", []))
        fut_raw.extend(snap.get("futures", {}).get("trades_normalized", []))

    spot_trades = _dedupe(spot_raw)
    fut_trades = _dedupe(fut_raw)

    # Actual coverage — measured, not requested. The doctrine requires the
    # envelope to record what the window REALLY contains: the true trade span,
    # dedupe effectiveness, how many snapshots fed the window, and how stale
    # the latest stream entry is. Never claim a window the data doesn't cover.
    def _trade_coverage(trades: list[dict[str, Any]], raw_count: int) -> dict[str, Any]:
        tss: list[int] = []
        for t in trades:
            try:
                tss.append(int(t["ts"]))
            except (KeyError, TypeError, ValueError):
                continue
        if tss:
            span = {"first_trade_ms": min(tss), "last_trade_ms": max(tss),
                    "span_seconds": (max(tss) - min(tss)) / 1000.0}
        else:
            span = {"first_trade_ms": None, "last_trade_ms": None,
                    "span_seconds": None}
        return {"trade_count": len(trades), "raw_trade_count": raw_count,
                "duplicates_removed": raw_count - len(trades), **span}

    raw_spot_count = sum(len(s.get("spot", {}).get("trades_normalized") or []) for s in snapshots)
    raw_fut_count = sum(len(s.get("futures", {}).get("trades_normalized") or []) for s in snapshots)
    latest_observed_ms = latest.get("observed_at_ms")
    now_ms = int(time.time() * 1000)
    coverage = {
        "requested_window_seconds": window_minutes * 60,
        "snapshots_used": len(snapshots),
        "latest_observed_at_ms": latest_observed_ms,
        "stream_staleness_ms": (
            now_ms - int(latest_observed_ms)
            if isinstance(latest_observed_ms, (int, float)) else None
        ),
        "spot_trades": _trade_coverage(spot_trades, raw_spot_count),
        "futures_trades": _trade_coverage(fut_trades, raw_fut_count),
    }

    return {
        # Evidence time: when the source snapshot was observed, not when the
        # harness happened to read it. The read instant is coverage
        # information, not evidence identity.
        "observed_at": latest.get("observed_at") or _utc_iso(),
        "observed_at_ms": int(time.time() * 1000),
        "fetch_window_ms": window_minutes * 60_000,
        "depth_levels": latest.get("depth_levels") or default_depth_levels(),
        "errors": latest.get("errors", []),
        "coverage": coverage,
        "spot": {
            "ticker_24h": latest.get("spot", {}).get("ticker_24h"),
            "order_book": latest.get("spot", {}).get("order_book") or {},
            "trades_raw": latest.get("spot", {}).get("trades_raw") or [],
            "trades_normalized": spot_trades,
        },
        "futures": {
            "ticker_24h": latest.get("futures", {}).get("ticker_24h"),
            "order_book": latest.get("futures", {}).get("order_book") or {},
            "trades_raw": latest.get("futures", {}).get("trades_raw") or [],
            "trades_normalized": fut_trades,
            "funding": latest.get("futures", {}).get("funding") or {},
            "open_interest": latest.get("futures", {}).get("open_interest") or {},
        },
    }


# ---------------------------------------------------------------------------
# Step 2 — run calculations (same adapters as nodes/calculations.py)
# ---------------------------------------------------------------------------

def _spot_trades(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return (evidence.get("spot") or {}).get("trades_normalized") or []


def _fut_trades(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return (evidence.get("futures") or {}).get("trades_normalized") or []


def _spot_book(evidence: dict[str, Any]) -> dict[str, Any]:
    return (evidence.get("spot") or {}).get("order_book") or {}


def _fut_book(evidence: dict[str, Any]) -> dict[str, Any]:
    return (evidence.get("futures") or {}).get("order_book") or {}


def _strict(fn, *args, name: str, **kwargs):
    return C.strict_call(name, fn, *args, **kwargs)


def _run_section(name: str, builder, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return builder()
    except C.ContractViolation as violation:
        errors.append(C.contract_error_entry(violation))
        log.warning("[pipeline] %s contract violation: %s", name, violation.reason)
        return None


def _safe_last_price(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


def _enrich_fut_keystone(keystone: dict[str, Any], asks: list[list[float]]) -> dict[str, Any]:
    """Resolve the ``ask`` alias for the futures keystone result.

    ``find_keystone`` natively emits ``bid`` (its contract is a densest
    *bid*-window search). The one alias that remains adapter-side is ``ask``:
    the nearest ask at-or-above the keystone price — the first price sellers
    defend, which the briefing compares against buyer defence. The ask needs
    the futures asks list, which the calc layer's ``find_keystone`` never
    receives.

    Round-number anchors + mega-tier percentage are computed elsewhere
    (``_adapt_wall_migration``).
    """
    if not isinstance(keystone, dict):
        return keystone
    if "ask" not in keystone:
        bid = keystone.get("bid") or keystone.get("keystone")
        keystone["ask"] = None
        if bid is not None:
            try:
                bid_f = float(bid)
            except (TypeError, ValueError):
                bid_f = None
            if bid_f is not None and asks:
                # nearest ask at-or-above keystone price
                best = None
                for p, q in asks:
                    try:
                        pf = float(p)
                        qf = float(q)
                    except (TypeError, ValueError):
                        continue
                    if pf >= bid_f:
                        if best is None or pf < best[0]:
                            best = (pf, qf)
                if best is not None:
                    keystone["ask"] = best[0]
    return keystone


def run_calculations(
    evidence: dict[str, Any],
    depth: int,
    window: int,
    sections: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Run the same deterministic calculations as the old calculations node.

    ``sections`` (calculation-model groups, Pass 3): when ``None`` (default)
    every section runs — the canonical full-cycle behavior. When a
    frozenset of section names is given, only those sections (plus their
    auto-resolved prerequisites) execute and only they appear in the output
    ``calculations`` dict. This is what the segregated ``--wall`` /
    ``--flow`` / ``--structure`` / ``--positioning`` commands use to skip
    work they don't need.
    """
    errors: list[dict[str, Any]] = []
    resolved = resolve_calc_sections(sections)

    def want(name: str) -> bool:
        return resolved is None or name in resolved

    flow = _run_section("flow", lambda: {
        "spot_flow": _strict(summarize, _spot_trades(evidence), _spot_book(evidence),
                             depth_levels=depth, name="summarize"),
        "futures_flow": _strict(summarize, _fut_trades(evidence), _fut_book(evidence),
                               depth_levels=depth, name="summarize"),
    }, errors) or {} if want("flow") else {}

    # Bucketed CVD is computed ONCE and shared by the bucketed_cvd section
    # and the correlation section (which previously re-ran bucketed_cvd
    # twice inline — redundant deterministic work).
    want_cvd_series = want("bucketed_cvd") or want("correlation")
    spot_cvd_series = (
        _strict(bucketed_cvd, _spot_trades(evidence), window_s=window, name="bucketed_cvd")
        if want_cvd_series else None
    )
    fut_cvd_series = (
        _strict(bucketed_cvd, _fut_trades(evidence), window_s=window, name="bucketed_cvd")
        if want_cvd_series else None
    )

    bucketed = _run_section("bucketed_cvd", lambda: {
        "spot_bucketed_cvd": spot_cvd_series,
        "futures_bucketed_cvd": fut_cvd_series,
    }, errors) or {} if want("bucketed_cvd") else {}

    correlation = (
        _run_section("correlation", lambda: _strict(
            cvd_series_corr, spot_cvd_series, fut_cvd_series,
            window_s=window, name="cvd_series_corr",
        ), errors) if want("correlation") else None
    )

    # Orderbook
    fut_book = _fut_book(evidence)
    spot_book = _spot_book(evidence)
    fut_bids = C.require_list_of_pairs(fut_book.get("bids"), function="orderbook.*", where="futures.order_book.bids", max_items=depth)
    fut_asks = C.require_list_of_pairs(fut_book.get("asks"), function="orderbook.*", where="futures.order_book.asks", max_items=depth)
    spot_bids = C.require_list_of_pairs(spot_book.get("bids"), function="orderbook.*", where="spot.order_book.bids", max_items=depth)
    spot_asks = C.require_list_of_pairs(spot_book.get("asks"), function="orderbook.*", where="spot.order_book.asks", max_items=depth)
    last_price = _safe_last_price(fut_bids, fut_asks)

    orderbook: dict[str, Any] = {}
    if want("orderbook") and last_price is not None:
        def _orderbook_builder() -> dict[str, Any]:
            fut_keystone = _enrich_fut_keystone(
                _strict(find_keystone, fut_bids, last_price, 0.20, -0.30, -0.05, None, name="find_keystone"),
                fut_asks,
            )
            kz_price = fut_keystone.get("keystone") if isinstance(fut_keystone, dict) else None
            kz_tight = (fut_keystone.get("tight") or {}) if isinstance(fut_keystone, dict) else {}
            kz_wide = (fut_keystone.get("wide") or {}) if isinstance(fut_keystone, dict) else {}
            return {
                "fut_keystone": fut_keystone,
                "spot_keystone": _strict(find_keystone, spot_bids, last_price, 0.20, -0.30, -0.05, None, name="find_keystone"),
                "fut_top_density_bids": _strict(top_density_windows, fut_book, 0.5, "bids", 5, name="top_density_windows"),
                "fut_absorption_ladder": _strict(absorption_ladder, fut_bids, last_price, count=10, name="absorption_ladder"),
                "fut_significant_levels": _strict(significant_levels, fut_bids + fut_asks, 0.0, name="significant_levels"),
                "fut_microprice_skew_bps": _strict(microprice_skew_bps, spot_bids, spot_asks, name="microprice_skew_bps"),
                # Legacy port (deep_keystone.py:104-200): cumulative bid stack
                # around the keystone — below / at / above decomposition.
                "keystone_bid_stack": (
                    _strict(keystone_bid_stack, fut_bids, kz_price, name="keystone_bid_stack")
                    if kz_price is not None else None
                ),
                # Legacy port (deep_keystone.py:74-101 + keystone_scan.py:130-165):
                # trade-flow intensity at the keystone defence zone.
                "keystone_trade_intensity": (
                    _strict(keystone_trade_intensity, _fut_trades(evidence),
                            kz_tight.get("lo"), kz_tight.get("hi"),
                            kz_wide.get("lo"), kz_wide.get("hi"),
                            name="keystone_trade_intensity")
                    if kz_price is not None and kz_tight.get("lo") is not None
                       and kz_wide.get("hi") is not None else None
                ),
                # Legacy port (wall_analysis.py:83-112): 0.05-bucket ask ladder
                # above price with cumulative notional (default 3.00 reach).
                "ask_wall_ladder": _strict(ask_wall_ladder, fut_asks, last_price,
                                           None, 0.05, 60, name="ask_wall_ladder"),
                # Legacy port (long_term_flow.py hourly keystone): intra-window
                # keystone migration over the current trade window. At the default
                # 15m window this yields a single bucket (verdict FLAT — valid);
                # meaningful at --window 1h/4h.
                "hourly_keystone_migration": _strict(
                    hourly_keystone_migration, _fut_trades(evidence), 0.05, 0.20,
                    name="hourly_keystone_migration"),
            }
        orderbook = _run_section("orderbook", _orderbook_builder, errors) or {}

    # Volume profile
    def _volume_profile_builder() -> dict[str, Any]:
        # Build the profile ONCE — the previous code constructed it twice
        # (once for ``buckets``, once for ``summary``) with identical inputs.
        profile = _strict(build_volume_profile, _fut_trades(evidence), 0.05, name="build_volume_profile")
        return {
            "buckets": profile,
            "summary": _strict(volume_profile_summary, profile, name="volume_profile_summary"),
        }
    volume_profile = _run_section("volume_profile", _volume_profile_builder, errors) or {} if want("volume_profile") else {}

    # Technical
    def _technical_builder() -> dict[str, Any]:
        # 5m klines arrive only via the on-demand derivative fetch
        # (_merge_derivatives); the 5-second poller never carries them.
        # When absent, emit null with an explicit source marker instead of
        # silently computing EMAs on an empty series.
        kline_rows = (evidence.get("futures") or {}).get("klines") or []
        closes = [float(row[4]) for row in kline_rows
                  if isinstance(row, (list, tuple)) and len(row) >= 5]
        return {
            "emas": _strict(ema_series, closes, name="ema_series") if closes else None,
            "emas_source": "derivatives.klines_5m" if closes else None,
        # Legacy port (sol_deep_monitor.py:191-247): tiered large-print flow
        # (large/huge/whale) over trailing 5m/15m windows.
        "tiered_large_flow": _strict(tiered_large_flow, _fut_trades(evidence),
                                     name="tiered_large_flow"),
        # Legacy port (seller_wall_check.py:162-240): seller aggression
        # classification from big prints >= 100 SOL in the last 5 min.
        "seller_aggression": _strict(seller_aggression_classify, _fut_trades(evidence),
                                     name="seller_aggression_classify"),
        }
    technical = _run_section("technical", _technical_builder, errors) or {} if want("technical") else {}

    # Turnover
    spot_flow = flow.get("spot_flow") or {}
    fut_flow = flow.get("futures_flow") or {}
    spot_notional = (spot_flow.get("buy_notional_usd") or 0.0) + (spot_flow.get("sell_notional_usd") or 0.0)
    fut_notional = (fut_flow.get("buy_notional_usd") or 0.0) + (fut_flow.get("sell_notional_usd") or 0.0)
    turnover = _run_section("turnover", lambda: {
        "spot_turnover_share": _strict(spot_turnover_share, spot_notional, fut_notional, name="spot_turnover_share"),
    }, errors) or {} if want("turnover") else {}

    # Signals
    signal_inputs = {
        "spot_buy_share": spot_flow.get("buy_share"),
        "futures_buy_share": fut_flow.get("buy_share"),
        "spot_obi_top_n": spot_flow.get("obi"),
        "open_interest": ((evidence.get("futures") or {}).get("open_interest") or {}).get("open_interest"),
    }
    signals: list[dict[str, Any]] = []
    if want("signals"):
        try:
            signals = list(_strict(deterministic_signals, signal_inputs, None, name="deterministic_signals"))
        except C.ContractViolation as violation:
            errors.append(C.contract_error_entry(violation))

    # Build the calculations dict with only the sections that were requested
    # (or all of them when sections=None). Unrequested sections are omitted
    # entirely — downstream readers see no trace of them.
    all_calc: dict[str, Any] = {
        "flow": flow,
        "bucketed_cvd": bucketed,
        "correlation": correlation,
        "signal_inputs": signal_inputs,
        "signals": signals,
        "orderbook": orderbook,
        "turnover": turnover,
        "volume_profile": volume_profile,
        "technical": technical,
    }
    calc_out = {k: v for k, v in all_calc.items() if want(k)} if resolved is not None else all_calc

    return {
        "status": "degraded" if errors else "healthy",
        "errors": errors,
        "calculations": calc_out,
        "coverage_seconds": window,
    }


# ---------------------------------------------------------------------------
# Step 3 — run analysis (same adapters as nodes/analysis.py)
# ---------------------------------------------------------------------------

def _fut_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("futures") or {}


def _spot_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("spot") or {}


def _fut_book_e(evidence: dict[str, Any]) -> dict[str, Any]:
    return _fut_evidence(evidence).get("order_book") or {}


def _levels(book: dict[str, Any], depth: int, *, function: str) -> tuple[list[list[float]], list[list[float]]]:
    bids = C.require_list_of_pairs(book.get("bids"), function=function, where="order_book.bids", max_items=depth)
    asks = C.require_list_of_pairs(book.get("asks"), function=function, where="order_book.asks", max_items=depth)
    return bids, asks


def _last_price_e(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    return _safe_last_price(bids, asks)


def _ticker_change(ticker: Any) -> dict[str, Any]:
    if not isinstance(ticker, dict):
        return {"change_pct": None}
    value = ticker.get("change_pct")
    if value is None:
        value = ticker.get("price_change_percent", ticker.get("priceChangePercent"))
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None
    return {**ticker, "change_pct": value}


def _bid_floors_from_significant_levels(
    bid_levels: list[list[float]],
    calc_significant_levels: list[dict] | None,
    price: float,
) -> list[float]:
    floors: list[float] = []
    if calc_significant_levels:
        for sl in calc_significant_levels:
            raw = sl.get("price") if isinstance(sl, dict) else None
            if raw is None:
                continue
            try:
                p = float(raw)
            except (TypeError, ValueError):
                continue
            if p <= 0:
                continue
            floors.append(p)
            if len(floors) >= 5:
                break
    if not floors and price > 0:
        floors = [price]
    return floors


def _single_cycle_wall_counts(asks: list[list[float]], direction_threshold: float = 1.15) -> tuple[int, int]:
    if not asks:
        return 0, 0
    built = 0
    eroded = 0
    prev_qty: float | None = None
    for _, qty in asks:
        try:
            q = float(qty)
        except (TypeError, ValueError):
            continue
        if prev_qty is not None and prev_qty > 0:
            if q >= prev_qty * direction_threshold:
                built += 1
            elif q <= prev_qty / direction_threshold:
                eroded += 1
        prev_qty = q
    return built, eroded


def run_analysis(
    evidence: dict[str, Any],
    calculations: dict[str, Any],
    *,
    prior_walls: dict[float, float] | None = None,
    prior_cycle_ts: str | None = None,
    depth: int | None = None,
    sections: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Run the same deterministic analysis as the old analysis node.

    ``depth`` is the centralized canonical order-book depth (defaults to the
    config resolver) and is threaded into every adapter so wall/OI/path
    analyses scan the full configured book instead of a hard-coded slice.

    ``sections`` (calculation-model groups, Pass 3): when ``None`` (default)
    every analysis adapter runs. When a frozenset of section names is given,
    only those adapters execute and only they appear in the output
    ``analysis`` dict. This is what the segregated ``--wall`` / ``--flow`` /
    ``--structure`` / ``--positioning`` commands use.
    """
    if depth is None:
        from market_service.config import default_depth_levels
        depth = default_depth_levels()
    errors: list[dict[str, Any]] = []

    def want(name: str) -> bool:
        return sections is None or name in sections

    calc_data = calculations.get("calculations") or {}
    orderbook_calc = calc_data.get("orderbook") or {}

    # Auction
    auction = _run_section("auction", lambda: _adapt_auction(evidence), errors) or {} if want("auction") else {}

    # Open interest
    oi = _run_section("oi", lambda: _adapt_oi(evidence, depth=depth), errors) or {} if want("oi") else {}

    # Wall migration
    pw = prior_walls or {}
    wall_migration = _run_section("wall_migration", lambda: _adapt_wall_migration(
        evidence, orderbook_calc.get("fut_significant_levels"), pw, prior_cycle_ts,
        orderbook=orderbook_calc, depth=depth,
    ), errors) or {} if want("wall_migration") else {}

    # Path absorption
    path_absorption = _run_section("path_absorption", lambda: _adapt_path_absorption(evidence, depth=depth), errors) or {} if want("path_absorption") else {}

    # Demand
    demand = _run_section("demand", lambda: _adapt_demand(evidence), errors) or {} if want("demand") else {}

    # Regime
    regime = _run_section("regime", lambda: _adapt_regime(evidence, calc_data), errors) or {} if want("regime") else {}

    # Stage
    stage = _run_section("stage", lambda: _adapt_stage(evidence), errors) or {} if want("stage") else {}

    # Delta — signed -2..+2 combo of wall imbalance + taker buy alignment.
    delta = _run_section("delta", lambda: _adapt_delta(evidence), errors) or {} if want("delta") else {}

    all_analysis: dict[str, Any] = {
        "auction": auction,
        "open_interest": oi,
        "wall_migration": wall_migration,
        "delta": delta,
        "path_absorption": path_absorption,
        "demand": demand,
        "regime": regime,
        "stage": stage,
    }
    # The OI adapter historically emits under ``open_interest`` while its
    # section id (GROUP_MAP, deps) is ``oi``. Filter by the section id, but
    # preserve the established output key for downstream readers.
    SECTION_IDS: dict[str, str] = {"open_interest": "oi"}
    analysis_out = {
        k: v
        for k, v in all_analysis.items()
        if want(SECTION_IDS.get(k, k))
    } if sections is not None else all_analysis

    return {
        "status": "degraded" if errors else "healthy",
        "errors": errors,
        "analysis": analysis_out,
    }


def _adapt_auction(evidence: dict[str, Any]) -> dict[str, Any]:
    fut_book = _fut_book_e(evidence)
    fut_trades = _fut_evidence(evidence).get("trades_normalized") or []
    funding = _fut_evidence(evidence).get("funding") or {}

    mp_spot = _strict(microprice, fut_book, name="microprice")
    initiated = _strict(initiated_flow, fut_trades, name="initiated_flow")
    persistence = _strict(flow_persistence, fut_trades, name="flow_persistence")

    state = {
        "spot": {"microprice": mp_spot, "flow": {"buy_share": None, "vwap": None, "cvd": None}, "persistence": {}},
        "futures": {"microprice": mp_spot, "flow": {"buy_share": None, "vwap": None, "cvd": None}, "persistence": persistence},
        "derivatives": {
            "funding": C.require_float(funding.get("last_funding_rate") if isinstance(funding, dict) else None,
                                       function="auction_verdict", where="derivatives.funding"),
            "taker_buy_ratio": None,
            "oi_change_pct": None,
        },
    }
    verdict, reasons = _strict(auction_verdict, state, name="auction_verdict")
    return {"microprice": mp_spot, "initiated_flow": initiated, "flow_persistence": persistence,
            "verdict": verdict, "reasons": reasons}


def _adapt_delta(evidence: dict[str, Any]) -> dict[str, Any]:
    """Compute the signed DELTA (-2..+2) from + taker buy alignment.

    The wall imbalance uses the futures order book (not spot — the legacy DELTA
    was a futures-only variable). The flow alignment consumes the
    ``taker_buy_sell`` series populated by the on-demand derivative fetch;
    before that fetch landed in Redis it is None and the function returns 0
    for the flow leg (the wall leg is still computable from the book).
    """
    fut = _fut_evidence(evidence)
    fut_book = fut.get("order_book") or {}
    tbr_series = fut.get("taker_buy_sell")
    last_price = _last_price_e(
        C.require_list_of_pairs(fut_book.get("bids"), function="delta.find_price",
                                where="futures.order_book.bids", max_items=20),
        C.require_list_of_pairs(fut_book.get("asks"), function="delta.find_price",
                                where="futures.order_book.asks", max_items=20),
    )
    if last_price is None or last_price <= 0:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "delta": None, "wall_imbalance": None, "flow_alignment": None,
            "tbr_last_pct": None, "tbr_3avg_pct": None,
            "bands": [], "range": {"lo": None, "hi": None},
        }
    delta = _strict(delta_variable, fut_book, tbr_series, float(last_price),
                    half_range=0.75, band_step=0.10, name="delta_variable")
    return {
        "delta": delta["delta"],
        "delta_raw": delta["delta_raw"],
        "wall_imbalance": delta["wall_imbalance"],
        "flow_alignment": delta["flow_alignment"],
        "tbr_last_pct": delta["tbr_last_pct"],
        "tbr_3avg_pct": delta["tbr_3avg_pct"],
        "n_bands": delta["n_bands"],
        "bands": delta["bands"],
        "range": delta["range"],
        "verdict": _delta_state_fn(delta["delta"]),
    }


def _adapt_oi(evidence: dict[str, Any], *, depth: int) -> dict[str, Any]:
    fut = _fut_evidence(evidence)
    fut_book = fut.get("order_book") or {}
    asks = C.require_list_of_pairs(fut_book.get("asks"), function="oi.find_walls", where="futures.order_book.asks", max_items=depth)
    bids = C.require_list_of_pairs(fut_book.get("bids"), function="oi.find_walls", where="futures.order_book.bids", max_items=depth)
    last_price = _last_price_e(bids, asks)
    # Null discipline: a missing/unparseable OI is ``None`` ("source did not
    # provide / could not compute"), never a fabricated 0.0 — zero OI and
    # absent OI are different market facts.
    oi_raw = fut.get("open_interest") or {}
    oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
    oi_float = float(oi_value) if isinstance(oi_value, (int, float)) else None
    walls = (
        _strict(find_walls, asks, float(last_price), 0.005, 0.05, name="find_walls")
        if last_price is not None and last_price > 0 else []
    )

    # OI history series — populated by the on-demand derivative fetch.
    # We extract the per-bar oi_value (USD notional) + oi (contracts) and feed
    # the deterministic layers that previously ran with [] (always empty).
    oi_hist = fut.get("oi_history") or []
    oi_series = _oi_series(oi_hist)
    oi_value_series = _oi_value_series(oi_hist)
    top_long_pct = _ls_last_pct(fut.get("top_ls"))
    glb_long_pct = _ls_last_pct(fut.get("global_ls"))
    # Weighted contracts need a real OI value — without one the output is an
    # explicit null-shaped dict, never OI=0 multiplied by long percentages.
    weighted = (
        _strict(oi_weighted_contracts, oi_float, top_long_pct, glb_long_pct,
                name="oi_weighted_contracts")
        if oi_float is not None
        else {"oi": None, "top_long_contracts": None, "global_long_contracts": None}
    )
    inflow = _strict(oi_inflow_outflow, oi_series, name="oi_inflow_outflow")
    implied_rows = [
        {"bucket": i * 300_000, "oi": v, "oi_value": nv}
        for i, (v, nv) in enumerate(zip(oi_series, oi_value_series))
    ]
    implied = _strict(oi_implied_value, implied_rows, name="oi_implied_value")
    return {"walls": walls, "weighted_contracts": weighted, "inflow_outflow": inflow,
            "implied_value": implied, "raw_open_interest": oi_float,
            "bars_available": len(oi_series)}


def _adapt_wall_migration(
    evidence: dict[str, Any],
    calc_significant_levels: list[dict] | None,
    prior_walls: dict[float, float],
    prior_cycle_ts: str | None,
    *,
    orderbook: dict[str, Any] | None = None,
    depth: int,
) -> dict[str, Any]:
    fut_book = _fut_book_e(evidence)
    fut = _fut_evidence(evidence)
    bids, asks = _levels(fut_book, depth, function="wall_migration.*")
    price = _last_price_e(bids, asks)
    if price is None or price <= 0:
        # Null discipline (same precedent as _adapt_delta): without a real
        # mid price the wall analysis cannot run — every price-dependent
        # metric is an explicit null / empty, never computed against a
        # fabricated 0.0. Tiers and round anchors are computed directly from
        # the (possibly empty) book, so a zero there is a true zero.
        return {
            "verdict": "INSUFFICIENT_DATA",
            "wall_delta": None, "fuel_ratio": None,
            "densest_clusters": [], "trap_assessment": None,
            "prior_cycle_ts": prior_cycle_ts,
            "prior_wall_count": len(prior_walls or {}),
            "tiers": compute_bid_tiers(bids),
            "round_anchors": compute_round_anchors(bids),
            "tier_balance": None, "mega_at_keystone": None,
            "keystone_wall_balance": None,
            "keystone_holds_scorecard": None,
            "level_absorption": [], "wall_break": None,
            "zone_ratio_grid": [], "zone_buy_sell": [],
            "inputs_used": {"floors_count": 0, "ask_walls_built": 0,
                            "ask_walls_eroded": 0, "fuel_ratio_value": None,
                            "reason": "no order-book mid price"},
        }
    bid_floor = price * 0.97
    ask_target = price * 1.03 if price else 0.0
    delta = _strict(wall_delta, prior_walls or {}, asks, 0.02, 1.15, name="wall_delta")
    fuel = _strict(wall_fuel_ratio, bids, asks, price, bid_floor, ask_target, name="fuel_ratio")
    floors = _bid_floors_from_significant_levels(bids, calc_significant_levels, price)
    clusters = _strict(densest_clusters, bids, floors, 0.10, name="densest_clusters")
    built, eroded = _single_cycle_wall_counts(asks)
    trap = _strict(wall_trap_assessment, float(fuel.get("ratio") or 0.0), built, eroded, name="wall_trap_assessment")
    # Tier counts + round-number anchors (legacy institutional_buyers.py work —
    # surfaced as canonical fields so briefings can reason over institutional
    # bid share without re-fetching the orderbook on every read).
    tiers = compute_bid_tiers(bids)
    round_anchors = compute_round_anchors(bids)
    # Institutional bid vs ask balance + mega-at-keystone (the two missing
    # legacy signals from institutional_buyers.py:129,167-171).
    tier_balance = _strict(bid_tier_balance, bids, asks, 5000.0, 1.2, name="bid_tier_balance")
    keystone_for_mega = (orderbook.get("fut_keystone") or {}).get("keystone") if isinstance(orderbook, dict) else None
    mega_kz = (
        _strict(mega_at_keystone, bids, float(keystone_for_mega), 5000.0, 0.10,
                name="mega_at_keystone")
        if keystone_for_mega is not None else {"keystone_price": None, "count": 0,
                                              "qty": 0.0, "notional": 0.0, "levels": []}
    )
    return {"wall_delta": delta, "fuel_ratio": fuel, "densest_clusters": clusters,
            "trap_assessment": trap, "prior_cycle_ts": prior_cycle_ts,
            "prior_wall_count": len(prior_walls or {}),
            "tiers": tiers, "round_anchors": round_anchors,
            "tier_balance": tier_balance, "mega_at_keystone": mega_kz,
            "keystone_wall_balance": _wall_keystone_balance(
                bids, asks, keystone_for_mega, price),
            "keystone_holds_scorecard": _wall_keystone_holds(
                fut, bids, asks, keystone_for_mega, price),
            "level_absorption": _wall_level_absorption(bids, floors, price),
            "wall_break": _wall_break(fut, asks),
            "zone_ratio_grid": _wall_zone_grid(fut, keystone_for_mega, price),
            "zone_buy_sell": _wall_zone_intensity(fut, keystone_for_mega, price),
            "inputs_used": {"floors_count": len(floors), "ask_walls_built": built,
                            "ask_walls_eroded": eroded, "fuel_ratio_value": fuel.get("ratio")}}


# ---------------------------------------------------------------------------
# Legacy-parity wall/keystone signal helpers (Issue 2 wiring).
#
# These wrap the migrated-but-dead pure functions so their outputs are emitted
# into ``analysis.wall_migration`` and persisted to Redis/Postgres via the
# run-cycle envelope. All inputs come from the already-merged evidence
# (order book + trades + derivative fetch), so no new data acquisition is
# needed. Test the pure functions directly (tests/test_analysis_m3.py,
# tests/test_calculations_m1.py) — these adapters only marshal evidence into
# the right argument shape.
# ---------------------------------------------------------------------------


def _wall_zone_band(keystone_for_mega: Any, price: float) -> tuple[float, float, float, float]:
    """Derive keystone + seller-wall bands around the current price.

    Keystone band: [kz-0.20, kz] (bid-side defense, legacy convention).
    Seller-wall band: [sw, sw+0.20] anchored at the densest ask cluster above
    price. Falls back to price-centric bands when the keystone is unavailable.
    """
    kf = float(keystone_for_mega) if keystone_for_mega is not None else price
    kz_lo, kz_hi = kf - 0.20, kf
    sw_lo, sw_hi = kf + 0.10, kf + 0.30
    return kz_lo, kz_hi, sw_lo, sw_hi


def _wall_keystone_balance(bids, asks, keystone_for_mega: Any, price: float) -> dict:
    """Keystone vs seller-wall qty/notional balance (keystone_wall_balance)."""
    kz_lo, kz_hi, sw_lo, sw_hi = _wall_zone_band(keystone_for_mega, price)
    return _strict(keystone_wall_balance, bids, asks, kz_lo, kz_hi, sw_lo, sw_hi,
                   name="keystone_wall_balance")


def _wall_keystone_holds(fut, bids, asks, keystone_for_mega: Any, price: float) -> dict:
    """Keystone-holds 0-10 scorecard fed by the balance + on-chain/flow inputs.

    Inputs are derived from evidence that is already present:
      bid_ask_qty_ratio — from keystone_wall_balance
      latest_tbr        — latest taker buy share from taker_buy_sell
      oi_chg_5m         — OI % change over the last two bars
      top_long_pct      — top-trader long account proportion
      net_buy_ratio     — latest trades buy share
    Each is None-safe; if a source is absent the scorecard still runs with
    zero where the deterministic function tolerates it.
    """
    balance = _wall_keystone_balance(bids, asks, keystone_for_mega, price)
    bid_ask_qty_ratio = float(balance.get("bid_ask_qty_ratio") or 0.0)
    latest_tbr = _taker_buy_share(fut.get("taker_buy_sell"))
    oi_chg_5m = _oi_pct_change(fut.get("oi_history"))
    top_long_pct = _ls_last_pct(fut.get("top_ls"))
    if top_long_pct is not None:
        try:
            top_long_pct = float(top_long_pct)
        except (TypeError, ValueError):
            top_long_pct = None
    net_buy_ratio = _net_buy_share(fut.get("trades_normalized"))
    return _strict(
        keystone_holds_scorecard, bid_ask_qty_ratio, latest_tbr, oi_chg_5m,
        top_long_pct or 0.0, net_buy_ratio, name="keystone_holds_scorecard",
    )


def _wall_level_absorption(bids, floors, price: float) -> list[dict]:
    """Absorption capacity (zero-bid vacuum detection) at key bid levels."""
    levels = list(floors) if floors else [price]
    return _strict(level_absorption, bids, levels, 0.01,
                   (1000.0, 5000.0, 10000.0), name="level_absorption")


def _wall_break(fut, asks) -> dict:
    """Can buyers clear the ask stack via buy-flow alone? (wall_break_assessment)."""
    total_wall_sol = sum(float(q) for _, q in asks)
    buy_per_min, peak_buy_per_min = _buy_rates(fut.get("trades_normalized") or [])
    return _strict(wall_break_assessment, total_wall_sol, buy_per_min,
                   peak_buy_per_min, 15, name="wall_break_assessment")


def _wall_zone_grid(fut, keystone_for_mega: Any, price: float) -> list[dict]:
    """Bid/ask ratio grid across the keystone corridor (zone_ratio_grid)."""
    kf = float(keystone_for_mega) if keystone_for_mega is not None else price
    return _strict(zone_ratio_grid, fut.get("order_book") or {}, kf - 0.40, kf + 0.70,
                   0.05, name="zone_ratio_grid")


def _wall_zone_intensity(fut, keystone_for_mega: Any, price: float) -> list[dict]:
    """Taker buy/sell intensity per price zone near the keystone (zone_buy_sell)."""
    kf = float(keystone_for_mega) if keystone_for_mega is not None else price
    return _strict(zone_buy_sell, fut.get("trades_normalized") or [],
                   kf - 0.40, kf + 0.70, 0.05, name="zone_buy_sell")


def _taker_buy_share(taker_bs: list[dict[str, Any]] | None) -> float:
    """Latest taker buy share (0..1) from the Binance taker_buy_sell series.

    Uses buyVol/(buyVol+sellVol) of the most recent bar; 0.5 neutral fallback
    when the series is missing/empty (mirrors summarize's buy_share default).
    """
    if not taker_bs:
        return 0.5
    last = taker_bs[-1] if isinstance(taker_bs[-1], dict) else None
    if not last:
        return 0.5
    bv = float(last.get("buyVol") or last.get("buy_vol") or 0)
    sv = float(last.get("sellVol") or last.get("sell_vol") or 0)
    s = bv + sv
    return bv / s if s > 0 else 0.5


def _oi_pct_change(oi_hist: list[dict[str, Any]] | None) -> float:
    """OI % change between the last two bars; 0.0 on insufficient data."""
    series = _oi_series(oi_hist)
    if len(series) >= 2 and series[-2]:
        return (series[-1] - series[-2]) / series[-2] * 100
    return 0.0


def _net_buy_share(trades: list[dict[str, Any]] | None) -> float:
    """Taker-buy share (0..1) over the recent trade window; 0.5 default."""
    trades = trades or []
    total = 0.0
    buys = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        try:
            qty = float(t.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        total += qty
        is_maker = t.get("is_buyer_maker")
        if is_maker is not None and not is_maker:
            buys += qty
        elif is_maker is None and str(t.get("side", "")).lower() == "buy":
            buys += qty
    return buys / total if total > 0 else 0.5


def _buy_rates(trades: list[dict[str, Any]]) -> tuple[float, float]:
    """Current + peak taker-buy rate (SOL/min) over the recent window.

    Buckets trades into per-minute buy volume; current = last full minute,
    peak = max of all minutes. Returns (0, 0) on empty input.
    """
    if not trades:
        return 0.0, 0.0
    per_min: dict[int, float] = {}
    now_ms = max(
        int(t.get("ts") or t.get("time") or 0) for t in trades if isinstance(t, dict)
    )
    for t in trades:
        if not isinstance(t, dict):
            continue
        is_maker = t.get("is_buyer_maker")
        side = t.get("side")
        if is_maker is not None and is_maker:
            continue
        if is_maker is None and str(side).lower() != "buy":
            continue
        ts = int(t.get("ts") or t.get("time") or 0)
        bucket = ts // 60000 * 60000
        per_min[bucket] = per_min.get(bucket, 0.0) + float(t.get("qty") or 0)
    if not per_min:
        return 0.0, 0.0
    peak = max(per_min.values())
    # current = most recent bucket (closest to now_ms)
    current_bucket = max(per_min)
    current = per_min[current_bucket]
    # Only treat as current if the newest bucket is reasonably close to now.
    if now_ms - current_bucket > 60000 * 2:
        current = 0.0
    return current, peak


def _adapt_path_absorption(evidence: dict[str, Any], *, depth: int) -> dict[str, Any]:
    fut_book = _fut_book_e(evidence)
    bids, asks = _levels(fut_book, depth, function="path_absorption.*")
    price = _last_price_e(bids, asks) or 0.0
    entry = price * 1.01 if price else 0.0
    bid_floor = price * 0.97 if price else 0.0
    levels = [price + i * 0.005 for i in range(1, 6)] if price else []
    total_bid_fuel = sum(p * q for p, q in bids)
    total_ask_fuel = sum(p * q for p, q in asks)
    fr = _strict(path_fuel_ratio, bids, asks, entry, bid_floor, name="path_absorption.fuel_ratio")
    ascent = _strict(simulated_ascent, asks, total_bid_fuel, levels, name="simulated_ascent")
    descent = _strict(simulated_descent, bids, total_ask_fuel, total_bid_fuel, levels, name="simulated_descent")
    return {"fuel_ratio": fr, "simulated_ascent": ascent, "simulated_descent": descent}


def _adapt_demand(evidence: dict[str, Any]) -> dict[str, Any]:
    fut = _fut_evidence(evidence)
    spot = _spot_evidence(evidence)
    cross = evidence.get("cross_asset") or {}
    # Pull BTC/ETH change_pct + funding from the cross-asset fetch the on-demand
    # derivative step populated. Without it, macro_climate returns "data
    # unavailable" so we treat this as best-effort.
    tickers = {t.get("symbol"): t for t in (cross.get("tickers_24h") or []) if isinstance(t, dict)}
    funding_rows = {row.get("symbol"): row.get("funding")
                    for row in (cross.get("funding") or []) if isinstance(row, dict)}

    def _funding_bps(sym: str) -> float | None:
        f = funding_rows.get(sym)
        if not isinstance(f, dict):
            return None
        try:
            return float(f.get("lastFundingRate") or f.get("last_funding_rate") or 0) * 10000
        except (TypeError, ValueError):
            return None

    def _change_pct(sym: str) -> float | None:
        t = tickers.get(sym)
        if not t:
            return None
        try:
            return float(t.get("priceChangePercent") or t.get("price_change_percent"))
        except (TypeError, ValueError):
            return None

    target_change_pct = None
    fut_ticker = fut.get("ticker_24h") or {}
    if isinstance(fut_ticker, dict):
        for k in ("priceChangePercent", "price_change_percent"):
            if k in fut_ticker and fut_ticker[k] is not None:
                try:
                    target_change_pct = float(fut_ticker[k]); break
                except (TypeError, ValueError):
                    pass

    macro_in = {
        "btc": {"change_pct": _change_pct("BTCUSDT"), "funding": _funding_bps("BTCUSDT")},
        "eth": {"change_pct": _change_pct("ETHUSDT"), "funding": _funding_bps("ETHUSDT")},
    }
    d = {
        "spot": {
            "trades": spot.get("trades_normalized") or [],
            "book": spot.get("order_book") or {},
            "ticker_24h": _ticker_change(spot.get("ticker_24h")),
        },
        "futures": {
            "trades": fut.get("trades_normalized") or [],
            "book": fut.get("order_book") or {},
            "open_interest": fut.get("open_interest") or {},
            "oi_history": fut.get("oi_history") or [],
            "mark_price": fut.get("mark_price"),
            "global_ls": fut.get("global_ls"),
            "top_ls": fut.get("top_ls"),
            "taker_buy_sell": fut.get("taker_buy_sell"),
            "ticker_24h": _ticker_change(fut.get("ticker_24h")),
        },
        "latency_ms": None,
    }
    dx = _strict(decompose_demand, d, name="decompose_demand")
    verdict, reasons = _strict(demand_verdict, dx, name="demand_verdict")
    climate = _strict(macro_climate, macro_in, target_change_pct, name="macro_climate")
    return {"decomposition": dx, "verdict": verdict, "reasons": reasons,
            "macro_climate": climate, "macro_inputs": macro_in,
            "cross_asset": {"symbols_seen": sorted(tickers.keys()),
                            "funding_symbols": sorted(funding_rows.keys())}}


def _adapt_regime(evidence: dict[str, Any], calc_data: dict[str, Any]) -> dict[str, Any]:
    flow = calc_data.get("flow") or {}
    fut_evidence = _fut_evidence(evidence)
    d = {
        "futures": {
            "flow": flow.get("futures_flow") or flow.get("spot_flow") or {},
            "open_interest": fut_evidence.get("open_interest") or {},
            "funding": fut_evidence.get("funding") or {},
            "mark_price": fut_evidence.get("mark_price") or {},
            "long_short_ratio": fut_evidence.get("global_ls"),
            "top_long_short_accounts": fut_evidence.get("top_ls"),
        },
    }
    verdict, reasons = _strict(regime_verdict, d, name="regime_verdict")
    return {"state": d, "verdict": verdict, "reasons": reasons}


def _adapt_stage(evidence: dict[str, Any]) -> dict[str, Any]:
    from market_service.analysis.stage import infer_stage
    fut = _fut_evidence(evidence)
    klines = fut.get("klines") or []
    closes = []
    for k in klines:
        if isinstance(k, (list, tuple)) and len(k) >= 5:
            try:
                closes.append(float(k[4]))
            except (TypeError, ValueError):
                pass
    if len(closes) < 2:
        raise C.ContractViolation(
            function="infer_stage",
            reason="need at least 2 closes to compute 4h change",
            details={"kline_count": len(klines)},
        )
    px_chg_4h = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0
    up_steps = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down_steps = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
    # Wire the L/S and funding signals the on-demand derivative fetch populates.
    funding_bps = None
    funding = fut.get("funding") or {}
    if isinstance(funding, dict):
        try:
            funding_bps = float(funding.get("lastFundingRate") or funding.get("last_funding_rate") or 0) * 10000
        except (TypeError, ValueError):
            funding_bps = None
    top_long_pct = _ls_last_pct(fut.get("top_ls"))
    glb_long_pct = _ls_last_pct(fut.get("global_ls"))
    # OI 4h change approximated from the history series if available.
    oi_series = _oi_series(fut.get("oi_history") or [])
    if len(oi_series) >= 2 and oi_series[0] > 0:
        oi_chg_4h = (oi_series[-1] - oi_series[0]) / oi_series[0] * 100
    else:
        oi_chg_4h = 0.0
    return {
        "stage": _strict(infer_stage,
                         px_chg_4h=px_chg_4h, oi_chg_4h=oi_chg_4h, up_pct_4h=50.0,
                         up_steps=up_steps, down_steps=down_steps,
                         funding_bps=funding_bps,
                         top_long_pct=top_long_pct, global_long_pct=glb_long_pct,
                         distribution_clusters=0.0, absorption_clusters=0.0,
                         name="infer_stage"),
        "kline_count": len(klines),
    }


# ---------------------------------------------------------------------------
# Step 4 — collate into the canonical run payload (plain dict)
# ---------------------------------------------------------------------------

def assemble_envelope(
    symbol: str,
    evidence: dict[str, Any],
    calculations: dict[str, Any],
    analysis: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the canonical run payload dict from the three domain outputs.

    The frozen ``MarketRunEnvelope`` dataclass was retired 2026-08-31: this
    function emits the EXACT field names the dataclass's ``to_dict()`` used
    to publish (schema_version, run_id, symbol, generated_at, completed_at,
    status, data_source, coverage, canonical_state, domain_outputs, errors,
    source_metadata) so the Redis/Postgres stored format is byte-compatible
    with every existing read path (``runtime.read_paths`` guards on the
    same schema_version) — the payload shape IS the contract. JSON-safety
    (NaN/Inf scrub via ``_json_safe``) happens here at the write seam,
    where the dataclass's ``to_dict()`` did it before.
    """
    started_ms = int(time.time() * 1000)
    completed_ms = started_ms
    run_id = run_id or str(uuid.uuid4())

    data_access_status = "healthy" if not evidence.get("errors") else "degraded"
    calc_status = calculations.get("status", "degraded")
    analysis_status = analysis.get("status", "degraded")

    domain_status = {
        "data-access": data_access_status,
        "calculations": calc_status,
        "analysis": analysis_status,
    }

    all_errors: list[dict[str, Any]] = []
    for err in evidence.get("errors", []):
        all_errors.append({"domain": "data-access", **err})
    for err in calculations.get("errors", []):
        all_errors.append({"domain": "calculations", **err})
    for err in analysis.get("errors", []):
        all_errors.append({"domain": "analysis", **err})

    if "invalid" in domain_status.values():
        status = "invalid"
    elif "degraded" in domain_status.values() or all_errors:
        status = "degraded"
    else:
        status = "healthy"

    coverage = {
        "flow_window_seconds": evidence.get("fetch_window_ms", 0) // 1000,
        "domain_observed_at": {
            "data-access": evidence.get("observed_at", _utc_iso()),
            "calculations": _utc_iso(),
            "analysis": _utc_iso(),
        },
        "domain_status": domain_status,
    }
    # Measured evidence coverage (actual trade span, dedupe stats, stream
    # staleness) recorded by read_raw_window — the run payload reports what
    # the window really contains, not just the requested window.
    if evidence.get("coverage"):
        coverage["evidence"] = dict(evidence["coverage"])

    canonical_state: dict[str, Any] = {"domain_status": dict(domain_status)}
    canonical_state["data-access"] = {
        "status": data_access_status,
        "errors": evidence.get("errors", []),
        "evidence": {
            "spot": evidence.get("spot", {}),
            "futures": evidence.get("futures", {}),
        },
        "coverage_seconds": evidence.get("fetch_window_ms", 0) // 1000,
    }
    canonical_state["calculations"] = dict(calculations)
    canonical_state["analysis"] = dict(analysis)

    payload: dict[str, Any] = {
        "schema_version": MARKET_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "symbol": symbol.upper(),
        "generated_at": _utc_iso(),
        "completed_at": _utc_iso(),
        "status": status,
        "data_source": "domain_pipeline",
        "coverage": _json_safe(coverage),
        "canonical_state": _json_safe(canonical_state),
        "domain_outputs": _json_safe(canonical_state),
        "errors": list(all_errors),
        "source_metadata": _json_safe({
            "runtime": "market_service",
            "path": "harness_pipeline",
            "latency_ms": round((completed_ms - started_ms), 1),
        }),
    }
    return payload


# ---------------------------------------------------------------------------
# Persistence — Postgres first, then Redis
# ---------------------------------------------------------------------------

async def persist_envelope(
    envelope: dict[str, Any],
    settings: Settings,
    *,
    postgres: PostgresRuntimeStore | None = None,
    redis: RedisRuntimeStore | None = None,
) -> dict[str, Any]:
    """Persist one canonical run payload dict — Postgres first, then Redis.

    Callers that already hold open stores (``run_cycle``) pass them via
    ``postgres``/``redis`` to avoid opening a second connection pair per
    cycle; standalone callers get fresh stores that are closed on exit.
    """
    own_pg = postgres is None
    own_redis = redis is None
    postgres = postgres or PostgresRuntimeStore(settings.database_url)
    redis = redis or RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        inserted = await postgres.insert_run(envelope)
        redis_stream_id = await redis.publish_run(envelope)
        return {
            "run_id": envelope["run_id"],
            "postgres_inserted": inserted,
            "redis_stream_id": redis_stream_id,
            "redis_key": redis.collated_latest_key(envelope["symbol"]),
            "schema_version": envelope["schema_version"],
            "status": envelope["status"],
        }
    finally:
        if own_pg:
            await postgres.close()
        if own_redis:
            await redis.close()

# ---------------------------------------------------------------------------
# Wall-snapshot seam — read the FULL recorded wall history into the migration
# analysis, and WRITE the current cycle's wall state so the next cycle sees it.
# ---------------------------------------------------------------------------


def _wall_snapshot_payload(
    symbol: str,
    run_id: str,
    evidence: dict[str, Any],
    analysis_result: dict[str, Any],
) -> dict[str, Any]:
    """Assemble one wall-snapshot payload from this cycle's evidence + analysis.

    The payload shape matches what ``record_wall_snapshot`` persists (both
    Redis and Postgres): asks/bids plus the deterministic fuel/migration
    metrics. ``cycle_ts`` is the envelope/run generation timestamp so a
    re-run within the same cycle is an idempotent upsert, not a duplicate.
    """
    futures_book = (evidence.get("futures") or {}).get("order_book") or {}
    asks_raw = futures_book.get("asks") or []
    bids_raw = futures_book.get("bids") or []
    wm = ((analysis_result.get("analysis") or {}).get("wall_migration")) or {}
    fuel = wm.get("fuel_ratio") or {}
    inputs = wm.get("inputs_used") or {}
    # Null discipline: ``None`` (analysis degraded / not measured) must reach
    # the ledger as NULL, not as a fabricated 0.0 fuel ratio. Keystone payload
    # already follows this via its ``_f`` helper.
    def _f(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "cycle_ts": _utc_iso(),
        "schema_version": 1,
        "asks": asks_raw,
        "bids": bids_raw,
        "fuel_ratio": _f(inputs.get("fuel_ratio_value")),
        "bid_pool": _f(fuel.get("bid_pool")),
        "ask_pool": _f(fuel.get("ask_pool")),
        "bid_floor": _f(fuel.get("bid_floor")),
        "ask_target": _f(fuel.get("ask_target")),
        "ask_walls_built": int(inputs.get("ask_walls_built") or 0),
        "ask_walls_eroded": int(inputs.get("ask_walls_eroded") or 0),
    }


async def _record_wall_snapshot(
    settings: Settings,
    symbol: str,
    run_id: str,
    evidence: dict[str, Any],
    analysis_result: dict[str, Any],
    *,
    postgres: PostgresRuntimeStore | None = None,
    redis: RedisRuntimeStore | None = None,
) -> dict[str, Any]:
    """WRITE the current cycle's wall snapshot (Postgres first, then Redis).

    This closes the write seam: every cycle appends its wall state to the
    durable ledger so subsequent cycles can call ALL recorded walls, not
    just the one that happened to be written last.

    Callers that already hold open stores (``run_cycle``) pass them via
    ``postgres``/``redis`` to avoid connection churn; standalone callers get
    fresh stores that are closed on exit.
    """
    payload = _wall_snapshot_payload(symbol, run_id, evidence, analysis_result)
    own_pg = postgres is None
    own_redis = redis is None
    postgres = postgres or PostgresRuntimeStore(settings.database_url)
    redis = redis or RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        pg_ok = await postgres.record_wall_snapshot(symbol, run_id, payload)
        redis_id = await redis.record_wall_snapshot(symbol, run_id, payload)
        return {
            "postgres_written": pg_ok,
            "redis_stream_id": redis_id,
            "cycle_ts": payload["cycle_ts"],
            "wall_levels_recorded": len(payload.get("asks") or []),
        }
    finally:
        if own_pg:
            await postgres.close()
        if own_redis:
            await redis.close()


def _keystone_snapshot_payload(
    symbol: str,
    run_id: str,
    calc_result: dict[str, Any],
) -> dict[str, Any]:
    """Assemble one keystone-snapshot payload from this cycle's calculations.

    Extracts the fut_keystone (price + tight/wide bands), the keystone bid
    stack tight-zone total qty, and the ask-wall ladder total notional —
    the scalar state needed to reconstruct cross-cycle keystone migration.
    ``cycle_ts`` is the cycle generation timestamp so a re-run within the
    same cycle is an idempotent upsert, not a duplicate.

    Null discipline: a missing keystone (orderbook section degraded / no
    last_price) yields keystone_price=None — never a fabricated price.
    """
    calculations = (calc_result.get("calculations") or {})
    orderbook = calculations.get("orderbook") or {}
    kz = orderbook.get("fut_keystone") or {}
    kz_price = kz.get("keystone")
    tight = kz.get("tight") or {}
    wide = kz.get("wide") or {}
    stack = orderbook.get("keystone_bid_stack") or {}
    ladder = orderbook.get("ask_wall_ladder") or {}

    def _f(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "cycle_ts": _utc_iso(),
        "schema_version": 1,
        "keystone_price": _f(kz_price),
        "window_qty": _f(kz.get("window_qty")),
        "tight_lo": _f(tight.get("lo")),
        "tight_hi": _f(tight.get("hi")),
        "wide_lo": _f(wide.get("lo")),
        "wide_hi": _f(wide.get("hi")),
        "keystone_bid_qty": _f((stack.get("tight") or {}).get("total_qty")),
        "ask_ladder_notional": _f(ladder.get("total_notional")),
    }


async def _record_keystone_snapshot(
    settings: Settings,
    symbol: str,
    run_id: str,
    calc_result: dict[str, Any],
    *,
    postgres: PostgresRuntimeStore | None = None,
    redis: RedisRuntimeStore | None = None,
) -> dict[str, Any]:
    """WRITE the current cycle's keystone snapshot (Postgres first, then Redis).

    Cross-cycle companion to ``_record_wall_snapshot``: every cycle appends
    its keystone state to the durable ledger so the migration verdict can
    probe ALL recorded keystones, not just the most recent pull.

    Shared-store discipline mirrors ``_record_wall_snapshot``.
    """
    payload = _keystone_snapshot_payload(symbol, run_id, calc_result)
    own_pg = postgres is None
    own_redis = redis is None
    postgres = postgres or PostgresRuntimeStore(settings.database_url)
    redis = redis or RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        pg_ok = await postgres.record_keystone_snapshot(symbol, run_id, payload)
        redis_id = await redis.record_keystone_snapshot(symbol, run_id, payload)
        return {
            "postgres_written": pg_ok,
            "redis_stream_id": redis_id,
            "cycle_ts": payload["cycle_ts"],
            "keystone_price": payload.get("keystone_price"),
        }
    finally:
        if own_pg:
            await postgres.close()
        if own_redis:
            await redis.close()


def _accumulate_prior_walls(
    snapshots: list[dict[str, Any]],
) -> tuple[dict[float, float], str | None]:
    """Merge the FULL recorded wall history into one prior-walls map.

    ``snapshots`` are newest-first (the order both stores return). We
    iterate oldest-first so the newest known qty wins for a given level,
    but every level ever recorded is retained - so wall_migration probes
    ALL recorded wall levels, and a wall removed since an older pull is
    still surfaced (as ERODED) rather than silently dropped.
    """
    prior_walls: dict[float, float] = {}
    prior_cycle_ts: str | None = None
    for snap in reversed(snapshots or []):
        ts = snap.get("cycle_ts")
        if ts and prior_cycle_ts is None:
            prior_cycle_ts = ts
        asks = snap.get("asks")
        if not isinstance(asks, list):
            continue
        for row in asks:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                prior_walls[float(row[0])] = float(row[1])
            except (TypeError, ValueError):
                continue
    return prior_walls, prior_cycle_ts


def _oi_series(oi_hist: list[dict[str, Any]] | None) -> list[float]:
    """Extract the per-bar OI contract count from a raw OI history list."""
    out: list[float] = []
    for row in oi_hist or []:
        if not isinstance(row, dict):
            continue
        v = row.get("sumOpenInterest") or row.get("sum_open_interest") \
            or row.get("openInterest") or row.get("open_interest")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _oi_value_series(oi_hist: list[dict[str, Any]] | None) -> list[float]:
    """Extract the per-bar OI USD-notional from a raw OI history list."""
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


def _ls_last_pct(series: list[dict[str, Any]] | None) -> float | None:
    """Extract the latest long-account proportion from a topL/S or globalL/S series."""
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


# ---------------------------------------------------------------------------
# Derivative evidence — command-triggered fetch, merged into evidence
# ---------------------------------------------------------------------------

# Cross-asset universe used for macro climate + cross-asset funding rows.
# Matches analysis/macro.py:DEFAULT_SYMBOLS so the two paths agree on the
# reference set (BTC/ETH/SOL/BNB/XRP/DOGE/AVAX/LINK).
MACRO_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT",
)

DERIV_TTL_S_DEFAULT = 300
DERIV_FRESH_MS_DEFAULT = 300_000  # 5 min — back-to-back cycles within this skip the fetch


def _unwrap(x: Any) -> Any:
    """Treat BaseException as None so one failed Binance call doesn't kill the batch."""
    if isinstance(x, BaseException):
        log.warning("derivative fetch returned exception: %s", x)
        return None
    return x


async def fetch_derivative_evidence(
    client: Binance,
    symbol: str,
    *,
    include_cross_asset: bool = True,
    macro_symbols: tuple[str, ...] = MACRO_SYMBOLS,
) -> dict[str, Any]:
    """One-shot fetch of historical-derivative endpoints the poller doesn't carry.

    Returns a dict the harness merges into the canonical evidence before
    running calculations + analysis. Each field is independently None-safe
    so a single Binance 5xx / timeout / rate-limit on one endpoint does not
    drop the whole batch.

    Endpoints:
      own symbol: oi_history, taker_buy_sell, top_ls, global_ls, klines, funding
      cross asset (optional): 8 x spot_24h + 8 x fut_funding
    """
    own = await asyncio.gather(
        client.fut_open_interest_history(symbol, period="5m", limit=48),
        client.fut_taker_buy_sell(symbol, period="5m", limit=48),
        client.fut_top_long_short_accounts(symbol, period="5m", limit=12),
        client.fut_long_short_ratio(symbol, period="5m", limit=12),
        client.fut_klines(symbol, interval="5m", limit=48),
        client.fut_funding(symbol),
        return_exceptions=True,
    )
    oi_hist, tbr, top_ls, glb_ls, klines_5m, funding_self = (_unwrap(v) for v in own)

    cross: dict[str, Any] = {"tickers_24h": [], "funding": []}
    if include_cross_asset:
        cross_batches = await asyncio.gather(
            asyncio.gather(*(client.spot_24h(s) for s in macro_symbols),
                           return_exceptions=True),
            asyncio.gather(*(client.fut_funding(s) for s in macro_symbols),
                           return_exceptions=True),
            return_exceptions=True,
        )
        tickers_raw, funding_raw = (_unwrap(b) for b in cross_batches)
        cross["tickers_24h"] = [t for t in (tickers_raw or []) if isinstance(t, dict)]
        cross["funding"] = [
            {"symbol": s, "funding": f}
            for s, f in zip(macro_symbols, (funding_raw or []))
            if isinstance(f, dict)
        ]

    return {
        "observed_at_ms": int(time.time() * 1000),
        "schema_version": 1,
        "futures": {
            "oi_history": oi_hist,
            "taker_buy_sell": tbr,
            "top_ls": top_ls,
            "global_ls": glb_ls,
            "klines": klines_5m,
            "funding": funding_self,
        },
        "cross_asset": cross,
    }


def _is_deriv_fresh(deriv: dict[str, Any] | None, now_ms: int, fresh_ms: int) -> bool:
    if not deriv or not isinstance(deriv, dict):
        return False
    ts = deriv.get("observed_at_ms")
    if not isinstance(ts, (int, float)):
        return False
    age_ms = now_ms - int(ts)
    # Reject future timestamps (clock skew / corrupted cache) and over-age.
    if age_ms < 0:
        return False
    return age_ms <= fresh_ms


def _merge_derivatives(evidence: dict[str, Any], deriv: dict[str, Any] | None) -> dict[str, Any]:
    """Merge one derivative evidence dict into the canonical evidence shape.

    The merged fields are added to ``evidence.futures`` (so existing adapters
    like ``_adapt_oi``, ``_adapt_demand``, ``_adapt_regime`` pick them up
    without code changes) and to ``evidence.cross_asset`` (a new top-level
    key consumed by the demand adapter's macro_climate call).
    """
    if not deriv or not isinstance(deriv, dict):
        return evidence
    out = dict(evidence)
    out["futures"] = dict(evidence.get("futures") or {})
    deriv_fut = deriv.get("futures") or {}
    for key in ("oi_history", "taker_buy_sell", "top_ls", "global_ls", "klines"):
        if key in deriv_fut and deriv_fut[key] is not None:
            out["futures"][key] = deriv_fut[key]
    if "cross_asset" in deriv and deriv["cross_asset"]:
        out["cross_asset"] = deriv["cross_asset"]
    out["derivative_observed_at_ms"] = deriv.get("observed_at_ms")
    # Bar-horizon metadata: every derivative series is 5-minute bars, so a
    # series of N bars covers N x 5 minutes — regardless of the requested
    # 15m/1h/4h analysis window. Making the horizon explicit stops downstream
    # readers from misreading the series as window-aligned.
    deriv_fut = deriv.get("futures") or {}
    out["derivatives_meta"] = {
        "bar_period_s": 300,
        "series": {
            key: (len(deriv_fut[key]) if isinstance(deriv_fut.get(key), list) else None)
            for key in ("oi_history", "taker_buy_sell", "top_ls", "global_ls", "klines")
        },
    }
    return out


# ---------------------------------------------------------------------------
# Specialized group envelopes — the interpretation-plane read interface.
#
# The canonical run payload dict is the persisted AUDIT record; these
# GroupEnvelopes are what analysts actually READ. They are emitted
# directly from the Redis raw stream via run_group_cycle (the harness
# group commands: --wall/--flow/--structure/--positioning) — bounded by
# construction, with a fresh run_id for audit.
# ---------------------------------------------------------------------------

# Hard per-array cap at contract-emission level. The largest legitimate
# arrays (volume-profile buckets over a 4h window, ask-wall ladders) fit
# comfortably; anything larger is capped WITH an explicit __truncated__
# marker — never silently dropped, and never left to break the LLM param
# limit downstream.
_GROUP_ARRAY_CAP = 128


def _bound_arrays(value: Any, cap: int = _GROUP_ARRAY_CAP) -> Any:
    """Deterministically cap any list/tuple at ``cap`` items, explicitly marked."""
    if isinstance(value, dict):
        return {k: _bound_arrays(v, cap) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_bound_arrays(v, cap) for v in value[:cap]]
        if len(value) > cap:
            return {"__truncated__": True, "count": len(value), "items": items}
        return items
    return value


def _evidence_headlines(evidence: dict[str, Any]) -> dict[str, Any]:
    """Compact shared evidence context (snake_case scalars, null-preserving).

    Every group envelope carries these so any specialist has the baseline
    market state without raw arrays. Mirrors the fixed path discipline of
    runtime.contracts._envelope_summary — pure path-reads, no arithmetic.
    """
    fut = (evidence or {}).get("futures") or {}
    spot = (evidence or {}).get("spot") or {}

    def _p(d: Any, *keys: str) -> Any:
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    ticker = _p(fut, "ticker_24h") or {}
    spot_ticker = _p(spot, "ticker_24h") or {}
    funding = _p(fut, "funding") or {}
    oi = _p(fut, "open_interest") or {}
    return {
        "last_price": _p(fut, "ticker_24h", "last_price") or _p(fut, "ticker_24h", "lastPrice"),
        "spot_last_price": _p(spot, "ticker_24h", "last_price") or _p(spot, "ticker_24h", "lastPrice"),
        "funding_rate": _p(funding, "last_funding_rate") or _p(funding, "lastFundingRate"),
        "mark_price": _p(funding, "mark_price") or _p(funding, "markPrice"),
        "open_interest": _p(oi, "open_interest") or _p(oi, "openInterest"),
        "high_24h": _p(ticker, "high_price") or _p(ticker, "highPrice"),
        "low_24h": _p(ticker, "low_price") or _p(ticker, "lowPrice"),
        "quote_volume_24h": _p(ticker, "quote_volume") or _p(ticker, "quoteVolume"),
        "spot_quote_volume_24h": _p(spot_ticker, "quote_volume") or _p(spot_ticker, "quoteVolume"),
    }


async def run_group_cycle(
    settings: Settings,
    symbol: str,
    window_minutes: int,
    groups: tuple[str, ...],
    *,
    deriv_ttl_s: int = DERIV_TTL_S_DEFAULT,
    include_cross_asset: bool = False,
    include_derivatives: bool = True,
    force_refresh_derivatives: bool = False,
    depth: int | None = None,
) -> dict[str, GroupEnvelope]:
    """Read raw evidence DIRECTLY from the Redis store and emit typed GroupEnvelopes.

    This is the harness group-command plane: no MarketRunEnvelope, no
    persistence. Each requested group gets its own GroupEnvelope containing
    only its GROUP_MAP sections. Redis is the single data source (the 5s
    poller feeds it); the on-demand derivative fetch is the only Binance
    touch and is cache-first. Wall history is read from Postgres (Redis
    fallback) only when the wall group needs it.
    """
    unknown = [g for g in groups if g not in GROUP_MAP]
    if unknown:
        raise ValueError(f"unknown calculation group(s): {unknown!r}")

    depth = depth or settings.depth_levels
    window_s = window_minutes * 60
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        evidence = await read_raw_window(store, symbol, window_minutes)

        # Warm the derivative cache for groups that need it: oi (oi_history,
        # L/S), demand (taker + cross_asset), regime (L/S), stage (klines).
        requested_analysis: set[str] = set()
        for g in groups:
            requested_analysis.update(GROUP_MAP[g]["analysis"])
        needs_deriv = bool({"oi", "demand", "regime", "stage"} & requested_analysis)
        deriv: dict[str, Any] | None = None
        if needs_deriv and include_derivatives:
            cached = await store.read_derivative_evidence(symbol)
            if cached and _is_deriv_fresh(cached, int(time.time() * 1000), DERIV_FRESH_MS_DEFAULT):
                deriv = cached
            else:
                async with Binance() as client:
                    deriv = await fetch_derivative_evidence(
                        client, symbol, include_cross_asset=include_cross_asset,
                    )
                try:
                    await store.publish_derivative_evidence(symbol, deriv, ttl_s=deriv_ttl_s)
                except Exception:
                    log.exception("run_group_cycle %s: failed to publish derivative cache", symbol)
        evidence = _merge_derivatives(evidence, deriv)
    finally:
        await store.close()

    calc_sections, anal_sections = sections_for_groups(groups)
    anal_sections, calc_sections = resolve_analysis_sections(anal_sections, calc_sections)
    calc_sections = resolve_calc_sections(calc_sections)

    calc_result = run_calculations(evidence, depth, window_s, sections=calc_sections)

    # Wall history only when the wall group is requested.
    prior_walls: dict[float, float] = {}
    prior_cycle_ts: str | None = None
    if "wall_migration" in (anal_sections or set()):
        pg: PostgresRuntimeStore | None = (
            PostgresRuntimeStore(settings.database_url) if settings.database_url else None
        )
        history: list[dict[str, Any]] = []
        try:
            if pg is not None:
                try:
                    history = await pg.read_wall_history(symbol)
                except Exception:
                    log.warning(
                        "run_group_cycle %s: postgres wall-history read failed; falling back to redis",
                        symbol)
                    history = []
            if not history:
                history = await RedisRuntimeStore(
                    settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
                ).read_wall_history(symbol)
        except Exception:
            log.exception("run_group_cycle %s: failed to read wall history", symbol)
        finally:
            if pg is not None:
                await pg.close()
        prior_walls, prior_cycle_ts = _accumulate_prior_walls(history)

    analysis_result = run_analysis(
        evidence, calc_result,
        prior_walls=prior_walls or None,
        prior_cycle_ts=prior_cycle_ts,
        depth=depth,
        sections=anal_sections,
    )

    all_errors = list(calc_result.get("errors") or []) + list(analysis_result.get("errors") or [])
    status = "degraded" if all_errors else "healthy"
    run_id = str(uuid.uuid4())
    generated_at = _utc_iso()
    calc_by_section = calc_result.get("calculations") or {}
    anal_by_section = analysis_result.get("analysis") or {}

    out: dict[str, GroupEnvelope] = {}
    for kind in groups:
        spec = GROUP_MAP[kind]
        calc_payload = {
            k: _bound_arrays(calc_by_section.get(k))
            for k in spec["calculations"]
        }
        anal_payload = {}
        for k in spec["analysis"]:
            out_key = "open_interest" if k == "oi" else k
            anal_payload[out_key] = _bound_arrays(anal_by_section.get(out_key))

        group_errors = [
            e for e in all_errors
            if not isinstance(e, dict)
            or (e.get("function") or "").split(".")[0] in _kind_function_prefixes(kind)
        ]
        out[kind] = GroupEnvelope(
            kind=kind,
            symbol=symbol.upper(),
            status="degraded" if group_errors else status,
            generated_at=generated_at,
            run_id=run_id,
            window_minutes=window_minutes,
            coverage={
                "requested_window_seconds": window_s,
                "analysis_sections": sorted(anal_payload.keys()),
                "calculation_sections": sorted(calc_payload.keys()),
            },
            calculations=calc_payload,
            analysis=anal_payload,
            evidence_headlines=_evidence_headlines(evidence),
            errors=tuple(group_errors),
            source="group_cycle",
        )
    return out


def _kind_function_prefixes(kind: str) -> tuple[str, ...]:
    """strict_call function-name prefixes that belong to a group's adapters."""
    prefixes: dict[str, tuple[str, ...]] = {
        "wall": ("orderbook", "find_keystone", "top_density", "absorption",
                 "significant", "microprice_skew", "keystone", "ask_wall",
                 "hourly", "wall_delta", "fuel_ratio", "densest", "wall_trap",
                 "bid_tier", "mega_at", "level_absorption", "wall_break",
                 "zone", "oi.find_walls", "oi_weighted", "oi_inflow",
                 "oi_implied", "path_absorption", "simulated"),
        "flow": ("summarize", "bucketed_cvd", "cvd_series_corr", "ema_series",
                 "tiered_large", "seller_aggression", "spot_turnover",
                 "decompose_demand", "demand_verdict", "macro_climate",
                 "auction_verdict", "microprice", "initiated_flow",
                 "flow_persistence", "delta_variable", "deterministic_signals"),
        "structure": ("build_volume_profile", "volume_profile_summary",
                      "ema_series", "tiered_large", "seller_aggression",
                      "regime_verdict", "stage"),
        "positioning": ("oi.find_walls", "oi_weighted", "oi_inflow", "oi_implied"),
    }
    return prefixes.get(kind, ())




async def run_cycle(
    settings: Settings,
    symbol: str,
    window_minutes: int,
    depth: int | None = None,
    *,
    deriv_ttl_s: int = DERIV_TTL_S_DEFAULT,
    deriv_fresh_ms: int = DERIV_FRESH_MS_DEFAULT,
    include_cross_asset: bool = False,
    include_derivatives: bool = True,
    force_refresh_derivatives: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    """Run one complete pipeline cycle and return the canonical run payload dict.

    New keyword args (all backward compatible — defaults preserve old behavior):
      deriv_ttl_s           — TTL of derivative cache in Redis (default 300s)
      deriv_fresh_ms        — how old the cache can be before re-fetching (default 300_000ms)
      include_cross_asset   — when True, also fetch 16 cross-asset calls (off by default)
      include_derivatives   — master switch; False = behave exactly like the pre-change pipeline
      force_refresh_derivatives — bypass cache and always re-fetch
      persist               — when False, skip Postgres + Redis persistence entirely
                             (real dry-run; the run payload is computed but not written)

    This module is a stream-fed calculation object: ``read_raw_window`` reads
    the poller-written Redis stream (the single coherent Binance source) — it
    never opens a live Binance session for core data. On-demand derivative
    fetch (``fetch_derivative_evidence``) is the only Binance touch and is
    cache-first.
    """
    depth = depth or settings.depth_levels
    window_s = window_minutes * 60

    # One Redis + one Postgres pair for the WHOLE cycle. The previous flow
    # opened/closed Redis twice and Postgres twice (wall-history read,
    # wall/keystone ledger writes, persist_envelope) — every one of those
    # seams now shares these two store instances.
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    pg: PostgresRuntimeStore | None = (
        PostgresRuntimeStore(settings.database_url) if settings.database_url else None
    )
    try:
        evidence = await read_raw_window(redis, symbol, window_minutes)
        deriv: dict[str, Any] | None = None
        if include_derivatives:
            now_ms = int(time.time() * 1000)
            if not force_refresh_derivatives:
                cached = await redis.read_derivative_evidence(symbol)
                if _is_deriv_fresh(cached, now_ms, deriv_fresh_ms):
                    deriv = cached
                    log.debug("run_cycle %s: derivative cache hit (age=%dms)",
                              symbol, now_ms - int(cached.get("observed_at_ms") or 0))
            if deriv is None:
                log.info("run_cycle %s: fetching derivative evidence (cross_asset=%s)",
                         symbol, include_cross_asset)
                async with Binance() as client:
                    deriv = await fetch_derivative_evidence(
                        client, symbol, include_cross_asset=include_cross_asset,
                    )
                try:
                    await redis.publish_derivative_evidence(
                        symbol, deriv, ttl_s=deriv_ttl_s,
                    )
                except Exception:
                    log.exception("run_cycle %s: failed to publish derivative cache", symbol)
        evidence = _merge_derivatives(evidence, deriv)

        calc_result = run_calculations(evidence, depth, window_s)

        # Read the FULL recorded wall history for analysis (async, done here).
        # Postgres is the durable authority; Redis is the live projection fallback.
        prior_walls: dict[float, float] = {}
        prior_cycle_ts: str | None = None
        if pg is not None:
            try:
                history = await pg.read_wall_history(symbol)
            except Exception:
                log.warning("run_cycle %s: postgres wall-history read failed; falling back to redis", symbol)
                history = []
            if not history:
                history = await redis.read_wall_history(symbol)
            prior_walls, prior_cycle_ts = _accumulate_prior_walls(history)
        else:
            # No durable ledger configured — Redis-only fallback.
            try:
                prior_walls, prior_cycle_ts = _accumulate_prior_walls(
                    await redis.read_wall_history(symbol))
            except Exception:
                log.exception("run_cycle %s: failed to read wall history from redis", symbol)

        analysis_result = run_analysis(evidence, calc_result,
                                       prior_walls=prior_walls or None,
                                       prior_cycle_ts=prior_cycle_ts,
                                       depth=depth)

        envelope = assemble_envelope(symbol, evidence, calc_result, analysis_result)

        if persist:
            # WRITE the current cycle's wall snapshot so the ledger records it and
            # later cycles can call ALL recorded walls (not just the last pull).
            try:
                await _record_wall_snapshot(settings, symbol, envelope["run_id"], evidence,
                                            analysis_result, postgres=pg, redis=redis)
            except Exception:
                log.exception("run_cycle %s: failed to record wall snapshot", symbol)
            # WRITE the current cycle's keystone snapshot (cross-cycle keystone
            # migration ledger — clean separation from the wall ledger).
            try:
                await _record_keystone_snapshot(settings, symbol, envelope["run_id"],
                                                calc_result, postgres=pg, redis=redis)
            except Exception:
                log.exception("run_cycle %s: failed to record keystone snapshot", symbol)
            try:
                await persist_envelope(envelope, settings, postgres=pg, redis=redis)
            except Exception:
                log.exception("failed to persist run payload for %s run_id=%s",
                              symbol, envelope["run_id"])
        else:
            log.info("run_cycle %s: dry-run (persist=False) — run_id=%s not written",
                     symbol, envelope["run_id"])

        return envelope
    finally:
        await redis.close()
        if pg is not None:
            await pg.close()


__all__ = [
    "WINDOW_MINUTES_MAP",
    "read_raw_window",
    "run_calculations",
    "run_analysis",
    "assemble_envelope",
    "persist_envelope",
    "run_cycle",
    "run_group_cycle",
    "fetch_derivative_evidence",
    "_merge_derivatives",
    "_is_deriv_fresh",
    "MACRO_SYMBOLS",
    "DERIV_TTL_S_DEFAULT",
    "DERIV_FRESH_MS_DEFAULT",
]