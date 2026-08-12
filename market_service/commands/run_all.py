"""
Run-every-script verifier for Phase 1.

Executes every script in the shared-domain manifest as a subprocess (with a
timeout), then reports a pass/fail table. This is the gate for the Phase 1
deliverable: "all scripts run and give clean market data."

It is a diagnostic/verification tool, not the data surface (that is
`market_service.commands.harness`). Returns exit code 0 only if every script
runs (imports + executes without crashing), whether or not the network is up.

Usage:
    .venv/bin/python -m market_service.commands.run_all
    .venv/bin/python -m market_service.commands.run_all --symbol SOLUSDT
    .venv/bin/python -m market_service.commands.run_all --domain analysis
    .venv/bin/python -m market_service.commands.run_all --timeout 30
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from market_service.manifest import LEGACY_SCRIPTS, by_domain

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def run_all(specs: list, symbol: str, timeout: int) -> list[dict]:
    results = []
    for spec in specs:
        args = [PYTHON, str(REPO_ROOT / spec["path"]), symbol]
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=timeout, cwd=str(REPO_ROOT))
            results.append({
                "script": spec["name"],
                "domain": spec["domain"],
                "kind": spec["kind"],
                "exit": proc.returncode,
                "ok": proc.returncode == 0,
                "timed_out": False,
                "stderr": (proc.stderr or "").strip()[-1500:],
            })
        except subprocess.TimeoutExpired:
            results.append({"script": spec["name"], "domain": spec["domain"], "kind": spec["kind"],
                            "exit": None, "ok": False, "timed_out": True, "stderr": f"timeout {timeout}s"})
        except Exception as exc:
            results.append({"script": spec["name"], "domain": spec["domain"], "kind": spec["kind"],
                            "exit": None, "ok": False, "timed_out": False, "stderr": f"{type(exc).__name__}: {exc}"})
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify every manifest script runs")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--domain", choices=("data-access", "calculation", "analysis", "monitor"))
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    specs = by_domain(args.domain) if args.domain else list(LEGACY_SCRIPTS)
    results = run_all(specs, args.symbol, args.timeout)

    if args.json:
        print(_json(results))
        return 0 if all(r["ok"] for r in results) else 1

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n=== run_all: {ok_count}/{len(results)} scripts ran (symbol={args.symbol}, timeout={args.timeout}s) ===\n")
    for r in results:
        mark = "OK " if r["ok"] else "FAIL"
        extra = f"timeout={r['timed_out']}" if r["timed_out"] else f"exit={r['exit']}"
        print(f"  [{mark}] {r['script']:22s} {r['domain']:12s} {extra}")
        if not r["ok"] and r["stderr"]:
            print(f"        {r['stderr'][-300:]}")
    print(f"\nRESULT: {'ALL_SCRIPTS_RAN' if ok_count == len(results) else f'{len(results)-ok_count} script(s) failed'}")
    return 0 if ok_count == len(results) else 1


def _json(results: list[dict]) -> str:
    import json as _jsonlib
    return _jsonlib.dumps({"scripts": results}, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
