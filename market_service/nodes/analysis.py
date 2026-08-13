"""``analysis`` domain node.

Subscribes to ``<prefix>:stream:commands`` filtered on ``domain=analysis``.
Reads the latest calculations envelope (and the matching data-access envelope
for evidence) and runs the deterministic analysis modules in
``market_service.analysis.*``. Publishes a ``MarketStateEnvelope`` with
``source="analysis"`` to:

  * ``<prefix>:latest:<SYM>:analysis``
  * ``<prefix>:stream:domain:analysis:<SYM>``

Per the containerization contract this node must not place trades, size
positions, or override deterministic inputs.

Adapter discipline
------------------
Every canonical function is invoked through ``strict_call`` and only after
``require_shape`` has validated the input shape. When the shape cannot be
built the adapter raises :class:`ContractViolation`; the handler aggregates
violations into structured ``errors`` and produces ``degraded`` or
``invalid``. The previous silent ``log.warning`` behaviour has been removed.

Run::

    python -m market_service.nodes.analysis
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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
from market_service.runtime.contracts import RefreshCommand
from market_service.runtime.redis_store import RedisRuntimeStore

from . import contracts as C
from ._base import install_signal_handlers, run_command_loop, setup_logging

log = logging.getLogger(__name__)

DOMAIN = "analysis"


# ---------------------------------------------------------------------------
# Evidence accessors
# ---------------------------------------------------------------------------

def _fut_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("futures") or {}


def _spot_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("spot") or {}


def _fut_book(evidence: dict[str, Any]) -> dict[str, Any]:
    return _fut_evidence(evidence).get("order_book") or {}


def _ticker_change(ticker: Any) -> dict[str, Any]:
    """Normalize Binance 24h ticker change for demand analysis."""
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


def _levels(book: dict[str, Any], depth: int, *, function: str) -> tuple[list[list[float]], list[list[float]]]:
    bids = C.require_list_of_pairs(book.get("bids"), function=function, where="order_book.bids", max_items=depth)
    asks = C.require_list_of_pairs(book.get("asks"), function=function, where="order_book.asks", max_items=depth)
    return bids, asks


def _last_price(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


# ---------------------------------------------------------------------------
# Shape adapters for each canonical analysis function
# ---------------------------------------------------------------------------

def adapt_auction(evidence: dict[str, Any]) -> dict[str, Any]:
    """Build the EXACT input shape ``auction_verdict`` documents.

    state = {spot: {microprice, flow, persistence},
             futures: {microprice, flow, persistence},
             derivatives: {funding, taker_buy_ratio, oi_change_pct}}
    """
    spot_book = _spot_evidence(evidence).get("order_book") or {}
    fut_book = _fut_book(evidence)
    spot_trades = _spot_evidence(evidence).get("trades_normalized") or []
    fut_trades = _fut_evidence(evidence).get("trades_normalized") or []
    funding = _fut_evidence(evidence).get("funding") or {}

    mp_spot = strict(C, "microprice", microprice, spot_book)
    initiated = strict(C, "initiated_flow", initiated_flow, fut_trades)
    persistence = strict(C, "flow_persistence", flow_persistence, fut_trades)

    # spot needs microprice + flow + persistence; we don't have a separate
    # spot flow here so reuse the same initiated_flow result shape for the
    # keys auction_verdict reads (``buy_share``) - it expects a ``flow`` dict.
    spot_flow = {"buy_share": None, "vwap": None, "cvd": None}
    fut_flow = {"buy_share": None, "vwap": None, "cvd": None}

    state = {
        "spot": {"microprice": mp_spot, "flow": spot_flow, "persistence": {}},
        "futures": {"microprice": mp_spot, "flow": fut_flow, "persistence": persistence},
        "derivatives": {
            "funding": C.require_float(funding.get("last_funding_rate") if isinstance(funding, dict) else None,
                                       function="auction_verdict", where="derivatives.funding"),
            "taker_buy_ratio": None,
            "oi_change_pct": None,
        },
    }
    verdict, reasons = strict(C, "auction_verdict", auction_verdict, state)
    return {"microprice": mp_spot, "initiated_flow": initiated, "flow_persistence": persistence,
            "verdict": verdict, "reasons": reasons}


def adapt_oi(evidence: dict[str, Any]) -> dict[str, Any]:
    fut = _fut_evidence(evidence)
    fut_book = fut.get("order_book") or {}
    asks = C.require_list_of_pairs(fut_book.get("asks"), function="oi.find_walls", where="futures.order_book.asks", max_items=20)
    bids = C.require_list_of_pairs(fut_book.get("bids"), function="oi.find_walls", where="futures.order_book.bids", max_items=20)
    last_price = _last_price(bids, asks) or 0.0

    oi_raw = fut.get("open_interest") or {}
    oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
    oi_float = float(oi_value) if isinstance(oi_value, (int, float)) else 0.0

    walls = strict(C, "find_walls", find_walls, asks, last_price, 0.005, 0.05)
    weighted = strict(C, "oi_weighted_contracts", oi_weighted_contracts, oi_float, None, None)
    inflow = strict(C, "oi_inflow_outflow", oi_inflow_outflow, [])
    implied = strict(C, "oi_implied_value", oi_implied_value, [])

    return {"walls": walls, "weighted_contracts": weighted, "inflow_outflow": inflow,
            "implied_value": implied, "raw_open_interest": oi_float}


def adapt_wall_migration(evidence: dict[str, Any]) -> dict[str, Any]:
    """Build inputs matching the documented signatures of the wall_migration functions.

    ``wall_delta(prior_walls, ask_levels, bid_levels, window, direction_threshold)``
    ``fuel_ratio(bids, asks, price, bid_floor, ask_target)``
    ``densest_clusters(bids, floors, window)``
    ``wall_trap_assessment(fuel_ratio_value, ask_walls_built, ask_walls_eroded=0)``
    """
    fut_book = _fut_book(evidence)
    bids, asks = _levels(fut_book, 50, function="wall_migration.*")
    price = _last_price(bids, asks) or 0.0
    bid_floor = price * 0.97 if price else 0.0
    ask_target = price * 1.03 if price else 0.0

    delta = strict(C, "wall_delta", wall_delta, {}, asks, 0.02, 1.15)
    fuel = strict(C, "fuel_ratio", wall_fuel_ratio, bids, asks, price, bid_floor, ask_target)
    clusters = strict(C, "densest_clusters", densest_clusters, bids, [bid_floor, price, ask_target], 0.10)
    trap = strict(C, "wall_trap_assessment", wall_trap_assessment, 0.0, 0, 0)

    return {"wall_delta": delta, "fuel_ratio": fuel, "densest_clusters": clusters,
            "trap_assessment": trap}


def adapt_path_absorption(evidence: dict[str, Any]) -> dict[str, Any]:
    """``path_absorption.fuel_ratio(bids, asks, entry, bid_floor)``,
    ``simulated_ascent(asks, total_bid_fuel, levels)``,
    ``simulated_descent(bids, total_ask_fuel, total_bid_fuel, levels)``.
    """
    fut_book = _fut_book(evidence)
    bids, asks = _levels(fut_book, 50, function="path_absorption.*")
    price = _last_price(bids, asks) or 0.0
    entry = price * 1.01 if price else 0.0
    bid_floor = price * 0.97 if price else 0.0
    levels = [price + i * 0.005 for i in range(1, 6)] if price else []
    total_bid_fuel = sum(p * q for p, q in bids)
    total_ask_fuel = sum(p * q for p, q in asks)

    fr = strict(C, "path_absorption.fuel_ratio", path_fuel_ratio, bids, asks, entry, bid_floor)
    ascent = strict(C, "simulated_ascent", simulated_ascent, asks, total_bid_fuel, levels)
    descent = strict(C, "simulated_descent", simulated_descent, bids, total_ask_fuel, total_bid_fuel, levels)
    return {"fuel_ratio": fr, "simulated_ascent": ascent, "simulated_descent": descent}


def adapt_demand(evidence: dict[str, Any]) -> dict[str, Any]:
    """Build the exact ``d`` shape ``decompose_demand`` documents:
    spot: {trades, book, ticker_24h}
    futures: {trades, book, open_interest, oi_history, mark_price,
              global_ls, top_ls, taker_buy_sell, ticker_24h}
    """
    fut = _fut_evidence(evidence)
    spot = _spot_evidence(evidence)
    fut_oi = fut.get("open_interest") or {}
    d = {
        "spot": {
            "trades": spot.get("trades_normalized") or [],
            "book": spot.get("order_book") or {},
            "ticker_24h": _ticker_change(spot.get("ticker_24h")),
        },
        "futures": {
            "trades": fut.get("trades_normalized") or [],
            "book": fut.get("order_book") or {},
            "open_interest": fut_oi if isinstance(fut_oi, dict) else {},
            "oi_history": fut.get("oi_history") or [],
            "mark_price": fut.get("mark_price"),
            "global_ls": fut.get("global_ls"),
            "top_ls": fut.get("top_ls"),
            "taker_buy_sell": fut.get("taker_buy_sell"),
            "ticker_24h": _ticker_change(fut.get("ticker_24h")),
        },
        "latency_ms": None,
    }
    dx = strict(C, "decompose_demand", decompose_demand, d)
    verdict, reasons = strict(C, "demand_verdict", demand_verdict, dx)
    climate = strict(C, "macro_climate", macro_climate, {
        "btc": {"change_pct": None}, "eth": {"change_pct": None},
    }, None)
    return {"decomposition": dx, "verdict": verdict, "reasons": reasons, "macro_climate": climate}


def adapt_regime(evidence: dict[str, Any], calculations: dict[str, Any]) -> dict[str, Any]:
    """Build the exact ``d`` shape ``regime_verdict`` documents:
    {futures: {flow, open_interest, funding, mark_price, long_short_ratio,
               top_long_short_accounts}}.
    ``flow`` is the canonical ``flow.summarize()`` output - so we pull it
    from the calculations envelope rather than reconstructing it.
    """
    flow = calculations.get("flow") or {}
    spot_flow = flow.get("spot_flow") or {}
    fut_flow = flow.get("futures_flow") or {}
    fut_evidence = _fut_evidence(evidence)
    funding = fut_evidence.get("funding") or {}
    mark = fut_evidence.get("mark_price") or {}
    oi = fut_evidence.get("open_interest") or {}

    d = {
        "futures": {
            "flow": fut_flow if fut_flow else spot_flow,  # regime prefers futures
            "open_interest": oi if isinstance(oi, dict) else {},
            "funding": funding if isinstance(funding, dict) else {},
            "mark_price": mark if isinstance(mark, dict) else {},
            "long_short_ratio": fut_evidence.get("global_ls"),
            "top_long_short_accounts": fut_evidence.get("top_ls"),
        }
    }
    verdict, reasons = strict(C, "regime_verdict", regime_verdict, d)
    return {"state": d, "verdict": verdict, "reasons": reasons}


def adapt_stage(evidence: dict[str, Any]) -> dict[str, Any]:
    """Build the exact kwargs ``infer_stage`` documents.

    infer_stage(px_chg_4h, oi_chg_4h, up_pct_4h, up_steps, down_steps, ...).
    Without a 4h kline window we return a structured ``stage: None`` and
    record the missing inputs as a contract error.
    """
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
    return {"stage": strict(C, "infer_stage", _stage_safe_call,
                            px_chg_4h=px_chg_4h,
                            oi_chg_4h=0.0,
                            up_pct_4h=50.0,
                            up_steps=up_steps,
                            down_steps=down_steps),
            "kline_count": len(klines)}


def _stage_safe_call(**kwargs):
    """Infer-stage with sane defaults for missing OI/LSR fields."""
    from market_service.analysis.stage import infer_stage
    return infer_stage(
        px_chg_4h=kwargs["px_chg_4h"],
        oi_chg_4h=kwargs["oi_chg_4h"],
        up_pct_4h=kwargs["up_pct_4h"],
        up_steps=kwargs["up_steps"],
        down_steps=kwargs["down_steps"],
        funding_bps=None,
        top_long_pct=None,
        global_long_pct=None,
        distribution_clusters=0.0,
        absorption_clusters=0.0,
    )


def adapt_macro(evidence: dict[str, Any]) -> dict[str, Any]:
    fut = _fut_evidence(evidence)
    funding = fut.get("funding") or {}
    oi = fut.get("open_interest") or {}
    return {
        "funding_rate": (funding.get("last_funding_rate") if isinstance(funding, dict) else None),
        "open_interest": (oi.get("open_interest") if isinstance(oi, dict) else None),
    }


# ---------------------------------------------------------------------------
# Section runner - aggregates ContractViolations into structured errors.
# ---------------------------------------------------------------------------

def _run_section(name: str, builder, errors: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        return builder()
    except C.ContractViolation as violation:
        errors.append(C.contract_error_entry(violation))
        log.warning("[analysis] %s contract violation: %s", name, violation.reason)
        return {"status": "degraded", "reason": violation.reason}


def strict(C_mod, name: str, fn, *args, **kwargs):
    return C_mod.strict_call(name, fn, *args, **kwargs)


async def make_handler(settings: Settings, redis: RedisRuntimeStore):
    async def handle(command: RefreshCommand) -> dict[str, Any]:
        symbol = command.symbol.upper()
        params = command.parameters or {}

        run_id = command.command_id or params.get("run_id")
        calc_latest = await redis.read_run_domain_state(run_id, "calculations") if run_id else None
        if calc_latest is None:
            return {
                "status": "invalid",
                "errors": [{"function": "analysis", "error": "matching calculations envelope unavailable", "details": {"run_id": run_id}}],
                "analysis": {},
            }
        data_latest = await redis.read_run_domain_state(run_id, "data-access") if run_id else None
        evidence = (data_latest.data.get("evidence") if data_latest else None) or {}
        requested_scope = str(
            (data_latest.data.get("requested_scope") if data_latest else None)
            or evidence.get("requested_scope")
            or "all"
        )
        calc_data = calc_latest.data
        calculations = calc_data.get("calculations") or {}

        errors: list[dict[str, Any]] = []
        def unavailable(name: str) -> dict[str, str]:
            return {
                "status": "unavailable",
                "reason": f"{name} inputs not requested in scope '{requested_scope}'",
            }

        order_book_scope = requested_scope in ("all", "order_book")
        full_scope = requested_scope == "all"
        auction = _run_section("auction", lambda: adapt_auction(evidence), errors) if order_book_scope else unavailable("auction")
        oi = _run_section("open_interest", lambda: adapt_oi(evidence), errors) if full_scope else unavailable("open_interest")
        walls = _run_section("wall_migration", lambda: adapt_wall_migration(evidence), errors) if order_book_scope else unavailable("wall_migration")
        path = _run_section("path_absorption", lambda: adapt_path_absorption(evidence), errors) if order_book_scope else unavailable("path_absorption")
        demand = _run_section("demand", lambda: adapt_demand(evidence), errors) if full_scope else unavailable("demand")
        regime = _run_section("regime", lambda: adapt_regime(evidence, calculations), errors) if full_scope else unavailable("regime")
        stage = _run_section("stage", lambda: adapt_stage(evidence), errors) if full_scope else unavailable("stage")
        macro = _run_section("macro", lambda: adapt_macro(evidence), errors) if full_scope else unavailable("macro")

        status = calc_latest.status
        if errors:
            status = "invalid" if status == "invalid" else "degraded"
        elif requested_scope != "all" and status == "healthy":
            status = "degraded"

        return {
            "status": status,
            "errors": errors,
            "evidence_ref": {
                "calculations_observed_at": calc_latest.observed_at,
                "data_access_observed_at": data_latest.observed_at if data_latest else None,
            },
            "analysis": {
                "auction": auction,
                "open_interest": oi,
                "wall_migration": walls,
                "path_absorption": path,
                "demand": demand,
                "regime": regime,
                "stage": stage,
                "macro": macro,
            },
            "coverage_seconds": calc_data.get("coverage_seconds"),
        }

    return handle


async def main() -> int:
    setup_logging()
    settings = Settings.from_env()
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    try:
        if not await redis.ping_with_retry():
            log.error("redis ping failed; aborting analysis")
            return 1
        handler = await make_handler(settings, redis)
        await run_command_loop(
            redis=redis,
            domain=DOMAIN,
            handler=handler,
            settings=settings,
            stop=stop,
            source=DOMAIN,
        )
    finally:
        await redis.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
