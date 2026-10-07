"""Thin CLI adapter — flags → package calls → stdout header.

No projection logic, no store construction, no prompt text lives here.
Same flags as the retired harness.py (Hermes byte-compatible); every
``--json`` response gains the ``interaction`` header exactly once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from market_service.interaction_plane import control as _control
from market_service.interaction_plane import parsing as _parsing
from market_service.interaction_plane import prompts as _prompts
from market_service.interaction_plane import reads as _reads
from market_service.interaction_plane import trigger as _trigger
from market_service.interaction_plane.manifest import describe as _describe


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Interaction-plane CLI: warm reads (default / --substrate-read), "
            "ledger reads (--read), history, micro status, poller control, "
            "and the task-directed inference trigger. Reads and triggers; "
            "never computes. Every --json response carries the interaction "
            "header (designated prompt + budget receipt)."
        ),
    )
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--depth", type=int, default=None,
                   help="order book depth (reserved; no compute in this plane)")
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")
    p.add_argument("--read", action="store_true",
                   help="ledger read: Redis-first, Postgres-fallback collated envelope")
    p.add_argument("--run-id", help="read one exact collated envelope by run ID")
    p.add_argument("--mode", choices=("snapshot", "inventory", "full"), default="snapshot",
                   help="output shape for --read (full is human-only)")
    p.add_argument("--read-errors", action="store_true",
                   help="--read: include source_metadata errors verbatim")
    p.add_argument("--substrate-read", action="store_true",
                   help="warm-plane worker state read")
    p.add_argument("--substrate", default=None,
                   help="with --substrate-read: single substrate name")
    p.add_argument("--poller-symbols", metavar="SYM[,SYM...]",
                   help="set poller active symbols via Redis control key")
    p.add_argument("--poller-symbols-reset", action="store_true",
                   help="delete Redis control key (fall back to env)")
    p.add_argument("--poller-status", action="store_true",
                   help="read poller live status from Redis")
    p.add_argument("--keystone-history", action="store_true",
                   help="cross-cycle keystone ledger + migration verdict")
    p.add_argument("--microstructure-status", action="store_true",
                   help="isolated capture health (Redis-only, no compute)")
    p.add_argument("--inference", action="store_true",
                   help="trigger ONE task-directed inference cycle")
    p.add_argument("--inference-force", action="store_true",
                   help="with --inference: bypass wake predicates")
    p.add_argument("--task", default=None,
                   help="trade hypothesis / question the cycle must answer")
    p.add_argument("--target", default=None,
                   help="scenario price target (quote currency)")
    p.add_argument("--horizon", default="1h", choices=("15m", "1h", "4h"),
                   help="scenario horizon (default 1h)")
    p.add_argument("--history-limit", type=int, default=100,
                   help="max keystone history entries (default 100)")
    p.add_argument("--span", default=None,
                   choices=("1s", "5s", "30s", "60s", "15m", "1h", "4h"),
                   help="horizon-span slice: read one deterministic horizon block "
                        "(Redis latest:horizon → Postgres fallback). "
                        "Native 1s/5s/30s/60s fit directly; long 15m/1h/4h are "
                        "bridge projections (extrapolated, provisional). "
                        "Unset = headline behavior (unchanged).")
    p.add_argument("--describe-interaction", action="store_true",
                   help="emit the segment/mode/budget/prompt manifest and exit")
    return p


def _emit(result: dict[str, Any], *, segment: str, mode: str, source: str | None) -> None:
    print(json.dumps(
        _prompts.attach_header(result, segment=segment, mode=mode, source=source),
        indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    if args.describe_interaction:
        print(json.dumps(_describe(), indent=2, default=str))
        return 0

    if args.poller_symbols or args.poller_symbols_reset or args.poller_status:
        result = asyncio.run(_poller_control(args))
        _emit(result, segment="control", mode="compact",
              source="redis" if result.get("status") == "ok" else None)
        return 0 if result.get("status") == "ok" else 1

    if args.substrate_read:
        result, source = asyncio.run(_read_substrates(args))
        _emit(result, segment="warm", mode=("full" if args.json else "compact"),
              source=source)
        return 0

    if args.read:
        if args.span is not None:
            result, source = asyncio.run(_read_horizon(args))
            _emit(result, segment="ledger", mode=args.mode, source=source)
            inner = result.get("data", {}) if isinstance(result, dict) else {}
            # Refused horizon (unsupported) exits non-zero; empty span exits 0
            # (null is legitimate "no data", same discipline as --read).
            if isinstance(inner, dict) and inner.get("refused"):
                return 1
            return 0
        result, source = asyncio.run(_read_market(args, args.mode))
        _emit(result, segment="ledger", mode=args.mode, source=source)
        return 0

    if args.keystone_history:
        result, source = asyncio.run(_read_keystone_history(args))
        _emit(result, segment="history", mode="compact", source=source)
        return 0 if result.get("data", {}).get("history") or result.get("data", {}).get("cycles") else 1

    if args.microstructure_status:
        result, source = asyncio.run(_read_microstructure_status(args))
        _emit(result, segment="micro", mode="compact", source=source)
        inner = result.get("data", {}) if isinstance(result, dict) else {}
        return 0 if (inner.get("status") is not None if isinstance(inner, dict) else False) else 1

    if args.inference:
        scenario, error = _trigger.parse_scenario(args.target, args.horizon)
        if error:
            _emit({"status": "error", "error": error},
                  segment="inference", mode="snapshot", source=None)
            return 1
        result = asyncio.run(_trigger.trigger_inference(
            args.symbol, force=args.inference_force,
            task=args.task, scenario=scenario))
        _emit(result, segment="inference", mode="snapshot",
              source="postgres" if result.get("artifact_id") else None)
        return 0 if result.get("status") != "no_wake" else 1

    result, source = asyncio.run(_read_substrates(args))
    _emit(result, segment="warm", mode=("full" if args.json else "compact"),
          source=source)
    return 0


# --- Route handlers: package calls wrapped in the ToolResult envelope ---

async def _poller_control(args: argparse.Namespace) -> dict[str, Any]:
    if args.poller_symbols:
        data = await _control.poller_set(args.poller_symbols.split(","))
    elif args.poller_symbols_reset:
        data = await _control.poller_reset()
    else:
        data = await _control.poller_status()
    status = "ok" if data.get("status") == "ok" else (
        "error" if data.get("status") == "error" else "ok")
    return _parsing.wrap_result(
        "poller.control", data, segment="control", mode="compact",
        status=status, reason=None if status == "ok" else data.get("error"))


async def _read_substrates(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    mode = "full" if args.json else "compact"
    substrates = [args.substrate] if getattr(args, "substrate", None) else None
    payload, source = await _reads.read_warm(
        args.symbol.upper(), substrates=substrates, mode=mode)
    return _parsing.wrap_result(
        "substrate.read", payload, segment="warm", mode=mode), source


async def _read_market(args: argparse.Namespace, mode: str) -> tuple[dict[str, Any], str | None]:
    payload, source, _ = await _reads.read_ledger(
        args.symbol.upper(), run_id=args.run_id, mode=mode,
        include_errors=bool(args.read_errors))
    if payload is None:
        surfaces = await _reads.read_surfaces(args.symbol.upper())
        return _parsing.empty_read(
            tool="market.read", segment="ledger", mode=mode,
            symbol=args.symbol.upper(), surfaces=surfaces,
            errors=["no run persisted (redis miss, postgres absent/empty)"]), None
    return _parsing.wrap_result(
        "market.read", payload, segment="ledger", mode=mode), source


async def _read_keystone_history(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    payload, source = await _reads.read_history(
        args.symbol.upper(), limit=int(args.history_limit or 100))
    if not payload.get("history"):
        surfaces = await _reads.read_surfaces(args.symbol.upper())
        return _parsing.empty_read(
            tool="market.keystone_history", segment="history", mode="compact",
            symbol=args.symbol.upper(), surfaces=surfaces,
            errors=["keystone ledger empty"]), source
    return _parsing.wrap_result(
        "market.keystone_history", payload, segment="history", mode="compact"), source


async def _read_microstructure_status(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    payload, source = await _reads.read_micro_status(args.symbol.upper())
    return _parsing.wrap_result(
        "micro.capture_status", payload, segment="micro", mode="compact"), source


async def _read_horizon(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Horizon-span slice: one deterministic block at the requested horizon."""
    from market_service.runtime.horizon_spans import (
        SPAN_HORIZONS,
        is_supported,
    )

    horizon = str(args.span or "").lower()
    if not is_supported(horizon):
        return _parsing.wrap_result(
            "market.read", {"refused": True, "horizon": args.span,
                              "supported_horizons": list(SPAN_HORIZONS)},
            segment="ledger", mode=args.mode,
            status="refused", reason="unsupported horizon"), None
    payload, source = await _reads.read_horizon(args.symbol.upper(), horizon)
    if payload is None:
        presence = await _reads.horizons_presence(args.symbol.upper())
        surfaces = await _reads.read_surfaces(args.symbol.upper())
        data = {"symbol": args.symbol.upper(), "horizon": horizon,
                "present_horizons": presence.get("present", []),
                "absent_horizons": presence.get("absent", []),
                "surfaces": (surfaces or {}).get("surfaces", []),
                "errors": [f"no span persisted for horizon {horizon!r} "
                             "(redis miss, postgres absent)"]}
        return _parsing.wrap_result(
            "market.read", data, segment="ledger", mode=args.mode,
            status="empty", reason="no span persisted"), None
    return _parsing.wrap_result(
        "market.read", payload, segment="ledger", mode=args.mode), source


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_parser",
    "main",
    "_poller_control",
    "_read_horizon",
    "_read_keystone_history",
    "_read_market",
    "_read_microstructure_status",
    "_read_substrates",
]
