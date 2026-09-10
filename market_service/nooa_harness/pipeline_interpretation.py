"""INTERPRETATION PLANE — envelope assembly + durable fetch surfaces.

This module owns the interpretation-plane surface that used to live in
``pipeline.py``: envelope assembly, the on-demand Binance derivative fetch,
and the cross-cycle wall/keystone payload builders. Computation cycles
(``run_cycle`` / ``run_group_cycle``) were removed with the tool-first
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
    MARKET_RUN_SCHEMA_VERSION,
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
    "assemble_envelope",
    "fetch_derivative_evidence",
    "read_raw_window",
    "run_analysis",
    "run_calculations",
]
