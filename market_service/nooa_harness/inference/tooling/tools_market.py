"""T1/T2 read plane — capture, market, substrate, memory (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry
from .registry import _bounded

import market_service.microstructure as fitting

async def dispatch_read_capture_status(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Capability: redis.read_capture_status."""
    cap = CAPABILITIES["redis.read_capture_status"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        status = await store.read_microstructure_status(venue, symbol.upper())
        return status, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_events(
    store: RedisRuntimeStore, symbol: str, venue: str, *, count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Capability: redis.read_events."""
    cap = CAPABILITIES["redis.read_events"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        payloads = await store.read_microstructure_events(
            venue, symbol.upper(), count=count,
        )
        return payloads, capability_log_entry(
            cap.name, scope, "ok", detail={"entries": len(payloads)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


def dispatch_replay(
    event_payloads: list[dict[str, Any]], *, symbol: str, venue: str, interval_ms: int,
) -> tuple[list[Any], list[Any], int, dict[str, Any]]:
    """Capability: fitting.replay — pure, synchronous.

    Returns (events, intervals, dropped_count, log_entry).
    """
    cap = CAPABILITIES["fitting.replay"]
    scope = {"symbol": symbol.upper(), "venue": venue, "interval_ms": interval_ms}
    try:
        cap.validate_scope(symbol, venue)
        events, dropped = fitting.replay_events_from_payloads(event_payloads)
        intervals = fitting.replay_intervals(events, interval_ms=interval_ms)
        return events, intervals, dropped, capability_log_entry(
            cap.name, scope, "ok",
            detail={"events": len(events), "intervals": len(intervals), "dropped": dropped},
        )
    except CapabilityDenied as exc:
        return [], [], 0, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


def dispatch_assemble_evidence(
    intervals: list[Any], *, symbol: str, venue: str, tick_size: Any,
    interval_seconds: int, evidence_id: str, generated_at_ms: int,
    events: list[Any] | None = None, coverage: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Capability: fitting.assemble_evidence — pure, synchronous.

    Returns (evidence_or_None, log_entry). A CapabilityDenied scope refusal
    yields (None, denied-entry) without touching the fitter.
    """
    cap = CAPABILITIES["fitting.assemble_evidence"]
    scope = {"symbol": symbol.upper(), "venue": venue,
             "interval_seconds": interval_seconds}
    try:
        cap.validate_scope(symbol, venue)
        evidence = fitting.assemble_evidence(
            intervals, symbol=symbol, venue=venue, tick_size=tick_size,
            interval_seconds=interval_seconds, evidence_id=evidence_id,
            generated_at_ms=generated_at_ms, events=events, coverage=coverage,
        )
        return evidence, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_intervals(
    store: RedisRuntimeStore, symbol: str, venue: str, *, count: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: micro.ofi_intervals — completed OFI interval rows."""
    cap = CAPABILITIES["redis.read_intervals"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        rows = await store.read_microstructure_intervals(
            venue, symbol.upper(), count=count,
        )
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_evidence(
    store: RedisRuntimeStore, symbol: str, venue: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: micro.evidence — latest immutable evidence object."""
    cap = CAPABILITIES["redis.read_evidence"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        evidence = await store.read_microstructure_evidence(venue, symbol.upper())
        return evidence, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_market_read(
    store: RedisRuntimeStore, symbol: str, *, mode: str = "snapshot",
    venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.read — latest collated market run, raw from Redis.

    Post-envelope deviation: no dataclass round trip. One GET, one
    json.loads, one schema-version guard (read_paths), then a bounded
    projection. ``mode`` selects the agent-facing shape:

      snapshot  — headline scalars + CVD multi-window sign series (default;
                  small by construction, the primary inference view)
      inventory — section key inventory + the snapshot
      full      — the raw collated payload (explicit deep-dive; the
                  engine's 40k tool-result gate is the bound)

    A schema-version mismatch is a structured ``error`` payload, never a
    coercion — the writer is on a different contract and must escalate.
    """
    from market_service.runtime import read_paths

    cap = CAPABILITIES["market.read"]
    scope = {"symbol": symbol.upper(), "venue": venue, "mode": mode}
    try:
        cap.validate_scope(symbol, venue)
        if mode not in ("snapshot", "inventory", "full"):
            raise CapabilityDenied(f"unknown market.read mode: {mode!r}")
        payload = await read_paths.read_collated(store, symbol.upper())
        if payload is None:
            return None, capability_log_entry(
                cap.name, scope, "ok", detail={"status": "no_run_persisted"},
            )
        if mode == "full":
            result: dict[str, Any] = payload
        elif mode == "inventory":
            result = read_paths.market_inventory(payload)
        else:
            result = read_paths.market_snapshot(payload)
        # NaN-safe at the tool seam: the stored payload passed the write
        # gate, but projections traverse live pipeline dicts that may hold
        # raw float('nan') (pipeline flow math). The engine's json.loads
        # round trip would choke on a bare NaN token.
        result = read_paths.json_safe(result)
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"schema_version": payload.get("schema_version")},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except ValueError as exc:
        # Schema guard breach — stale writer on a different contract.
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


async def dispatch_read_derivatives(
    store: RedisRuntimeStore, symbol: str, *, venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.derivatives — cached funding/OI/cross-asset evidence."""
    cap = CAPABILITIES["market.read_derivatives"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
        payload = await store.read_derivative_evidence(symbol.upper())
        return payload, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_keystone_history(
    store: RedisRuntimeStore, symbol: str, *, count: int = 100,
    venue: str = "spot",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: market.keystone_history — bounded cross-cycle keystone series."""
    cap = CAPABILITIES["market.read_keystone_history"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        rows = await store.read_keystone_history(symbol.upper(), count=count)
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_read_wall_history(
    store: RedisRuntimeStore, symbol: str, *, count: int = 100,
    venue: str = "spot",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: market.wall_history — bounded cross-cycle wall series."""
    cap = CAPABILITIES["market.read_wall_history"]
    scope = {"symbol": symbol.upper(), "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        rows = await store.read_wall_history(symbol.upper(), count=count)
        return _bounded(rows, count), capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))


async def dispatch_substrate_read(
    store: RedisRuntimeStore, symbol: str, *, substrate: str | None = None,
    mode: str = "compact", venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: substrate.read — always-fresh worker projections.

    The warm-plane companion of ``market.read``: same compact-by-default
    discipline, ``available:false`` for missing workers (never errors),
    ``json_safe`` at the seam, schema mismatch as structured error.
    """
    from market_service.runtime import read_paths
    from market_service.substrate_worker import tools as substrate_tools

    cap = CAPABILITIES["substrate.read"]
    scope = {"symbol": symbol.upper(), "venue": venue, "substrate": substrate, "mode": mode}
    try:
        cap.validate_scope(symbol, venue)
        if mode not in ("compact", "full"):
            raise CapabilityDenied(f"unknown substrate.read mode: {mode!r}")
        result = await substrate_tools.read_state(
            store, symbol.upper(),
            substrates=[substrate] if substrate else None, mode=mode)
        result = read_paths.json_safe(result)
        return result, capability_log_entry(cap.name, scope, "ok")
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except ValueError as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


async def dispatch_substrate_invoke(
    store: RedisRuntimeStore, symbol: str, substrate: str | None,
    *, postgres: Any | None = None, venue: str = "spot",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: substrate.<name> / substrate.invoke — bounded worker ticks.

    The engine decides WHEN a calculation must run; this dispatches that
    decision to the calculation container, which decides whether the tick
    actually fires (its cooldown gates are unchanged) and owns the durable
    ledger write. A named worker gets one fire-tick; ``substrate.invoke``
    with no substrate covers every registered worker. Each invocation audits
    under its own capability, exactly as before.

    The request goes over the control plane rather than building a worker
    here: worker identity is ``(substrate, symbol)`` and never the process,
    so an in-process fire would overwrite the live container's supervisor
    heartbeat (and the fire-dedupe high-water state sharing that key) and
    consume its raw-stream entries with ``noack=True``. ``postgres`` is
    accepted for signature compatibility but unused — the calculation
    container holds its own ledger handle.

    An unreachable plane is an ``error`` audit the engine reports as a
    finding; it never falls back to in-process invocation.
    """
    from market_service.substrate_worker.control_client import (
        CalcPlaneRejected,
        CalcPlaneUnreachable,
        request_invoke,
    )

    target = (substrate or "").lower() or None
    cap_name = f"substrate.{target}" if target else "substrate.invoke"
    if cap_name not in CAPABILITIES:
        return None, capability_log_entry(
            "substrate.invoke", {"symbol": symbol.upper(), "substrate": substrate},
            "denied", detail=f"unknown substrate worker {substrate!r}")
    cap = CAPABILITIES[cap_name]
    scope = {"symbol": symbol.upper(), "venue": venue, "substrate": target}
    try:
        cap.validate_scope(symbol, venue)
        report = await request_invoke(
            symbol.upper(), [target] if target else None)
        if target is None:
            return report, capability_log_entry(
                cap.name, scope, "ok",
                detail={"invoked": report.get("invoked"),
                        "fired": report.get("fired")})
        result = _single_report(report, target)
        if not result.get("invoked"):
            return None, capability_log_entry(
                cap.name, scope, "denied", detail=result.get("error"))
        return result, capability_log_entry(
            cap.name, scope, "ok",
            detail={"fired": result.get("fired"),
                    "trigger_source": result.get("trigger_source")})
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except CalcPlaneRejected as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except CalcPlaneUnreachable as exc:
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


def _single_report(report: dict[str, Any], target: str) -> dict[str, Any]:
    """Unwrap the plane's per-worker report so the tool shape is unchanged.

    ``POST /invoke`` always answers in the ``invoke_many`` shape; a single
    named worker's caller expects the ``invoke`` shape it got in-process.
    """
    for entry in report.get("reports") or []:
        if isinstance(entry, dict) and entry.get("substrate") == target:
            return entry
    return {
        "substrate": target, "symbol": report.get("symbol"), "invoked": False,
        "error": f"calculation plane returned no report for {target!r}",
    }


async def dispatch_memory_recall_paper(
    symbol: str, venue: str, *, query: str = "Cont OFI AD beta",
    memory: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: memory.recall_paper — recall paper facts through the ENGINE's own
    MemoryNode (two-plane boundary pass, 2026-09-04).

    The node and the paper-KB session are injected; this dispatcher never
    constructs stores and never re-reads the environment. The paper session
    UUID comes from the single source of truth ``memory.paper_kb_session_id``
    shared with scripts/seed_paper_kb.py. With no memory node (Redis-only
    deployment) the result is an explicit null payload — never a fabricated
    recall and never a second connection pool.
    """
    from market_service.nooa_harness.memory import paper_kb_session_id

    cap = CAPABILITIES["memory.recall_paper"]
    scope = {"symbol": symbol.upper(), "venue": venue, "query": query}
    try:
        cap.validate_scope(symbol, venue)
        if memory is None:
            return [], capability_log_entry(
                cap.name, scope, "ok",
                detail={"facts": 0, "reason": "memory_node_not_configured"},
            )
        mems = await memory.recall(paper_kb_session_id(), query=query, limit=8)
        projected = [{"content": m.content[:600], "tags": list(m.tags),
                      "importance": m.importance} for m in mems]
        return projected, capability_log_entry(
            cap.name, scope, "ok",
            detail={"facts": len(projected), "query": query,
                    "session": "paper-kb (injected MemoryNode)"},
        )
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:
        return [], capability_log_entry(cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

