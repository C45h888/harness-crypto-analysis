"""Analysis-plane read/invoke tools — the ``market.read`` discipline, mirrored.

``read_state`` — snapshot (compact-by-default) or single-analysis read;
``invoke_many`` — one bounded fire-tick per named worker (same deterministic
code path the container's POST /invoke uses; run in-process via the CLI).
"""

from __future__ import annotations

from typing import Any

from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY
from market_service.runtime.read_paths import read_analysis_latest, read_analysis_snapshot


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


async def read_state(
    store: Any,
    symbol: str,
    *,
    analyses: list[str] | tuple[str, ...] | None = None,
    mode: str = "compact",
) -> dict[str, Any]:
    """Read analysis state — snapshot (default) or single analysis."""
    symbol = symbol.upper()
    if analyses:
        out: dict[str, Any] = {}
        for name in analyses:
            entry = await read_analysis_latest(store, name.lower(), symbol)
            out[name.lower()] = _project(entry, mode)
        return {"symbol": symbol, "analyses": out}
    snapshot = await read_analysis_snapshot(store, symbol)
    return {
        "symbol": symbol,
        "analyses": {
            name: _project(entry, mode) for name, entry in snapshot.items()
        },
    }


async def invoke_one(store: Any, symbol: str, analysis: str) -> dict[str, Any]:
    """One bounded fire-tick for one (analysis, symbol) — in-process."""
    from market_service.substrate_worker.core import SubstrateWorkerCore

    name = analysis.strip().lower()
    worker_cls = ANALYSIS_WORKER_REGISTRY.get(name)
    if worker_cls is None:
        raise ValueError(f"unknown analysis worker {name!r}")
    worker = worker_cls(store, symbol=symbol.upper())
    from market_service.substrate_worker.core.base import _now_ms

    await worker.start()
    try:
        await worker._tick(_now_ms_safe())
        rows = await worker._read_once(block_ms=10)
        await worker._handle_rows(rows, _now_ms_safe())
        return {"analysis": name, "symbol": symbol.upper(),
                "fired": worker.fired_count}
    finally:
        await worker.stop()


def _now_ms_safe() -> int:
    import time
    return int(time.time() * 1000)


async def invoke_many(
    store: Any,
    symbol: str,
    analyses: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """One bounded fire-tick per named analysis worker (or all)."""
    symbol = symbol.upper()
    names = list(analyses) if analyses else sorted(ANALYSIS_WORKER_REGISTRY)
    out: dict[str, Any] = {}
    for name in names:
        if name.lower() not in ANALYSIS_WORKER_REGISTRY:
            out[name] = {"error": "unknown analysis worker"}
            continue
        out[name.lower()] = await invoke_one(store, symbol, name)
    return {"symbol": symbol, "results": out}