"""Live-Redis read paths — the consumer discipline for live projections.

The collated-run envelope plane (``MarketRunEnvelope`` dataclass, then the
raw-Redis collated payload) was retired: its writer plane never existed in
production, so every reader served frozen-or-absent data. The remaining
readers in this module hit planes the runtime actually writes:
substrate/analysis worker projections, derivatives cache, microstructure
evidence/status/book, and the canonical-output ledger.

Doctrine carried forward:
- Null means not-provided. Readers preserve ``None``; never zero-substitute.
- Readers perform reads ONLY — every value here was computed by the
  capture/poller/calculation layers before publish. The one exception is
  ``read_live_price``, which resolves precedence across live sections.
- Every number returned to an agent carries ``age_ms`` (see ``_age_ms``).
"""

from __future__ import annotations

import json
import math
from typing import Any

__all__ = [
    "json_safe",
    "json_safe_dumps",
    "read_analysis_latest",
    "read_analysis_snapshot",
    "read_live_price",
    "read_substrate_latest",
    "read_substrate_snapshot",
    "read_surface_inventory",
]


# The agent-facing read surface map: which tool reads which plane. Rendered
# into empty-read responses so an empty read always redirects the agent to a
# surface that DOES hold data instead of leaving it re-calling the same wall.
READ_SURFACE_TOOLS: dict[str, str] = {
    "market.read": "live venue-scoped projections (microstructure + derivatives + substrate), each with age_ms",
    "market.derivatives": "derivatives/OI state for the symbol",
    "market.keystone_history": "cross-cycle keystone migration ledger (Redis stream)",
    "market.wall_history": "cross-cycle seller-wall ledger (Redis stream)",
    "micro.capture_status": "capture worker state + sequence gaps",
    "micro.events": "raw tape events (bounded)",
    "micro.ofi_intervals": "interval-attached OFI series (bounded)",
    "micro.evidence": "price-impact / depth-scaling fit evidence",
    "substrate.read": "substrate worker inventory + freshness (age_ms)",
}


async def read_surface_inventory(store: Any, symbol: str) -> dict[str, Any]:
    """Which LIVE surfaces hold data for this symbol (bounded, cheap).

    Every row names a plane the runtime actually writes. An EMPTY read
    returns this as DATA — a read failure must leave a citable trace
    instead of a bare null the agent cannot reason about.
    """
    sym = symbol.upper()
    rows: list[dict[str, Any]] = []

    async def _value_row(name: str, get_fn: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "surface": name, "plane": "redis", "present": False,
            "age_ms": None,
        }
        try:
            payload = await get_fn() if callable(get_fn) else None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            row["present"] = True
            row["age_ms"] = _age_ms(
                payload.get("computed_at_ms")
                or payload.get("observed_at_ms")
                or payload.get("updated_at_ms")
                or payload.get("generated_at")
            )
        elif payload is not None:
            row["present"] = True
        return row

    rows.append(await _value_row(
        "derivatives", lambda: store.read_derivative_evidence(sym)))
    try:
        venue = getattr(store, "venue", None) or "futures"
        rows.append(await _value_row(
            "microstructure.status",
            lambda: store.read_microstructure_status(venue, sym)))
        rows.append(await _value_row(
            "microstructure.evidence",
            lambda: store.read_microstructure_evidence(venue, sym)))
    except Exception:
        pass
    try:
        from market_service.substrate_worker import WORKER_REGISTRY
        for name in sorted(WORKER_REGISTRY):
            rows.append(await _value_row(
                f"substrate.{name}",
                lambda n=name: store.read_substrate_latest(n, sym)))
    except Exception:
        pass
    return {
        "symbol": sym,
        "surfaces": rows,
        "available_surfaces": [r["surface"] for r in rows if r.get("present")],
        "read_tools": dict(READ_SURFACE_TOOLS),
    }


def _json_default(value: Any) -> Any:
    """Render non-JSON-native values as str (Decimal, datetime, ...).

    NOTE: this cannot catch NaN/Infinity — float is JSON-serializable for
    Python's json and emits the invalid bare ``NaN``/``Infinity`` tokens
    before ``default`` is consulted. Strict renders go through
    :func:`json_safe_dumps` (tree-walk sanitization) instead.
    """
    return str(value)


def json_safe(payload: Any) -> Any:
    """Tree-walk a value into strictly-JSON-safe form: non-finite floats
    -> None, other values unchanged. (Consumers receiving pipeline payloads
    — which may hold live NaN/Inf — must pass results through this before
    handing them to json.dumps/LLM tool seams.)"""
    return _json_safe_walk(payload)


def json_safe_dumps(payload: Any) -> str:
    """Serialize to strict valid JSON: non-finite floats -> null, other
    non-native values -> str. The post-envelope replacement for the
    dataclass ``to_json`` sanitization at any consumer seam.
    """
    return json.dumps(_json_safe_walk(payload), default=_json_default)


def _json_safe_walk(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe_walk(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_walk(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)  # Decimal/datetime etc — same norm as the write gate








def _age_ms(timestamp: Any) -> int | None:
    """Age of an ISO/epoch timestamp in ms — ``None`` when not provided.

    Null discipline: an unparseable/absent timestamp is None, never 0.
    """
    import datetime as _dt

    if timestamp is None:
        return None
    try:
        if isinstance(timestamp, (int, float)):
            ts_ms = int(timestamp)
            if ts_ms < 10 ** 12:  # epoch seconds
                ts_ms *= 1000
        else:
            text = str(timestamp).replace("Z", "+00:00")
            parsed = _dt.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            ts_ms = int(parsed.timestamp() * 1000)
        now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
        return max(0, now_ms - ts_ms)
    except Exception:
        return None




# ---------------------------------------------------------------------------
# Substrate worker plane reads (Phase 2, Task 4) — the always-fresh
# projections each substrate worker aggregates. The harness reads these
# instead of computing (Phase 4 cutover); until then they are the warm
# standby alongside the pull path.
# ---------------------------------------------------------------------------

async def read_substrate_latest(store: Any, substrate: str, symbol: str) -> dict[str, Any] | None:
    """Read one substrate's latest projection plus its derived age.

    Returns ``{"payload": ..., "age_ms": ...}`` or ``None`` when nothing has
    been aggregated yet (null = absent). ``age_ms`` is measured (now minus
    ``computed_at_ms``), never stored — freshness is read-time truth.
    """
    import time as _time

    payload = await store.read_substrate_latest(substrate, symbol.upper())
    if payload is None:
        return None
    computed_at = payload.get("computed_at_ms") if isinstance(payload, dict) else None
    age_ms = (
        int(_time.time() * 1000) - int(computed_at)
        if isinstance(computed_at, (int, float)) else None
    )
    return {"payload": payload, "age_ms": age_ms}


async def read_substrate_snapshot(store: Any, symbol: str) -> dict[str, Any]:
    """Read every registered substrate's latest projection for a symbol.

    Returns ``{substrate: {"payload", "age_ms"}}``. A substrate with nothing
    aggregated appears as ``{"available": False}`` — never silently omitted,
    so readers can distinguish "worker quiet" from "worker missing".
    """
    from market_service.substrate_worker import WORKER_REGISTRY

    snapshot: dict[str, Any] = {}
    for name in sorted(WORKER_REGISTRY):
        entry = await read_substrate_latest(store, name, symbol.upper())
        snapshot[name] = entry if entry is not None else {"available": False}
    return snapshot


async def read_analysis_latest(store: Any, analysis: str, symbol: str) -> dict[str, Any] | None:
    """Read one analysis worker's latest projection plus its derived age.

    Same discipline as ``read_substrate_latest``: ``{payload, age_ms}`` or
    ``None`` (null = cold start, never a fabricated payload).
    """
    import time as _time

    payload = await store.read_analysis_latest(analysis, symbol.upper())
    if payload is None:
        return None
    computed_at = payload.get("computed_at_ms") if isinstance(payload, dict) else None
    age_ms = (
        int(_time.time() * 1000) - int(computed_at)
        if isinstance(computed_at, (int, float)) else None
    )
    return {"payload": payload, "age_ms": age_ms}


async def read_analysis_snapshot(store: Any, symbol: str) -> dict[str, Any]:
    """Read every registered analysis worker's latest projection for a symbol."""
    from market_service.analysis_worker import ANALYSIS_WORKER_REGISTRY

    snapshot: dict[str, Any] = {}
    for name in sorted(ANALYSIS_WORKER_REGISTRY):
        entry = await read_analysis_latest(store, name, symbol.upper())
        snapshot[name] = entry if entry is not None else {"available": False}
    return snapshot


async def read_live_price(
    store: Any, symbol: str, venue: str = "futures",
) -> tuple[Any | None, str | None]:
    """Current price from LIVE projections (never the collated plane).

    Precedence: derivatives cache ``funding.mark_price`` (TTL-bounded, same
    exchange) → microstructure evidence (``mark_price``/``last_price``/
    ``mid_price``) → microstructure book mid. Returns ``(price, source)``
    or ``(None, None)`` when nothing live holds a price.
    """
    sym = symbol.upper()
    try:
        deriv = await store.read_derivative_evidence(sym)
        funding = ((deriv or {}).get("futures") or {}).get("funding") or {}
        mark = funding.get("mark_price")
        if mark is not None:
            return mark, "derivatives:funding.mark_price"
    except Exception:  # noqa: BLE001 — derivatives are best-effort
        pass
    try:
        evidence = await store.read_microstructure_evidence(venue, sym)
        if isinstance(evidence, dict):
            for key in ("mark_price", "last_price", "mid_price",
                        "microprice"):
                if evidence.get(key) is not None:
                    return evidence[key], f"microstructure.evidence:{key}"
    except Exception:  # noqa: BLE001 — evidence is best-effort
        pass
    try:
        book_raw = await store.redis.get(
            store.microstructure_book_key(venue, sym))
        import json as _json
        book = _json.loads(book_raw) if book_raw else None
        if isinstance(book, dict):
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if bids and asks:
                mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
                return mid, "microstructure.book:mid"
    except Exception:  # noqa: BLE001 — a bad book must not fail the read
        pass
    return None, None
