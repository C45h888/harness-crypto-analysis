"""INTERPRETATION PLANE — canonical run pipeline + durable persistence.

This module owns the interpretation-plane surface that used to live in
``pipeline.py``: envelope assembly, Postgres-first persistence, the cross-
cycle wall/keystone ledger writes, the on-demand Binance derivative fetch,
and the two cycle runners (``run_cycle`` = canonical persisted envelope,
``run_group_cycle`` = typed GroupEnvelope command surface).

Semantic boundary (two-plane doctrine): the INFERENCE PLANE must not import
this module. The agent's read seam into the same deterministic math is
``pipeline_inference.run_inference_group`` — it consumes ``bedrock`` with the
engine's own injected store and never touches these persistence/Binance
surfaces.

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

from market_service.clients.binance import Binance
from market_service.config import Settings
from market_service.runtime.contracts import (
    MARKET_RUN_SCHEMA_VERSION,
    _json_safe,
)
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from . import bedrock
from .bedrock import (  # noqa: F401  (re-export: stable public pipeline API)
    DERIV_FRESH_MS_DEFAULT,
    DERIV_TTL_S_DEFAULT,
    GROUP_MAP,
    WINDOW_MINUTES_MAP,
    _accumulate_prior_walls,
    _adapt_oi,
    _adapt_wall_migration,
    _enrich_fut_keystone,
    _evidence_headlines,
    _is_deriv_fresh,
    _bound_arrays,
    _merge_derivatives,
    _resolve_scorecard_weights,
    _resolve_tier_config,
    _utc_iso,
    read_raw_window,
    resolve_analysis_sections,
    resolve_calc_sections,
    run_analysis,
    run_calculations,
    sections_for_groups,
)
from .contracts import GroupEnvelope

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
        evidence = await bedrock.read_raw_window(store, symbol, window_minutes)

        # Warm the derivative cache for groups that need it: oi (oi_history,
        # L/S), demand (taker + cross_asset), regime (L/S), stage (klines).
        requested_analysis: set[str] = set()
        for g in groups:
            requested_analysis.update(GROUP_MAP[g]["analysis"])
        needs_deriv = bool({"oi", "demand", "regime", "stage"} & requested_analysis)
        deriv: dict[str, Any] | None = None
        if needs_deriv and include_derivatives:
            cached = await store.read_derivative_evidence(symbol)
            if cached and bedrock._is_deriv_fresh(cached, int(time.time() * 1000), DERIV_FRESH_MS_DEFAULT):
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
        evidence = bedrock._merge_derivatives(evidence, deriv)
    finally:
        await store.close()

    calc_sections, anal_sections = bedrock.sections_for_groups(groups)
    anal_sections, calc_sections = bedrock.resolve_analysis_sections(anal_sections, calc_sections)
    calc_sections = bedrock.resolve_calc_sections(calc_sections)

    calc_result = bedrock.run_calculations(evidence, depth, window_s, sections=calc_sections)

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
        prior_walls, prior_cycle_ts = bedrock._accumulate_prior_walls(history)

    analysis_result = bedrock.run_analysis(
        evidence, calc_result,
        prior_walls=prior_walls or None,
        prior_cycle_ts=prior_cycle_ts,
        depth=depth,
        sections=anal_sections,
        tier_config=bedrock._resolve_tier_config(settings),
        scorecard_weights=bedrock._resolve_scorecard_weights(settings),
    )

    all_errors = list(calc_result.get("errors") or []) + list(analysis_result.get("errors") or [])
    status = "degraded" if all_errors else "healthy"
    run_id = str(uuid.uuid4())
    generated_at = _utc_iso()
    calc_by_section = calc_result.get("calculations") or {}
    anal_by_section = analysis_result.get("analysis") or {}

    out: dict[str, GroupEnvelope] = {}
    calc_prov = calc_result.get("substrate_provenance") or {}
    anal_prov = analysis_result.get("substrate_provenance") or {}
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

        # Substrate attribution: which decomposition substrate owns each section
        # in THIS group's envelope (only sections that actually ran appear).
        provenance: dict[str, Any] = {}
        for k in spec["calculations"]:
            if k in calc_prov:
                provenance[k] = calc_prov[k]
        for k in spec["analysis"]:
            if k in anal_prov:
                provenance[k] = anal_prov[k]

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
            substrate_provenance=provenance,
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
        evidence = await bedrock.read_raw_window(redis, symbol, window_minutes)
        deriv: dict[str, Any] | None = None
        if include_derivatives:
            now_ms = int(time.time() * 1000)
            if not force_refresh_derivatives:
                cached = await redis.read_derivative_evidence(symbol)
                if bedrock._is_deriv_fresh(cached, now_ms, deriv_fresh_ms):
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
        evidence = bedrock._merge_derivatives(evidence, deriv)

        calc_result = bedrock.run_calculations(evidence, depth, window_s)

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
            prior_walls, prior_cycle_ts = bedrock._accumulate_prior_walls(history)
        else:
            # No durable ledger configured — Redis-only fallback.
            try:
                prior_walls, prior_cycle_ts = bedrock._accumulate_prior_walls(
                    await redis.read_wall_history(symbol))
            except Exception:
                log.exception("run_cycle %s: failed to read wall history from redis", symbol)

        analysis_result = bedrock.run_analysis(evidence, calc_result,
                                       prior_walls=prior_walls or None,
                                       prior_cycle_ts=prior_cycle_ts,
                                       depth=depth,
                                       tier_config=bedrock._resolve_tier_config(settings),
                                       scorecard_weights=bedrock._resolve_scorecard_weights(settings))

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
    "GROUP_MAP",
    "read_raw_window",
    "run_calculations",
    "run_analysis",
    "assemble_envelope",
    "persist_envelope",
    "run_cycle",
    "run_group_cycle",
    "fetch_derivative_evidence",
    "MACRO_SYMBOLS",
    "DERIV_TTL_S_DEFAULT",
    "DERIV_FRESH_MS_DEFAULT",
]
