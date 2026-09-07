"""INFERENCE PLANE — the OO agent's single read seam into the shared math.

This module replaces the inference plane's old habit of importing the
interpretation-plane pipeline directly (``from ... import pipeline as
pipeline_mod`` in ``inference.dispatch_market_group``). The agent's
``market.group`` tool now calls exactly one function here, and this function:

* uses the CALLER's Redis store — the engine's own injected connection,
  never a second pool built from a re-read of the environment;
* threads the operator's full ``Settings`` into the deterministic core
  (depth levels, wall tier config, scorecard weights), so a group computed
  through the inference plane is IDENTICAL to the same group computed
  through the interpretation plane — the 1.1/1.3/C3 semantic divergence
  (depth=20 fallback, missing tier config, missing wall history) is gone;
* reads the cross-cycle wall ledger from the SAME injected store for the
  wall group (Redis live projection; never opens a Postgres connection);
* NEVER persists, NEVER fetches Binance derivatives, NEVER touches the
  interpretation plane's envelope/persistence surface.

The one-directional import rule this file encodes:

    composition  <-  pipeline_inference   (inference plane reads the math)
    composition  <-  pipeline_interpretation (interpretation plane reads the math)
    pipeline_inference -/-X pipeline_interpretation (planes never import each other)
"""

from __future__ import annotations

from typing import Any

from market_service.config import Settings
from market_service.runtime.redis_store import RedisRuntimeStore

from market_service.calculations import composition

__all__ = ["run_inference_group"]


async def run_inference_group(
    store: RedisRuntimeStore,
    settings: Settings,
    symbol: str,
    group: str,
    *,
    window_minutes: int = 15,
) -> dict[str, Any]:
    """Run one calculation-model group with the engine's own store + settings.

    Returns ``{"group", "window_minutes", "calculations", "analysis"}`` —
    the same section semantics as ``composition.GROUP_MAP``. No persistence, no
    Binance touch, no second connection pool.
    """
    if group not in composition.GROUP_MAP:
        raise ValueError(
            f"unknown calculation group: {group!r}; "
            f"allowed: {sorted(composition.GROUP_MAP)}"
        )

    # The store is injected — this function NEVER constructs one. That is
    # the boundary: the engine owns the connection lifecycle, the tool owns
    # the read.
    evidence = await composition.read_raw_window(store, symbol.upper(), window_minutes)

    calc_sections, anal_sections = composition.sections_for_groups((group,))
    anal_sections, calc_sections = composition.resolve_analysis_sections(
        anal_sections, calc_sections,
    )
    calc_sections = composition.resolve_calc_sections(calc_sections)

    depth = settings.depth_levels
    window_s = window_minutes * 60
    calculations = composition.run_calculations(
        evidence, depth, window_s, sections=calc_sections,
    )

    prior_walls: dict[float, float] = {}
    prior_cycle_ts: str | None = None
    if "wall_migration" in (anal_sections or set()):
        # Same live projection the interpretation plane records to; the
        # durable Postgres fallback is the interpretation plane's job.
        history = await store.read_wall_history(symbol.upper())
        prior_walls, prior_cycle_ts = composition.accumulate_prior_walls(history)

    analysis = composition.run_analysis(
        evidence, calculations,
        prior_walls=prior_walls or None,
        prior_cycle_ts=prior_cycle_ts,
        depth=depth,
        sections=anal_sections,
        tier_config=composition.resolve_tier_config(settings),
        scorecard_weights=composition.resolve_scorecard_weights(settings),
    )

    return {
        "group": group,
        "window_minutes": window_minutes,
        "depth_levels": depth,
        "calculations": calculations,
        "analysis": analysis,
    }
