"""T1/T2 read plane — capture, market, substrate, memory (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from market_service.runtime.scope import CANONICAL_VENUE

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry
from .registry import _bounded

import market_service.microstructure as fitting

log = logging.getLogger(__name__)


async def dispatch_ns_read(
    store: RedisRuntimeStore, symbol: str, *, namespace: str,
    venue: str = "futures", count: int | None = None,
    newest_first: bool = True,
) -> tuple[Any | None, dict[str, Any]]:
    """Tool: ns.read — one namespace-addressed read over the singular store.

    The agent's ONLY state read path. ``namespace`` selects the configured
    family (``Namespace.kind`` + template); ``(symbol, venue)`` selects the
    identity. The stored pared document is returned verbatim plus three
    derived fields the store cannot precompute: ``key`` (resolved key),
    ``age_ms`` (now minus the first present ``spec.age_fields`` entry) and
    ``source``. Streams are newest-first and bounded by ``count``.
    An absent key is ``(None, ok-empty-log)`` naming the probed key — never
    a null the agent cannot cite and never an invented value.
    """
    from market_service.runtime import read_paths
    from market_service.runtime.scope import normalize_venue

    cap = CAPABILITIES["ns.read"]
    scope = {"symbol": symbol.upper(), "namespace": namespace,
             "venue": venue, "count": count}
    try:
        cap.validate_scope(symbol, venue)
        venue = normalize_venue(venue)
        sym = symbol.upper()
        try:
            _control = await store.read("poller.active", ids={})
            if _control:
                cap.validate_live_scope(sym, tuple(_control))
                scope["active_symbols"] = list(_control)
        except CapabilityDenied:
            raise
        except Exception:  # noqa: BLE001 — liveness is best-effort here
            pass
        spec = store.ns(namespace)
        template = spec.template
        if "artifact" in template or "session" in template:
            raise CapabilityDenied(
                f"capability 'ns.read' denied: {namespace!r} is agent-memory "
                "state — use the memory.* tools")
        if "plane" in template or "consumer" in template or "event_id" in template:
            raise CapabilityDenied(
                f"capability 'ns.read' denied: {namespace!r} is not an "
                "agent-readable market namespace")
        ids: dict[str, Any] = {}
        if "{symbol}" in template:
            ids["symbol"] = sym
        if "{venue}" in template:
            ids["venue"] = venue
        key = store.key(namespace, **ids)
        as_of_ms = int(time.time() * 1000)
        if spec.kind == "stream":
            rows = await store.read(
                namespace, ids=ids, count=count or 200,
                newest_first=newest_first,
            )
            rows = _bounded(rows, count or 200)
            head = rows[0] if rows else None
            age_ms = read_paths._age_ms(
                next((head.get(f) for f in spec.age_fields
                      if isinstance(head, dict) and head.get(f) is not None),
                     None))
            if not rows:
                return None, capability_log_entry(
                    cap.name, scope, "ok",
                    detail={"status": "empty", "key": key,
                            "namespace": namespace, "venue": venue},
                )
            return read_paths.json_safe({
                "tool": "ns.read", "namespace": namespace, "symbol": sym,
                "venue": venue, "key": key, "source": "redis-live",
                "as_of_ms": as_of_ms, "status": "ok", "age_ms": age_ms,
                "data": rows,
            }), capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "ok", "namespace": namespace,
                        "key": key, "rows": len(rows)},
            )
        payload = await store.read(namespace, ids=ids)
        if payload is None:
            return None, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "empty", "key": key,
                        "namespace": namespace, "venue": venue},
            )
        age_ms = None
        if isinstance(payload, dict):
            for field_name in spec.age_fields:
                value = payload.get(field_name)
                if value is not None:
                    age_ms = read_paths._age_ms(value)
                    if age_ms is not None:
                        break
        return read_paths.json_safe({
            "tool": "ns.read", "namespace": namespace, "symbol": sym,
            "venue": venue, "key": key, "source": "redis-live",
            "as_of_ms": as_of_ms, "status": "ok", "age_ms": age_ms,
            "data": payload,
        }), capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": "ok", "namespace": namespace, "key": key},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — unknown namespace / bad key
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


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


# ---------------------------------------------------------------------------
# memory.* — the AGENT's OWN segmented episodic memory (B1 surface for the B2
# ``AgentMemorySurface``). The agent writes and reads its own observations in flight;
# the engine does NOT dispose these writes — only structural hygiene (kind /
# size / pinned segment) applies, and it lives in the skill.
# ---------------------------------------------------------------------------


def _agent_memory(memory: Any, session_id: Any, segment: Any):
    """Bind the agent memory home to this cycle's session + task segment."""
    from market_service.nooa_harness.memory import AgentMemorySurface
    return AgentMemorySurface(
        memory=memory,
        session_id=str(session_id),
        segment=(str(segment) if segment else None),
    )


async def dispatch_memory_remember(
    symbol: str, venue: str, *, args: dict[str, Any],
    memory: Any = None, session_id: Any = None, segment: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Tool: memory.remember — the agent writes one of ITS OWN observations."""
    args = args or {}
    cap = CAPABILITIES["memory.remember"]
    scope = {"symbol": symbol.upper(), "venue": venue,
             "kind": str(args.get("kind") or "observation")}
    try:
        cap.validate_scope(symbol, venue)
    except CapabilityDenied as exc:
        return {"stored": False}, capability_log_entry(
            cap.name, scope, "denied", detail=str(exc))
    if memory is None or not session_id:
        result = {"stored": False, "reason": "memory_node_not_configured"}
        return result, capability_log_entry(cap.name, scope, "error", detail=result)
    try:
        agent = _agent_memory(memory, session_id, segment)
        result = await agent.remember(
            str(args.get("kind") or "observation"),
            str(args.get("content") or ""),
            importance=args.get("importance", 5.0),
            tags=args.get("tags") or (),
            evidence_refs=args.get("evidence_refs") or (),
            title=args.get("title"),
        )
        if isinstance(result, dict) and result.get("stored"):
            return result, capability_log_entry(cap.name, scope, "ok", detail=result)
        return result, capability_log_entry(cap.name, scope, "denied", detail=result)
    except Exception as exc:  # noqa: BLE001
        return {"stored": False}, capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_memory_recall(
    symbol: str, venue: str, *, args: dict[str, Any],
    memory: Any = None, session_id: Any = None, segment: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tool: memory.recall — the agent reads back ITS OWN segment memory."""
    args = args or {}
    cap = CAPABILITIES["memory.recall"]
    scope = {"symbol": symbol.upper(), "venue": venue,
             "kind": args.get("kind"), "query": args.get("query")}
    try:
        cap.validate_scope(symbol, venue)
    except CapabilityDenied as exc:
        return [], capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    if memory is None or not session_id:
        return [], capability_log_entry(
            cap.name, scope, "ok", detail={"rows": 0, "reason": "memory_node_not_configured"})
    try:
        agent = _agent_memory(memory, session_id, segment)
        rows = await agent.recall(
            kind=(str(args["kind"]) if args.get("kind") else None),
            query=(str(args["query"]) if args.get("query") else None),
            limit=args.get("limit", 8),
        )
        return rows, capability_log_entry(
            cap.name, scope, "ok", detail={"rows": len(rows)})
    except Exception as exc:  # noqa: BLE001
        return [], capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_memory_forget(
    symbol: str, venue: str, *, args: dict[str, Any],
    memory: Any = None, session_id: Any = None, segment: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Tool: memory.forget — tombstone one of the agent's OWN memories."""
    args = args or {}
    cap = CAPABILITIES["memory.forget"]
    scope = {"symbol": symbol.upper(), "venue": venue,
             "memory_id": str(args.get("memory_id") or "")}
    try:
        cap.validate_scope(symbol, venue)
    except CapabilityDenied as exc:
        return {"forgotten": False}, capability_log_entry(
            cap.name, scope, "denied", detail=str(exc))
    if memory is None or not session_id:
        result = {"forgotten": False, "reason": "memory_node_not_configured"}
        return result, capability_log_entry(cap.name, scope, "ok", detail=result)
    try:
        agent = _agent_memory(memory, session_id, segment)
        result = await agent.forget(str(args.get("memory_id") or ""))
        return result, capability_log_entry(cap.name, scope, "ok", detail=result)
    except Exception as exc:  # noqa: BLE001
        return {"forgotten": False}, capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")


async def dispatch_memory_stats(
    symbol: str, venue: str, *, args: dict[str, Any],
    memory: Any = None, session_id: Any = None, segment: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Tool: memory.stats — per-kind counts for the agent's session."""
    args = args or {}
    cap = CAPABILITIES["memory.stats"]
    scope = {"symbol": symbol.upper(), "venue": venue}
    try:
        cap.validate_scope(symbol, venue)
    except CapabilityDenied as exc:
        return {}, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    if memory is None or not session_id:
        return {"configured": False, "counts": {}}, capability_log_entry(
            cap.name, scope, "ok", detail={"configured": False})
    try:
        agent = _agent_memory(memory, session_id, segment)
        result = await agent.stats()
        return result, capability_log_entry(cap.name, scope, "ok", detail=result)
    except Exception as exc:  # noqa: BLE001
        return {}, capability_log_entry(
            cap.name, scope, "error", detail=f"{type(exc).__name__}: {exc}")

