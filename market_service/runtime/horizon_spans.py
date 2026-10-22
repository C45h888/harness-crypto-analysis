"""Horizon names — the span vocabulary (pure module, no I/O, no payloads).

This module is the NAME owner for horizon families; it no longer owns a span
plane. The per-horizon ``horizon_span`` payload contract (``build_span`` /
``guard_span`` / ``HORIZON_SPAN_SCHEMA_VERSION``) and its Redis/Postgres
read-write pair were retired: ``publish_horizon_span`` and
``insert_horizon_span`` had zero production callers while an operator surface
(``--span``) read them, so the plane was removed end-to-end rather than left
to answer "no data" forever. The ``horizon_span`` table from alembic 0013 is
retained as dead storage; nothing in the runtime reads or writes it.

What remains is what other modules actually import: the frozen horizon names,
the fit-status family, and the horizon→regime classifier.

Vocabulary mirrors the deterministic plane exactly:

* native ``1s/5s/30s/60s`` — fitted directly
* long   ``15m/1h/4h``    — long-horizon bridge
"""

from __future__ import annotations

from market_service.runtime.horizons import (
    HORIZON_NAMES, NATIVE_HORIZON_NAMES, SPAN_HORIZON_NAMES,
)

# Phase S-2/S-4: the span horizons resolve to the charter owner's name
# vocabulary (identity, not a local copy).
NATIVE_HORIZONS: tuple[str, ...] = NATIVE_HORIZON_NAMES
LONG_HORIZONS: tuple[str, ...] = HORIZON_NAMES
SPAN_HORIZONS: tuple[str, ...] = SPAN_HORIZON_NAMES

STATUSES: frozenset[str] = frozenset({"validated", "provisional", "insufficient"})


def regime(horizon: str) -> str | None:
    """native | long_horizon | None (None = unsupported)."""
    if horizon in NATIVE_HORIZONS:
        return "native"
    if horizon in LONG_HORIZONS:
        return "long_horizon"
    return None


def is_supported(horizon: str) -> bool:
    return horizon in SPAN_HORIZONS


__all__ = [
    "LONG_HORIZONS",
    "NATIVE_HORIZONS",
    "SPAN_HORIZONS",
    "STATUSES",
    "is_supported",
    "regime",
]
