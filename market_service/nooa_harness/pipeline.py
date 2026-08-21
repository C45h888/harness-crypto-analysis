"""Harness-owned pipeline — reads raw evidence from Redis, runs math + analysis, collates.

Replaces the four-node pipeline (data-access → calculations → analysis → collator).
The 5-second poller continuously feeds the raw stream; the harness calls
``run_cycle()`` on its own schedule with a configurable time window.

The output is a ``MarketRunEnvelope`` persisted to the same Redis keys
the harness already reads from — the harness agents see zero difference.
"""

from __future__ import annotations

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
    find_keystone,
    significant_levels,
    top_density_windows,
)
from market_service.calculations.signals import deterministic_signals
from market_service.calculations.technical import ema_series
from market_service.calculations.volume_profile import build_volume_profile, volume_profile_summary
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
)
from market_service.analysis.path_absorption import (
    fuel_ratio as path_fuel_ratio,
    simulated_ascent,
    simulated_descent,
)
from market_service.analysis.regime import regime_verdict
from market_service.analysis.wall_migration import (
    densest_clusters,
    fuel_ratio as wall_fuel_ratio,
    wall_delta,
    wall_trap_assessment,
)
from market_service.config import Settings
from market_service.runtime.contracts import MarketRunEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from . import contracts as C

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window config
# ---------------------------------------------------------------------------

WINDOW_MINUTES_MAP: dict[str, int] = {
    "15m": 15,
    "1h": 60,
    "4h": 240,
}


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

    The poller writes a full snapshot every 5s. We read the latest snapshot
    for order book / funding / OI / tickers (point-in-time), and use the
    stream to accumulate trades across the window.
    """
    latest = await redis.read_raw_latest(symbol)
    if latest is None:
        return {
            "observed_at": _utc_iso(),
            "observed_at_ms": int(time.time() * 1000),
            "fetch_window_ms": window_minutes * 60_000,
            "depth_levels": 20,
            "errors": [{"endpoint": "all", "error": "no raw evidence in Redis"}],
            "spot": {"ticker_24h": None, "order_book": {}, "trades_raw": [], "trades_normalized": []},
            "futures": {"ticker_24h": None, "order_book": {}, "trades_raw": [], "trades_normalized": [],
                        "funding": {}, "open_interest": {}},
        }

    since_ms = int(time.time() * 1000) - window_minutes * 60_000
    snapshots = await redis.read_raw_window(symbol, since_ms)

    # Merge trades across all snapshots in the window.
    spot_trades: list[dict[str, Any]] = []
    fut_trades: list[dict[str, Any]] = []
    for snap in snapshots:
        spot_trades.extend(snap.get("spot", {}).get("trades_normalized", []))
        fut_trades.extend(snap.get("futures", {}).get("trades_normalized", []))

    return {
        "observed_at": _utc_iso(),
        "observed_at_ms": int(time.time() * 1000),
        "fetch_window_ms": window_minutes * 60_000,
        "depth_levels": latest.get("depth_levels", 20),
        "errors": latest.get("errors", []),
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


def run_calculations(
    evidence: dict[str, Any],
    depth: int,
    window: int,
) -> dict[str, Any]:
    """Run the same deterministic calculations as the old calculations node."""
    errors: list[dict[str, Any]] = []

    flow = _run_section("flow", lambda: {
        "spot_flow": _strict(summarize, _spot_trades(evidence), _spot_book(evidence),
                             depth_levels=depth, name="summarize"),
        "futures_flow": _strict(summarize, _fut_trades(evidence), _fut_book(evidence),
                               depth_levels=depth, name="summarize"),
    }, errors) or {}

    bucketed = _run_section("bucketed_cvd", lambda: {
        "spot_bucketed_cvd": _strict(bucketed_cvd, _spot_trades(evidence), window_s=window, name="bucketed_cvd"),
        "futures_bucketed_cvd": _strict(bucketed_cvd, _fut_trades(evidence), window_s=window, name="bucketed_cvd"),
    }, errors) or {}

    correlation = _run_section("correlation", lambda: (
        _strict(cvd_series_corr,
                _strict(bucketed_cvd, _spot_trades(evidence), window_s=window, name="bucketed_cvd"),
                _strict(bucketed_cvd, _fut_trades(evidence), window_s=window, name="bucketed_cvd"),
                window_s=window, name="cvd_series_corr")
    ), errors)

    # Orderbook
    fut_book = _fut_book(evidence)
    spot_book = _spot_book(evidence)
    fut_bids = C.require_list_of_pairs(fut_book.get("bids"), function="orderbook.*", where="futures.order_book.bids", max_items=depth)
    fut_asks = C.require_list_of_pairs(fut_book.get("asks"), function="orderbook.*", where="futures.order_book.asks", max_items=depth)
    spot_bids = C.require_list_of_pairs(spot_book.get("bids"), function="orderbook.*", where="spot.order_book.bids", max_items=depth)
    spot_asks = C.require_list_of_pairs(spot_book.get("asks"), function="orderbook.*", where="spot.order_book.asks", max_items=depth)
    last_price = _safe_last_price(fut_bids, fut_asks)

    orderbook: dict[str, Any] = {}
    if last_price is not None:
        orderbook = _run_section("orderbook", lambda: {
            "fut_keystone": _strict(find_keystone, fut_bids, last_price, 0.20, -0.30, -0.05, None, name="find_keystone"),
            "spot_keystone": _strict(find_keystone, spot_bids, last_price, 0.20, -0.30, -0.05, None, name="find_keystone"),
            "fut_top_density_bids": _strict(top_density_windows, fut_book, 0.5, "bids", 5, name="top_density_windows"),
            "fut_absorption_ladder": _strict(absorption_ladder, fut_bids, last_price, count=10, name="absorption_ladder"),
            "fut_significant_levels": _strict(significant_levels, fut_bids + fut_asks, 0.0, name="significant_levels"),
            "fut_microprice_skew_bps": _strict(microprice_skew_bps, spot_bids, spot_asks, name="microprice_skew_bps"),
        }, errors) or {}

    # Volume profile
    volume_profile = _run_section("volume_profile", lambda: {
        "buckets": _strict(build_volume_profile, _fut_trades(evidence), 0.05, name="build_volume_profile"),
        "summary": _strict(volume_profile_summary,
                          _strict(build_volume_profile, _fut_trades(evidence), 0.05, name="build_volume_profile"),
                          name="volume_profile_summary"),
    }, errors) or {}

    # Technical
    technical = _run_section("technical", lambda: {
        "emas": _strict(ema_series,
                       [float(row[4]) for row in ((evidence.get("futures") or {}).get("klines") or [])
                        if isinstance(row, (list, tuple)) and len(row) >= 5],
                       name="ema_series"),
    }, errors) or {}

    # Turnover
    spot_flow = flow.get("spot_flow") or {}
    fut_flow = flow.get("futures_flow") or {}
    spot_notional = (spot_flow.get("buy_notional_usd") or 0.0) + (spot_flow.get("sell_notional_usd") or 0.0)
    fut_notional = (fut_flow.get("buy_notional_usd") or 0.0) + (fut_flow.get("sell_notional_usd") or 0.0)
    turnover = _run_section("turnover", lambda: {
        "spot_turnover_share": _strict(spot_turnover_share, spot_notional, fut_notional, name="spot_turnover_share"),
    }, errors) or {}

    # Signals
    signal_inputs = {
        "spot_buy_share": spot_flow.get("buy_share"),
        "futures_buy_share": fut_flow.get("buy_share"),
        "spot_obi_top_n": spot_flow.get("obi"),
        "open_interest": ((evidence.get("futures") or {}).get("open_interest") or {}).get("open_interest"),
    }
    signals: list[dict[str, Any]] = []
    try:
        signals = list(_strict(deterministic_signals, signal_inputs, None, name="deterministic_signals"))
    except C.ContractViolation as violation:
        errors.append(C.contract_error_entry(violation))

    return {
        "status": "degraded" if errors else "healthy",
        "errors": errors,
        "calculations": {
            "flow": flow,
            "bucketed_cvd": bucketed,
            "correlation": correlation,
            "signal_inputs": signal_inputs,
            "signals": signals,
            "orderbook": orderbook,
            "turnover": turnover,
            "volume_profile": volume_profile,
            "technical": technical,
        },
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
) -> dict[str, Any]:
    """Run the same deterministic analysis as the old analysis node.

    ``depth`` is the centralized canonical order-book depth (defaults to the
    config resolver) and is threaded into every adapter so wall/OI/path
    analyses scan the full configured book instead of a hard-coded slice.
    """
    if depth is None:
        from market_service.config import default_depth_levels
        depth = default_depth_levels()
    errors: list[dict[str, Any]] = []

    calc_data = calculations.get("calculations") or {}
    orderbook_calc = calc_data.get("orderbook") or {}

    # Auction
    auction = _run_section("auction", lambda: _adapt_auction(evidence), errors) or {}

    # Open interest
    oi = _run_section("oi", lambda: _adapt_oi(evidence, depth=depth), errors) or {}

    # Wall migration
    pw = prior_walls or {}
    wall_migration = _run_section("wall_migration", lambda: _adapt_wall_migration(
        evidence, orderbook_calc.get("fut_significant_levels"), pw, prior_cycle_ts, depth=depth,
    ), errors) or {}

    # Path absorption
    path_absorption = _run_section("path_absorption", lambda: _adapt_path_absorption(evidence, depth=depth), errors) or {}

    # Demand
    demand = _run_section("demand", lambda: _adapt_demand(evidence), errors) or {}

    # Regime
    regime = _run_section("regime", lambda: _adapt_regime(evidence, calc_data), errors) or {}

    # Stage
    stage = _run_section("stage", lambda: _adapt_stage(evidence), errors) or {}

    return {
        "status": "degraded" if errors else "healthy",
        "errors": errors,
        "analysis": {
            "auction": auction,
            "open_interest": oi,
            "wall_migration": wall_migration,
            "path_absorption": path_absorption,
            "demand": demand,
            "regime": regime,
            "stage": stage,
        },
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


def _adapt_oi(evidence: dict[str, Any], *, depth: int) -> dict[str, Any]:
    fut = _fut_evidence(evidence)
    fut_book = fut.get("order_book") or {}
    asks = C.require_list_of_pairs(fut_book.get("asks"), function="oi.find_walls", where="futures.order_book.asks", max_items=depth)
    bids = C.require_list_of_pairs(fut_book.get("bids"), function="oi.find_walls", where="futures.order_book.bids", max_items=depth)
    last_price = _last_price_e(bids, asks) or 0.0
    oi_raw = fut.get("open_interest") or {}
    oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
    oi_float = float(oi_value) if isinstance(oi_value, (int, float)) else 0.0
    walls = _strict(find_walls, asks, last_price, 0.005, 0.05, name="find_walls")
    weighted = _strict(oi_weighted_contracts, oi_float, None, None, name="oi_weighted_contracts")
    inflow = _strict(oi_inflow_outflow, [], name="oi_inflow_outflow")
    implied = _strict(oi_implied_value, [], name="oi_implied_value")
    return {"walls": walls, "weighted_contracts": weighted, "inflow_outflow": inflow,
            "implied_value": implied, "raw_open_interest": oi_float}


def _adapt_wall_migration(
    evidence: dict[str, Any],
    calc_significant_levels: list[dict] | None,
    prior_walls: dict[float, float],
    prior_cycle_ts: str | None,
    *, depth: int,
) -> dict[str, Any]:
    fut_book = _fut_book_e(evidence)
    bids, asks = _levels(fut_book, depth, function="wall_migration.*")
    price = _last_price_e(bids, asks) or 0.0
    bid_floor = price * 0.97 if price else 0.0
    ask_target = price * 1.03 if price else 0.0
    delta = _strict(wall_delta, prior_walls or {}, asks, 0.02, 1.15, name="wall_delta")
    fuel = _strict(wall_fuel_ratio, bids, asks, price, bid_floor, ask_target, name="fuel_ratio")
    floors = _bid_floors_from_significant_levels(bids, calc_significant_levels, price)
    clusters = _strict(densest_clusters, bids, floors, 0.10, name="densest_clusters")
    built, eroded = _single_cycle_wall_counts(asks)
    trap = _strict(wall_trap_assessment, float(fuel.get("ratio") or 0.0), built, eroded, name="wall_trap_assessment")
    return {"wall_delta": delta, "fuel_ratio": fuel, "densest_clusters": clusters,
            "trap_assessment": trap, "prior_cycle_ts": prior_cycle_ts,
            "prior_wall_count": len(prior_walls or {}),
            "inputs_used": {"floors_count": len(floors), "ask_walls_built": built,
                            "ask_walls_eroded": eroded, "fuel_ratio_value": float(fuel.get("ratio") or 0.0)}}


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
    climate = _strict(macro_climate, {"btc": {"change_pct": None}, "eth": {"change_pct": None}}, None, name="macro_climate")
    return {"decomposition": dx, "verdict": verdict, "reasons": reasons, "macro_climate": climate}


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
    klines = _fut_evidence(evidence).get("klines") or []
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
    return {
        "stage": _strict(infer_stage,
                         px_chg_4h=px_chg_4h, oi_chg_4h=0.0, up_pct_4h=50.0,
                         up_steps=up_steps, down_steps=down_steps,
                         funding_bps=None, top_long_pct=None, global_long_pct=None,
                         distribution_clusters=0.0, absorption_clusters=0.0,
                         name="infer_stage"),
        "kline_count": len(klines),
    }


# ---------------------------------------------------------------------------
# Step 4 — collate into MarketRunEnvelope
# ---------------------------------------------------------------------------

def assemble_envelope(
    symbol: str,
    evidence: dict[str, Any],
    calculations: dict[str, Any],
    analysis: dict[str, Any],
    run_id: str | None = None,
) -> MarketRunEnvelope:
    """Assemble a MarketRunEnvelope from the three domain outputs."""
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

    return MarketRunEnvelope(
        run_id=run_id,
        symbol=symbol.upper(),
        generated_at=_utc_iso(),
        completed_at=_utc_iso(),
        status=status,
        data_source="domain_pipeline",
        coverage=coverage,
        canonical_state=canonical_state,
        domain_outputs=canonical_state,
        errors=tuple(all_errors),
        source_metadata={
            "runtime": "market_service",
            "path": "harness_pipeline",
            "latency_ms": round((completed_ms - started_ms), 1),
        },
    )


# ---------------------------------------------------------------------------
# Persistence — Postgres first, then Redis
# ---------------------------------------------------------------------------

async def persist_envelope(
    envelope: MarketRunEnvelope,
    settings: Settings,
) -> dict[str, Any]:
    """Persist to Postgres first, then publish to Redis."""
    postgres = PostgresRuntimeStore(settings.database_url)
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        await postgres.connect()
        inserted = await postgres.insert_run(envelope)
        redis_stream_id = await redis.publish_run(envelope)
        return {
            "run_id": envelope.run_id,
            "postgres_inserted": inserted,
            "redis_stream_id": redis_stream_id,
            "redis_key": redis.collated_latest_key(envelope.symbol),
            "schema_version": envelope.schema_version,
            "status": envelope.status,
        }
    finally:
        await postgres.close()
        await redis.close()


# ---------------------------------------------------------------------------
# Full cycle — read → calc → analyze → collate → persist
# ---------------------------------------------------------------------------

async def run_cycle(
    settings: Settings,
    symbol: str,
    window_minutes: int,
    depth: int | None = None,
) -> MarketRunEnvelope:
    """Run one complete pipeline cycle and return the persisted envelope."""
    depth = depth or settings.depth_levels
    window_s = window_minutes * 60

    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        evidence = await read_raw_window(redis, symbol, window_minutes)
    finally:
        await redis.close()

    calc_result = run_calculations(evidence, depth, window_s)

    # Read prior wall snapshot for analysis (async, done here)
    prior_walls: dict[float, float] = {}
    prior_cycle_ts: str | None = None
    redis2 = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        prior_snapshot = await redis2.read_last_wall_snapshot(symbol)
        if prior_snapshot and isinstance(prior_snapshot.get("asks"), list):
            prior_cycle_ts = prior_snapshot.get("cycle_ts")
            for row in prior_snapshot["asks"]:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                try:
                    prior_walls[float(row[0])] = float(row[1])
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    finally:
        await redis2.close()

    analysis_result = run_analysis(evidence, calc_result,
                                   prior_walls=prior_walls or None,
                                   prior_cycle_ts=prior_cycle_ts,
                                   depth=depth)

    envelope = assemble_envelope(symbol, evidence, calc_result, analysis_result)

    try:
        await persist_envelope(envelope, settings)
    except Exception:
        log.exception("failed to persist envelope for %s run_id=%s", symbol, envelope.run_id)

    return envelope


__all__ = [
    "WINDOW_MINUTES_MAP",
    "read_raw_window",
    "run_calculations",
    "run_analysis",
    "assemble_envelope",
    "persist_envelope",
    "run_cycle",
]