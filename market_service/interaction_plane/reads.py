"""Segment reads — async implementations moved verbatim out of harness.py.

Each function returns a plain dict; the CLI wraps it in the ToolResult
envelope (parsing.wrap_result) + stdout header (prompts.attach_header).
Read-only: no writes, no agents, no compute.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from market_service.interaction_plane import stores as _stores

log = logging.getLogger(__name__)


async def read_warm(
    symbol: str, *, substrates: list[str] | None = None, mode: str = "compact",
) -> tuple[dict[str, Any], str | None]:
    """Warm-plane worker projections. Returns (payload, source=redis)."""
    from market_service.substrate_worker import tools as substrate_tools

    settings = _stores.settings_redis()
    store = _stores.open_redis(settings)
    try:
        payload = await substrate_tools.read_state(
            store, symbol.upper(), substrates=substrates, mode=mode)
        return payload, "redis"
    finally:
        await store.close()


async def read_ledger(
    symbol: str, *, run_id: str | None = None, mode: str = "snapshot",
    include_errors: bool = False, venue: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Live market read. Returns (payload, source, surfaces_or_None).

    Operator view over the same singular store the agent reads through
    ``ns.read``: live venue-scoped Redis projections (microstructure +
    derivatives + substrate), each with ``age_ms``. ``run_id`` is not
    addressable here — run records live under the canonical output plane.
    """
    from market_service.runtime import read_paths
    from market_service.runtime.scope import normalize_venue

    settings = _stores.settings_redis()
    symbol = symbol.upper()
    venue = normalize_venue(venue or _stores.venue())
    redis = _stores.open_redis(settings)
    try:
        if mode not in ("snapshot", "inventory", "full", "surfaces"):
            mode = "snapshot"
        sym = symbol

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

        async def _section(factory, default=None):
            try:
                return await factory(), None
            except Exception as exc:  # noqa: BLE001 — isolate, never fail the read
                log.warning("read_ledger section failed: %r", exc)
                return default, f"{type(exc).__name__}: {exc}"

        async def _read_book():
            raw = await redis.redis.get(
                redis.microstructure_book_key(venue, sym))
            return json.loads(raw) if raw else None

        async def _read_substrate():
            from market_service.substrate_worker import tools as substrate_tools
            return await substrate_tools.read_state(
                redis, sym, substrates=None, mode="compact")

        evidence, evidence_err = await _section(
            lambda: redis.read("micro.evidence", ids={"venue": venue, "symbol": sym}))
        status, status_err = await _section(
            lambda: redis.read("micro.status", ids={"venue": venue, "symbol": sym}))
        intervals, intervals_err = await _section(
            lambda: redis.read("micro.ofi", ids={"venue": venue, "symbol": sym},
                               count=3, newest_first=False), [])
        derivatives, derivatives_err = await _section(
            lambda: redis.read("derivatives.latest", ids={"symbol": sym}))
        book, book_err = await _section(_read_book)
        substrate, substrate_err = await _section(_read_substrate)
        intervals = intervals or []
        if isinstance(substrate, dict):
            subs = substrate.get("substrates") or {}
            if subs and all(
                not isinstance(v, dict) or not v.get("available")
                for v in subs.values()
            ):
                substrate = None

        def _key(method_name, *args, fallback=""):
            fn = getattr(redis, method_name, None)
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
        }
        if run_id:
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
                base["surfaces"] = {name: row["status"]
                                    for name, row in sections.items()}
                base["available_sections"] = []
                base["available_surfaces"] = []
            return base, "redis-live", None

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

        return read_paths.json_safe(base), "redis-live", None
    finally:
        await redis.close()


async def read_surfaces(symbol: str) -> dict[str, Any]:
    """Surface inventory: which LIVE planes hold data (empty-read redirect)."""
    from market_service.runtime import read_paths

    settings = _stores.settings_redis()
    redis = _stores.open_redis(settings)
    try:
        return await read_paths.read_surface_inventory(redis, symbol.upper())
    finally:
        await redis.close()


async def read_micro_status(symbol: str) -> tuple[dict[str, Any], str | None]:
    """Isolated capture health projection, Redis-only, runtime venue."""
    settings = _stores.settings_redis()
    symbol = symbol.upper()
    venue = _stores.venue()
    store = _stores.open_redis(settings)
    try:
        status = await store.read("micro.status", ids={"venue": venue, "symbol": symbol})
        return {
            "symbol": symbol,
            "venue": venue,
            "status_key": store.microstructure_status_key(venue, symbol),
            "raw_stream": store.microstructure_raw_stream(venue, symbol),
            "event_stream": store.microstructure_event_stream(venue, symbol),
            "ofi_stream": store.microstructure_ofi_stream(venue, symbol),
            "status": status,
        }, "redis"
    finally:
        await store.close()


__all__ = [
    "read_ledger",
    "read_micro_status",
    "read_surfaces",
    "read_warm",
]
