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
        """Format gate: well-formed symbol, known venue vocabulary.

        This does NOT decide whether the symbol is captured — liveness is
        enforced at the dispatch seams (wake, ``run_inference_once``, and
        each tool's live control-key check) via ``validate_live_scope``.
        A static set cannot know a runtime re-point, so it must never be
        the authority on what is captured.
        """
        if not str(symbol or "").strip():
            raise CapabilityDenied(
                f"capability {self.name!r} denied: symbol is required"
            )
        if venue not in self.allowed_venues:
            raise CapabilityDenied(
                f"capability {self.name!r} denied: venue {venue} outside "
                f"bounded scope {sorted(self.allowed_venues)}"
            )

    def validate_live_scope(
        self, symbol: str, active_symbols: tuple[str, ...] | list[str] | None,
    ) -> None:
        """Liveness gate: refuse symbols nothing captures.

        ``active_symbols`` is the live resolved set (Redis control key >
        env). ``None``/empty means "unknown — defer to the wake guard"
        so a store-less probe never invents a refusal.
        """
        if not active_symbols:
            return
        if symbol.upper() not in {s.upper() for s in active_symbols}:
            raise CapabilityDenied(
                f"capability {self.name!r} denied: symbol_not_captured: "
                f"{symbol} is not in the live active set "
                f"({', '.join(active_symbols)}) — refusing before any work"
            )


# Static format bound for the capability gates. Liveness ("is this symbol
# captured right now?") is NOT decided here — the wake guard, the
# inference runner, and each tool's live control-key check own that, since
# a static set cannot track a runtime re-point of the poller control key.
_INITIAL_SYMBOLS = frozenset({"BTCUSDT", "SOLUSDT", "ETHUSDT"})
_INITIAL_VENUES = frozenset({"spot", "perps", "perp", "usdm", "futures"})

CAPABILITIES: dict[str, Capability] = {
    "ns.read": Capability(
        name="ns.read",
        description="Namespace-addressed live read of one configured Redis family (verbatim pared doc + key/age_ms/source).",
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
        "ns.read": "Namespace-addressed live read (verbatim pared doc + key/age_ms/source). Agents read micro/derivatives/substrate/analysis/inference families through this one tool; count bounds streams.",
        "calc.ofi_intervals": "Deterministic OFI per interval: sum e_n in [t_{k-1},t_k) — clock-bound, no AD. Paper Cont eq OFI_k.",
        "calc.ad_average": "Deterministic AD per block: event-average (qB+qA)/2 — separate from OFI, needs tick_size. Paper AD_i.",
        "calc.observation_build": "Join OFI intervals + AD blocks + mid → PriceImpactObservation[] (ΔP ticks vs OFI), quality filtered.",
        "calc.fit_price_impact": "OLS ΔP_k = α + β·OFI_k (HC0 SE) — returns PriceImpactFit, status trichotomy. Takes observations, not raw intervals.",
        "calc.fit_depth_scaling": "Log-log ln β = ln c - λ ln AD across blocks — needs ≥3 distinct AD_i, derived diagnostic only.",
        "calc.derived_diagnostic": "NUMERIC derived ΔP (alias: calc.price.delta): pass ofi (else latest interval OFI) → route A ΔP=α+β·OFI with 95% band + route B depth-scaled when c/λ exist. Refuses on insufficient fits. Heteroskedastic ν·OFI — diagnostic, not prediction.",
        "calc.scenario.evaluate": "SCENARIO price-target evaluation (interaction plane): pass target_price + horizon 15m|1h|4h → required horizon flow OFI_req=(Δ−n·α)/β vs empirical rolling-sum OFI distribution at that horizon → direction-matched exceedance + SE-band range + route-B cross-check. Current price resolved inside the tool from ns.read (never agent-supplied). Refuses on insufficient fits, β≈0, missing price, thin tapes.",
        "memory.remember": "Write one of the AGENT's OWN observations into its task-segment memory. No engine disposition: kind/size/segment hygiene only.",
        "memory.recall": "Read back the AGENT's own task-segment memories (kind/query filtered, bounded).",
        "memory.forget": "Tombstone one of the agent's own memories (audit row survives).",
        "memory.stats": "Per-kind counts for the agent's own memory session.",
        "calc.feature.build": "Track D forward feature vector xt-v2: Decimal OFI/AD/Dmu/spread/OBI/skew/CVD promotion with def_versions + input_hash. Floats rejected. Unproven field = NULL.",
        "calc.forward.join": "Track D forward join Y(h)=P_{t+h}-P_t at event grain (1s/5s/30s/60s) with per-horizon exclusion log. Gaps never bridged.",
        "calc.forward.fit": "Track D per-horizon multivariate OLS Y(h)~X (Decimal, time-ordered 70/30 OOS) with univariate Cont comparator. Trichotomy gated.",
        "calc.forward.distribution": "Track D conditional distribution: expected Y(h) + 95% PI + P(>0)/P(>theta). Normal-approx stated. Miscalibrated/insufficient -> NULL/refusal.",
        "calc.forward.scenario": "Track D horizon-native P(T)/P(S) curves with bands from forward fits. Legacy calc.scenario.evaluate retained as flow-requirement.",
        "calc.forward.forecast": "Canonical deterministic forward ForecastResult: schema-checked features, train-only OOS evaluation, calibration-gated probabilities, native horizon metadata, and preserved Route A/Route B disagreement.",
        "calc.hypothesis.test": "Track D formal hypothesis test (independent module, post-fit only): effect/SE/CI/p + n/split/OOS + multiplicity. p<0.05 is evidence, never execution predicate.",
        "calc.events.absorption": "Track D typed Absorption detector (Decimal, event grain) + replay-agreement. No institutional attribution.",
        "calc.events.walls": "Track D 10-field wall lifecycle detector (Decimal, event grain) + replay-agreement. No institutional attribution.",
        "calc.decay.report": "Track D population + skill-decay report: per-horizon OOS skill, regime splits, finalized horizon set. Nulls are results.",
        "calc.discipline.audit": "Track D discipline lock: 10-check audit (leakage/estimation/time-order/horizon/regime/baseline/cost/calibration/multiplicity/pins) -> go/no-go memo. Phase-12 opens only on go.",
    }.items()
}
CAPABILITIES.update(_MARKET_TOOLS)
