"""Test-only compat: singular store.read/publish dispatch for legacy fakes.

Production uses RedisRuntimeStore.publish/read exclusively. Test fakes
predate the collapse and still speak the old per-domain names
(read_substrate_latest, publish_microstructure_event, ...). This helper
lets each fake add a 2-method singular surface without rewriting every
fixture: it dispatches by namespace to the old method when present,
otherwise returns the honest empty (None / [] / dummy id).
"""

from __future__ import annotations

from typing import Any


async def fake_read(fake: Any, namespace: str, *, ids=None, count=None,
                    newest_first=True, start="-", end="+") -> Any:
    ids = ids or {}
    symbol = str(ids.get("symbol", "SOLUSDT")).upper()
    venue = str(ids.get("venue", "futures")).lower()
    name = str(ids.get("name", ids.get("substrate", ids.get("analysis", "")))).lower()
    plane = str(ids.get("plane", "")).lower()

    def _call(*names, **kw):
        for n in names:
            fn = getattr(fake, n, None)
            if callable(fn):
                return fn, kw
        return None, None

    # Latest singletons
    if namespace == "derivatives.latest":
        fn, kw = _call("read_derivative_evidence")
        if fn:
            return await fn(symbol)
        return None
    if namespace == "micro.status":
        fn, kw = _call("read_microstructure_status")
        if fn:
            return await fn(venue, symbol)
        return None
    if namespace == "micro.evidence":
        fn, kw = _call("read_microstructure_evidence")
        if fn:
            return await fn(venue, symbol)
        return None
    if namespace == "micro.book":
        fn, kw = _call("read_microstructure_book")
        if fn:
            return await fn(venue, symbol)
        get = getattr(fake, "redis", None)
        return None
    if namespace == "raw.latest":
        fn, kw = _call("read_raw_latest")
        if fn:
            return await fn(symbol)
        return None
    if namespace == "inference.latest":
        fn, kw = _call("read_latest_canonical_output", "read_canonical_output")
        if fn:
            try:
                return await fn(symbol, venue)
            except TypeError:
                return await fn(symbol)
        return None
    if namespace == "poller.active":
        fn, kw = _call("read_poller_symbols")
        if fn:
            return await fn()
        return None
    if namespace == "poller.status":
        fn, kw = _call("read_poller_status")
        if fn:
            return await fn()
        return None
    if namespace.startswith("substrate.") and not namespace.endswith(".events"):
        fn, kw = _call("read_substrate_latest")
        if fn:
            try:
                return await fn(namespace.split(".", 1)[1], symbol)
            except TypeError:
                pass
        return None
    if namespace.startswith("analysis.") and not namespace.endswith(".events"):
        fn, kw = _call("read_analysis_latest")
        if fn:
            try:
                return await fn(namespace.split(".", 1)[1], symbol)
            except TypeError:
                pass
        return None
    if namespace == "governance.latest":
        fn, kw = _call("read_governance_latest")
        if fn:
            substrate = ids.get("name", ids.get("substrate", ""))
            try:
                if substrate:
                    return await fn(plane or "calculation", symbol, substrate=substrate)
                return await fn(plane or "calculation", symbol)
            except TypeError:
                try:
                    return await fn(plane or "calculation", symbol)
                except Exception:
                    return None
        return None
    # Streams
    if namespace in ("micro.events", "micro.ofi", "micro.raw", "micro.status.events"):
        mapping = {
            "micro.events": "read_microstructure_events",
            "micro.ofi": "read_microstructure_intervals",
            "micro.raw": "read_microstructure_raw",
            "micro.status.events": "read_microstructure_status_transitions",
        }
        fn, kw = _call(mapping[namespace])
        if fn:
            try:
                return await fn(venue, symbol, start=start, end=end, count=count)
            except TypeError:
                try:
                    return await fn(venue, symbol, count=count)
                except TypeError:
                    return await fn(venue, symbol)
        return []
    if namespace in ("raw.stream",):
        fn, kw = _call("read_raw_window")
        if fn:
            try:
                return await fn(symbol, 0)
            except TypeError:
                return []
        return []
    if namespace.endswith(".events") or namespace in (
        "governance.stream", "governance.denials",
    ):
        if "denial" in namespace:
            fn, kw = _call("read_governance_denials")
            if fn:
                return await fn(plane or "calculation", symbol, count=count or 20)
            return []
        if namespace.startswith("governance."):
            fn, kw = _call("read_governance_history")
            if fn:
                return await fn(plane or "calculation", symbol, count=count or 100)
            return []
        if namespace.startswith("substrate."):
            fn, kw = _call("read_substrate_history")
            if fn:
                sub = namespace.split(".", 1)[1].removesuffix(".events")
                return await fn(sub, symbol, count=count or 100)
            return []
        if namespace.startswith("analysis."):
            fn, kw = _call("read_analysis_history")
            if fn:
                sub = namespace.split(".", 1)[1].removesuffix(".events")
                return await fn(sub, symbol, count=count or 100)
            return []
        return []
    if namespace == "agent.artifacts":
        fn, kw = _call("read_recent_memories")
        if fn:
            try:
                return await fn(ids.get("session", ""), count=count or 32)
            except TypeError:
                return await fn(ids.get("session", ""))
        return []
    # Sequence / text / marker singletons
    fn, kw = _call("read_poller_symbols", "read_poller_status")
    return None


async def fake_publish(fake: Any, namespace: str, payload: Any, *, ids=None,
                       **kw) -> Any:
    ids = ids or {}
    symbol = str(ids.get("symbol", "SOLUSDT")).upper()
    venue = str(ids.get("venue", "futures")).lower()
    # Prefer a legacy per-domain publisher when the fake defines one.
    table = {
        "micro.events": ("publish_microstructure_event", (venue, symbol), {}),
        "micro.raw": ("publish_microstructure_delta", (venue, symbol), {}),
        "micro.ofi": ("publish_microstructure_interval", (venue, symbol), {}),
        "micro.book": ("set_microstructure_book", (venue, symbol), {}),
        "micro.status": ("set_microstructure_status", (venue, symbol), {}),
        "micro.status.events": ("publish_microstructure_status_transition", (venue, symbol), {}),
        "micro.evidence": ("publish_microstructure_evidence", (venue, symbol), {}),
        "derivatives.latest": ("publish_derivative_evidence", (symbol,), {}),
        "raw.latest": ("publish_raw_evidence", (symbol,), {}),
        "inference.latest": ("publish_canonical_output", (), {}),
        "governance.latest": ("publish_governance_decision", (), {}),
        "poller.status": ("publish_poller_status", (), {}),
        "poller.active": ("set_poller_symbols", (), {}),
        "agent.artifacts": ("publish_agent_artifact", (), {}),
    }
    # Substrate / analysis bare namespaces map to their state publishers.
    if namespace.startswith("substrate.") and not namespace.endswith(".events"):
        sub = namespace.split(".", 1)[1]
        fn = getattr(fake, "publish_substrate_state", None)
        if callable(fn):
            return await fn(sub, symbol, payload)
        rec = getattr(fake, "published", None)
        if isinstance(rec, list):
            rec.append((namespace, payload))
        return "1-1"
    if namespace.startswith("analysis.") and not namespace.endswith(".events"):
        sub = namespace.split(".", 1)[1]
        fn = getattr(fake, "publish_analysis_state", None)
        if callable(fn):
            return await fn(sub, symbol, payload)
        rec = getattr(fake, "published", None)
        if isinstance(rec, list):
            rec.append((namespace, payload))
        return "1-1"
    if namespace in table:
        old, args, kwargs = table[namespace]
        fn = getattr(fake, old, None)
        if callable(fn):
            try:
                if old == "publish_canonical_output":
                    return await fn(payload)
                if old == "publish_governance_decision":
                    plane = str(ids.get("plane", "calculation"))
                    substrate = str(ids.get("substrate", ids.get("name", "")))
                    return await fn(plane, substrate, symbol, payload)
                if old == "publish_agent_artifact":
                    session = str(ids.get("session", ""))
                    artifact = str(ids.get("artifact", "memory"))
                    return await fn(session, artifact, payload, run_id=kw.get("run_id"))
                if old in ("publish_derivative_evidence", "publish_raw_evidence"):
                    return await fn(symbol, payload)
                if old == "set_poller_symbols":
                    return await fn(payload if isinstance(payload, list) else [symbol])
                if old == "publish_poller_status":
                    return await fn(payload)
                return await fn(*args, payload, **{**kwargs, **{k: v for k, v in kw.items() if k in ("maxlen",)}})
            except TypeError:
                pass
    rec = getattr(fake, "published", None)
    if isinstance(rec, list):
        rec.append((namespace, payload))
    # Record latest-style writes when the fake exposes those attrs.
    for attr in ("latest", "store", "data"):
        pass
    return "1-1"


def fake_ns(namespace: str) -> Any:
    """Namespace spec for fakes: the real configured spec, no Redis needed."""
    from market_service.runtime.redis_store import NAMESPACES
    return NAMESPACES[namespace]
