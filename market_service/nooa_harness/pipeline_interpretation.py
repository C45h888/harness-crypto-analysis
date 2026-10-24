"""INTERPRETATION PLANE — envelope assembly + durable fetch surfaces.

This module owns the interpretation-plane surface that used to live in
``pipeline.py``: envelope assembly and the on-demand Binance derivative
fetch. Computation cycles
(the former cycle wrappers) were removed with the tool-first
migration: agents and operators invoke substrate workers as tools
(``substrate_worker.tools``) instead of running cycles.

Semantic boundary (two-plane doctrine): the INFERENCE PLANE must not import
this module. The agent's tool base consumes ``calculations.composition``
and ``substrate_worker.tools`` with the engine's own injected store and
never touches these persistence/Binance surfaces.

Backward-compat: ``market_service.nooa_harness.pipeline`` remains as a thin
re-export shim of this module for consumers outside the runtime wiring
(``nooa market`` read commands, scripts, tests).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from market_service.calculations.composition import (  # noqa: F401  (re-export: stable public pipeline API)
    GROUP_MAP,
    WINDOW_MINUTES_MAP,
    _accumulate_prior_walls,
    _adapt_oi,
    _adapt_wall_migration,
    _enrich_fut_keystone,
    _resolve_scorecard_weights,
    _resolve_tier_config,
    _utc_iso,
    resolve_analysis_sections,
    resolve_calc_sections,
    run_analysis,
    run_calculations,
    sections_for_groups,
)
from market_service.clients.binance import Binance
from market_service.runtime.bounds import (  # noqa: F401
    _bound_arrays,
    _evidence_headlines,
)
from market_service.runtime.contracts import (
    _json_safe,
)
from market_service.runtime.derivatives import (  # noqa: F401
    DERIV_FRESH_MS_DEFAULT,
    DERIV_TTL_S_DEFAULT,
    _is_deriv_fresh,
    _merge_derivatives,
)
from market_service.runtime.raw_window import (
    build_raw_window as read_raw_window,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Derivative evidence — interpretation-plane only (the ONE Binance touch).
# ---------------------------------------------------------------------------

# Cross-asset universe used for macro climate + cross-asset funding rows.
# Matches analysis/macro.py:DEFAULT_SYMBOLS so the two paths agree on the
# reference set (BTC/ETH/SOL/BNB/XRP/DOGE/AVAX/LINK).
MACRO_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT",
)


def _unwrap(x: Any) -> Any:
    """Treat BaseException as None so one failed Binance call doesn't kill the batch."""
    if isinstance(x, BaseException):
        # %r (repr) — bare %s renders as an empty string for exceptions
        # with no message (e.g. CancelledError, some client errors),
        # making /futures/data batch failures undiagnosable.
        log.warning("derivative fetch returned exception: %r", x)
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
        # Phase 2.2: funding history for trend-aware scorecard. The funding
        # endpoint above is the CURRENT snapshot; this is the historical
        # fundingRate series (Binance /fapi/v1/fundingRate, ~3 events/day).
        # 30 events covers ~10 days of 8h settlements — enough for a
        # robust trend + z-score without bloating the cycle.
        client.fut_funding_history(symbol, limit=30),
        return_exceptions=True,
    )
    oi_hist, tbr, top_ls, glb_ls, klines_5m, funding_self, funding_hist = (
        _unwrap(v) for v in own
    )

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
            "funding_history": funding_hist,
        },
        "cross_asset": cross,
    }




__all__ = [
    "DERIV_FRESH_MS_DEFAULT",
    "DERIV_TTL_S_DEFAULT",
    "GROUP_MAP",
    "MACRO_SYMBOLS",
    "WINDOW_MINUTES_MAP",
    "fetch_derivative_evidence",
    "read_raw_window",
    "run_analysis",
    "run_calculations",
]
