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
    venue: str = "futures", run_id: str | None = None,
    postgres: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tool: market.read — LIVE Redis projections, scoped by (symbol, venue).

    This tool used to read the collated run plane. That plane has no writer:
    ``publish_run`` and ``insert_run`` are referenced only by their
    definitions and a test, and ``assemble_envelope`` has no production
    caller. So the tool returned whatever was frozen there years ago — a
    BTCUSDT read answered ``last_price 145.2`` with ``fut_microprice_skew_bps``
    attached, because the payload was futures-captured data under a stale key.

    It now reads the LIVE, venue-scoped projections the capture and worker
    planes actually write, and reports the age of every one of them. A number
    can no longer be returned without an age, and an absence can no longer be
    returned without naming the keys that were probed.

      snapshot  — price/book/capture status + derivatives + substrate
                  summaries, each with ``age_ms`` (default; the primary view)
      inventory — section presence and freshness only, no payloads
      surfaces  — alias of ``inventory`` (the cross-plane presence view)
      full      — every live section verbatim (the 40k tool gate is the bound)

    Partial and empty results are DATA, never null: a read that finds nothing
    returns ``status:"empty"`` naming the exact venue-scoped keys probed, so
    the agent sees "no capture at microstructure:futures:SOLUSDT" instead of
    a re-callable wall or an invented number.
    """
    from market_service.runtime import read_paths
    from market_service.runtime.scope import normalize_venue

    cap = CAPABILITIES["market.read"]
    scope = {"symbol": symbol.upper(), "venue": venue, "mode": mode,
             "run_id": run_id}
    try:
        cap.validate_scope(symbol, venue)
        if mode not in ("snapshot", "inventory", "full", "surfaces"):
            raise CapabilityDenied(f"unknown market.read mode: {mode!r}")
        # Canonicalise BEFORE any key is built: ``perps``/``usdm`` are accepted
        # inputs but the store keys carry ``futures``.
        venue = normalize_venue(venue)
        scope["venue"] = venue
        sym = symbol.upper()
        # Liveness gate: refuse symbols nothing captures, naming the live
        # set. Best-effort — a store that cannot answer defers to the wake
        # guard and the empty-read contract below.
        try:
            _control = await store.read_poller_symbols()
            if _control:
                cap.validate_live_scope(sym, tuple(_control))
                scope["active_symbols"] = list(_control)
        except CapabilityDenied:
            raise
        except Exception:  # noqa: BLE001 — liveness is best-effort here
            pass

        def _age(payload: Any, *fields: str) -> int | None:
            if not isinstance(payload, dict):
                return None
            for field_name in fields:
                value = payload.get(field_name)
                if value is not None:
                    age = read_paths._age_ms(value)
                    if age is not None:
                        return age
            return None

        # --- read every live section independently -------------------------
        # A composite read must DEGRADE per source. One dead worker or a
        # wiped cache is a section status, never a tool failure: the agent
        # still needs the sources that ARE alive, and it needs to SEE which
        # one is down (an absent section is data).
        async def _section(factory, default=None):
            # Takes a zero-arg factory, not a coroutine: a store that lacks
            # the method must be CAUGHT here, not raised at the call site.
            try:
                return await factory(), None
            except Exception as exc:  # noqa: BLE001 — isolate, never fail the read
                log.warning("market.read section failed: %r", exc)
                return default, f"{type(exc).__name__}: {exc}"

        async def _read_book():
            raw = await store.redis.get(
                store.microstructure_book_key(venue, sym))
            return json.loads(raw) if raw else None

        async def _read_substrate():
            from market_service.substrate_worker import tools as substrate_tools
            return await substrate_tools.read_state(
                store, sym, substrates=None, mode="compact")

        evidence, evidence_err = await _section(
            lambda: store.read_microstructure_evidence(venue, sym))
        status, status_err = await _section(
            lambda: store.read_microstructure_status(venue, sym))
        intervals, intervals_err = await _section(
            lambda: store.read_microstructure_intervals(venue, sym, count=3),
            [])
        derivatives, derivatives_err = await _section(
            lambda: store.read_derivative_evidence(sym))
        book, book_err = await _section(_read_book)
        substrate, substrate_err = await _section(_read_substrate)
        intervals = intervals or []

        # Key names are advisory diagnostics. A store that cannot name a key
        # (partial stub, older adapter) must not fail the read — the sections
        # above already carry the authoritative present/absent verdict.
        def _key(method_name, *args, fallback=""):
            # Resolved by NAME: an adapter that lacks the method must fall
            # back, not raise. (Passing the bound attribute would evaluate it
            # eagerly, outside this guard.)
            fn = getattr(store, method_name, None)
            if fn is None:
                return fallback
            try:
                return fn(*args)
            except Exception:  # noqa: BLE001 — diagnostics must never raise
                return fallback

        probed = {
            "microstructure.evidence": _key(
                "microstructure_evidence_key", venue, sym),
            "microstructure.status": _key(
                "microstructure_status_key", venue, sym),
            "microstructure.book": _key(
                "microstructure_book_key", venue, sym),
            "microstructure.intervals": _key(
                "microstructure_ofi_stream", venue, sym),
            "derivatives": _key("derivatives_key", sym),
            "substrate": _key(
                "substrate_latest_key", "*", sym,
                fallback=f"latest:substrate:*:{sym}"),
        }
        def _row(payload, err, *age_fields):
            if err:
                # A dead source is reported WITH its reason — never silently
                # absent, or the agent cannot tell "no data" from "broken".
                return {"status": "error", "age_ms": None, "error": err}
            return {
                "status": "present" if payload is not None else "absent",
                "age_ms": _age(payload, *age_fields),
            }

        sections = {
            "microstructure": {
                **_row(evidence, evidence_err,
                       "received_ts_ms", "updated_at_ms", "observed_at_ms"),
                "capture_state": (status or {}).get("state"),
                "status_age_ms": _age(status, "updated_at_ms",
                                      "received_ts_ms"),
                "intervals_available": len(intervals),
                "intervals_error": intervals_err,
                "book_age_ms": _age(book, "received_ts_ms", "ts_ms",
                                    "updated_at_ms"),
                "book_error": book_err,
                "status_error": status_err,
            },
            "derivatives": _row(derivatives, derivatives_err,
                                "observed_at_ms", "ts_ms", "fetched_at_ms"),
            "substrate": _row(substrate, substrate_err,
                              "computed_at_ms", "updated_at_ms"),
        }
        live = [name for name, row in sections.items()
                if row["status"] == "present"]

        base: dict[str, Any] = {
            "tool": "market.read",
            "symbol": sym,
            "venue": venue,
            "mode": mode,
            "source": "redis-live",
            "as_of_ms": int(time.time() * 1000),
            # Static map: which tool reads which live plane. Carried on every
            # response so an empty read still tells the agent where to look.
            "read_tools": {
                "market.read":
                    "live redis: microstructure evidence/status/book/interval"
                    "s, derivatives, substrate",
                "market.derivatives": "live redis: latest:{symbol}:derivatives",
                "substrate.read": "live redis: latest:substrate:{name}:{symbol}",
                "micro.capture_status": "live redis: microstructure status",
            },
        }
        if run_id:
            # Run addressing moved to the canonical run record
            # (``inference read --run-id``); the collated run plane this key
            # used to address has no writer.
            base["run_id_note"] = (
                "run_id is not addressable here — use `inference read "
                "--run-id` for a cycle record")

        if not live:
            base.update({
                "status": "empty",
                "reason": (f"no live data for {sym} on {venue} — nothing "
                           "captured or computed for this identity"),
                "checked": probed,
                "sections": sections,
            })
            if mode in ("inventory", "surfaces"):
                # The inventory must render even when nothing is present —
                # that is exactly when the agent needs to know WHICH planes
                # were consulted and what each one reported.
                base["surfaces"] = {name: row["status"]
                                    for name, row in sections.items()}
                base["available_sections"] = []
                base["available_surfaces"] = []
            return base, capability_log_entry(
                cap.name, scope, "ok",
                detail={"status": "empty", "venue": venue,
                        "probed": sorted(probed)},
            )

        base["status"] = "ok" if len(live) == len(sections) else "partial"
        base["sections"] = sections

        if mode in ("inventory", "surfaces"):
            base["available_sections"] = live
            base["available_surfaces"] = live
            base["surfaces"] = {name: row["status"]
                                for name, row in sections.items()}
            base["checked"] = probed
        elif mode == "full":
            base["microstructure_evidence"] = evidence
            base["capture_status"] = status
            base["book"] = book
            base["intervals"] = intervals
            base["derivatives"] = derivatives
            base["substrate"] = substrate
        else:  # snapshot — bounded by construction
            summary: dict[str, Any] = {}
            if isinstance(evidence, dict):
                for key in ("last_price", "mark_price", "mid_price",
                            "best_bid", "best_ask", "spread_bps", "regime",
                            "microprice", "capture_state"):
                    if key in evidence:
                        summary[key] = evidence[key]
            base["microstructure"] = summary or None
            base["capture_state"] = (status or {}).get("state")
            base["intervals_available"] = len(intervals)
            base["derivatives"] = (derivatives
                                   if isinstance(derivatives, dict) else None)
            base["substrate"] = substrate

        return read_paths.json_safe(base), capability_log_entry(
            cap.name, scope, "ok",
            detail={"status": base["status"], "venue": venue,
                    "available_sections": live},
        )
    except CapabilityDenied as exc:
        return None, capability_log_entry(cap.name, scope, "denied", detail=str(exc))
    except ValueError as exc:
        # Scope/schema breach — stale writer or unknown venue vocabulary.
        return None, capability_log_entry(cap.name, scope, "error", detail=str(exc))


async def dispatch_read_derivatives(
    store: RedisRuntimeStore, symbol: str, *, venue: str = CANONICAL_VENUE,
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
    venue: str = CANONICAL_VENUE,
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
    venue: str = CANONICAL_VENUE,
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
    mode: str = "compact", venue: str = CANONICAL_VENUE,
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
    *, postgres: Any | None = None, venue: str = CANONICAL_VENUE,
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

