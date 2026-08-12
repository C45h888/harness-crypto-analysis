"""Verify that every canonical runtime module imports cleanly."""

from __future__ import annotations

import argparse
import importlib
import json

from market_service.manifest import CLEAN_MODULES


def domain_for(key: str) -> str:
    if key == "clients":
        return "data-access"
    if key in {"flow", "orderbook", "volume_profile", "technical", "signals"}:
        return "calculation"
    return "analysis"


def run_all(specs: list[tuple[str, str]]) -> list[dict]:
    results = []
    for domain, module in specs:
        try:
            importlib.import_module(module)
            results.append({"module": module, "domain": domain, "ok": True, "error": None})
        except Exception as exc:
            results.append({"module": module, "domain": domain, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify canonical runtime modules")
    parser.add_argument("--domain", choices=("data-access", "calculation", "analysis", "monitor"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    specs = [(domain_for(key), module) for key, module in CLEAN_MODULES.items()]
    if args.domain:
        specs = [item for item in specs if item[0] == args.domain]
    results = run_all(specs)
    if args.json:
        print(json.dumps({"modules": results}, indent=2))
    else:
        ok_count = sum(1 for result in results if result["ok"])
        print(f"\n=== run_all: {ok_count}/{len(results)} canonical modules imported ===\n")
        for result in results:
            mark = "OK " if result["ok"] else "FAIL"
            print(f"  [{mark}] {result['module']:44s} {result['domain']:12s}")
            if result["error"]:
                print(f"        {result['error']}")
        print("\nRESULT: " + ("ALL_CANONICAL_MODULES_IMPORTED" if ok_count == len(results)
                              else f"{len(results) - ok_count} module(s) failed"))
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
