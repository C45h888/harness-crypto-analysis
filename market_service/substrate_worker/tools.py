"""Substrate worker tools — the tool-first invocation system.

Each substrate worker is directly invocable as a tool by agents (inference
dispatch) and CLIs (outer harness, inner ``nooa market``). No cycles, no
envelopes, no pull path: one bounded fire-tick per invocation, then a
report. This module REPLACES ``run_cycle`` as the computation entry point;
the old cycle methods are deleted after this system verifies green + live.

Design rules (no legacy debt carried forward):

* Store is always caller-injected — this module never reads env for
  connections and never builds pools. PG handle is an explicit argument
  (None = Redis-only, the honest degradation).
* One invocation = start → tick → single read+handle → drain tasks → stop.
  Loops stay compose-owned; cooldowns still gate inside the core.
* Reports are compact by construction; full payloads only via the read
  tool. Missing projections are ``{"available": false}``, never errors.
"""

from __future__ import annotations

import time
from typing import Any

from market_service.runtime import read_paths
from market_service.substrate_worker import WORKER_REGISTRY

READ_TOOL = "substrate.read"
INVOKE_TOOL_PREFIX = "substrate."


def invoke_tool_names() -> list[str]:
    """Agent tool names — one per registered worker (``substrate.<name>``)."""
    return [f"{INVOKE_TOOL_PREFIX}{name}" for name in sorted(WORKER_REGISTRY)]


def _now_ms() -> int:
    return int(time.time() * 1000)


async def invoke(
    store: Any,
    symbol: str,
    substrate: str,
    *,
    window_minutes: int = 15,
    depth: int | None = None,
    pg_store: Any | None = None,
    pg_strict: bool = True,
) -> dict[str, Any]:
    """Invoke ONE worker for one bounded fire-tick; return its report.

    Unknown substrate names are a structured ``unknown_tool`` report, never
    an exception — the dispatch layer turns it into a denial.
    """
    name = substrate.lower()
    worker_cls = WORKER_REGISTRY.get(name)
    if worker_cls is None:
        return {
            "substrate": name, "symbol": symbol.upper(), "invoked": False,
            "error": f"unknown substrate worker {substrate!r}; "
                     f"registered: {sorted(WORKER_REGISTRY)}",
        }
    worker = worker_cls(
        store, symbol=symbol.upper(), window_minutes=window_minutes,
        depth=depth, pg_store=pg_store, pg_strict=pg_strict,
    )
    fired_before = worker.fired_count
    await worker.start()
    try:
        await worker._tick(_now_ms())
        rows = await worker._read_once(block_ms=10)
        ws_rows, recovery = await worker._read_ws_once(block_ms=10)
        await worker._handle_rows(rows, _now_ms(), ws_rows=ws_rows, recovery=recovery)
        if worker._tasks:
            await _drain(worker)
    finally:
        await worker.stop()
    latest = await store.read_substrate_latest(name, symbol.upper())
    trigger = (latest or {}).get("trigger") or {}
    return {
        "substrate": name,
        "symbol": symbol.upper(),
        "invoked": True,
        "fired": worker.fired_count - fired_before,
        "trigger_source": trigger.get("source"),
        "status": (latest or {}).get("status"),
        "dormant_reason": worker._last_dormant_reason,
        "last_error": worker._last_error,
        "available": latest is not None,
    }


async def _drain(worker: Any) -> None:
    import asyncio as _asyncio

    tasks = list(worker._tasks)
    if tasks:
        await _asyncio.gather(*tasks, return_exceptions=True)


async def invoke_many(
    store: Any,
    symbol: str,
    substrates: list[str] | tuple[str, ...] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Invoke several workers (None/empty = all registered); one report each."""
    names = [s.lower() for s in (substrates or sorted(WORKER_REGISTRY))]
    reports = [await invoke(store, symbol, name, **kwargs) for name in names]
    return {
        "symbol": symbol.upper(),
        "invoked": sum(1 for r in reports if r.get("invoked")),
        "fired": sum(1 for r in reports if r.get("fired")),
        "reports": reports,
    }


async def read_state(
    store: Any,
    symbol: str,
    *,
    substrates: list[str] | tuple[str, ...] | None = None,
    mode: str = "compact",
) -> dict[str, Any]:
    """Read worker state — snapshot (default) or single substrate.

    Compact mode returns per-substrate ``{status, trigger_source, age_ms}``
    (the ``market.read`` snapshot discipline); ``mode="full"`` returns raw
    payloads. Unknown single names are ``{"available": false}``.
    """
    symbol = symbol.upper()
    if substrates:
        out: dict[str, Any] = {}
        for name in substrates:
            entry = await read_paths.read_substrate_latest(store, name.lower(), symbol)
            out[name.lower()] = _project(entry, mode)
        return {"symbol": symbol, "substrates": out}
    snapshot = await read_paths.read_substrate_snapshot(store, symbol)
    if mode == "full":
        return {"symbol": symbol, "substrates": snapshot}
    return {
        "symbol": symbol,
        "substrates": {
            name: _project(entry, mode) for name, entry in snapshot.items()
        },
    }


def _project(entry: dict[str, Any] | None, mode: str) -> dict[str, Any]:
    if entry is None or entry.get("available") is False:
        return {"available": False}
    if mode == "full":
        return dict(entry.get("payload") or {})
    payload = entry.get("payload") or {}
    trigger = payload.get("trigger") or {}
    return {
        "available": True,
        "status": payload.get("status"),
        "trigger_source": trigger.get("source"),
        "age_ms": entry.get("age_ms"),
        "computed_at_ms": payload.get("computed_at_ms"),
    }
