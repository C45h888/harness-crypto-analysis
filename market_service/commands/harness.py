"""
Clean aggregated market-data harness for the Hermes / analyst model.

Returns ONE structured contract holding the canonical market data the system
can produce for a symbol: the unified snapshot (raw evidence, deterministic flow
metrics, signals, plus OI / liquidation / macro analyses).

The model reads this — the single clean surface, so it never has to scrape
terminal prose or reach into individual scripts. (The legacy exploratory
scripts were fully migrated into `market_service` and deleted.)

Usage:
    .venv/bin/python -m market_service.commands.harness SOLUSDT --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT
    .venv/bin/python -m market_service.commands.harness \
        --nooa market envelope SOLUSDT --latest

The NOOA CLI is mounted at the canonical harness mount point: the repo-root
`nooa` CLI (and the Docker `nooa` service) run the same `market` harness
group through `market_service/commands/nooa_cli.py`, and `--nooa` forwards
into it from this harness command.

Follows the repo null discipline: `null` means a source did not provide a
value — it is not a substitute for zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid

from market_service.analysis.market import analyze, render
from market_service.config import Settings
from market_service.runtime.contracts import HarnessRunRequest, RefreshCommand, RuntimeRunState
from market_service.runtime.redis_store import RedisRuntimeStore


async def build(symbol: str, trades: int, depth: int, window: int) -> dict:
    started = int(time.time() * 1000)
    core = await analyze(symbol, trade_limit=trades, depth_limit=depth, bucket_window_s=window)
    return {
        "contract": {
            "name": "crypto-ai-market-harness",
            "version": 2,
            "null_semantics": "null means the source did not return a value; it is not zero",
            "clean_sources": ["market_service.clients", "market_service.calculations", "market_service.analysis"],
        },
        "symbol": symbol,
        "requested": {"trade_limit": trades, "depth_limit": depth, "bucket_window_s": window},
        "generated_at_ms": started,
        "core": core,
        "status": core.get("status", "degraded"),
        "errors": list(core.get("errors") or []),
        "latency_ms": round((time.time() * 1000) - started, 1),
    }


async def trigger_domain(symbol: str, domain: str, scope: str, timeout_s: float) -> dict:
    """Trigger one bounded domain refresh and return its completion event."""
    settings = Settings.from_env()
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    command_id = str(uuid.uuid4())
    try:
        stream_id = await redis.request_refresh(RefreshCommand(
            domain=domain,
            symbol=symbol,
            requested_by="harness",
            command_id=command_id,
            parameters={"run_id": command_id, "scope": scope},
        ))
        event = await redis.wait_for_result(command_id, domain, timeout_s=timeout_s)
        return {
            "request_id": command_id, "command_stream_id": stream_id,
            "domain": domain, "symbol": symbol.upper(),
            "completed": event is not None,
            "result": event.payload if event else None,
        }
    finally:
        await redis.close()


async def trigger_full_cycle(symbol: str, timeout_s: float, scope: str = "all") -> dict:
    """Ask the autonomous orchestrator to run one complete cycle."""
    settings = Settings.from_env()
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    request_id = str(uuid.uuid4())
    try:
        requested_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        await redis.write_runtime_run(RuntimeRunState(
            run_id=request_id,
            symbol=symbol,
            phase="REQUESTED",
            started_at=requested_at,
            updated_at=requested_at,
            domains={"data-access": "pending", "calculations": "pending", "analysis": "pending"},
        ))
        await redis.request_harness_run(HarnessRunRequest(
            symbol=symbol,
            request_id=request_id,
            parameters={"scope": scope},
        ))
        deadline = time.monotonic() + timeout_s
        state = None
        while time.monotonic() < deadline:
            # The orchestrator's runtime key is the authoritative completion
            # record. The request ID is carried through as the run ID.
            state = await redis.read_runtime_run(request_id)
            if state and state.get("phase") in ("PUBLISHED", "FAILED", "INVALID", "DEGRADED"):
                break
            await asyncio.sleep(0.25)
        envelope = await redis.read_run(request_id)
        return {
            "request_id": request_id,
            "scope": scope,
            "completed": bool(state and state.get("phase") == "PUBLISHED"),
            "runtime": state,
            "envelope": envelope.to_dict() if envelope else None,
        }
    finally:
        await redis.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Clean aggregated market-data harness for the model")
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--trades", type=int, default=500)
    p.add_argument("--depth", type=int, default=50)
    p.add_argument("--window", type=int, default=60)
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")
    p.add_argument("--trigger", action="store_true", help="request one autonomous orchestrator cycle")
    p.add_argument("--domain", choices=("data-access", "calculations", "analysis"),
                   help="trigger one domain instead of a full cycle")
    p.add_argument("--scope", choices=("all", "order_book", "trades", "funding", "open_interest", "tickers"),
                   default="all", help="requested data scope for a domain trigger")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--latest", action="store_true", help="read the latest persisted collated envelope")
    p.add_argument("--run-id", help="read one exact persisted collated envelope by run ID")
    p.add_argument("--analyst-loop", action="store_true",
                   help="read canonical state and run the NOOA analyst suite")
    p.add_argument("--interval", type=float, default=60.0,
                   help="seconds between NOOA analyst cycles")
    p.add_argument("--cycles", type=int, default=0,
                   help="number of NOOA analyst cycles; 0 means run forever")
    p.add_argument("--with-memory", action="store_true",
                   help="NOOA analyst loop: recall prior session memory before each "
                        "cycle and remember each cycle's outputs (durable ledger + "
                        "Redis live projection)")
    p.add_argument("--session-id", default=None,
                   help="stable UUID analyst session id; default env NOOA_SESSION_ID or generated UUID")
    p.add_argument("--briefing", action="store_true",
                   help="read one persisted NOOA briefing by session and canonical run ID")
    p.add_argument("--nooa", nargs=argparse.REMAINDER, metavar="ARGS",
                   help="delegate to the mounted NOOA CLI through harness.py "
                        "(e.g. --nooa market envelope SOLUSDT --latest; "
                        "a leading -- is allowed but optional)")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    if args.nooa is not None:
        # NOOA CLI mounted at the canonical harness mount point: forward the
        # remaining argv verbatim to the repo-root-mounted CLI (import-time
        # mount, no writes into the installed nooa-cli package).
        from market_service.commands.nooa_cli import main as nooa_main

        passthrough = list(args.nooa)
        if passthrough and passthrough[0] == "--":
            passthrough = passthrough[1:]
        return nooa_main(passthrough)

    if args.briefing:
        if args.analyst_loop or not args.session_id or not args.run_id:
            print(json.dumps({
                "error": "--briefing requires --session-id and --run-id and cannot combine with --analyst-loop"
            }))
            return 2
        settings = Settings.from_env()
        from market_service.nooa_harness.runner import read_briefing
        briefing = asyncio.run(read_briefing(settings, args.session_id, args.run_id))
        result = briefing.to_dict() if briefing else None
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.analyst_loop:
        from market_service.nooa_harness.runner import run_analyst_loop
        # The analyst path is read-only. --run-id takes precedence for one
        # exact analysis; --latest polls persisted canonical state. Canonical
        # refresh remains available only through the explicit --trigger path.
        if args.run_id and args.latest:
            print(json.dumps({"error": "--run-id and --latest are mutually exclusive"}))
            return 2
        asyncio.run(run_analyst_loop(
            args.symbol,
            interval_s=args.interval,
            timeout_s=args.timeout,
            cycles=args.cycles,
            run_id=args.run_id,
            use_latest=args.latest,
            session_id=args.session_id,
            with_memory=args.with_memory,
        ))
        return 0
    if args.domain:
        result = asyncio.run(trigger_domain(args.symbol, args.domain, args.scope, args.timeout))
    elif args.trigger:
        result = asyncio.run(trigger_full_cycle(args.symbol, args.timeout, args.scope))
    elif args.latest or args.run_id:
        settings = Settings.from_env()
        async def _read():
            store = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
            try:
                value = await (store.read_run(args.run_id) if args.run_id else store.read_latest_run(args.symbol))
                return value.to_dict() if value else None
            finally:
                await store.close()
        result = asyncio.run(_read())
    else:
        result = asyncio.run(build(args.symbol, args.trades, args.depth, args.window))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif args.domain or args.trigger or args.latest or args.run_id:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result["core"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
