"""
Outer harness CLI — Calculation pipeline surface + router to the mounted NOOA CLI.

This module is the **outer** CLI surface and is the mount point the
terminal-based coding agents (pi, hermes, claude code) shell out to. It owns
exactly three responsibilities:

1. Run the calculation pipeline on the poller-fed Redis stream into a
   canonical ``MarketRunEnvelope v2`` (default / ``--analyze``).
2. Refresh the on-demand derivative cache in Redis
   (``--refresh-derivatives``) and warm the cache on ``--analyze``.
3. Route analyst / briefing / memory / agent operations to the mounted NOOA
   CLI via ``--nooa``. The NOOA CLI (``market_service/commands/nooa_cli.py``
   + ``nooa_cli_ext.py``) is the **inner** CLI and is the legitimate direct
   caller of the subordinate ``market_service/nooa_harness/`` runtime module.

``--live`` exposes the legacy one-shot Binance live waveform (``build()``)
as a dev-only diagnostic; it is NOT the canonical surface.

The runtime authority split is:

  harness.py                   outer CLI       calculation pipeline
                                                + derivative cache warm
                       + router to mounted NOOA CLI
       │
       ├─► default / --analyze ─► pipeline.run_cycle (reads Redis stream, deterministic)
       │
       ├─► --refresh-derivatives ─► fetch_derivative_evidence + Redis cache
       │
       ├─► --live ─► build() (legacy live waveform — dev only)
       │
       └─► --nooa ─► nooa_cli.py       inner CLI (mounted into the framework
                                        ``oo`` group at import time)
                        │
                        └─► nooa_cli_ext.py  the ``market`` click group
                                              (envelope, briefing, memory,
                                               analyst) — sole direct caller
                                              of nooa_harness.*

The envelope reads (``--latest`` / ``--run-id``) are Redis-only reads and do
NOT require Postgres ``DATABASE_URL``; the derivative cache warm
(``--refresh-derivatives``) is likewise Redis-only.

Usage:
     # canonical calculated envelope (default runs the pipeline once)
     .venv/bin/python -m market_service.commands.harness SOLUSDT --json
     .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --window 1h --json
     .venv/bin/python -m market_service.commands.harness SOLUSDT --refresh-derivatives --json
     .venv/bin/python -m market_service.commands.harness SOLUSDT --latest --json
     .venv/bin/python -m market_service.commands.harness --run-id <UUID> --json
     .venv/bin/python -m market_service.commands.harness SOLUSDT --live --json  # legacy dev

      # any analyst / briefing / memory / agent operation — routed via --nooa
     .venv/bin/python -m market_service.commands.harness --nooa market analyst SOLUSDT --cycles 1 --with-memory

Follows the repo null discipline: ``null`` means a source did not provide a
value — it is not a substitute for zero.

The runtime authority split is:

  harness.py              outer CLI       clean market-data contract
                                            + calculation pipeline
                                            + Redis derivative cache
                                            + router to NOOA inner CLI
       │
       ├─► --analyze ─► pipeline.run_cycle (no agents, deterministic only)
       │
       ├─► --refresh-derivatives ─► fetch_derivative_evidence + Redis cache
       │
       └─► --nooa ─► nooa_cli.py         inner CLI (mounted into the framework
                                          ``oo`` group at import time)
                          │
                          └─► nooa_cli_ext.py  the ``market`` click group
                                              (envelope, briefing, memory,
                                               analyst) — sole direct caller
                                              of nooa_harness.*

The envelope reads (``--latest`` / ``--run-id``) go directly to the
``MarketRunEnvelope`` contract in ``runtime.contracts`` via
``RedisRuntimeStore``. They are data reads, not agent reads — the
nooa_harness boundary is never crossed on those paths.

The NOOA inner CLI is **calculation-agnostic**: its ``analyst`` command
calls ``run_analyze_once`` which in turn calls ``pipeline.run_cycle`` and
reuses whatever derivatives are already in Redis (pre-populated by
``harness.py --refresh-derivatives`` or implicitly by ``--analyze``). NOOA
itself will be reworked in a future pass to be driven by mathematical /
statistical derivations; the calculation pipeline remains the single
source of truth for canonical numbers.

Usage:
    # clean data contract (no agents)
    .venv/bin/python -m market_service.commands.harness SOLUSDT --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --latest --json
    .venv/bin/python -m market_service.commands.harness --run-id <UUID> --json

    # calculation pipeline — populates the canonical ledger
    .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --window 15m --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --envelope-summary --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --refresh-derivatives --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --refresh-derivatives --with-cross-asset --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --no-derivatives --json

    # any analyst / briefing / memory / agent operation — routed via --nooa
    .venv/bin/python -m market_service.commands.harness --nooa market analyst SOLUSDT --cycles 1 --with-memory
    .venv/bin/python -m market_service.commands.harness --nooa market envelope SOLUSDT --latest
    .venv/bin/python -m market_service.commands.harness --nooa market briefing --session-id <UUID> --run-id <UUID>
    .venv/bin/python -m market_service.commands.harness --nooa market memory recall --session-id <UUID>

Follows the repo null discipline: `null` means a source did not provide a
value — it is not a substitute for zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any

from market_service.analysis.market import analyze, render
from market_service.config import Settings
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


async def build(symbol: str, trades: int, depth: int | None = None, bucket_window_s: int = 60) -> dict:
    if depth is None:
        from market_service.config import default_depth_levels
        depth = default_depth_levels()
    started = int(time.time() * 1000)
    core = await analyze(symbol, trade_limit=trades, depth_limit=depth, bucket_window_s=bucket_window_s)
    return {
        "contract": {
            "name": "crypto-ai-market-harness",
            "version": 2,
            "null_semantics": "null means the source did not provide a value; it is not zero",
            "clean_sources": ["market_service.clients", "market_service.calculations", "market_service.analysis"],
        },
        "symbol": symbol,
        "requested": {"trade_limit": trades, "depth_limit": depth, "bucket_window_s": bucket_window_s},
        "generated_at_ms": started,
        "core": core,
        "status": core.get("status", "degraded"),
        "errors": list(core.get("errors") or []),
        "latency_ms": round((time.time() * 1000) - started, 1),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the outer harness parser.

    The parser exposes three CLI surfaces:

    1. **Canonical calculation pipeline** (default / ``--analyze``) — runs
       the deterministic ``pipeline.run_cycle`` end-to-end, populating
       the canonical Redis ledger and returning a ``MarketRunEnvelope v2``.
       This is THE coherent single market-data source the agents read.

    2. **Envelope reads** (``--latest`` / ``--run-id``) — direct
       ``runtime.contracts`` reads of the persisted collated envelope. These
       are Redis-only reads and require no ``DATABASE_URL``.

    3. **NOOA routing** (``--nooa ...``) — passthrough to the mounted NOOA
       CLI. The NOOA CLI is calculation-agnostic; it reuses whatever
       derivatives are already cached in Redis and never re-fetches by
       itself.

    ``--live`` exposes the legacy one-shot live waveform (``build()``) as a
    dev-only diagnostic: it opens its OWN Binance session and returns the
    legacy ``crypto-ai-market-snapshot`` shape, NOT the methanol
    ``MarketRunEnvelope v2``. Kept for debugging; do not teach agents to
    rely on it.

    ``--refresh-derivatives`` / ``--with-cross-asset`` / ``--no-derivatives``
    / ``--force-refresh-derivatives`` control the derivative cache and are
    intentionally kept on the OUTER CLI. The inner NOOA CLI is being
    repurposed and removed its derivative surface.

    Note: ``--briefing`` / ``--with-memory`` / ``--session-id`` live on the
    NOOA inner CLI and are reached via ``--nooa``. They were never on
    harness.py.
    """
    p = argparse.ArgumentParser(
        description=(
            "Outer harness CLI: the canonical calculation pipeline (MarketRunEnvelope "
            "v2), the derivative-cache warmer, legacy --live waveform (dev-only), "
            "and a router to the mounted NOOA CLI. Default (no flags) runs the "
            "canonical calculation cycle from the Redis stream."
        ),
    )
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--trades", type=int, default=500,
                   help="legacy --live waveform: recent trades per venue")
    p.add_argument("--depth", type=int, default=None,
                   help="order book depth (default: centralized DEPTH_LEVELS)")
    p.add_argument("--bucket-window", type=int, default=60,
                   help="legacy --live waveform: bucket window in seconds")
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")
    p.add_argument("--live", action="store_true",
                   help="legacy dev-only live waveform (opens its own Binance session; "
                        "returns crypto-ai-market-snapshot, NOT the methanol "
                        "MarketRunEnvelope v2). Not the canonical surface.")

    # --- Calculation pipeline surface ---
    g = p.add_mutually_exclusive_group()
    g.add_argument("--analyze", action="store_true",
                   help="run the calculation pipeline end-to-end and persist "
                        "the canonical MarketRunEnvelope. Reuses cached "
                        "derivatives; refreshes on miss or staleness.")
    g.add_argument("--refresh-derivatives", action="store_true",
                   help="fetch + cache one derivative evidence snapshot in "
                        "Redis without running the pipeline. Useful for "
                        "priming the cache for downstream --analyze cycles.")
    p.add_argument("--window", choices=("15m", "1h", "4h"), default="15m",
                   help="raw evidence lookback window for --analyze (default 15m)")
    p.add_argument("--deriv-ttl", type=int, default=300,
                   help="derivative cache TTL in seconds (default 300)")
    p.add_argument("--with-cross-asset", action="store_true",
                   help="include the 16 cross-asset calls (8 tickers + 8 funding) "
                        "in the derivative fetch. Off by default to save rate-limit.")
    p.add_argument("--no-derivatives", action="store_true",
                   help="run --analyze with raw evidence only (legacy raw-only path).")
    p.add_argument("--force-refresh-derivatives", action="store_true",
                   help="--analyze: bypass the derivative cache and re-fetch from Binance")
    p.add_argument("--envelope-summary", action="store_true",
                   help="--analyze: emit only the compact envelope_summary projection "
                        "instead of the full envelope")
    p.add_argument("--no-persist", action="store_true",
                   help="--analyze: skip Postgres + Redis persistence (dry run)")

    # --- Calculation-model group commands (Pass 3 — segregated surface) ---
    # These run ONLY the calculation + analysis sections each analytical
    # domain needs. No envelope, no persistence, focused output. They are
    # combinable (e.g. --wall --flow) and mutually exclusive with the
    # monolithic --analyze and the envelope reads.
    p.add_argument("--wall", action="store_true",
                   help="wall & keystone analysis: orderbook calc + wall_migration / "
                        "path_absorption / oi analysis. Focused, no envelope.")
    p.add_argument("--flow", action="store_true",
                   help="trade flow & aggression: flow / cvd / correlation / technical "
                        "calc + demand / auction / delta analysis. Focused, no envelope.")
    p.add_argument("--structure", action="store_true",
                   help="market structure: volume_profile / technical calc + "
                        "regime / stage analysis. Focused, no envelope.")
    p.add_argument("--positioning", action="store_true",
                   help="positioning & derivatives: oi analysis only (weighted "
                        "contracts, inflow/outflow, implied value). Focused, no envelope.")

    # --- Envelope reads (canonical ledger) ---
    p.add_argument("--latest", action="store_true",
                   help="read the latest persisted collated MarketRunEnvelope "
                        "(direct runtime.contracts read, NOT a nooa_harness op)")
    p.add_argument("--run-id", help="read one exact persisted collated envelope by run ID")
    p.add_argument("--keystone-history", action="store_true",
                   help="read the cross-cycle keystone ledger (Redis first, "
                        "Postgres fallback) and derive the keystone migration "
                        "verdict. Returns the recorded keystone series + "
                        "UP/DOWN/FLAT per cycle + the aggregate verdict.")
    p.add_argument("--microstructure-status", action="store_true",
                   help="read the isolated Binance spot microstructure capture status from Redis; "
                        "does not start capture, run calculations, or invoke NOOA")
    p.add_argument("--history-limit", type=int, default=100,
                   help="max keystone history entries to read for "
                        "--keystone-history (default 100)")

    # --- NOOA routing ---
    p.add_argument("--nooa", nargs=argparse.REMAINDER, metavar="ARGS",
                   help="delegate to the mounted NOOA CLI through harness.py "
                        "(e.g. --nooa market envelope SOLUSDT --latest; "
                        "use this for any analyst / briefing / memory / "
                        "agent operation; a leading -- is allowed but "
                        "optional)")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    # --- Route 0: passthrough to the mounted NOOA CLI (inner CLI).
    # This is the ONLY path that crosses the nooa_harness boundary from
    # harness.py. The inner NOOA CLI (nooa_cli.py + nooa_cli_ext.py) is
    # the legitimate direct caller of the subordinate nooa_harness runtime.
    if args.nooa is not None:
        from market_service.commands.nooa_cli import main as nooa_main
        passthrough = list(args.nooa)
        if passthrough and passthrough[0] == "--":
            passthrough = passthrough[1:]
        return nooa_main(passthrough)

    # --- Route 1: refresh derivative evidence (write-only to Redis cache).
    if args.refresh_derivatives:
        result = asyncio.run(_refresh_derivatives(args))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(json.dumps({"status": "ok" if result.get("stream_id") else "failed",
                              "symbol": result.get("symbol"),
                              "cache_ttl_seconds": result.get("cache_ttl_seconds"),
                              "with_cross_asset": result.get("with_cross_asset")},
                             indent=2, default=str))
        return 0 if result.get("stream_id") else 1

    # --- Route 1.5: calculation-model group commands (Pass 3 segregated surface).
    # Runs ONLY the calc + analysis sections for the requested domain groups.
    # No envelope, no persistence. Redis-only (no DATABASE_URL) except --wall
    # which reads wall history (Postgres fallback).
    requested_groups = tuple(
        g for g, flag in (("wall", args.wall), ("flow", args.flow),
                          ("structure", args.structure), ("positioning", args.positioning))
        if flag
    )
    if requested_groups:
        result = asyncio.run(_run_groups(args, requested_groups))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") in ("healthy", "degraded") else 1

    # --- Route 2: run the calculation pipeline end-to-end (canonical).
    if args.analyze:
        result = asyncio.run(_run_analyze(args))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            status = result.get("status", "unknown")
            run_id = result.get("run_id")
            persisted = result.get("persistence", {})
            summary = {
                "status": status,
                "symbol": result.get("symbol"),
                "run_id": run_id,
                "window_minutes": result.get("window_minutes"),
                "derivatives_cache": result.get("derivatives_cache"),
                "persistence": persisted,
                "elapsed_ms": result.get("elapsed_ms"),
            }
            print(json.dumps(summary, indent=2, default=str))
        return 0

    # --- Route 3: read a persisted collated MarketRunEnvelope directly.
    # Redis-only read — no DATABASE_URL required. For Postgres fallback on
    # envelope reads, use --nooa market envelope --run-id <UUID>.
    if args.latest or args.run_id:
        settings = Settings.from_redis_env()
        async def _read():
            store = RedisRuntimeStore(
                settings.redis_url,
                settings.redis_key_prefix,
                settings.redis_stream_maxlen,
            )
            try:
                value = await (
                    store.read_run(args.run_id) if args.run_id
                    else store.read_latest_run(args.symbol)
                )
                return value.to_dict() if value else None
            finally:
                await store.close()
        result = asyncio.run(_read())
        print(json.dumps(result, indent=2, default=str))
        return 0

    # --- Route 3.5: cross-cycle keystone ledger read + migration verdict.
    # Redis is the live projection (read first); Postgres is the durable
    # fallback when the Redis stream is empty. No agents involved.
    if args.keystone_history:
        result = asyncio.run(_read_keystone_history(args))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("history") or result.get("cycles") else 1

    # --- Route 3.6: isolated microstructure capture health only.
    if args.microstructure_status:
        result = asyncio.run(_read_microstructure_status(args))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") is not None else 1

    # --- Route 4: legacy dev-only live waveform (explicit --live).
    if args.live:
        result = asyncio.run(build(args.symbol, args.trades, args.depth, args.bucket_window))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(render(result["core"]))
        return 0

    # --- Default (no action flags): run the canonical calculation pipeline.
    # This is the coherent E2E: the poller feeds the Redis stream, and the
    # harness reads that stream and runs the deterministic calculations into
    # a MarketRunEnvelope v2. Replaces the old default of the legacy live
    # waveform (now behind --live).
    result = asyncio.run(_run_analyze(args))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(json.dumps({
            "status": result.get("status"),
            "symbol": result.get("symbol"),
            "run_id": result.get("run_id"),
            "window_minutes": result.get("window_minutes"),
            "derivatives_cache": result.get("derivatives_cache"),
            "persistence": result.get("persistence"),
            "envelope": result.get("envelope")
            if result.get("envelope_summary") is None
            else result.get("envelope_summary"),
            "elapsed_ms": result.get("elapsed_ms"),
        }, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Calculation-pipeline route handlers
# ---------------------------------------------------------------------------

async def _refresh_derivatives(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch + cache one derivative evidence snapshot in Redis.

    No pipeline run, no agents — pure Redis write so downstream
    ``--analyze`` cycles can reuse the cache within ``--deriv-ttl`` seconds.
    """
    from market_service.clients.binance import Binance
    from market_service.nooa_harness.pipeline import fetch_derivative_evidence

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    log.info("harness --refresh-derivatives %s (cross_asset=%s ttl=%ss)",
             symbol, args.with_cross_asset, args.deriv_ttl)

    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix,
        settings.redis_stream_maxlen,
    )
    try:
        async with Binance() as client:
            deriv = await fetch_derivative_evidence(
                client, symbol,
                include_cross_asset=args.with_cross_asset,
            )
        stream_id = await store.publish_derivative_evidence(
            symbol, deriv, ttl_s=args.deriv_ttl,
        )
        remaining_ttl = await store.derivative_cache_ttl(symbol)
        return {
            "status": "ok",
            "symbol": symbol,
            "stream_id": stream_id,
            "cache_key": store.derivatives_key(symbol),
            "cache_ttl_seconds": remaining_ttl,
            "with_cross_asset": args.with_cross_asset,
            "deriv_ttl_seconds": args.deriv_ttl,
            "observed_at_ms": deriv.get("observed_at_ms"),
            "futures_keys_present": sorted(
                k for k, v in (deriv.get("futures") or {}).items() if v is not None
            ),
            "cross_asset_keys_present": sorted(
                k for k, v in (deriv.get("cross_asset") or {}).items() if v
            ),
        }
    finally:
        await store.close()


async def _run_analyze(args: argparse.Namespace) -> dict[str, Any]:
    """Run the calculation pipeline and persist to the canonical ledger.

    No agents are invoked. Returns a dict with the run_id, persistence
    result, derivatives cache status, and either the full envelope or the
    compact envelope_summary (per ``--envelope-summary``).
    """
    from market_service.nooa_harness.pipeline import WINDOW_MINUTES_MAP, run_cycle

    # Postgres is only required when we actually persist. Redis-only.
    settings = Settings.from_env() if not args.no_persist else Settings.from_redis_env()
    symbol = args.symbol.upper()
    window_minutes = WINDOW_MINUTES_MAP.get(args.window, 15)
    log.info(
        "harness --analyze %s window=%dm deriv_ttl=%ss cross_asset=%s "
        "include_derivatives=%s force_refresh=%s persist=%s",
        symbol, window_minutes, args.deriv_ttl, args.with_cross_asset,
        not args.no_derivatives, args.force_refresh_derivatives,
        not args.no_persist,
    )

    started = time.monotonic()

    envelope = await run_cycle(
        settings, symbol, window_minutes,
        deriv_ttl_s=args.deriv_ttl,
        include_cross_asset=args.with_cross_asset,
        include_derivatives=not args.no_derivatives,
        force_refresh_derivatives=args.force_refresh_derivatives,
        persist=not args.no_persist,
    )

    # --no-persist is now enforced at the pipeline boundary: persist=False
    # skips Postgres + Redis write entirely (real dry run).
    persistence_status = "persisted" if not args.no_persist else "dry_run"

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    envelope_dict = envelope.to_dict()

    derivatives_cache = _summarize_derivatives_cache(envelope_dict)
    out: dict[str, Any] = {
        "status": envelope_dict.get("status"),
        "symbol": envelope_dict.get("symbol"),
        "run_id": envelope_dict.get("run_id"),
        "window_minutes": window_minutes,
        "elapsed_ms": elapsed_ms,
        "persistence": {"mode": persistence_status},
        "derivatives_cache": derivatives_cache,
    }

    if args.envelope_summary:
        # MarketRunEnvelope.to_dict() has NO envelope_summary field (that lives
        # on AnalystBriefing). Always derive the compact projection explicitly.
        out["envelope_summary"] = _projection(envelope_dict)
    else:
        out["envelope"] = envelope_dict

    return out


async def _run_groups(args: argparse.Namespace, groups: tuple[str, ...]) -> dict[str, Any]:
    """Calculation-model group commands (Pass 3): focused, segregated analysis.

    Runs ONLY the calculation + analysis sections each requested domain group
    needs. No envelope assembly, no persistence. Redis-only (no DATABASE_URL)
    except ``wall`` which reads the wall-history ledger (Postgres fallback).

    The model gets exactly the data its question needs — not the 5.7MB
    combined envelope. This is the new primary interface for targeted
    analysis; ``--analyze`` remains for the canonical persisted record.
    """
    from market_service.nooa_harness.pipeline import (
        GROUP_MAP, WINDOW_MINUTES_MAP, read_raw_window, resolve_analysis_sections,
        resolve_calc_sections, run_analysis, run_calculations, sections_for_groups,
        _merge_derivatives, fetch_derivative_evidence,
    )
    from market_service.clients.binance import Binance

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    window_minutes = WINDOW_MINUTES_MAP.get(args.window, 15)
    window_s = window_minutes * 60
    depth = args.depth or settings.depth_levels

    try:
        calc_sections, anal_sections = sections_for_groups(groups)
    except ValueError as e:
        return {"status": "invalid", "symbol": symbol, "groups": list(groups), "error": str(e)}

    # Auto-include calculation prerequisites for the requested analysis adapters.
    anal_sections, calc_sections = resolve_analysis_sections(anal_sections, calc_sections)
    calc_sections = resolve_calc_sections(calc_sections)

    started = time.monotonic()
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        evidence = await read_raw_window(store, symbol, window_minutes)

        # Warm the derivative cache for groups that need it: oi (oi_history,
        # L/S), demand (taker + cross_asset), regime (L/S), stage (klines).
        # Cache-first; fetch on miss.
        needs_deriv = bool({"oi", "demand", "regime", "stage"} & set(anal_sections or set()))
        deriv = None
        if needs_deriv and not args.no_derivatives:
            cached = await store.read_derivative_evidence(symbol)
            from market_service.nooa_harness.pipeline import DERIV_FRESH_MS_DEFAULT, _is_deriv_fresh
            if cached and _is_deriv_fresh(cached, int(time.time() * 1000), DERIV_FRESH_MS_DEFAULT):
                deriv = cached
            else:
                async with Binance() as client:
                    deriv = await fetch_derivative_evidence(client, symbol, include_cross_asset=args.with_cross_asset)
                try:
                    await store.publish_derivative_evidence(symbol, deriv, ttl_s=args.deriv_ttl)
                except Exception:
                    log.exception("_run_groups %s: failed to publish derivative cache", symbol)
        evidence = _merge_derivatives(evidence, deriv)
    finally:
        await store.close()

    calc_result = run_calculations(evidence, depth, window_s, sections=calc_sections)

    # Wall history for wall_migration (Postgres-first, Redis fallback) — only
    # when the wall group is actually requested.
    prior_walls: dict[float, float] = {}
    prior_cycle_ts = None
    if "wall_migration" in (anal_sections or set()):
        prior_walls, prior_cycle_ts = await _load_prior_walls(settings, symbol)

    analysis_result = run_analysis(
        evidence, calc_result,
        prior_walls=prior_walls or None,
        prior_cycle_ts=prior_cycle_ts,
        depth=depth,
        sections=anal_sections,
    )

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    all_errors = list(calc_result.get("errors") or []) + list(analysis_result.get("errors") or [])
    status = "degraded" if all_errors else "healthy"

    # Assemble focused per-group output: only the sections each group owns.
    out_groups: dict[str, Any] = {}
    for g in groups:
        spec = GROUP_MAP[g]
        anal_out = analysis_result.get("analysis") or {}
        out_groups[g] = {
            "calculations": {
                k: (calc_result.get("calculations") or {}).get(k)
                for k in spec["calculations"]
            },
            "analysis": {
                ("open_interest" if k == "oi" else k):
                    anal_out.get("open_interest" if k == "oi" else k)
                for k in spec["analysis"]
            },
        }

    out: dict[str, Any] = {
        "status": status,
        "symbol": symbol,
        "groups": list(groups),
        "window_minutes": window_minutes,
        "results": out_groups,
        "elapsed_ms": elapsed_ms,
        "errors": all_errors,
    }
    # Keystone-history verdict rides along with the wall group.
    if "wall" in groups:
        out["keystone_history"] = await _keystone_history_payload(settings, symbol, args.history_limit)
    return out


async def _load_prior_walls(settings: Settings, symbol: str) -> tuple[dict[float, float], str | None]:
    """Load the wall-history ledger for wall_migration (Postgres-first, Redis fallback)."""
    from market_service.nooa_harness.pipeline import _accumulate_prior_walls
    from market_service.runtime.postgres_store import PostgresRuntimeStore
    history: list[dict[str, Any]] = []
    if settings.database_url:
        pg = PostgresRuntimeStore(settings.database_url)
        try:
            await pg.connect()
            history = await pg.read_wall_history(symbol)
        except Exception:
            log.warning("_load_prior_walls %s: postgres read failed; falling back to redis", symbol)
            history = []
        finally:
            await pg.close()
    if not history:
        store = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
        try:
            history = await store.read_wall_history(symbol)
        finally:
            await store.close()
    return _accumulate_prior_walls(history)


async def _keystone_history_payload(settings: Settings, symbol: str, limit: int) -> dict[str, Any]:
    """Read the cross-cycle keystone ledger + migration verdict (for --wall)."""
    from market_service.calculations.orderbook import keystone_cycle_migration
    from market_service.runtime.postgres_store import PostgresRuntimeStore
    store = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    history: list[dict[str, Any]] = []
    try:
        history = await store.read_keystone_history(symbol, count=max(1, limit or 100))
    finally:
        await store.close()
    if not history and settings.database_url:
        pg = PostgresRuntimeStore(settings.database_url)
        try:
            await pg.connect()
            history = await pg.read_keystone_history(symbol, limit=max(1, limit or 100))
        finally:
            await pg.close()
    migration = keystone_cycle_migration(history) if history else {"cycles": [], "verdict": None, "net_buckets": 0}
    return {
        "history_count": len(history),
        "cycles": migration.get("cycles"),
        "verdict": migration.get("verdict"),
        "net_buckets": migration.get("net_buckets"),
    }


async def _read_keystone_history(args: argparse.Namespace) -> dict[str, Any]:
    """Read the cross-cycle keystone ledger and derive the migration verdict.

    Redis is the live projection (read first); Postgres is the durable
    fallback when the Redis stream is empty. The migration verdict is
    derived read-side via ``keystone_cycle_migration`` (pure function) —
    no agents, no re-fetch. Follows the null discipline: an empty ledger
    returns ``history: []`` and ``verdict: None`` (no fabricated state).
    """
    from market_service.calculations.orderbook import keystone_cycle_migration
    from market_service.runtime.postgres_store import PostgresRuntimeStore

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    limit = max(1, int(args.history_limit or 100))

    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    history: list[dict[str, Any]] = []
    source = "redis"
    try:
        history = await store.read_keystone_history(symbol, count=limit)
    finally:
        await store.close()

    if not history and settings.database_url:
        # Durable fallback — the Redis stream is live/bounded, Postgres is
        # the ledger that survives Redis restarts.
        pg = PostgresRuntimeStore(settings.database_url)
        try:
            await pg.connect()
            history = await pg.read_keystone_history(symbol, limit=limit)
            source = "postgres"
        finally:
            await pg.close()

    migration = keystone_cycle_migration(history) if history else {
        "cycles": [], "verdict": None, "net_buckets": 0,
    }
    return {
        "symbol": symbol,
        "source": source if history else None,
        "history_count": len(history),
        "history": history,
        "cycles": migration.get("cycles"),
        "verdict": migration.get("verdict"),
        "net_buckets": migration.get("net_buckets"),
    }


async def _read_microstructure_status(args: argparse.Namespace) -> dict[str, Any]:
    """Read the separate spot-capture health projection, Redis-only."""
    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    store = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    try:
        status = await store.read_microstructure_status("spot", symbol)
        return {
            "symbol": symbol,
            "venue": "spot",
            "status_key": store.microstructure_status_key("spot", symbol),
            "raw_stream": store.microstructure_raw_stream("spot", symbol),
            "event_stream": store.microstructure_event_stream("spot", symbol),
            "ofi_stream": store.microstructure_ofi_stream("spot", symbol),
            "status": status,
        }
    finally:
        await store.close()


def _projection(envelope_dict: dict[str, Any]) -> dict[str, Any]:
    """Signal-inventory projection of an envelope: keys per section + scalar
    headlines, never raw arrays.

    CLI convenience for ``--analyze --envelope-summary``. The canonical
    read path for the model is the FULL envelope via ``--latest`` /
    ``--run-id``; this projection only inventories what signal groups are
    present and surfaces a handful of scalar headlines so a human can
    verify the Pass 1/Pass 2 ports landed without dumping the whole
    envelope. Null discipline: headlines pass through None untouched.
    """
    out = {k: v for k, v in envelope_dict.items()
           if k in ("schema_version", "symbol", "status", "generated_at",
                    "completed_at", "coverage", "run_id")}
    cs = envelope_dict.get("canonical_state") or {}
    analysis = (cs.get("analysis") or {}).get("analysis") or {}
    calculations = (cs.get("calculations") or {}).get("calculations") or {}
    orderbook = calculations.get("orderbook") or {}
    technical = calculations.get("technical") or {}

    # Signal inventory: which groups are present per section.
    out["analysis_keys"] = sorted(analysis.keys()) if isinstance(analysis, dict) else []
    out["calculations_keys"] = sorted(calculations.keys()) if isinstance(calculations, dict) else []
    out["orderbook_keys"] = sorted(orderbook.keys()) if isinstance(orderbook, dict) else []
    out["technical_keys"] = sorted(technical.keys()) if isinstance(technical, dict) else []

    # Scalar headlines for the Pass 1/Pass 2 ports (bounded, no raw arrays).
    def _path(d: Any, *keys: str) -> Any:
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    fz = orderbook.get("fut_keystone") or {}
    out["headlines"] = {
        "fut_keystone_bid": fz.get("bid") if isinstance(fz, dict) else None,
        "fut_keystone_ask": fz.get("ask") if isinstance(fz, dict) else None,
        "keystone_bid_qty": _path(orderbook, "keystone_bid_stack", "tight", "total_qty"),
        "ask_ladder_notional": _path(orderbook, "ask_wall_ladder", "total_notional"),
        "keystone_trade_buy_qty": _path(orderbook, "keystone_trade_intensity", "tight", "buy_qty"),
        "keystone_trade_sell_qty": _path(orderbook, "keystone_trade_intensity", "tight", "sell_qty"),
        "seller_aggression": _path(technical, "seller_aggression", "classification"),
        "hourly_keystone_verdict": _path(orderbook, "hourly_keystone_migration", "verdict"),
    }
    return out


def _summarize_derivatives_cache(envelope_dict: dict[str, Any]) -> dict[str, Any]:
    """Pull the derivative-fetch metadata from an envelope dict."""
    meta: dict[str, Any] = {"include_derivatives": None, "observed_at_ms": None}
    cs = envelope_dict.get("canonical_state") or {}
    evidence = (cs.get("data-access") or {}).get("evidence") or {}
    if isinstance(evidence, dict):
        meta["observed_at_ms"] = evidence.get("derivative_observed_at_ms")
        fut = evidence.get("futures") or {}
        if isinstance(fut, dict):
            present = [k for k in ("oi_history", "taker_buy_sell", "top_ls",
                                    "global_ls", "klines") if fut.get(k)]
            meta["fields_present"] = present
            meta["include_derivatives"] = bool(present)
        cross = evidence.get("cross_asset")
        if cross:
            meta["cross_asset"] = {
                "tickers": len(cross.get("tickers_24h") or []),
                "funding": len(cross.get("funding") or []),
            }
    return meta


if __name__ == "__main__":
    raise SystemExit(main())
