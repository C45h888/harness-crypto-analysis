"""NOOA container — the wake-driven inference runtime as one service.

Collapses the former ``wake-worker`` sidecar into the NOOA container: the
main command runs the data-driven ``WakeSupervisor`` (blocking XREAD "$" on
the event + status-transition streams — no timer). On a wake fire the
supervisor dispatches the engine cycle as an async task through its lazy
dispatcher seam (``_engine_dispatcher_factory`` — the engine is built on
first fire, no OpenAI import at boot, and the supervisor closes it via the
``on_stop`` callback on shutdown).

``run_until_stopped`` owns the build/run/stop lifecycle including engine
tear-down; this module is a thin process entrypoint, exactly the size the
container needs.

The interactive ``nooa market …`` CLI remains reachable as a one-shot under
``--profile tools`` (entrypoint unchanged); this super-loop is the
container's MAIN command under ``--profile inference``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from market_service.nooa_harness.wake_worker import (
    WakeSupervisorConfig,
    _once,
    run_until_stopped,
)

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NOOA container (wake-driven)")
    parser.add_argument("symbol", nargs="?", default=None)
    parser.add_argument("--read-block-ms", type=int, default=None)
    parser.add_argument("--once", action="store_true",
                        help="one bounded smoke tick, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = WakeSupervisorConfig.from_env(args.symbol)
    if args.read_block_ms is not None:
        config.read_block_ms = args.read_block_ms

    log.info(
        "nooa container starting wake supervisor %s:%s (read_block_ms=%s)",
        config.symbol, config.venue, config.read_block_ms,
    )
    if args.once:
        return asyncio.run(_once(config))
    return asyncio.run(run_until_stopped(config))


if __name__ == "__main__":
    raise SystemExit(main())