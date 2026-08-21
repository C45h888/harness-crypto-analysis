"""
Clean aggregated market-data harness for the Hermes / analyst model.

Returns ONE structured contract holding the canonical market data the system
can produce for a symbol: the unified snapshot (raw evidence, deterministic flow
metrics, signals, plus OI / liquidation / macro analyses).

The poller continuously feeds raw evidence into Redis. The harness runs on
demand: reads the raw window, runs the pipeline, collates, and optionally
invokes the NOOA analyst suite.

Usage:
    .venv/bin/python -m market_service.commands.harness SOLUSDT --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --window 15m --json

Follows the repo null discipline: `null` means a source did not provide a
value — it is not a substitute for zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from market_service.analysis.market import analyze, render
from market_service.config import Settings
from market_service.nooa_harness.pipeline import WINDOW_MINUTES_MAP
from market_service.runtime.redis_store import RedisRuntimeStore


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
            "null_semantics": "null means the source did not return a value; it is not zero",
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
    p = argparse.ArgumentParser(description="Clean aggregated market-data harness for the model")
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--trades", type=int, default=500)
    p.add_argument("--depth", type=int, default=None,
                   help="order book depth (default: centralized DEPTH_LEVELS)")
    p.add_argument("--bucket-window", type=int, default=60,
                   help="legacy bucket window in seconds for build() path")
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")
    p.add_argument("--latest", action="store_true", help="read the latest persisted collated envelope")
    p.add_argument("--run-id", help="read one exact persisted collated envelope by run ID")
    p.add_argument("--analyze", action="store_true",
                   help="run pipeline + NOOA analyst suite once and exit")
    p.add_argument("--window", choices=("15m", "1h", "4h"), default="15m",
                   help="raw evidence lookback window for --analyze (default: 15m)")
    p.add_argument("--with-memory", action="store_true",
                   help="recall prior session memory and remember this cycle's outputs")
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
        from market_service.commands.nooa_cli import main as nooa_main
        passthrough = list(args.nooa)
        if passthrough and passthrough[0] == "--":
            passthrough = passthrough[1:]
        return nooa_main(passthrough)

    if args.briefing:
        if not args.session_id or not args.run_id:
            print(json.dumps({
                "error": "--briefing requires --session-id and --run-id"
            }))
            return 2
        settings = Settings.from_env()
        from market_service.nooa_harness.runner import read_briefing
        briefing = asyncio.run(read_briefing(settings, args.session_id, args.run_id))
        result = briefing.to_dict() if briefing else None
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.analyze:
        from market_service.nooa_harness.runner import run_analyze_once
        window_minutes = WINDOW_MINUTES_MAP.get(args.window, 15)
        result = asyncio.run(run_analyze_once(
            args.symbol,
            window_minutes=window_minutes,
            run_id=args.run_id,
            use_latest=args.latest,
            session_id=args.session_id,
            with_memory=args.with_memory,
        ))
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.latest or args.run_id:
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
        result = asyncio.run(build(args.symbol, args.trades, args.depth, args.bucket_window))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif args.latest or args.run_id:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result["core"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())