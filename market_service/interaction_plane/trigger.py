"""Inference trigger — the ONLY write path in the interaction plane."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from market_service.interaction_plane import stores as _stores


def parse_scenario(
    target: str | None, horizon: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate --target/--horizon into a scenario dict. Returns (scenario, error)."""
    if target is None:
        return None, None
    try:
        target_dec = Decimal(str(target))
    except Exception:
        target_dec = None
    if target_dec is None or target_dec <= 0:
        return None, f"unparseable --target: {target!r}"
    return {"target_price": str(target_dec), "horizon": horizon}, None


async def trigger_inference(
    symbol: str,
    *,
    force: bool = False,
    task: str | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fire ONE task-directed cycle via the canonical runner."""
    from market_service.nooa_harness.inference_runner import run_inference_once

    return await run_inference_once(
        symbol.upper(), venue=_stores.venue(),
        force=force, task=task, scenario=scenario,
    )


__all__ = ["parse_scenario", "trigger_inference"]
