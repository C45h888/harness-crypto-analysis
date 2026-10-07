"""Horizon spans — per-horizon deterministic blocks (pure module).

No I/O. Vocabulary mirrors the deterministic plane exactly:

* native ``1s/5s/30s/60s`` — fitted directly (``extrapolated: false``)
* long   ``15m/1h/4h``    — long-horizon bridge (``extrapolated: true``)

Anything else is ``unsupported`` — recorded, never guessed (same rule as
``task_directive``). Each span is self-describing: its own status,
coverage, and run back-pointer. Uncomputable = ``insufficient`` + nulls.
"""

from __future__ import annotations

import time
from typing import Any

HORIZON_SPAN_SCHEMA_VERSION = 1

NATIVE_HORIZONS: tuple[str, ...] = ("1s", "5s", "30s", "60s")
LONG_HORIZONS: tuple[str, ...] = ("15m", "1h", "4h")
SPAN_HORIZONS: tuple[str, ...] = NATIVE_HORIZONS + LONG_HORIZONS

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


def build_span(
    symbol: str,
    horizon: str,
    *,
    run_id: str | None = None,
    status: str = "insufficient",
    fit: dict[str, Any] | None = None,
    distribution: dict[str, Any] | None = None,
    bridge: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    decay: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Build one span payload. Returns (payload, error).

    Unsupported horizon → (None, reason). Long horizons without a bridge
    note are refused the same way — a bridge block without stated
    assumptions is a guess, not a projection.
    """
    reg = regime(horizon)
    if reg is None:
        return None, (
            f"horizon {horizon!r} outside the span vocabulary "
            f"(native {list(NATIVE_HORIZONS)}, long {list(LONG_HORIZONS)})"
        )
    if status not in STATUSES:
        return None, f"status {status!r} not in {sorted(STATUSES)}"
    if reg == "long_horizon" and status != "insufficient" and not bridge:
        return None, (
            f"long-horizon span {horizon!r} requires a bridge block "
            "(sigma scaling + skill decay, stated — never bare numbers)"
        )
    return {
        "schema_version": HORIZON_SPAN_SCHEMA_VERSION,
        "run_id": run_id,
        "symbol": symbol.upper(),
        "horizon": horizon,
        "regime": reg,
        "extrapolated": reg == "long_horizon",
        "fit": fit,
        "distribution": distribution,
        "bridge": bridge,
        "coverage": coverage or {},
        "decay": decay or {},
        "status": status,
        "computed_at_ms": int(time.time() * 1000),
        "errors": list(errors or []),
    }, None


def guard_span(payload: Any) -> dict[str, Any] | None:
    """Schema-version guard. None passes through; mismatch raises."""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("horizon span payload is not a JSON object")
    version = payload.get("schema_version")
    if version != HORIZON_SPAN_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported horizon span schema version: {version!r} "
            f"(reader is on {HORIZON_SPAN_SCHEMA_VERSION})"
        )
    return payload


__all__ = [
    "HORIZON_SPAN_SCHEMA_VERSION",
    "LONG_HORIZONS",
    "NATIVE_HORIZONS",
    "SPAN_HORIZONS",
    "STATUSES",
    "build_span",
    "guard_span",
    "is_supported",
    "regime",
]
