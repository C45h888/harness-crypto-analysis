"""
Outer harness CLI — substrate worker tool surface.

This module is the **outer** CLI surface and is the mount point the
terminal-based coding agents (pi, hermes, claude code) shell out to. It owns
exactly three responsibilities:

1. Invoke substrate workers as tools (``--invoke``) — one bounded fire-tick
   per worker. The tool-first replacement for the removed run_cycle.
2. Read worker state (default / ``--substrate-read``) — the always-fresh
   projections from the warm plane.
3. Refresh the on-demand derivative cache in Redis
   (``--refresh-derivatives``) that derivative-dependent workers read.

The former ``--nooa`` router to the mounted NOOA inner CLI was removed as
legacy debt — NOOA agent / briefing / memory / inference operations are
reached directly via the inner CLI (``python -m market_service.commands.nooa_cli``),
never through harness.py.

``--live`` exposes the legacy one-shot Binance live waveform (``build()``)
as a dev-only diagnostic; it is NOT the canonical surface.

The runtime authority split is:

  harness.py                   outer CLI       worker invocation + reads
                                                + derivative cache warm
       │
       ├─► default / --substrate-read ─► substrate_worker.tools.read_state
       │
       ├─► --invoke ─► substrate_worker.tools.invoke_many (bounded ticks)
       │
       ├─► --refresh-derivatives ─► fetch_derivative_evidence + Redis cache
       │
       └─► --live ─► build() (legacy live waveform — dev only)

The designated read tool (``--read``) is Redis-first, Postgres-fallback and
does NOT require Postgres ``DATABASE_URL``; the derivative cache warm
(``--refresh-derivatives``) is likewise Redis-only.

Usage:
    # worker state (warm plane reads; default needs no flags)
    .venv/bin/python -m market_service.commands.harness SOLUSDT --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --substrate-read --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --substrate-read --substrate tape --json

    # invoke workers as tools (bounded fire-ticks, then reports)
    .venv/bin/python -m market_service.commands.harness SOLUSDT --invoke tape,density --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --invoke --json

    # derivative cache warm (worker inputs for delta/technicals/oi)
    .venv/bin/python -m market_service.commands.harness SOLUSDT --refresh-derivatives --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --refresh-derivatives --with-cross-asset --json

    # legacy dev-only diagnostic
    .venv/bin/python -m market_service.commands.harness SOLUSDT --live --json

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


# ---------------------------------------------------------------------------
# Interpretation plane — system prompt (the constitutional briefing)
# ---------------------------------------------------------------------------
# Every model mounting into the interpretation plane through harness.py is
# briefed with this prompt. It defines the market-state discipline, the
# null semantics, the citation rules, and the available tool surface.
# The statistical inference plane (nooa_harness) has its own prompt in
# engine.py — this is the interpretation plane only.


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

    1. **Worker invocation** (``--invoke``) — one bounded fire-tick per
       named substrate worker. The tool-first computation entry.

    2. **Designated read tools** — ``--substrate-read`` (warm-plane worker
       state; the default with no flags) and ``--read`` (the collated
       envelope ledger, Redis-first/Postgres-fallback with source tagging).
       ``--run-id``, ``--mode``, and ``--read-errors`` are ``--read``
       companions; ``--substrate`` selects one worker for ``--substrate-read``.

    ``--live`` exposes the legacy one-shot live waveform (``build()``) as a
    dev-only diagnostic: it opens its OWN Binance session and returns the
    legacy ``crypto-ai-market-snapshot`` shape, NOT the methanol
    ``MarketRunEnvelope v2``. Kept for debugging; do not teach agents to
    rely on it.

    ``--refresh-derivatives`` / ``--with-cross-asset`` control the derivative cache and are
    intentionally kept on the OUTER CLI.
    """
    p = argparse.ArgumentParser(
        description=(
            "Outer harness CLI: substrate worker invocation (--invoke), "
            "worker-state reads (default / --substrate-read), the derivative-cache "
            "warmer, and legacy --live waveform (dev-only). "
            "Default (no flags) reads the warm-plane worker snapshot."
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

    # --- Derivative cache controls (for --refresh-derivatives) ---
    p.add_argument("--refresh-derivatives", action="store_true",
                   help="fetch + cache one derivative evidence snapshot in "
                        "Redis without invoking workers. Primes the cache the "
                        "derivative-dependent workers (delta/technicals/oi) read.")
    p.add_argument("--deriv-ttl", type=int, default=300,
                   help="derivative cache TTL in seconds (default 300)")
    p.add_argument("--with-cross-asset", action="store_true",
                   help="include the 16 cross-asset calls (8 tickers + 8 funding) "
                        "in the derivative fetch. Off by default to save rate-limit.")

    # --- Designated read tool (the interpretation plane's read surface) ---
    p.add_argument("--read", action="store_true",
                   help="designated read tool: Redis-first, Postgres-fallback read "
                        "of the canonical MarketRunEnvelope. The outer-CLI read path "
                        "that reaches both containers. Combine with --mode.")
    p.add_argument("--run-id", help="read one exact persisted collated envelope by run ID")
    p.add_argument("--mode", choices=("snapshot", "inventory", "full"), default="snapshot",
                   help="output shape for --read: snapshot (headline scalars, default), "
                        "inventory (section key lists + coverage), or full (raw payload)")
    p.add_argument("--read-errors", action="store_true",
                   help="--read: include the source_metadata errors list verbatim in the "
                        "response (default: only the error count is surfaced)")

    # --- Substrate worker plane (direct invocation + worker-state reads) ---
    # The harness invokes workers instead of operators reaching past it to
    # ``python -m market_service.substrate_worker.runner``: --workers-once
    # runs one bounded fire-tick per enabled worker; --substrate-read is the
    # read tool for worker state (the --read companion for the warm plane).
    p.add_argument("--invoke", metavar="NAME[,NAME...]", default=None,
                   help="invoke substrate workers directly as tools: one bounded "
                        "fire-tick per named worker (default: all registered). "
                        "The tool-first replacement for run_cycle — agents and "
                        "operators call workers here instead of the cycle.")
    p.add_argument("--substrate-read", action="store_true",
                   help="read tool for worker state: latest projection per "
                        "substrate (or one via --substrate) with status, "
                        "trigger source and age. The --read companion for the "
                        "warm plane; --json emits full payloads.")
    p.add_argument("--substrate", default=None,
                   help="with --substrate-read: single substrate name "
                        "(default: all registered → snapshot)")

    # --- Poller control plane (dynamic symbol selection) ---
    # Redis control-key writes/reads. No Binance calls, no envelope, no
    # persistence. The poller picks up changes within one poll interval.
    p.add_argument("--poller-symbols", metavar="SYM[,SYM...]",
                   help="set the poller's active symbols via the Redis control "
                        "key (e.g. --poller-symbols SOLUSDT). Takes effect "
                        "within one poll interval without restarting the poller.")
    p.add_argument("--poller-symbols-reset", action="store_true",
                   help="delete the Redis control key so the poller falls back "
                        "to POLL_SYMBOLS / SYMBOLS env defaults")
    p.add_argument("--poller-status", action="store_true",
                   help="read the poller's live status (active symbols, source, "
                        "last cycle time) from Redis")
    p.add_argument("--keystone-history", action="store_true",
                   help="read the cross-cycle keystone ledger (Redis first, "
                        "Postgres fallback) and derive the keystone migration "
                        "verdict. Returns the recorded keystone series + "
                        "UP/DOWN/FLAT per cycle + the aggregate verdict.")
    p.add_argument("--microstructure-status", action="store_true",
                   help="read the isolated Binance spot microstructure capture status from Redis; "
                        "does not start capture, run calculations, or invoke NOOA")
    p.add_argument("--inference", action="store_true",
                   help="trigger ONE statistical inference cycle (manual trigger; "
                        "combine with --inference-force — the manual wake IS the trigger)")
    p.add_argument("--inference-force", action="store_true",
                   help="with --inference: bypass wake predicates — the manual trigger IS the wake")
    p.add_argument("--history-limit", type=int, default=100,
                   help="max keystone history entries to read for "
                        "--keystone-history (default 100)")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    # --- Route 0.5: poller control plane (dynamic symbol selection).
    # Pure Redis read/write — no Binance calls, no envelope, no persist.
    if args.poller_symbols or args.poller_symbols_reset or args.poller_status:
        result = asyncio.run(_poller_control(args))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "ok" else 1

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

    # --- Route 2 is the default (see bottom): worker-state snapshot. ---

    # --- Route 1.6: substrate worker tools — direct invocation.
    # The harness is the entry point: agents and operators invoke workers
    # here instead of run_cycle computing everything.
    if args.invoke is not None:
        result = asyncio.run(_invoke_substrates(args))
        print(json.dumps(result, indent=2, default=str))
        return 0

    # --- Route 1.7: worker-state read tool (the --read companion).
    # Reads the always-fresh worker projections; the pull-path --read is
    # untouched. Missing projections are {"available": false}, never errors.
    if args.substrate_read:
        result = asyncio.run(_read_substrates(args))
        print(json.dumps(result, indent=2, default=str))
        return 0


    # --- Route 3: designated read tool — Redis-first, Postgres-fallback.
    if args.read:
        result = asyncio.run(_read_market(args, args.mode))
        print(json.dumps(result, indent=2, default=str))
        # Empty read (redis miss + postgres absent) exits 0 — null is a
        # legitimate "no data" result. Schema mismatch raises out of
        # _read_market (non-zero), never coerced to a soft failure.
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

    # --- Route 3.7: statistical inference cycle (outer-CLI trigger).
    # Delegates to the engine runner — one wake-aware cycle, or a forced
    # cycle with --inference-force. Never a lazy loop; use the inner CLI's
    # `market inference watch` for the event-driven engine loop.
    if args.inference:
        from market_service.nooa_harness.inference_runner import run_inference_once

        result = asyncio.run(run_inference_once(
            args.symbol, force=args.inference_force,
        ))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") != "no_wake" else 1

    # --- Route 4: legacy dev-only live waveform (explicit --live).
    if args.live:
        result = asyncio.run(build(args.symbol, args.trades, args.depth, args.bucket_window))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(render(result["core"]))
        return 0

    # --- Default (no action flags): worker-state snapshot.
    # The warm plane is the source: the poller feeds Redis, workers
    # aggregate continuously, the harness reads. See Route 2 note above.
    result = asyncio.run(_read_substrates(args))
    print(json.dumps(result, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Calculation-pipeline route handlers
# ---------------------------------------------------------------------------


async def _poller_control(args: argparse.Namespace) -> dict[str, Any]:
    """Poller control plane: set / reset / read the dynamic symbol selection.

    Pure Redis read/write — no Binance calls, no envelope, no persistence.
    The long-running poller container reads the control key once per cycle,
    so a change takes effect within one poll interval without a restart.

    Precedence inside the poller:
        Redis control key  >  POLL_SYMBOLS env  >  SYMBOLS env
    """
    settings = Settings.from_redis_env()
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        if args.poller_symbols:
            symbols = [s.strip().upper() for s in args.poller_symbols.split(",") if s.strip()]
            if not symbols:
                return {"status": "error", "error": "no valid symbols provided"}
            await store.set_poller_symbols(symbols)
            return {
                "status": "ok",
                "action": "set",
                "control_key": store.poller_control_key(),
                "active_symbols": sorted(set(symbols)),
                "note": "poller picks this up within one poll interval",
            }
        if args.poller_symbols_reset:
            await store.clear_poller_symbols()
            return {
                "status": "ok",
                "action": "reset",
                "control_key": store.poller_control_key(),
                "fallback_symbols": list(settings.poll_symbols),
                "note": "poller reverted to POLL_SYMBOLS / SYMBOLS env",
            }
        # --poller-status
        status = await store.read_poller_status()
        if status is None:
            return {
                "status": "error",
                "action": "status",
                "error": "no poller status found — is the poller container running?",
            }
        return {"status": "ok", "action": "status", **status}
    finally:
        await store.close()


async def _refresh_derivatives(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch + cache one derivative evidence snapshot in Redis.

    No pipeline run, no agents — pure Redis write so derivative-dependent
    workers (delta/technicals/oi) can read the cache within ``--deriv-ttl`` seconds.
    """
    from market_service.clients.binance import Binance
    from market_service.nooa_harness.pipeline_interpretation import fetch_derivative_evidence

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


async def _read_market(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    """Designated outer-CLI read tool — Redis-first, Postgres fallback.

    Reads the canonical collated market run payload and routes it through
    the shared ``read_paths`` projections (snapshot / inventory / full).
    ``source`` tags which plane served the payload. Postgres is opened only
    when ``DATABASE_URL`` is set, so the tool stays usable on a Redis-only
    host venv. Read-only: no writes, no agents, no nooa_harness crossing.
    """
    from market_service.runtime import read_paths
    from market_service.runtime.postgres_store import PostgresRuntimeStore

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()

    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    postgres = None
    if settings.database_url:
        postgres = PostgresRuntimeStore(settings.database_url)
    try:
        if postgres is not None:
            await postgres.connect()
        payload, source = await read_paths.read_collated_with_fallback(
            redis, postgres, symbol=symbol, run_id=args.run_id,
        )
    finally:
        await redis.close()
        if postgres is not None:
            await postgres.close()

    if payload is None:
        return {
            "symbol": symbol,
            "run_id": args.run_id,
            "source": None,
            "mode": mode,
            "read": None,
            "errors": ["no run persisted (redis miss, postgres absent/empty)"],
        }

    if mode == "full":
        read = payload
    elif mode == "inventory":
        read = read_paths.market_inventory(payload)
    else:
        read = read_paths.market_snapshot(payload)

    out: dict[str, Any] = {
        "symbol": symbol,
        "run_id": payload.get("run_id"),
        "source": source,
        "mode": mode,
        "read": read,
    }
    if args.read_errors:
        out["errors"] = list(payload.get("errors") or [])
    return out


async def _invoke_substrates(args: argparse.Namespace) -> dict[str, Any]:
    """Harness worker-invoke tool: bounded fire-tick per named worker.

    Calls the tool-first system (``substrate_worker.tools``) — the same
    seam the agent's ``substrate.*`` tools use. PG rides the env
    (``DATABASE_URL`` unset = Redis-only, honest degradation).
    """
    from market_service.runtime.redis_store import RedisRuntimeStore
    from market_service.substrate_worker import tools as substrate_tools
    from market_service.substrate_worker.runner import (
        pg_store_from_env,
        pg_strict_from_env,
    )

    settings = Settings.from_redis_env()
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    pg_store = pg_store_from_env()
    try:
        names = [n.strip().lower() for n in (args.invoke or "").split(",") if n.strip()]
        return await substrate_tools.invoke_many(
            store, args.symbol.upper(), names or None,
            pg_store=pg_store, pg_strict=pg_strict_from_env())
    finally:
        await store.close()
        if pg_store is not None:
            await pg_store.close()


async def _read_substrates(args: argparse.Namespace) -> dict[str, Any]:
    """Harness worker-state read tool (the --read companion).

    Same seam as the agent's ``substrate.read``: compact default,
    ``--json`` for full payloads, missing workers as available:false.
    """
    from market_service.runtime.redis_store import RedisRuntimeStore
    from market_service.substrate_worker import tools as substrate_tools

    settings = Settings.from_redis_env()
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        return await substrate_tools.read_state(
            store, args.symbol.upper(),
            substrates=[args.substrate] if args.substrate else None,
            mode="full" if args.json else "compact")
    finally:
        await store.close()


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


if __name__ == "__main__":
    raise SystemExit(main())
