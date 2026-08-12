"""
Clean aggregated market-data harness for the Hermes / analyst model.

Returns ONE structured contract holding all the clean market data the system
can produce for a symbol: the canonical snapshot (raw evidence, deterministic
flow metrics, signals, plus OI / liquidation / macro analyses) and — when
`--with-scripts` is set — captured runs of the curated legacy analysis scripts.

The model reads this. It is the single clean surface, so the model never has to
scrape terminal prose or reach into individual scripts.

Usage:
    .venv/bin/python -m market_service.commands.harness SOLUSDT --json
    .venv/bin/python -m market_service.commands.harness BTCUSDT --json --with-scripts
    .venv/bin/python -m market_service.commands.harness SOLUSDT            # pretty text

The contract follows the repo's null discipline: `null` means a source did not
provide a value — it is not a substitute for zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from market_service.analysis.market import analyze, render
from market_service.manifest import LEGACY_SCRIPTS, ScriptSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

# Curated subset of legacy analysis scripts the harness captures as evidence.
# Kept small & tolerant so the harness stays fast and reliable by default.
CURATED_RUN = {
    "session_regime",
    "demand_diagnostic",
    "spot_fut_assess",
    "path_absorption",
    "wall_state_check",
    "auction_dynamics",
}
SCRIPT_TIMEOUT_S = 60


def _trim(text: str | None, n: int) -> str:
    return (text or "").strip()[-n:]


async def _run_legacy(spec: ScriptSpec, symbol: str) -> dict:
    """Run one legacy script as a subprocess; capture its output tolerantly."""
    args = [PYTHON, str(REPO_ROOT / spec["path"]), symbol]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, args, capture_output=True, text=True,
            timeout=SCRIPT_TIMEOUT_S, cwd=str(REPO_ROOT),
        )
        return {
            "script": spec["name"],
            "domain": spec["domain"],
            "kind": spec["kind"],
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "timed_out": False,
            "output": _trim(proc.stdout, 8000),
            "stderr": _trim(proc.stderr, 2000),
        }
    except subprocess.TimeoutExpired:
        return {"script": spec["name"], "domain": spec["domain"], "kind": spec["kind"],
                "ok": False, "exit": None, "timed_out": True,
                "output": "", "stderr": f"timeout after {SCRIPT_TIMEOUT_S}s"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"script": spec["name"], "domain": spec["domain"], "kind": spec["kind"],
                "ok": False, "exit": None, "timed_out": False,
                "output": "", "stderr": f"{type(exc).__name__}: {exc}"}


async def build(symbol: str, trades: int, depth: int, window: int, with_scripts: bool) -> dict:
    started = int(time.time() * 1000)
    core = await analyze(
        symbol, trade_limit=trades, depth_limit=depth, bucket_window_s=window,
    )

    out: dict = {
        "contract": {
            "name": "crypto-ai-market-harness",
            "version": 1,
            "null_semantics": "null means the source did not return a value; it is not zero",
            "clean_sources": ["market_service.clients", "market_service.calculations", "market_service.analysis"],
        },
        "symbol": symbol,
        "requested": {"trade_limit": trades, "depth_limit": depth, "bucket_window_s": window, "with_scripts": with_scripts},
        "generated_at_ms": started,
    }

    # Authoritative clean market data (the canonical snapshot).
    out["core"] = core

    # Supplementary captured runs of curated legacy analysis scripts.
    if with_scripts:
        specs = [s for s in LEGACY_SCRIPTS if s["name"] in CURATED_RUN]
        out["legacy_script_runs"] = [await _run_legacy(s, symbol) for s in specs]

    out["status"] = core.get("status", "degraded")
    out["errors"] = list(core.get("errors") or [])
    out["latency_ms"] = round((time.time() * 1000) - started, 1)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Clean aggregated market-data harness for the model")
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--trades", type=int, default=500)
    p.add_argument("--depth", type=int, default=50)
    p.add_argument("--window", type=int, default=60)
    p.add_argument("--with-scripts", action="store_true",
                   help="also capture curated legacy analysis-script runs")
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")
    args = p.parse_args(argv)

    result = asyncio.run(build(args.symbol, args.trades, args.depth, args.window, args.with_scripts))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result["core"]))
        if result.get("legacy_script_runs"):
            print("\n--- legacy script runs ---")
            for run in result["legacy_script_runs"]:
                print(f"  {run['script']:20s} domain={run['domain']:12s} ok={run['ok']} exit={run['exit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
