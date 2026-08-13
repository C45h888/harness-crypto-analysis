"""``calculations`` domain node.

Subscribes to ``<prefix>:stream:commands`` filtered on ``domain=calculations``.
Reads the latest ``<prefix>:latest:<SYM>:data-access`` envelope, runs pure
deterministic math from ``market_service.calculations.*``, and publishes a
``MarketStateEnvelope`` with ``source="calculations"`` to:

  * ``<prefix>:latest:<SYM>:calculations``
  * ``<prefix>:stream:domain:calculations:<SYM>``

Per the containerization contract this node performs NO network I/O and does
not reinterpret missing values - ``null`` means unavailable, never zero.

Adapter discipline
------------------
Every canonical function is invoked through ``strict_call`` and only after
``require_shape`` has validated the input shape. When a required input is
missing or malformed the adapter raises ``ContractViolation``; the handler
aggregates those violations into structured ``errors`` and produces a
``degraded`` or ``invalid`` envelope rather than swallowing them.

Run::

    python -m market_service.nodes.calculations
"""

from __future__ import annotations

import asyncio
import logging
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
from market_service.config import Settings
from market_service.runtime.contracts import RefreshCommand
from market_service.runtime.redis_store import RedisRuntimeStore

from . import contracts as C
from ._base import install_signal_handlers, run_command_loop, setup_logging

log = logging.getLogger(__name__)

DOMAIN = "calculations"


# ---------------------------------------------------------------------------
# Shape adapters. Each one builds the EXACT input a canonical function
# documents and raises ContractViolation when the shape cannot be built.
# ---------------------------------------------------------------------------

def _spot_trades(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return (evidence.get("spot") or {}).get("trades_normalized") or []


def _fut_trades(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return (evidence.get("futures") or {}).get("trades_normalized") or []


def _spot_book(evidence: dict[str, Any]) -> dict[str, Any]:
    return (evidence.get("spot") or {}).get("order_book") or {}


def _fut_book(evidence: dict[str, Any]) -> dict[str, Any]:
    return (evidence.get("futures") or {}).get("order_book") or {}


def adapt_flow_section(evidence: dict[str, Any], depth: int) -> dict[str, Any]:
    """Adapt evidence into the inputs of ``flow.summarize``.

    Returns the dict directly produced by ``summarize`` for spot and futures.
    """
    spot = strict(C, "summarize", summarize, _spot_trades(evidence), _spot_book(evidence), depth_levels=depth)
    fut = strict(C, "summarize", summarize, _fut_trades(evidence), _fut_book(evidence), depth_levels=depth)
    return {"spot_flow": spot, "futures_flow": fut}


def adapt_bucketed_section(evidence: dict[str, Any], window: int) -> dict[str, Any]:
    spot = strict(C, "bucketed_cvd", bucketed_cvd, _spot_trades(evidence), window_s=window)
    fut = strict(C, "bucketed_cvd", bucketed_cvd, _fut_trades(evidence), window_s=window)
    return {"spot_bucketed_cvd": spot, "futures_bucketed_cvd": fut}


def adapt_correlation(evidence: dict[str, Any], window: int) -> dict[str, Any]:
    spot = strict(C, "bucketed_cvd", bucketed_cvd, _spot_trades(evidence), window_s=window)
    fut = strict(C, "bucketed_cvd", bucketed_cvd, _fut_trades(evidence), window_s=window)
    return strict(C, "cvd_series_corr", cvd_series_corr, spot, fut, window_s=window)


def adapt_signal_inputs(flow: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Build the exact shape ``calculations.signals.deterministic_signals`` accepts.

    That function reads ``spot_buy_share``, ``futures_buy_share``,
    ``spot_obi_top_n``, ``open_interest`` from the snapshot dict and treats
    absent values as zero for thresholds - but ``null`` is preserved here as
    the field value.
    """
    spot_flow = flow.get("spot_flow") or {}
    fut_flow = flow.get("futures_flow") or {}
    oi_raw = (evidence.get("futures") or {}).get("open_interest") or {}
    oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
    return {
        "spot_buy_share": spot_flow.get("buy_share"),
        "futures_buy_share": fut_flow.get("buy_share"),
        "spot_obi_top_n": spot_flow.get("obi"),
        "open_interest": oi_value,
    }


def adapt_orderbook_section(evidence: dict[str, Any], depth: int) -> dict[str, Any]:
    """Build the EXACT input shape each orderbook function requires.

    ``find_keystone`` needs (bids, price, width, lo_offset, hi_offset, fallback).
    ``top_density_windows`` needs (book, width, side, top_n).
    ``absorption_ladder`` needs (bids, price, count).
    ``significant_levels`` needs (levels, min_qty).
    """
    fut_book = _fut_book(evidence)
    spot_book = _spot_book(evidence)
    fut_bids = C.require_list_of_pairs(fut_book.get("bids"), function="orderbook.*", where="evidence.futures.order_book.bids", max_items=depth)
    fut_asks = C.require_list_of_pairs(fut_book.get("asks"), function="orderbook.*", where="evidence.futures.order_book.asks", max_items=depth)
    spot_bids = C.require_list_of_pairs(spot_book.get("bids"), function="orderbook.*", where="evidence.spot.order_book.bids", max_items=depth)
    spot_asks = C.require_list_of_pairs(spot_book.get("asks"), function="orderbook.*", where="evidence.spot.order_book.asks", max_items=depth)

    raw_mark = (evidence.get("futures") or {}).get("mark_price")
    if isinstance(raw_mark, dict):
        raw_mark = raw_mark.get("mark_price") or raw_mark.get("markPrice")
    last_price = raw_mark or _safe_last_price(fut_bids, fut_asks)

    if last_price is None:
        return {
            "fut_keystone": None, "spot_keystone": None,
            "fut_top_density_bids": [], "fut_absorption_ladder": [],
            "fut_significant_levels": [], "fut_microprice_skew_bps": None,
        }

    fut_keystone = strict(C, "find_keystone", find_keystone, fut_bids, last_price, 0.20, -0.30, -0.05, None)
    spot_keystone = strict(C, "find_keystone", find_keystone, spot_bids, last_price, 0.20, -0.30, -0.05, None)
    fut_top_density = strict(C, "top_density_windows", top_density_windows, fut_book, 0.5, "bids", 5)
    fut_absorption = strict(C, "absorption_ladder", absorption_ladder, fut_bids, last_price, count=10)
    fut_levels = strict(C, "significant_levels", significant_levels, fut_bids + fut_asks, 0.0)

    return {
        "fut_keystone": fut_keystone,
        "spot_keystone": spot_keystone,
        "fut_top_density_bids": fut_top_density,
        "fut_absorption_ladder": fut_absorption,
        "fut_significant_levels": fut_levels,
        "fut_microprice_skew_bps": strict(C, "microprice_skew_bps", microprice_skew_bps, spot_bids, spot_asks),
    }


def _safe_last_price(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


def adapt_volume_profile(evidence: dict[str, Any]) -> dict[str, Any]:
    fut_trades = _fut_trades(evidence)
    buckets = strict(C, "build_volume_profile", build_volume_profile, fut_trades, 0.05)
    summary = strict(C, "volume_profile_summary", volume_profile_summary, buckets)
    return {"buckets": buckets, "summary": summary}


def adapt_technical(evidence: dict[str, Any]) -> dict[str, Any]:
    """Build the EXACT shape ``calculations.technical.ema_series`` accepts: a list of floats."""
    klines = (evidence.get("futures") or {}).get("klines") or []
    closes: list[float] = []
    for row in klines:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            try:
                closes.append(float(row[4]))
                continue
            except (TypeError, ValueError):
                pass
        if isinstance(row, dict):
            for k in ("close", "c", "Close"):
                if k in row:
                    try:
                        closes.append(float(row[k]))
                        break
                    except (TypeError, ValueError):
                        pass
    if not closes:
        return {}
    return {"emas": strict(C, "ema_series", ema_series, closes)}


def adapt_turnover(flow: dict[str, Any]) -> dict[str, Any]:
    """``spot_turnover_share`` expects (spot_notional_usd, futures_notional_usd)."""
    spot_flow = flow.get("spot_flow") or {}
    fut_flow = flow.get("futures_flow") or {}
    spot_notional = (spot_flow.get("buy_notional_usd") or 0.0) + (spot_flow.get("sell_notional_usd") or 0.0)
    fut_notional = (fut_flow.get("buy_notional_usd") or 0.0) + (fut_flow.get("sell_notional_usd") or 0.0)
    return {"spot_turnover_share": strict(C, "spot_turnover_share", spot_turnover_share, spot_notional, fut_notional)}


# ---------------------------------------------------------------------------
# Section runner - aggregates ContractViolations into structured errors
# instead of silent log warnings.
# ---------------------------------------------------------------------------

def _run_section(name: str, builder, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return builder()
    except C.ContractViolation as violation:
        errors.append(C.contract_error_entry(violation))
        log.warning("[calculations] %s contract violation: %s", name, violation.reason)
        return None


def strict(C_mod, name: str, fn, *args, **kwargs):
    """Wrap a canonical call with strict contract validation.

    This is a thin alias around ``C_mod.strict_call`` that uses the module
    already imported under the name ``C`` at the top of this file. It exists
    so adapters read like ``strict(C, "find_keystone", find_keystone, ...)``.
    """
    return C_mod.strict_call(name, fn, *args, **kwargs)


async def make_handler(settings: Settings, redis: RedisRuntimeStore):
    async def handle(command: RefreshCommand) -> dict[str, Any]:
        symbol = command.symbol.upper()
        params = command.parameters or {}
        depth = int(params.get("depth_levels", settings.depth_levels))
        window = int(params.get("flow_window_seconds", settings.flow_window_seconds))

        run_id = command.command_id or params.get("run_id")
        latest = await redis.read_run_domain_state(run_id, "data-access") if run_id else None
        if latest is None:
            return {
                "status": "invalid",
                "errors": [{"function": "calculations", "error": "matching data-access envelope unavailable", "details": {"run_id": run_id}}],
                "evidence": {},
                "calculations": {},
            }

        evidence = latest.data.get("evidence") or latest.data
        requested_scope = str(
            latest.data.get("requested_scope")
            or evidence.get("requested_scope")
            or "all"
        )
        errors: list[dict[str, Any]] = []

        spot_evidence = evidence.get("spot") or {}
        fut_evidence = evidence.get("futures") or {}
        if not spot_evidence.get("order_book") or not fut_evidence.get("order_book"):
            errors.append({
                "function": "calculations.input_quality",
                "error": "required spot/futures order-book evidence unavailable",
                "details": {
                    "spot_order_book": bool(spot_evidence.get("order_book")),
                    "futures_order_book": bool(fut_evidence.get("order_book")),
                },
            })

        spot_trades_available = spot_evidence.get("trades_raw") is not None
        fut_trades_available = fut_evidence.get("trades_raw") is not None
        trades_available = requested_scope in ("all", "trades") and spot_trades_available and fut_trades_available
        books_available = requested_scope in ("all", "order_book")

        if trades_available:
            flow = _run_section("flow", lambda: adapt_flow_section(evidence, depth), errors) or {}
            bucketed = _run_section("bucketed_cvd", lambda: adapt_bucketed_section(evidence, window), errors) or {}
            correlation = _run_section("correlation", lambda: adapt_correlation(evidence, window), errors)
            signal_inputs = _run_section("signal_inputs", lambda: adapt_signal_inputs(flow, evidence), errors) or {}
            volume_profile = _run_section("volume_profile", lambda: adapt_volume_profile(evidence), errors) or {}
            turnover = _run_section("turnover", lambda: adapt_turnover(flow), errors) or {}
        else:
            unavailable = {
                "status": "unavailable",
                "reason": f"trade evidence not available for requested scope '{requested_scope}'",
            }
            flow = {"status": "unavailable", "spot_flow": None, "futures_flow": None, "reason": unavailable["reason"]}
            bucketed = {"status": "unavailable", "spot_bucketed_cvd": None, "futures_bucketed_cvd": None, "reason": unavailable["reason"]}
            correlation = unavailable
            signal_inputs = {"status": "unavailable", "reason": unavailable["reason"]}
            volume_profile = unavailable
            turnover = unavailable

        if books_available:
            orderbook = _run_section("orderbook", lambda: adapt_orderbook_section(evidence, depth), errors) or {}
        else:
            orderbook = {
                "status": "unavailable",
                "reason": f"order-book evidence not available for requested scope '{requested_scope}'",
            }
        technical = _run_section("technical", lambda: adapt_technical(evidence), errors) or {}

        # signals need the shape built by adapt_signal_inputs; run only when it succeeded.
        signals: list[dict[str, Any]] = []
        if trades_available and signal_inputs:
            try:
                signals = list(strict(C, "deterministic_signals", deterministic_signals, signal_inputs, None))
            except C.ContractViolation as violation:
                errors.append(C.contract_error_entry(violation))

        # Status: healthy if no errors AND upstream data-access was healthy.
        if latest.status == "invalid":
            status = "invalid"
        elif errors:
            status = "degraded"
        elif requested_scope != "all":
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            "errors": errors,
            "data_access_observed_at": latest.observed_at,
            "requested_scope": requested_scope,
            "evidence_ref": {"source": "data-access", "observed_at": latest.observed_at},
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

    return handle


async def main() -> int:
    setup_logging()
    settings = Settings.from_env()
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    try:
        if not await redis.ping_with_retry():
            log.error("redis ping failed; aborting calculations")
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
