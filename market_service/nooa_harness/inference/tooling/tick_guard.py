"""Tick-size guard — frozen instrument resolution (extracted verbatim from dispatch.py monolith)."""
from __future__ import annotations

from typing import Any

from ..capability import CAPABILITIES, CapabilityDenied, capability_log_entry

def _frozen_tick(symbol: str, venue: str, provided: str | None = None) -> str:
    """Resolve the registered tick and reject caller mismatches."""
    from decimal import Decimal
    from market_service.microstructure.tick import resolve_tick_size

    expected = resolve_tick_size(symbol, venue)
    if provided is not None and Decimal(str(provided)) != expected:
        raise ValueError(
            f"tick_size mismatch for {(symbol.upper(), venue)}: "
            f"expected {expected}, received {provided}"
        )
    return str(expected)


def _resolved_tick_for_dispatch(symbol: str, venue: str, args: dict[str, Any]) -> str:
    """Supply a candidate tick; the tool body owns structured refusal."""
    from decimal import Decimal
    from market_service.microstructure.tick import resolve_tick_size

    provided = args.get("tick_size")
    try:
        expected = resolve_tick_size(symbol, venue)
    except (KeyError, ValueError):
        return str(provided) if provided is not None else ""
    if provided is not None:
        try:
            if Decimal(str(provided)) != expected:
                return str(provided)
        except Exception:
            return str(provided)
    return str(expected)


def _legacy_tick(data: dict[str, Any]) -> Any:
    """Resolve historical tick metadata without guessing an instrument."""
    from market_service.microstructure.tick import resolve_tick_size

    return resolve_tick_size(
        str(data.get("symbol") or ""), str(data.get("venue") or "")
    )

