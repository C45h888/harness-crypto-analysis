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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Clean aggregated market-data harness for the model")
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--trades", type=int, default=500)
    p.add_argument("--depth", type=int, default=50)
    p.add_argument("--window", type=int, default=60)
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")
    args = p.parse_args(argv)

    result = asyncio.run(build(args.symbol, args.trades, args.depth, args.window))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result["core"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
