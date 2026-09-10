"""Capability registry — the engine's bounded authority over supporting modules.

Every dispatch is exercised ONLY through named, scope-validated
capabilities. Each dispatch produces an audit log entry that lands in the
artifact's ``capability_log``, so every artifact is self-documenting about
how its deterministic state was produced. Out-of-scope requests are denied
before any module runs.

Moved verbatim from the inference.py monolith (decomposition Phase 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CapabilityDenied(ValueError):
    """A capability dispatch was refused by scope or quality validation."""


@dataclass(frozen=True)
class Capability:
    """One named, scope-bounded supporting-module dispatch surface."""

    name: str
    description: str
    allowed_symbols: frozenset[str]
    allowed_venues: frozenset[str]

    def validate_scope(self, symbol: str, venue: str) -> None:
        if symbol.upper() not in self.allowed_symbols:
            raise CapabilityDenied(
                f"capability {self.name!r} denied: symbol {symbol} outside "
                f"bounded scope {sorted(self.allowed_symbols)}"
            )
        if venue not in self.allowed_venues:
            raise CapabilityDenied(
                f"capability {self.name!r} denied: venue {venue} outside "
                f"bounded scope {sorted(self.allowed_venues)}"
            )


# Frozen initial scope — extended for SOL-USDT perps (per/user request).
# Microstructure capture remains spot-only (event-level tape), but
# calculation modules (substrate tools/market.read) are venue-agnostic via raw poller
# which fetches spot + USD-M perps. This lets inference validate SOL perps
# statistically even when micro evidence is spot-derived.
_INITIAL_SYMBOLS = frozenset({"BTCUSDT", "SOLUSDT", "ETHUSDT"})
_INITIAL_VENUES = frozenset({"spot", "perps", "perp", "usdm", "futures"})

CAPABILITIES: dict[str, Capability] = {
    "redis.read_capture_status": Capability(
        name="redis.read_capture_status",
        description="Read the isolated microstructure capture status object.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
    "redis.read_events": Capability(
        name="redis.read_events",
        description="Read best-quote transition events from the capture ledger.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
    "fitting.replay": Capability(
        name="fitting.replay",
        description="Deterministically replay events into OFI intervals.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
    "fitting.assemble_evidence": Capability(
        name="fitting.assemble_evidence",
        description="Run the deterministic beta/c/lambda fitter over replayed intervals.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    ),
}


def capability_log_entry(
    name: str, scope: dict[str, Any], result: str, *, detail: Any = None,
) -> dict[str, Any]:
    """One audit-trail row for the artifact's ``capability_log``."""
    entry: dict[str, Any] = {"capability": name, "scope": scope, "result": result}
    if detail is not None:
        entry["detail"] = detail
    return entry


# Registry additions for the T2 read tools (T1 already registered in Pass A).
_MARKET_TOOLS: dict[str, Capability] = {
    name: Capability(
        name=name,
        description=description,
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    )
    for name, description in {
        "redis.read_intervals": "Read completed OFI interval rows from the capture ledger.",
        "redis.read_evidence": "Read the latest immutable MicrostructureEvidence projection.",
        "market.read": "Read the latest collated market run from Redis. Modes: snapshot (bounded headline view, default), inventory (section keys + snapshot), full (raw payload deep-dive).",
        "market.read_derivatives": "Read the cached derivative evidence (funding, OI, cross-asset).",
        "market.read_keystone_history": "Read the bounded keystone cross-cycle ledger.",
        "market.read_wall_history": "Read the bounded wall cross-cycle ledger.",
        "substrate.read": "Read the always-fresh substrate worker projections (snapshot over all workers, or one substrate). Compact by default, full payloads on mode=full. Missing workers are available:false, never errors.",
        "substrate.invoke": "Invoke one substrate worker for a single bounded fire-tick (never a loop). Cooldowns still gate inside the core; reports fired/trigger/dormant per worker.",
        "calc.ofi_intervals": "Deterministic OFI per interval: sum e_n in [t_{k-1},t_k) — clock-bound, no AD. Paper Cont eq OFI_k.",
        "calc.ad_average": "Deterministic AD per block: event-average (qB+qA)/2 — separate from OFI, needs tick_size. Paper AD_i.",
        "calc.observation_build": "Join OFI intervals + AD blocks + mid → PriceImpactObservation[] (ΔP ticks vs OFI), quality filtered.",
        "calc.fit_price_impact": "OLS ΔP_k = α + β·OFI_k (HC0 SE) — returns PriceImpactFit, status trichotomy. Takes observations, not raw intervals.",
        "calc.fit_depth_scaling": "Log-log ln β = ln c - λ ln AD across blocks — needs ≥3 distinct AD_i, derived diagnostic only.",
        "calc.derived_diagnostic": "NUMERIC derived ΔP (alias: calc.price.delta): pass ofi (else latest interval OFI) → route A ΔP=α+β·OFI with 95% band + route B depth-scaled when c/λ exist. Refuses on insufficient fits. Heteroskedastic ν·OFI — diagnostic, not prediction.",
        "calc.scenario.evaluate": "SCENARIO price-target evaluation (interaction plane): pass target_price + horizon 15m|1h|4h → required horizon flow OFI_req=(Δ−n·α)/β vs empirical rolling-sum OFI distribution at that horizon → direction-matched exceedance + SE-band range + route-B cross-check. Current price resolved inside the tool from market.read (never agent-supplied). Refuses on insufficient fits, β≈0, missing price, thin tapes.",
        "memory.recall_paper": "Recall Cont-Kukanov-Stoikov paper facts from real MemoryNode (kind=fact, paper-kb session) — not prompt.",
    }.items()
}
CAPABILITIES.update(_MARKET_TOOLS)

# T3 — substrate worker plane: one capability per worker tool plus the
# snapshot reader and the generic invoker (explicit registry, same frozen
# scope as market.* — each worker invocation audits under its own name).
_SUBSTRATE_WORKERS = (
    "anchors", "density", "delta", "ladders", "large_print",
    "migration", "oi", "signals", "tape", "technicals", "tiers",
    "volume_profile",
)
CAPABILITIES.update({
    f"substrate.{worker}": Capability(
        name=f"substrate.{worker}",
        description=f"Invoke the {worker} substrate worker for a single "
                    f"bounded fire-tick (never a loop); reports fired/trigger.",
        allowed_symbols=_INITIAL_SYMBOLS,
        allowed_venues=_INITIAL_VENUES,
    )
    for worker in _SUBSTRATE_WORKERS
})
