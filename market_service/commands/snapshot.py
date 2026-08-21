"""Single live JSON contract for the market-state harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from market_service.analysis.market import analyze, render


async def run(symbol: str, trades: int, depth: int | None = None, window: int = 60) -> dict:
    """Return one live snapshot with raw source evidence included."""
    if depth is None:
        from market_service.config import default_depth_levels
        depth = default_depth_levels()
    return await analyze(symbol.upper(), trade_limit=trades, depth_limit=depth, bucket_window_s=window)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a live market snapshot for the harness")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--trades", type=int, default=500)
    parser.add_argument("--depth", type=int, default=None,
                        help="order book depth (default: centralized DEPTH_LEVELS)")
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--json", action="store_true", help="emit the JSON contract")
    args = parser.parse_args(argv)
    if args.trades <= 0 or args.window <= 0:
        parser.error("--trades and --window must be positive")
    try:
        snapshot = asyncio.run(run(args.symbol, args.trades, args.depth, args.window))
    except Exception as exc:
        print(f"snapshot failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
    else:
        print(render(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
