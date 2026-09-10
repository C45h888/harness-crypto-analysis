"""
Outer harness CLI — the read / interpret surface.

This module is the **outer** CLI surface and is the mount point the
terminal-based coding agents (pi, hermes, claude code) shell out to. It reads
canonical state and triggers task-directed inference. **It never computes and
never invokes workers.**

Worker identity on the Redis plane is ``(substrate, symbol)`` and never the
process, so a second process that builds a ``SubstrateWorkerCore`` collides
with the live calculation container three ways: it overwrites the supervisor
heartbeat (and with it the fire-dedupe high-water state, which shares that
key), and it consumes raw-stream entries from the shared consumer group with
``noack=True`` — unrecoverably. The harness therefore carries no trigger: it
has no semantic authority to decide a calculation must run. That authority
belongs to the inference plane, which exercises it through the calculation
container's control plane (see ``substrate_worker/control_client.py``).

Removed as part of that boundary:

* ``--invoke``              the calculation container owns every fire-tick.
* ``--refresh-derivatives`` the poller's warm loop owns the derivative cache
                            (``poller._derivative_warm_loop``); egress belongs
                            to the poller and capture containers.
* ``--live``                opened its own Binance session and computed.

The runtime authority split is:

  harness.py                   outer CLI       reads + inference trigger
       │
       ├─► default / --substrate-read ─► substrate_worker.tools.read_state
       │
       ├─► --read ─► collated envelope (Redis-first, Postgres fallback)
       │
       ├─► --keystone-history / --microstructure-status ─► Redis projections
       │
       ├─► --poller-* ─► Redis control keys (no compute, no egress)
       │
       └─► --inference --task ─► the statistical inference plane, which may
                                 request worker fire-ticks from the
                                 calculation container

Reads are Redis-first with a Postgres fallback and do NOT require
``DATABASE_URL``; only ``--inference`` needs the durable ledger.

Usage:
    # worker state (warm plane reads; default needs no flags)
    .venv/bin/python -m market_service.commands.harness SOLUSDT --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --substrate-read --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --substrate-read --substrate tape --json

    # canonical envelope ledger
    .venv/bin/python -m market_service.commands.harness SOLUSDT --read --mode snapshot --json

    # task-directed inference (interactive plane — the canonical trigger)
    .venv/bin/python -m market_service.commands.harness SOLUSDT --inference --inference-force --task "is short-term sell pressure exhausting?" --json

Follows the repo null discipline: `null` means a source did not provide a
value — it is not a substitute for zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

from market_service.config import Settings
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

DEFAULT_VENUE = "futures"


def _venue() -> str:
    """Resolve the runtime venue the rest of the system is running on.

    ``MICROSTRUCTURE_VENUE`` is canonical — capture writes it, the substrate
    core reads it, and the inference plane keys its artifacts on it. The
    harness must agree or it reads empty keys and reasons about the wrong book.
    """
    return (os.getenv("MICROSTRUCTURE_VENUE") or DEFAULT_VENUE).lower().strip()


def build_parser() -> argparse.ArgumentParser:
    """Build the outer harness parser.

    Every flag is a read, a Redis control write, or the inference trigger —
    no flag computes, fetches from an exchange, or fires a worker.

    **Designated read tools** — ``--substrate-read`` (warm-plane worker state;
    the default with no flags) and ``--read`` (the collated envelope ledger,
    Redis-first/Postgres-fallback with source tagging). ``--run-id``,
    ``--mode``, and ``--read-errors`` are ``--read`` companions;
    ``--substrate`` selects one worker for ``--substrate-read``.

    **Inference trigger** — ``--inference --inference-force --task "..."``
    fires ONE task-directed cycle in the statistical inference plane.
    """
    p = argparse.ArgumentParser(
        description=(
            "Outer harness CLI: worker-state reads (default / --substrate-read), "
            "the canonical envelope ledger (--read), Redis projections, and the "
            "task-directed inference trigger. Reads and interprets; never "
            "computes. Default (no flags) reads the warm-plane worker snapshot."
        ),
    )
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--depth", type=int, default=None,
                   help="order book depth (default: centralized DEPTH_LEVELS)")
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")

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

    # --- Substrate worker plane (READ ONLY) ---
    # There is deliberately no invocation flag here. The calculation
    # container is the only process that constructs workers; it fires on its
    # own data cadence and self-refreshes stale projections. Judge freshness
    # from the returned age_ms rather than forcing a fire.
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
                   help="trigger ONE statistical inference cycle (interactive plane: "
                        "combine with --inference-force and --task — the manual wake "
                        "IS the trigger, the task IS the direction)")
    p.add_argument("--inference-force", action="store_true",
                   help="with --inference: bypass wake predicates — the manual trigger IS the wake")
    p.add_argument("--task", default=None,
                   help="interactive-plane directive: free-text trade hypothesis / "
                        "question the inference cycle must answer "
                        "(e.g. --task 'is short-term sell pressure exhausting on SOLUSDT?'). "
                        "Steers narration (H0/H1 frames the task) and persists on "
                        "deterministic_state.task. Requires --inference --inference-force.")
    p.add_argument("--target", default=None,
                   help="scenario price target (quote currency, e.g. --target 245.30): "
                        "the 'can price hit X?' level evaluated at --horizon via "
                        "calc.scenario.evaluate. Persists on deterministic_state.scenario. "
                        "Requires --inference --inference-force.")
    p.add_argument("--horizon", default="1h", choices=("15m", "1h", "4h"),
                   help="scenario horizon for the OFI exceedance distribution "
                        "(default 1h). Only meaningful with --target.")
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

    # --- Route 1: worker-state read tool (the --read companion).
    # Reads the always-fresh worker projections the calculation container
    # publishes. Missing projections are {"available": false}, never errors.
    if args.substrate_read:
        result = asyncio.run(_read_substrates(args))
        print(json.dumps(result, indent=2, default=str))
        return 0

    # --- Route 2 is the default (see bottom): worker-state snapshot. ---

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

    # --- Route 3.7: statistical inference cycle (interaction plane).
    # The ONLY trigger: a human (or terminal agent) supplies --task
    # (the trade hypothesis) and fires ONE task-directed cycle via
    # run_inference_once(force=True, task=...). The wake worker / loop was
    # removed — there is no autonomous firing path.
    #
    # The cycle runs on the SAME venue the rest of the runtime is on; the
    # engine may request worker fire-ticks from the calculation container,
    # which is the only process that constructs workers.
    if args.inference:
        from market_service.nooa_harness.inference_runner import run_inference_once

        scenario = None
        if args.target is not None:
            try:
                from decimal import Decimal
                target_dec = Decimal(str(args.target))
            except Exception:
                target_dec = None
            if target_dec is None or target_dec <= 0:
                print(json.dumps({"status": "error",
                                  "error": f"unparseable --target: {args.target!r}"},
                                 indent=2, default=str))
                return 1
            scenario = {"target_price": str(target_dec), "horizon": args.horizon}
        result = asyncio.run(run_inference_once(
            args.symbol, venue=_venue(),
            force=args.inference_force, task=args.task, scenario=scenario,
        ))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") != "no_wake" else 1

    # --- Default (no action flags): worker-state snapshot.
    # The warm plane is the source: the poller feeds Redis, the calculation
    # container's workers aggregate continuously, the harness reads.
    result = asyncio.run(_read_substrates(args))
    print(json.dumps(result, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Read + control route handlers
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
    """Read the isolated capture health projection, Redis-only.

    Keyed on the runtime venue (``MICROSTRUCTURE_VENUE``, default ``futures``)
    — the same one ``microstructure.capture`` writes under. Hardcoding a venue
    here reads keys nothing populates and reports a healthy-looking null.
    """
    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    venue = _venue()
    store = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    try:
        status = await store.read_microstructure_status(venue, symbol)
        return {
            "symbol": symbol,
            "venue": venue,
            "status_key": store.microstructure_status_key(venue, symbol),
            "raw_stream": store.microstructure_raw_stream(venue, symbol),
            "event_stream": store.microstructure_event_stream(venue, symbol),
            "ofi_stream": store.microstructure_ofi_stream(venue, symbol),
            "status": status,
        }
    finally:
        await store.close()


if __name__ == "__main__":
    raise SystemExit(main())
