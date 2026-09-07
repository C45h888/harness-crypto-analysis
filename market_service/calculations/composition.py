"""Composition root for the DECOMPOSED calculation layer.

Option-A decomposition (2026-09-06, replaces ``nooa_harness/bedrock.py``):
the substrate packages (``market_service.calculations.substrates.*`` and
``market_service.analysis.*``) are the CORE OWNERS of their math; this
module is the single composition root that wires them together — the
SUBSTRATE_GRAPH, GROUP_MAP, section resolvers, ``run_calculations`` /
``run_analysis`` and every evidence-marshaling adapter.

Moved verbatim from bedrock.py (move-don't-rewrite). Purity discipline
(tests/test_substrate_graph.py) still holds: substrates never import each
other or analysis; analysis never imports calculations at module scope;
composition happens ONLY here (and in analysis/market.py, the standalone
analyzer). Both runtime planes (pipeline_interpretation /
pipeline_inference) import this module — a rule change lands here once and
BOTH planes move together.

Discipline:
- Pure computation over injected data. Nothing here constructs a
  RedisRuntimeStore/PostgresRuntimeStore or opens a Binance client; the
  store is always passed in by the caller.
- Null means not-provided — never substitute zero.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from market_service.analysis.auction import (
    auction_verdict,
    flow_persistence,
    initiated_flow,
    microprice,
)
from market_service.analysis.demand import (
    bind_flow_summary,
    decompose_demand,
    demand_verdict,
    macro_climate,
)
from market_service.analysis.oi import (
    find_walls,
    oi_implied_value,
    oi_inflow_outflow,
    oi_weighted_contracts,
    wall_break_assessment,
)
from market_service.analysis.path_absorption import (
    fuel_ratio as path_fuel_ratio,
)
from market_service.analysis.path_absorption import (
    simulated_ascent,
    simulated_descent,
)
from market_service.analysis.regime import regime_verdict
from market_service.analysis.wall_migration import (
    default_wall_band,
    densest_clusters,
    keystone_holds_scorecard,
    keystone_wall_balance,
    level_absorption,
    wall_delta,
    wall_trap_assessment,
)
from market_service.analysis.wall_migration import (
    fuel_ratio as wall_fuel_ratio,
)
from market_service.calculations.substrates.anchors import (
    compute_round_anchors,
    derive_round_anchors,
)
from market_service.calculations.substrates.delta import (
    delta_state as _delta_state_fn,
)
from market_service.calculations.substrates.delta import (
    delta_variable,
)
from market_service.calculations.substrates.density import (
    find_keystone,
    keystone_trade_intensity,
    significant_levels,
    top_density_windows,
    zone_buy_sell,
    zone_ratio_grid,
)
from market_service.calculations.substrates.ladders import (
    absorption_ladder,
    ask_wall_ladder,
    keystone_bid_stack,
)
from market_service.calculations.substrates.large_print import (
    seller_aggression_classify,
    tiered_large_flow,
)
from market_service.calculations.substrates.migration import (
    hourly_keystone_migration,
)
from market_service.calculations.substrates.signals import deterministic_signals
from market_service.calculations.substrates.tape import (
    bucketed_cvd,
    cvd_series_corr,
    microprice_skew_bps,
    spot_turnover_share,
    summarize,
)
from market_service.calculations.substrates.technicals import (
    atr_pct_from_klines,
    ema_series,
    trend_drift,
    trend_slope,
)
from market_service.calculations.substrates.tiers import (
    TierConfig,
    bid_tier_balance,
    compute_bid_tiers_usd,
    mega_at_keystone,
)
from market_service.calculations.substrates.volume_profile import (
    build_volume_profile,
    volume_profile_summary,
)
from market_service.config import Settings, default_depth_levels
from market_service.runtime.redis_store import RedisRuntimeStore

from market_service.nooa_harness import contracts as C

log = logging.getLogger(__name__)

# The canonical composition binding for the analysis layer's flow-summary
# port: every analysis module that consumes calculation output via the
# injected provider (e.g. ``decompose_demand``) resolves to the ``tape``
# substrate's ``summarize`` when running inside this runtime.
bind_flow_summary(summarize)

# ---------------------------------------------------------------------------
# Window config
# ---------------------------------------------------------------------------

WINDOW_MINUTES_MAP: dict[str, int] = {
    "15m": 15,
    "1h": 60,
    "4h": 240,
}


def _resolve_tier_config(settings: Settings | None) -> TierConfig | None:
    """Translate ``Settings.wall_tier_config`` into a ``TierConfig``.

    Returns ``None`` when ``settings`` is not provided (so callers like
    the test path use the legacy raw-qty thresholds). When settings
    carry the default USD buckets this returns a fully populated
    ``TierConfig``; operators override via ``WALL_TIER_CONFIG``.
    """
    if settings is None:
        return None
    raw = settings.wall_tier_config or {}
    return TierConfig(
        mega_usd=float(raw.get("mega_usd", 250_000.0)),
        large_usd=float(raw.get("large_usd", 50_000.0)),
        medium_usd=float(raw.get("medium_usd", 10_000.0)),
    )


def _resolve_scorecard_weights(settings: Settings | None) -> dict[str, float] | None:
    """Return the scorecard weight override dict, or ``None`` to keep defaults."""
    if settings is None:
        return None
    weights = dict(settings.wall_scorecard_weights or {})
    if not weights:
        return None
    return weights


# ---------------------------------------------------------------------------
# Calculation-model groups — segregated command surface (Pass 3 pivot)
#
# The monolithic run-cycle stays as the canonical persisted envelope path
# (--analyze). The calculation-model commands (--wall / --flow /
# --structure / --positioning) run ONLY the sections each analytical
# domain needs — no envelope, no persistence, focused output.
#
# GROUP_MAP is the single source of truth for which calculation and
# analysis sections belong to each domain group. The section names here
# are exactly the keys produced by run_calculations / run_analysis.
# ---------------------------------------------------------------------------

GROUP_MAP: dict[str, dict[str, tuple[str, ...]]] = {
    "wall": {
        "calculations": ("orderbook",),
        "analysis": ("wall_migration", "path_absorption", "oi"),
    },
    "flow": {
        "calculations": ("flow", "bucketed_cvd", "correlation", "technical"),
        "analysis": ("demand", "auction", "delta"),
    },
    "structure": {
        "calculations": ("volume_profile", "technical"),
        "analysis": ("regime", "stage"),
    },
    "positioning": {
        "calculations": (),
        "analysis": ("oi",),
    },
}

# ---------------------------------------------------------------------------
# Substrate composition graph — the DECOMPOSED calculation layer.
#
# The calculation modules are decomposed into individual substrates
# (``calculations.substrates``). A substrate is one single-responsibility
# pure-math module that NEVER imports another substrate (or any analysis
# module). Composition happens HERE, in the orchestrator, which maps every
# calculation/analysis section to the substrate that owns it and the inputs
# it consumes.
#
# SUBSTRATE_GRAPH is the declared single source of truth for section →
# substrate ownership (GROUP_MAP declares section → domain group). A rule
# change lands in one substrate; a composition change lands here. The
# contract suite (tests/test_substrate_graph.py) asserts this graph stays
# consistent with the real substrate package and with GROUP_MAP.
# ---------------------------------------------------------------------------

SUBSTRATE_GRAPH: dict[str, dict[str, Any]] = {
    # --- calculation sections ---
    "flow":           {"substrates": ("tape",),
                        "consumes": ("evidence.spot.trades_normalized", "evidence.futures.trades_normalized",
                                      "evidence.spot.order_book", "evidence.futures.order_book")},
    "bucketed_cvd":   {"substrates": ("tape",),
                        "consumes": ("evidence.spot.trades_normalized", "evidence.futures.trades_normalized")},
    "correlation":    {"substrates": ("tape",),
                        "consumes": ("calc.bucketed_cvd",)},
    "turnover":       {"substrates": ("tape",),
                        "consumes": ("calc.flow",)},
    "signals":        {"substrates": ("signals",),
                        "consumes": ("calc.flow", "evidence.futures.open_interest")},
    "orderbook":      {"substrates": ("density", "ladders", "migration", "anchors"),
                        "consumes": ("evidence.spot.order_book", "evidence.futures.order_book",
                                      "evidence.futures.trades_normalized")},
    "volume_profile": {"substrates": ("volume_profile",),
                        "consumes": ("evidence.futures.trades_normalized",)},
    "technical":      {"substrates": ("technicals", "large_print"),
                        "consumes": ("evidence.futures.trades_normalized", "evidence.futures.klines")},
    # --- analysis sections (consume calculation output, never import it) ---
    "auction":        {"substrates": ("analysis.auction",),
                        "consumes": ("evidence.futures.order_book", "evidence.futures.trades_normalized",
                                      "evidence.futures.funding")},
    "oi":             {"substrates": ("positioning",),
                        "consumes": ("evidence.futures.order_book", "evidence.futures.open_interest",
                                      "evidence.futures.oi_history", "evidence.futures.top_ls",
                                      "evidence.futures.global_ls")},
    "wall_migration": {"substrates": ("analysis.wall_migration", "tiers", "anchors", "ladders", "density"),
                        "consumes": ("calc.orderbook", "evidence.futures.order_book",
                                      "evidence.futures.taker_buy_sell", "evidence.futures.oi_history",
                                      "evidence.futures.top_ls", "evidence.futures.global_ls",
                                      "evidence.futures.trades_normalized", "evidence.futures.klines")},
    "path_absorption": {"substrates": ("analysis.path_absorption",),
                         "consumes": ("evidence.futures.order_book",)},
    "demand":         {"substrates": ("analysis.demand",),
                        "consumes": ("evidence.spot.*", "evidence.futures.*", "evidence.cross_asset"),
                        "note": "flow summaries arrive via the injected flow_provider (tape substrate)"},
    "regime":         {"substrates": ("analysis.regime",),
                        "consumes": ("calc.flow", "evidence.futures.*"),
                        "note": "consumes calc.flow output (tape substrate)"},
    "stage":          {"substrates": ("analysis.stage",),
                        "consumes": ("evidence.futures.klines", "calc.orderbook")},
    "delta":          {"substrates": ("delta",),
                        "consumes": ("evidence.futures.order_book", "evidence.futures.taker_buy_sell")},
}


def substrate_for(section: str) -> str | None:
    """The substrate(s) that own a calculation/analysis section (composition graph)."""
    entry = SUBSTRATE_GRAPH.get(section)
    return "/".join(entry["substrates"]) if entry else None


def section_inputs(section: str) -> tuple[str, ...]:
    """Declared inputs consumed by a section (for graph audits)."""
    entry = SUBSTRATE_GRAPH.get(section)
    return entry["consumes"] if entry else ()


def _substrate_provenance(ran_sections: set[str]) -> dict[str, tuple[str, ...]]:
    """Map section ids that actually ran to their owning substrate(s).

    Pulled from SUBSTRATE_GRAPH — the single source of truth for section →
    substrate ownership (GROUP_MAP declares section → domain group). Only
    sections that EXECUTED appear, so provenance never claims work that was
    skipped (e.g. ``run_calculations(sections={...})`` emits provenance only
    for the requested sections). The interpretation plane reads this map to
    explain which substrate produced each section of an envelope.
    """
    return {
        sec: tuple(SUBSTRATE_GRAPH[sec]["substrates"])
        for sec in sorted(ran_sections)
        if sec in SUBSTRATE_GRAPH
    }


# Calculation-section dependencies: a requested section pulls its
# prerequisites in automatically (turnover reads flow output; signals
# reads flow output + OI). Kept separate from GROUP_MAP so the group
# definitions stay domain-pure while dependency resolution stays generic.
_CALC_SECTION_DEPS: dict[str, tuple[str, ...]] = {
    "turnover": ("flow",),
    "signals": ("flow",),
}

# Analysis adapters that consume the calculations layer need the
# corresponding calculation sections present (regime reads calc flow;
# wall_migration reads calc orderbook). Auto-included on request.
_ANALYSIS_CALC_DEPS: dict[str, tuple[str, ...]] = {
    "regime": ("flow",),
    "wall_migration": ("orderbook",),
}


def resolve_calc_sections(sections: frozenset[str] | None) -> frozenset[str] | None:
    """Expand a requested calculation-section set with its prerequisites.

    ``None`` means "all sections" (the canonical full-cycle behavior) and
    passes through untouched. The expansion is transitive so a dependency
    of a dependency is also pulled in.
    """
    if sections is None:
        return None
    resolved = set(sections)
    frontier = list(resolved)
    while frontier:
        sec = frontier.pop()
        for dep in _CALC_SECTION_DEPS.get(sec, ()):
            if dep not in resolved:
                resolved.add(dep)
                frontier.append(dep)
    return frozenset(resolved)


def resolve_analysis_sections(
    sections: frozenset[str] | None,
    calc_sections: frozenset[str] | None,
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    """Expand analysis sections and merge their calculation prerequisites.

    Returns (analysis_sections, calc_sections). An analysis adapter that
    consumes the calculations layer auto-includes the calculation sections
    it reads (regime→flow, wall_migration→orderbook). ``None`` analysis
    sections means "all" and passes through.
    """
    if sections is None:
        return None, calc_sections
    calc = set(calc_sections) if calc_sections is not None else None
    for sec in sections:
        for dep in _ANALYSIS_CALC_DEPS.get(sec, ()):
            if calc is None:
                break  # all calculations already requested
            calc.add(dep)
    return sections, (frozenset(calc) if calc is not None else None)


def sections_for_groups(groups: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    """Flatten requested group names into (calc_sections, analysis_sections).

    Unknown group names raise ValueError so the CLI surfaces a clean error
    instead of silently returning empty output.
    """
    calc: set[str] = set()
    anal: set[str] = set()
    for g in groups:
        spec = GROUP_MAP.get(g)
        if spec is None:
            raise ValueError(f"unknown calculation group: {g!r}")
        calc.update(spec["calculations"])
        anal.update(spec["analysis"])
    return frozenset(calc), frozenset(anal)

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Step 1 — read raw evidence from Redis stream
# ---------------------------------------------------------------------------

# The raw-evidence window builder lives in runtime/raw_window.py so the
# substrate workers share the exact same evidence construction without
# importing the harness layer. Re-exported here under the historical name —
# zero behavior change for every caller.
from market_service.runtime.raw_window import build_raw_window as read_raw_window  # noqa: E402

# ---------------------------------------------------------------------------
# Step 2 — run calculations (same adapters as nodes/calculations.py)
# ---------------------------------------------------------------------------

def _spot_trades(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return (evidence.get("spot") or {}).get("trades_normalized") or []


def _fut_trades(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return (evidence.get("futures") or {}).get("trades_normalized") or []


def _spot_book(evidence: dict[str, Any]) -> dict[str, Any]:
    return (evidence.get("spot") or {}).get("order_book") or {}


def _fut_book(evidence: dict[str, Any]) -> dict[str, Any]:
    return (evidence.get("futures") or {}).get("order_book") or {}


def _strict(fn, *args, name: str, **kwargs):
    return C.strict_call(name, fn, *args, **kwargs)


def _run_section(name: str, builder, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return builder()
    except C.ContractViolation as violation:
        errors.append(C.contract_error_entry(violation))
        log.warning("[pipeline] %s contract violation: %s", name, violation.reason)
        return None


def _safe_last_price(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return None


def _enrich_fut_keystone(keystone: dict[str, Any], asks: list[list[float]]) -> dict[str, Any]:
    """Resolve the ``ask`` alias for the futures keystone result.

    ``find_keystone`` natively emits ``bid`` (its contract is a densest
    *bid*-window search). The one alias that remains adapter-side is ``ask``:
    the nearest ask at-or-above the keystone price — the first price sellers
    defend, which the briefing compares against buyer defence. The ask needs
    the futures asks list, which the calc layer's ``find_keystone`` never
    receives.

    Round-number anchors + mega-tier percentage are computed elsewhere
    (``_adapt_wall_migration``).
    """
    if not isinstance(keystone, dict):
        return keystone
    if "ask" not in keystone:
        bid = keystone.get("bid") or keystone.get("keystone")
        keystone["ask"] = None
        if bid is not None:
            try:
                bid_f = float(bid)
            except (TypeError, ValueError):
                bid_f = None
            if bid_f is not None and asks:
                # nearest ask at-or-above keystone price
                best = None
                for p, q in asks:
                    try:
                        pf = float(p)
                        qf = float(q)
                    except (TypeError, ValueError):
                        continue
                    if pf >= bid_f:
                        if best is None or pf < best[0]:
                            best = (pf, qf)
                if best is not None:
                    keystone["ask"] = best[0]
    return keystone


def run_calculations(
    evidence: dict[str, Any],
    depth: int,
    window: int,
    sections: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Run the same deterministic calculations as the old calculations node.

    ``sections`` (calculation-model groups, Pass 3): when ``None`` (default)
    every section runs — the canonical full-cycle behavior. When a
    frozenset of section names is given, only those sections (plus their
    auto-resolved prerequisites) execute and only they appear in the output
    ``calculations`` dict. This is what the segregated ``--wall`` /
    ``--flow`` / ``--structure`` / ``--positioning`` commands use to skip
    work they don't need.
    """
    errors: list[dict[str, Any]] = []
    resolved = resolve_calc_sections(sections)

    def want(name: str) -> bool:
        return resolved is None or name in resolved

    flow = _run_section("flow", lambda: {
        "spot_flow": _strict(summarize, _spot_trades(evidence), _spot_book(evidence),
                             depth_levels=depth, name="summarize"),
        "futures_flow": _strict(summarize, _fut_trades(evidence), _fut_book(evidence),
                               depth_levels=depth, name="summarize"),
    }, errors) or {} if want("flow") else {}

    # Bucketed CVD is computed ONCE and shared by the bucketed_cvd section
    # and the correlation section (which previously re-ran bucketed_cvd
    # twice inline — redundant deterministic work).
    want_cvd_series = want("bucketed_cvd") or want("correlation")
    spot_cvd_series = (
        _strict(bucketed_cvd, _spot_trades(evidence), window_s=window, name="bucketed_cvd")
        if want_cvd_series else None
    )
    fut_cvd_series = (
        _strict(bucketed_cvd, _fut_trades(evidence), window_s=window, name="bucketed_cvd")
        if want_cvd_series else None
    )

    bucketed = _run_section("bucketed_cvd", lambda: {
        "spot_bucketed_cvd": spot_cvd_series,
        "futures_bucketed_cvd": fut_cvd_series,
    }, errors) or {} if want("bucketed_cvd") else {}

    correlation = (
        _run_section("correlation", lambda: _strict(
            cvd_series_corr, spot_cvd_series, fut_cvd_series,
            window_s=window, name="cvd_series_corr",
        ), errors) if want("correlation") else None
    )

    # Orderbook
    fut_book = _fut_book(evidence)
    spot_book = _spot_book(evidence)
    fut_bids = C.require_list_of_pairs(fut_book.get("bids"), function="orderbook.*", where="futures.order_book.bids", max_items=depth)
    fut_asks = C.require_list_of_pairs(fut_book.get("asks"), function="orderbook.*", where="futures.order_book.asks", max_items=depth)
    spot_bids = C.require_list_of_pairs(spot_book.get("bids"), function="orderbook.*", where="spot.order_book.bids", max_items=depth)
    spot_asks = C.require_list_of_pairs(spot_book.get("asks"), function="orderbook.*", where="spot.order_book.asks", max_items=depth)
    last_price = _safe_last_price(fut_bids, fut_asks)

    orderbook: dict[str, Any] = {}
    if want("orderbook") and last_price is not None:
        def _orderbook_builder() -> dict[str, Any]:
            fut_keystone = _enrich_fut_keystone(
                _strict(find_keystone, fut_bids, last_price, 0.20, -0.30, -0.05, None, name="find_keystone"),
                fut_asks,
            )
            kz_price = fut_keystone.get("keystone") if isinstance(fut_keystone, dict) else None
            kz_tight = (fut_keystone.get("tight") or {}) if isinstance(fut_keystone, dict) else {}
            kz_wide = (fut_keystone.get("wide") or {}) if isinstance(fut_keystone, dict) else {}
            return {
                "fut_keystone": fut_keystone,
                "spot_keystone": _strict(find_keystone, spot_bids, last_price, 0.20, -0.30, -0.05, None, name="find_keystone"),
                "fut_top_density_bids": _strict(top_density_windows, fut_book, 0.5, "bid", 5, name="top_density_windows"),
                "fut_absorption_ladder": _strict(absorption_ladder, fut_bids, last_price, count=10, name="absorption_ladder"),
                "fut_significant_levels": _strict(significant_levels, fut_bids + fut_asks, 0.0, name="significant_levels"),
                "fut_microprice_skew_bps": _strict(microprice_skew_bps, spot_bids, spot_asks, name="microprice_skew_bps"),
                # Legacy port (deep_keystone.py:104-200): cumulative bid stack
                # around the keystone — below / at / above decomposition.
                "keystone_bid_stack": (
                    _strict(keystone_bid_stack, fut_bids, kz_price, name="keystone_bid_stack")
                    if kz_price is not None else None
                ),
                # Legacy port (deep_keystone.py:74-101 + keystone_scan.py:130-165):
                # trade-flow intensity at the keystone defence zone.
                "keystone_trade_intensity": (
                    _strict(keystone_trade_intensity, _fut_trades(evidence),
                            kz_tight.get("lo"), kz_tight.get("hi"),
                            kz_wide.get("lo"), kz_wide.get("hi"),
                            name="keystone_trade_intensity")
                    if kz_price is not None and kz_tight.get("lo") is not None
                       and kz_wide.get("hi") is not None else None
                ),
                # Legacy port (wall_analysis.py:83-112): 0.05-bucket ask ladder
                # above price with cumulative notional (default 3.00 reach).
                "ask_wall_ladder": _strict(ask_wall_ladder, fut_asks, last_price,
                                           None, 0.05, 60, name="ask_wall_ladder"),
                # Legacy port (long_term_flow.py hourly keystone): intra-window
                # keystone migration over the current trade window. At the default
                # 15m window this yields a single bucket (verdict FLAT — valid);
                # meaningful at --window 1h/4h.
                "hourly_keystone_migration": _strict(
                    hourly_keystone_migration, _fut_trades(evidence), 0.05, 0.20,
                    name="hourly_keystone_migration"),
            }
        orderbook = _run_section("orderbook", _orderbook_builder, errors) or {}

    # Volume profile
    def _volume_profile_builder() -> dict[str, Any]:
        # Build the profile ONCE — the previous code constructed it twice
        # (once for ``buckets``, once for ``summary``) with identical inputs.
        profile = _strict(build_volume_profile, _fut_trades(evidence), 0.05, name="build_volume_profile")
        return {
            "buckets": profile,
            "summary": _strict(volume_profile_summary, profile, name="volume_profile_summary"),
        }
    volume_profile = _run_section("volume_profile", _volume_profile_builder, errors) or {} if want("volume_profile") else {}

    # Technical
    def _technical_builder() -> dict[str, Any]:
        # 5m klines arrive only via the on-demand derivative fetch
        # (_merge_derivatives); the 5-second poller never carries them.
        # When absent, emit null with an explicit source marker instead of
        # silently computing EMAs on an empty series.
        kline_rows = (evidence.get("futures") or {}).get("klines") or []
        closes = [float(row[4]) for row in kline_rows
                  if isinstance(row, (list, tuple)) and len(row) >= 5]
        return {
            "emas": _strict(ema_series, closes, name="ema_series") if closes else None,
            "emas_source": "derivatives.klines_5m" if closes else None,
        # Legacy port (sol_deep_monitor.py:191-247): tiered large-print flow
        # (large/huge/whale) over trailing 5m/15m windows.
        "tiered_large_flow": _strict(tiered_large_flow, _fut_trades(evidence),
                                     name="tiered_large_flow"),
        # Legacy port (seller_wall_check.py:162-240): seller aggression
        # classification from big prints >= 100 SOL in the last 5 min.
        "seller_aggression": _strict(seller_aggression_classify, _fut_trades(evidence),
                                     name="seller_aggression_classify"),
        }
    technical = _run_section("technical", _technical_builder, errors) or {} if want("technical") else {}

    # Turnover
    spot_flow = flow.get("spot_flow") or {}
    fut_flow = flow.get("futures_flow") or {}
    spot_notional = (spot_flow.get("buy_notional_usd") or 0.0) + (spot_flow.get("sell_notional_usd") or 0.0)
    fut_notional = (fut_flow.get("buy_notional_usd") or 0.0) + (fut_flow.get("sell_notional_usd") or 0.0)
    turnover = _run_section("turnover", lambda: {
        "spot_turnover_share": _strict(spot_turnover_share, spot_notional, fut_notional, name="spot_turnover_share"),
    }, errors) or {} if want("turnover") else {}

    # Signals
    signal_inputs = {
        "spot_buy_share": spot_flow.get("buy_share"),
        "futures_buy_share": fut_flow.get("buy_share"),
        "spot_obi_top_n": spot_flow.get("obi"),
        "open_interest": ((evidence.get("futures") or {}).get("open_interest") or {}).get("open_interest"),
    }
    signals: list[dict[str, Any]] = []
    if want("signals"):
        try:
            signals = list(_strict(deterministic_signals, signal_inputs, None, name="deterministic_signals"))
        except C.ContractViolation as violation:
            errors.append(C.contract_error_entry(violation))

    # Build the calculations dict with only the sections that were requested
    # (or all of them when sections=None). Unrequested sections are omitted
    # entirely — downstream readers see no trace of them.
    all_calc: dict[str, Any] = {
        "flow": flow,
        "bucketed_cvd": bucketed,
        "correlation": correlation,
        "signal_inputs": signal_inputs,
        "signals": signals,
        "orderbook": orderbook,
        "turnover": turnover,
        "volume_profile": volume_profile,
        "technical": technical,
    }
    calc_out = {k: v for k, v in all_calc.items() if want(k)} if resolved is not None else all_calc

    return {
        "status": "degraded" if errors else "healthy",
        "errors": errors,
        "calculations": calc_out,
        "coverage_seconds": window,
        "substrate_provenance": _substrate_provenance(set(calc_out)),
    }

# ---------------------------------------------------------------------------
# Step 3 — run analysis (same adapters as nodes/analysis.py)
# ---------------------------------------------------------------------------

def _fut_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("futures") or {}


def _spot_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("spot") or {}


def _fut_book_e(evidence: dict[str, Any]) -> dict[str, Any]:
    return _fut_evidence(evidence).get("order_book") or {}


def _levels(book: dict[str, Any], depth: int, *, function: str) -> tuple[list[list[float]], list[list[float]]]:
    bids = C.require_list_of_pairs(book.get("bids"), function=function, where="order_book.bids", max_items=depth)
    asks = C.require_list_of_pairs(book.get("asks"), function=function, where="order_book.asks", max_items=depth)
    return bids, asks


def _last_price_e(bids: list[list[float]], asks: list[list[float]]) -> float | None:
    return _safe_last_price(bids, asks)


def _ticker_change(ticker: Any) -> dict[str, Any]:
    if not isinstance(ticker, dict):
        return {"change_pct": None}
    value = ticker.get("change_pct")
    if value is None:
        value = ticker.get("price_change_percent", ticker.get("priceChangePercent"))
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None
    return {**ticker, "change_pct": value}


def _bid_floors_from_significant_levels(
    bid_levels: list[list[float]],
    calc_significant_levels: list[dict] | None,
    price: float,
) -> list[float]:
    floors: list[float] = []
    if calc_significant_levels:
        for sl in calc_significant_levels:
            raw = sl.get("price") if isinstance(sl, dict) else None
            if raw is None:
                continue
            try:
                p = float(raw)
            except (TypeError, ValueError):
                continue
            if p <= 0:
                continue
            floors.append(p)
            if len(floors) >= 5:
                break
    if not floors and price > 0:
        floors = [price]
    return floors


def _single_cycle_wall_counts(asks: list[list[float]], direction_threshold: float = 1.15) -> tuple[int, int]:
    if not asks:
        return 0, 0
    built = 0
    eroded = 0
    prev_qty: float | None = None
    for _, qty in asks:
        try:
            q = float(qty)
        except (TypeError, ValueError):
            continue
        if prev_qty is not None and prev_qty > 0:
            if q >= prev_qty * direction_threshold:
                built += 1
            elif q <= prev_qty / direction_threshold:
                eroded += 1
        prev_qty = q
    return built, eroded


def run_analysis(
    evidence: dict[str, Any],
    calculations: dict[str, Any],
    *,
    prior_walls: dict[float, float] | None = None,
    prior_cycle_ts: str | None = None,
    depth: int | None = None,
    sections: frozenset[str] | None = None,
    tier_config: TierConfig | None = None,
    scorecard_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run the same deterministic analysis as the old analysis node.

    ``depth`` is the centralized canonical order-book depth (defaults to the
    config resolver) and is threaded into every adapter so wall/OI/path
    analyses scan the full configured book instead of a hard-coded slice.

    ``tier_config`` and ``scorecard_weights`` thread Phase 1.2 / Phase 2.4
    configuration through to ``_adapt_wall_migration``. When omitted, the
    legacy defaults (raw-qty tiers, equal-weight scorecard) are used so
    every existing call site keeps its previous behavior.

    ``sections`` (calculation-model groups, Pass 3): when ``None`` (default)
    every analysis adapter runs. When a frozenset of section names is given,
    only those adapters execute and only they appear in the output
    ``analysis`` dict. This is what the segregated ``--wall`` / ``--flow`` /
    ``--structure`` / ``--positioning`` commands use.
    """
    if depth is None:
        from market_service.config import default_depth_levels
        depth = default_depth_levels()
    errors: list[dict[str, Any]] = []

    def want(name: str) -> bool:
        return sections is None or name in sections

    calc_data = calculations.get("calculations") or {}
    orderbook_calc = calc_data.get("orderbook") or {}

    # Auction
    auction = _run_section("auction", lambda: _adapt_auction(evidence), errors) or {} if want("auction") else {}

    # Open interest
    oi = _run_section("oi", lambda: _adapt_oi(evidence, depth=depth), errors) or {} if want("oi") else {}

    # Wall migration
    pw = prior_walls or {}
    wall_migration = _run_section("wall_migration", lambda: _adapt_wall_migration(
        evidence, orderbook_calc.get("fut_significant_levels"), pw, prior_cycle_ts,
        orderbook=orderbook_calc, depth=depth,
        tier_config=tier_config, scorecard_weights=scorecard_weights,
    ), errors) or {} if want("wall_migration") else {}

    # Path absorption
    path_absorption = _run_section("path_absorption", lambda: _adapt_path_absorption(evidence, depth=depth), errors) or {} if want("path_absorption") else {}

    # Demand
    demand = _run_section("demand", lambda: _adapt_demand(evidence), errors) or {} if want("demand") else {}

    # Regime
    regime = _run_section("regime", lambda: _adapt_regime(evidence, calc_data), errors) or {} if want("regime") else {}

    # Stage
    stage = _run_section("stage", lambda: _adapt_stage(evidence), errors) or {} if want("stage") else {}

    # Delta — signed -2..+2 combo of wall imbalance + taker buy alignment.
    delta = _run_section("delta", lambda: _adapt_delta(evidence), errors) or {} if want("delta") else {}

    all_analysis: dict[str, Any] = {
        "auction": auction,
        "open_interest": oi,
        "wall_migration": wall_migration,
        "delta": delta,
        "path_absorption": path_absorption,
        "demand": demand,
        "regime": regime,
        "stage": stage,
    }
    # The OI adapter historically emits under ``open_interest`` while its
    # section id (GROUP_MAP, deps) is ``oi``. Filter by the section id, but
    # preserve the established output key for downstream readers.
    SECTION_IDS: dict[str, str] = {"open_interest": "oi"}
    analysis_out = {
        k: v
        for k, v in all_analysis.items()
        if want(SECTION_IDS.get(k, k))
    } if sections is not None else all_analysis

    # Provenance is keyed by the section ID that ran (GROUP_MAP ids), so
    # ``oi`` not ``open_interest`` — the interpretation plane joins it
    # against spec["analysis"] directly.
    ran_ids = {SECTION_IDS.get(k, k) for k in analysis_out}
    return {
        "status": "degraded" if errors else "healthy",
        "errors": errors,
        "analysis": analysis_out,
        "substrate_provenance": _substrate_provenance(ran_ids),
    }


def _adapt_auction(evidence: dict[str, Any]) -> dict[str, Any]:
    fut_book = _fut_book_e(evidence)
    fut_trades = _fut_evidence(evidence).get("trades_normalized") or []
    funding = _fut_evidence(evidence).get("funding") or {}

    mp_spot = _strict(microprice, fut_book, name="microprice")
    initiated = _strict(initiated_flow, fut_trades, name="initiated_flow")
    persistence = _strict(flow_persistence, fut_trades, name="flow_persistence")

    state = {
        "spot": {"microprice": mp_spot, "flow": {"buy_share": None, "vwap": None, "cvd": None}, "persistence": {}},
        "futures": {"microprice": mp_spot, "flow": {"buy_share": None, "vwap": None, "cvd": None}, "persistence": persistence},
        "derivatives": {
            "funding": C.require_float(funding.get("last_funding_rate") if isinstance(funding, dict) else None,
                                       function="auction_verdict", where="derivatives.funding"),
            "taker_buy_ratio": None,
            "oi_change_pct": None,
        },
    }
    verdict, reasons = _strict(auction_verdict, state, name="auction_verdict")
    return {"microprice": mp_spot, "initiated_flow": initiated, "flow_persistence": persistence,
            "verdict": verdict, "reasons": reasons}


def _adapt_delta(evidence: dict[str, Any]) -> dict[str, Any]:
    """Compute the signed DELTA (-2..+2) from + taker buy alignment.

    The wall imbalance uses the futures order book (not spot — the legacy DELTA
    was a futures-only variable). The flow alignment consumes the
    ``taker_buy_sell`` series populated by the on-demand derivative fetch;
    before that fetch landed in Redis it is None and the function returns 0
    for the flow leg (the wall leg is still computable from the book).
    """
    fut = _fut_evidence(evidence)
    fut_book = fut.get("order_book") or {}
    tbr_series = fut.get("taker_buy_sell")
    last_price = _last_price_e(
        C.require_list_of_pairs(fut_book.get("bids"), function="delta.find_price",
                                where="futures.order_book.bids", max_items=20),
        C.require_list_of_pairs(fut_book.get("asks"), function="delta.find_price",
                                where="futures.order_book.asks", max_items=20),
    )
    if last_price is None or last_price <= 0:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "delta": None, "wall_imbalance": None, "flow_alignment": None,
            "tbr_last_pct": None, "tbr_3avg_pct": None,
            "bands": [], "range": {"lo": None, "hi": None},
        }
    delta = _strict(delta_variable, fut_book, tbr_series, float(last_price),
                    half_range=0.75, band_step=0.10, name="delta_variable")
    return {
        "delta": delta["delta"],
        "delta_raw": delta["delta_raw"],
        "wall_imbalance": delta["wall_imbalance"],
        "flow_alignment": delta["flow_alignment"],
        "tbr_last_pct": delta["tbr_last_pct"],
        "tbr_3avg_pct": delta["tbr_3avg_pct"],
        "n_bands": delta["n_bands"],
        "bands": delta["bands"],
        "range": delta["range"],
        "verdict": _delta_state_fn(delta["delta"]),
    }


def _adapt_oi(evidence: dict[str, Any], *, depth: int) -> dict[str, Any]:
    fut = _fut_evidence(evidence)
    fut_book = fut.get("order_book") or {}
    asks = C.require_list_of_pairs(fut_book.get("asks"), function="oi.find_walls", where="futures.order_book.asks", max_items=depth)
    bids = C.require_list_of_pairs(fut_book.get("bids"), function="oi.find_walls", where="futures.order_book.bids", max_items=depth)
    last_price = _last_price_e(bids, asks)
    # Null discipline: a missing/unparseable OI is ``None`` ("source did not
    # provide / could not compute"), never a fabricated 0.0 — zero OI and
    # absent OI are different market facts.
    oi_raw = fut.get("open_interest") or {}
    oi_value = oi_raw.get("open_interest") if isinstance(oi_raw, dict) else None
    oi_float = float(oi_value) if isinstance(oi_value, (int, float)) else None
    walls = (
        _strict(find_walls, asks, float(last_price), 0.005, 0.05, name="find_walls")
        if last_price is not None and last_price > 0 else []
    )

    # OI history series — populated by the on-demand derivative fetch.
    # We extract the per-bar oi_value (USD notional) + oi (contracts) and feed
    # the deterministic layers that previously ran with [] (always empty).
    oi_hist = fut.get("oi_history") or []
    oi_series = _oi_series(oi_hist)
    oi_value_series = _oi_value_series(oi_hist)
    top_long_pct = _ls_last_pct(fut.get("top_ls"))
    glb_long_pct = _ls_last_pct(fut.get("global_ls"))
    # Weighted contracts need a real OI value — without one the output is an
    # explicit null-shaped dict, never OI=0 multiplied by long percentages.
    weighted = (
        _strict(oi_weighted_contracts, oi_float, top_long_pct, glb_long_pct,
                name="oi_weighted_contracts")
        if oi_float is not None
        else {"oi": None, "top_long_contracts": None, "global_long_contracts": None}
    )
    inflow = _strict(oi_inflow_outflow, oi_series, name="oi_inflow_outflow")
    implied_rows = [
        {"bucket": i * 300_000, "oi": v, "oi_value": nv}
        for i, (v, nv) in enumerate(zip(oi_series, oi_value_series))
    ]
    implied = _strict(oi_implied_value, implied_rows, name="oi_implied_value")
    return {"walls": walls, "weighted_contracts": weighted, "inflow_outflow": inflow,
            "implied_value": implied, "raw_open_interest": oi_float,
            "bars_available": len(oi_series)}


def _adapt_wall_migration(
    evidence: dict[str, Any],
    calc_significant_levels: list[dict] | None,
    prior_walls: dict[float, float],
    prior_cycle_ts: str | None,
    *,
    orderbook: dict[str, Any] | None = None,
    depth: int,
    tier_config: TierConfig | None = None,
    scorecard_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    fut_book = _fut_book_e(evidence)
    fut = _fut_evidence(evidence)
    bids, asks = _levels(fut_book, depth, function="wall_migration.*")
    price = _last_price_e(bids, asks)
    if price is None or price <= 0:
        # Null discipline (same precedent as _adapt_delta): without a real
        # mid price the wall analysis cannot run — every price-dependent
        # metric is an explicit null / empty, never computed against a
        # fabricated 0.0. Tiers and round anchors are computed directly from
        # the (possibly empty) book, so a zero there is a true zero.
        return {
            "verdict": "INSUFFICIENT_DATA",
            "wall_delta": None, "fuel_ratio": None,
            "densest_clusters": [], "trap_assessment": None,
            "prior_cycle_ts": prior_cycle_ts,
            "prior_wall_count": len(prior_walls or {}),
            "tiers": compute_bid_tiers_usd(bids, tier_config),
            "round_anchors": compute_round_anchors(bids),  # INSUFFICIENT_DATA: legacy fallback
            "tier_balance": None, "mega_at_keystone": None,
            "keystone_wall_balance": None,
            "keystone_holds_scorecard": None,
            "level_absorption": [], "wall_break": None,
            "zone_ratio_grid": [], "zone_buy_sell": [],
            "inputs_used": {"floors_count": 0, "ask_walls_built": 0,
                            "ask_walls_eroded": 0, "fuel_ratio_value": None,
                            "reason": "no order-book mid price"},
        }
    # Phase 1.3: ATR-aware band replaces the hardcoded price * 0.97 /
    # price * 1.03 magic. The 5m kline series (only available via the
    # on-demand derivative fetch) feeds atr_pct; when absent, the band
    # falls back to the conservative 30bps floor.
    klines = (fut.get("klines") or [])
    atr_pct = atr_pct_from_klines(klines, period=14)
    band = default_wall_band(price, atr_pct=atr_pct)
    bid_floor = band["bid_floor"]
    ask_target = band["ask_target"]

    # Phase 1.1: dynamic round anchors derived from the current price and
    # tick size, replacing the legacy hardcoded SOL list. Tick size is
    # approximated as ``price * 0.0001`` for non-micro instruments
    # (matches Binance's default tick grid for majors); for sub-dollar
    # prices we floor to 0.0001 to keep anchor resolution sensible.
    tick_size = max(price * 0.0001, 0.0001)
    # Step is set to a reasonable fraction of the tick size for round-
    # anchor density: every 5 ticks for non-micro instruments (gives
    # X.00, X.05 etc on a 0.01 tick), every 5 ticks for tick=10 BTC.
    step = max(tick_size * 5, 0.05) if price < 1000 else max(tick_size * 5, 5.0)
    anchors = derive_round_anchors(
        price=price, tick_size=tick_size, step=step,
        depth_below=4, depth_above=4, tol=0.02,
    )

    delta = _strict(wall_delta, prior_walls or {}, asks, 0.02, 1.15, name="wall_delta")
    fuel = _strict(wall_fuel_ratio, bids, asks, price, bid_floor, ask_target, name="fuel_ratio")
    floors = _bid_floors_from_significant_levels(bids, calc_significant_levels, price)
    clusters = _strict(densest_clusters, bids, floors, 0.10, name="densest_clusters")
    built, eroded = _single_cycle_wall_counts(asks)
    trap = _strict(wall_trap_assessment, float(fuel.get("ratio") or 0.0), built, eroded, name="wall_trap_assessment")
    # Phase 1.2: USD-notional tier buckets via TierConfig (price-aware).
    tiers = compute_bid_tiers_usd(bids, tier_config)
    round_anchors = compute_round_anchors(bids, anchors=anchors)
    # Institutional bid vs ask balance + mega-at-keystone now use the same
    # TierConfig as the tier buckets (price-aware). When no TierConfig is
    # passed, the legacy raw-qty fallback (mega=5000) is used.
    if tier_config is not None:
        tier_balance = _strict(bid_tier_balance, bids, asks, tier_config=tier_config,
                               name="bid_tier_balance")
    else:
        tier_balance = _strict(bid_tier_balance, bids, asks, 5000.0, 1.2, name="bid_tier_balance")
    keystone_for_mega = (orderbook.get("fut_keystone") or {}).get("keystone") if isinstance(orderbook, dict) else None
    if tier_config is not None and keystone_for_mega is not None:
        mega_kz = _strict(mega_at_keystone, bids, float(keystone_for_mega),
                          tier_config=tier_config, name="mega_at_keystone")
    elif keystone_for_mega is not None:
        mega_kz = _strict(mega_at_keystone, bids, float(keystone_for_mega),
                          5000.0, 0.10, name="mega_at_keystone")
    else:
        mega_kz = {"keystone_price": None, "count": 0,
                   "qty": 0.0, "notional": 0.0, "levels": []}
    return {"wall_delta": delta, "fuel_ratio": fuel, "densest_clusters": clusters,
            "trap_assessment": trap, "prior_cycle_ts": prior_cycle_ts,
            "prior_wall_count": len(prior_walls or {}),
            "tiers": tiers, "round_anchors": round_anchors,
            "tier_balance": tier_balance, "mega_at_keystone": mega_kz,
            "keystone_wall_balance": _wall_keystone_balance(
                bids, asks, keystone_for_mega, price),
            "keystone_holds_scorecard": _wall_keystone_holds(
                fut, bids, asks, keystone_for_mega, price, scorecard_weights),
            "level_absorption": _wall_level_absorption(bids, floors, price),
            "wall_break": _wall_break(fut, asks),
            "zone_ratio_grid": _wall_zone_grid(fut, keystone_for_mega, price),
            "zone_buy_sell": _wall_zone_intensity(fut, keystone_for_mega, price),
            "inputs_used": {"floors_count": len(floors), "ask_walls_built": built,
                            "ask_walls_eroded": eroded, "fuel_ratio_value": fuel.get("ratio"),
                            "band_bps": band["band_bps"],
                            "band_source": band["scaling_source"],
                            "atr_pct": band["atr_pct"],
                            "anchor_count": len(anchors)}}


# ---------------------------------------------------------------------------
# Legacy-parity wall/keystone signal helpers (Issue 2 wiring).
#
# These wrap the migrated-but-dead pure functions so their outputs are emitted
# into ``analysis.wall_migration`` and persisted to Redis/Postgres via the
# run-cycle envelope. All inputs come from the already-merged evidence
# (order book + trades + derivative fetch), so no new data acquisition is
# needed. Test the pure functions directly (tests/test_analysis_m3.py,
# tests/test_calculations_m1.py) — these adapters only marshal evidence into
# the right argument shape.
# ---------------------------------------------------------------------------


def _wall_zone_band(keystone_for_mega: Any, price: float) -> tuple[float, float, float, float]:
    """Derive keystone + seller-wall bands around the current price.

    Keystone band: [kz-0.20, kz] (bid-side defense, legacy convention).
    Seller-wall band: [sw, sw+0.20] anchored at the densest ask cluster above
    price. Falls back to price-centric bands when the keystone is unavailable.
    """
    kf = float(keystone_for_mega) if keystone_for_mega is not None else price
    kz_lo, kz_hi = kf - 0.20, kf
    sw_lo, sw_hi = kf + 0.10, kf + 0.30
    return kz_lo, kz_hi, sw_lo, sw_hi


def _wall_keystone_balance(bids, asks, keystone_for_mega: Any, price: float) -> dict:
    """Keystone vs seller-wall qty/notional balance (keystone_wall_balance)."""
    kz_lo, kz_hi, sw_lo, sw_hi = _wall_zone_band(keystone_for_mega, price)
    return _strict(keystone_wall_balance, bids, asks, kz_lo, kz_hi, sw_lo, sw_hi,
                   name="keystone_wall_balance")


def _wall_keystone_holds(fut, bids, asks, keystone_for_mega: Any, price: float,
                         scorecard_weights: dict[str, float] | None = None) -> dict:
    """Keystone-holds 0-10 scorecard fed by the balance + on-chain/flow inputs.

    Inputs are derived from evidence that is already present:
      bid_ask_qty_ratio   — from keystone_wall_balance
      latest_tbr          — latest taker buy share from taker_buy_sell
      oi_chg_5m           — OI % change over the last two bars
      top_long_pct        — top-trader long account proportion
      net_buy_ratio       — latest trades buy share
    Each is None-safe; if a source is absent the scorecard still runs with
    zero where the deterministic function tolerates it.

    Phase 2.3: the scorecard inputs are now TREND-AWARE. Single-bar
    snapshots are replaced with multi-bar helpers (see
    ``_taker_buy_trend``, ``_oi_change_trend``, ``_top_long_drift``,
    ``_net_buy_trend``, ``_funding_trend``, ``_funding_zscore``). The
    legacy 0-10 scorecard still receives the LAST value for each input
    so its threshold logic remains unchanged; the trend values are
    surfaced in ``trend_inputs`` for the briefing to consume.

    Phase 2.4: ``scorecard_weights`` (when provided) overrides the
    equal-weight (2 / factor) default so operators can re-prioritize
    drivers without code changes. The trend inputs and the legacy
    probabilities remain identical regardless of weights; only the
    weighted score changes.
    """
    balance = _wall_keystone_balance(bids, asks, keystone_for_mega, price)
    bid_ask_qty_ratio = float(balance.get("bid_ask_qty_ratio") or 0.0)

    tbr_trend = _taker_buy_trend(fut.get("taker_buy_sell"), n=3)
    oi_trend = _oi_change_trend(fut.get("oi_history"), n=3)
    top_long_drift = _top_long_drift(fut.get("top_ls"), n=3)
    glb_long_drift = _global_long_drift(fut.get("global_ls"), n=3)
    net_buy_trend = _net_buy_trend(fut.get("trades_normalized"), n=3)
    funding = _funding_trend(fut.get("funding_history"), n=3)
    funding_z = _funding_zscore(fut.get("funding_history"))

    # Legacy scorecard still wants single-bar inputs so its thresholds
    # remain valid: pass the LAST value of each trend.
    latest_tbr = tbr_trend["last"] if tbr_trend["last"] is not None else 0.5
    oi_chg_5m = oi_trend["last_pct"] if oi_trend["last_pct"] is not None else 0.0
    top_long_pct = top_long_drift["last"] if top_long_drift["last"] is not None else 0.0
    net_buy_ratio = net_buy_trend["last"] if net_buy_trend["last"] is not None else 0.5

    score = _strict(
        keystone_holds_scorecard, bid_ask_qty_ratio, latest_tbr, oi_chg_5m,
        top_long_pct, net_buy_ratio, scorecard_weights, name="keystone_holds_scorecard",
    )

    # Augment the scorecard with the trend inputs so downstream briefings
    # can reason over direction, not just magnitude.
    score["trend_inputs"] = {
        "tbr": tbr_trend,
        "oi_chg": oi_trend,
        "top_long_drift": top_long_drift,
        "global_long_drift": glb_long_drift,
        "net_buy": net_buy_trend,
        "funding": funding,
        "funding_zscore": funding_z,
    }
    return score


def _wall_level_absorption(bids, floors, price: float) -> list[dict]:
    """Absorption capacity (zero-bid vacuum detection) at key bid levels."""
    levels = list(floors) if floors else [price]
    return _strict(level_absorption, bids, levels, 0.01,
                   (1000.0, 5000.0, 10000.0), name="level_absorption")


def _wall_break(fut, asks) -> dict:
    """Can buyers clear the ask stack via buy-flow alone? (wall_break_assessment)."""
    total_wall_sol = sum(float(q) for _, q in asks)
    buy_per_min, peak_buy_per_min = _buy_rates(fut.get("trades_normalized") or [])
    return _strict(wall_break_assessment, total_wall_sol, buy_per_min,
                   peak_buy_per_min, 15, name="wall_break_assessment")


def _wall_zone_grid(fut, keystone_for_mega: Any, price: float) -> list[dict]:
    """Bid/ask ratio grid across the keystone corridor (zone_ratio_grid)."""
    kf = float(keystone_for_mega) if keystone_for_mega is not None else price
    return _strict(zone_ratio_grid, fut.get("order_book") or {}, kf - 0.40, kf + 0.70,
                   0.05, name="zone_ratio_grid")


def _wall_zone_intensity(fut, keystone_for_mega: Any, price: float) -> list[dict]:
    """Taker buy/sell intensity per price zone near the keystone (zone_buy_sell)."""
    kf = float(keystone_for_mega) if keystone_for_mega is not None else price
    return _strict(zone_buy_sell, fut.get("trades_normalized") or [],
                   kf - 0.40, kf + 0.70, 0.05, name="zone_buy_sell")


def _taker_buy_share(taker_bs: list[dict[str, Any]] | None) -> float:
    """Latest taker buy share (0..1) from the Binance taker_buy_sell series.

    Uses buyVol/(buyVol+sellVol) of the most recent bar; 0.5 neutral fallback
    when the series is missing/empty (mirrors summarize's buy_share default).
    """
    if not taker_bs:
        return 0.5
    last = taker_bs[-1] if isinstance(taker_bs[-1], dict) else None
    if not last:
        return 0.5
    bv = float(last.get("buyVol") or last.get("buy_vol") or 0)
    sv = float(last.get("sellVol") or last.get("sell_vol") or 0)
    s = bv + sv
    return bv / s if s > 0 else 0.5


def _taker_buy_series(taker_bs: list[dict[str, Any]] | None) -> list[float]:
    """Per-bar taker-buy share (0..1) from a taker_buy_sell series.

    Returns a list ordered oldest -> newest. Empty/malformed series
    return an empty list so callers can distinguish "no signal" from
    a fabricated neutral value.
    """
    out: list[float] = []
    for row in taker_bs or []:
        if not isinstance(row, dict):
            continue
        bv = row.get("buyVol") or row.get("buy_vol")
        sv = row.get("sellVol") or row.get("sell_vol")
        if bv is None or sv is None:
            continue
        try:
            bv_f = float(bv); sv_f = float(sv)
        except (TypeError, ValueError):
            continue
        s = bv_f + sv_f
        out.append(bv_f / s if s > 0 else 0.5)
    return out


def _taker_buy_trend(taker_bs: list[dict[str, Any]] | None, n: int = 3) -> dict[str, Any]:
    """Multi-bar taker-buy trend.

    Returns ``{last, slope_per_bar, drift, n, n_used}`` where:
      ``last``       — most recent bar's taker-buy share (0..1), None if no data.
      ``slope_per_bar`` — linear slope per bar over the last ``n`` bars (None if insufficient).
      ``drift``      — last - first of trailing ``n`` (None if insufficient).
      ``n`` / ``n_used`` — requested vs available.
    """
    series = _taker_buy_series(taker_bs)
    last = series[-1] if series else None
    window = series[-n:] if len(series) >= n else series
    slope = trend_slope(window, n=len(window)) if len(window) >= 2 else None
    drift = trend_drift(window, n=len(window)) if len(window) >= 2 else None
    return {
        "last": last,
        "slope_per_bar": slope,
        "drift": drift,
        "n": n,
        "n_used": len(window),
    }


def _oi_pct_change(oi_hist: list[dict[str, Any]] | None) -> float:
    """OI % change between the last two bars; 0.0 on insufficient data."""
    series = _oi_series(oi_hist)
    if len(series) >= 2 and series[-2]:
        return (series[-1] - series[-2]) / series[-2] * 100
    return 0.0


def _oi_change_trend(oi_hist: list[dict[str, Any]] | None, n: int = 3) -> dict[str, Any]:
    """Multi-bar OI pct change trend.

    Each value in the trend series is the per-bar pct change between
    adjacent OI bars. Returns ``{last_pct, slope_per_bar, drift, n,
    n_used}``. ``last_pct`` is the most recent per-bar pct change so
    the legacy single-bar semantics survive (the scorecard's ``> 0``
    threshold still triggers on the latest bar).
    """
    series = _oi_series(oi_hist)
    if len(series) < 2:
        return {"last_pct": None, "slope_per_bar": None, "drift": None, "n": n, "n_used": 0}
    pcts: list[float] = []
    for i in range(1, len(series)):
        prev = series[i - 1]
        if prev == 0:
            pcts.append(0.0)
        else:
            pcts.append((series[i] - prev) / prev * 100.0)
    window = pcts[-n:] if len(pcts) >= n else pcts
    last = pcts[-1] if pcts else None
    slope = trend_slope(window, n=len(window)) if len(window) >= 2 else None
    drift = trend_drift(window, n=len(window)) if len(window) >= 2 else None
    return {"last_pct": last, "slope_per_bar": slope, "drift": drift, "n": n, "n_used": len(window)}


def _ls_series(series: list[dict[str, Any]] | None) -> list[float]:
    """Per-bar long-account proportion (0..1) from a topL/S or globalL/S series."""
    out: list[float] = []
    for row in series or []:
        if not isinstance(row, dict):
            continue
        v = row.get("longAccount") or row.get("long_account")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _ls_drift(series: list[dict[str, Any]] | None, n: int = 3) -> dict[str, Any]:
    """Multi-bar drift (last - first) of long-account proportion.

    Returns ``{last, drift, slope_per_bar, n, n_used}``. Drift is the
    simplest signal: positive drift = top traders accumulating longs,
    negative = distributing.
    """
    long_series = _ls_series(series)
    if not long_series:
        return {"last": None, "drift": None, "slope_per_bar": None, "n": n, "n_used": 0}
    window = long_series[-n:] if len(long_series) >= n else long_series
    last = long_series[-1]
    drift = trend_drift(window, n=len(window)) if len(window) >= 2 else None
    slope = trend_slope(window, n=len(window)) if len(window) >= 2 else None
    return {"last": last, "drift": drift, "slope_per_bar": slope, "n": n, "n_used": len(window)}


def _top_long_drift(top_ls: list[dict[str, Any]] | None, n: int = 3) -> dict[str, Any]:
    """Drift of top-trader long-account proportion over the last ``n`` bars."""
    return _ls_drift(top_ls, n=n)


def _global_long_drift(glb_ls: list[dict[str, Any]] | None, n: int = 3) -> dict[str, Any]:
    """Drift of global-trader long-account proportion over the last ``n`` bars."""
    return _ls_drift(glb_ls, n=n)


def _net_buy_share(trades: list[dict[str, Any]] | None) -> float:
    """Taker-buy share (0..1) over the recent trade window; 0.5 default."""
    trades = trades or []
    total = 0.0
    buys = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        try:
            qty = float(t.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        total += qty
        is_maker = t.get("is_buyer_maker")
        if is_maker is not None and not is_maker:
            buys += qty
        elif is_maker is None and str(t.get("side", "")).lower() == "buy":
            buys += qty
    return buys / total if total > 0 else 0.5


def _net_buy_trend(trades: list[dict[str, Any]] | None, n: int = 3,
                   bucket_s: int = 60_000) -> dict[str, Any]:
    """Multi-bar taker-buy share via bucketed CVD.

    ``trades`` are the trade window from the poller. Buckets are 60s
    wide by default (so ``n=3`` covers ~3 minutes of recent activity).
    The trend's ``last`` is the most recent bucket's buy share; the
    slope is across the bucket timeline.
    """
    series = _bucketed_buy_share(trades, bucket_s=bucket_s)
    if not series:
        return {"last": None, "drift": None, "slope_per_bar": None, "n": n, "n_used": 0}
    window = series[-n:] if len(series) >= n else series
    last = series[-1]
    drift = trend_drift(window, n=len(window)) if len(window) >= 2 else None
    slope = trend_slope(window, n=len(window)) if len(window) >= 2 else None
    return {"last": last, "drift": drift, "slope_per_bar": slope, "n": n, "n_used": len(window)}


def _bucketed_buy_share(trades: list[dict[str, Any]] | None, bucket_s: int) -> list[float]:
    """Per-bucket taker-buy share (0..1) from a raw trade list.

    Empty buckets are skipped (no fabrication); ``bucketed_cvd`` from
    the calculations layer gives a richer structure but its buy_share
    is not exposed, so we re-implement the minimal slice needed by the
    trend helpers.
    """
    buckets: dict[int, dict[str, float]] = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        try:
            ts = int(t.get("ts") or 0); qty = float(t.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        bucket = (ts // bucket_s) * bucket_s
        entry = buckets.setdefault(bucket, {"buy": 0.0, "sell": 0.0})
        is_maker = t.get("is_buyer_maker")
        if is_maker is not None and is_maker:
            entry["sell"] += qty
        elif is_maker is None and str(t.get("side", "")).lower() == "buy":
            entry["buy"] += qty
        else:
            entry["buy"] += qty
    out: list[float] = []
    for k in sorted(buckets):
        e = buckets[k]
        s = e["buy"] + e["sell"]
        out.append(e["buy"] / s if s > 0 else 0.5)
    return out


def _funding_series(funding_hist: list[dict[str, Any]] | None) -> list[float]:
    """Per-event funding rate (raw, not bps) from a funding_history list.

    Returns the rate in Binance's native units (typically a small
    decimal like 0.0001 = 1bp/8h). Empty/malformed lists return [].
    """
    out: list[float] = []
    for row in funding_hist or []:
        if not isinstance(row, dict):
            continue
        v = row.get("fundingRate") or row.get("funding_rate")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _funding_trend(funding_hist: list[dict[str, Any]] | None, n: int = 3) -> dict[str, Any]:
    """Multi-event funding rate trend.

    Returns ``{last, slope_per_event, drift, n, n_used}``. ``last`` is
    the most recent rate. ``drift`` is last - first of trailing ``n``
    (positive = funding has been rising = longs paying more carry).
    """
    series = _funding_series(funding_hist)
    if not series:
        return {"last": None, "slope_per_event": None, "drift": None, "n": n, "n_used": 0}
    window = series[-n:] if len(series) >= n else series
    last = series[-1]
    drift = trend_drift(window, n=len(window)) if len(window) >= 2 else None
    slope = trend_slope(window, n=len(window)) if len(window) >= 2 else None
    return {"last": last, "slope_per_event": slope, "drift": drift, "n": n, "n_used": len(window)}


def _funding_zscore(funding_hist: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Z-score of the most recent funding rate vs the trailing window.

    Returns ``{last, mean, std, zscore, n}``. ``zscore=None`` when the
    window has fewer than 3 events or zero variance (no signal, never
    fabricated). Positive z = funding hotter than typical; negative =
    cooler than typical. The scorecard can use this as a \"carry cost
    penalty\" against an over-leveraged long side.
    """
    series = _funding_series(funding_hist)
    if len(series) < 3:
        return {"last": None, "mean": None, "std": None, "zscore": None, "n": len(series)}
    window = series[-min(len(series), 30):]
    last = window[-1]
    mean = sum(window) / len(window)
    var = sum((x - mean) ** 2 for x in window) / len(window)
    std = var ** 0.5
    if std == 0:
        return {"last": last, "mean": mean, "std": 0.0, "zscore": None, "n": len(window)}
    return {"last": last, "mean": mean, "std": std, "zscore": (last - mean) / std, "n": len(window)}


def _buy_rates(trades: list[dict[str, Any]]) -> tuple[float, float]:
    """Current + peak taker-buy rate (SOL/min) over the recent window.

    Buckets trades into per-minute buy volume; current = last full minute,
    peak = max of all minutes. Returns (0, 0) on empty input.
    """
    if not trades:
        return 0.0, 0.0
    per_min: dict[int, float] = {}
    now_ms = max(
        int(t.get("ts") or t.get("time") or 0) for t in trades if isinstance(t, dict)
    )
    for t in trades:
        if not isinstance(t, dict):
            continue
        is_maker = t.get("is_buyer_maker")
        side = t.get("side")
        if is_maker is not None and is_maker:
            continue
        if is_maker is None and str(side).lower() != "buy":
            continue
        ts = int(t.get("ts") or t.get("time") or 0)
        bucket = ts // 60000 * 60000
        per_min[bucket] = per_min.get(bucket, 0.0) + float(t.get("qty") or 0)
    if not per_min:
        return 0.0, 0.0
    peak = max(per_min.values())
    # current = most recent bucket (closest to now_ms)
    current_bucket = max(per_min)
    current = per_min[current_bucket]
    # Only treat as current if the newest bucket is reasonably close to now.
    if now_ms - current_bucket > 60000 * 2:
        current = 0.0
    return current, peak


def _adapt_path_absorption(evidence: dict[str, Any], *, depth: int) -> dict[str, Any]:
    fut_book = _fut_book_e(evidence)
    bids, asks = _levels(fut_book, depth, function="path_absorption.*")
    price = _last_price_e(bids, asks) or 0.0
    # Phase 1.3: ATR-aware band replaces the hardcoded 1% entry / 3% floor.
    # The 5m kline series is the same source the wall adapter uses so the
    # two paths agree on the band shape for a given cycle.
    fut = _fut_evidence(evidence)
    klines = (fut.get("klines") or [])
    atr_pct = atr_pct_from_klines(klines, period=14)
    band = default_wall_band(price, atr_pct=atr_pct) if price > 0 else {
        "bid_floor": 0.0, "entry": 0.0, "ask_target": 0.0,
        "band_bps": 0.0, "scaling_source": "no_price", "atr_pct": None,
    }
    entry = band["entry"]
    bid_floor = band["bid_floor"]
    # Five simulated levels spaced one bid_floor-side step apart (legacy
    # used 0.005 absolute; here we use 1/5 of the band so the simulation
    # range matches the band width regardless of volatility).
    band_step = band["band_bps"] / 10000.0 / 5.0 if band["band_bps"] else 0.005
    levels = [price + i * band_step for i in range(1, 6)] if price else []
    total_bid_fuel = sum(p * q for p, q in bids)
    total_ask_fuel = sum(p * q for p, q in asks)
    fr = _strict(path_fuel_ratio, bids, asks, entry, bid_floor, name="path_absorption.fuel_ratio")
    ascent = _strict(simulated_ascent, asks, total_bid_fuel, levels, name="simulated_ascent")
    descent = _strict(simulated_descent, bids, total_ask_fuel, total_bid_fuel, levels, name="simulated_descent")
    return {
        "fuel_ratio": fr,
        "simulated_ascent": ascent,
        "simulated_descent": descent,
        "band": {
            "bid_floor": bid_floor,
            "entry": entry,
            "ask_target": band["ask_target"],
            "band_bps": band["band_bps"],
            "scaling_source": band["scaling_source"],
            "atr_pct": band["atr_pct"],
        },
    }


def _adapt_demand(evidence: dict[str, Any]) -> dict[str, Any]:
    fut = _fut_evidence(evidence)
    spot = _spot_evidence(evidence)
    cross = evidence.get("cross_asset") or {}
    # Pull BTC/ETH change_pct + funding from the cross-asset fetch the on-demand
    # derivative step populated. Without it, macro_climate returns "data
    # unavailable" so we treat this as best-effort.
    tickers = {t.get("symbol"): t for t in (cross.get("tickers_24h") or []) if isinstance(t, dict)}
    funding_rows = {row.get("symbol"): row.get("funding")
                    for row in (cross.get("funding") or []) if isinstance(row, dict)}

    def _funding_bps(sym: str) -> float | None:
        f = funding_rows.get(sym)
        if not isinstance(f, dict):
            return None
        try:
            return float(f.get("lastFundingRate") or f.get("last_funding_rate") or 0) * 10000
        except (TypeError, ValueError):
            return None

    def _change_pct(sym: str) -> float | None:
        t = tickers.get(sym)
        if not t:
            return None
        try:
            return float(t.get("priceChangePercent") or t.get("price_change_percent"))
        except (TypeError, ValueError):
            return None

    target_change_pct = None
    fut_ticker = fut.get("ticker_24h") or {}
    if isinstance(fut_ticker, dict):
        for k in ("priceChangePercent", "price_change_percent"):
            if k in fut_ticker and fut_ticker[k] is not None:
                try:
                    target_change_pct = float(fut_ticker[k]); break
                except (TypeError, ValueError):
                    pass

    macro_in = {
        "btc": {"change_pct": _change_pct("BTCUSDT"), "funding": _funding_bps("BTCUSDT")},
        "eth": {"change_pct": _change_pct("ETHUSDT"), "funding": _funding_bps("ETHUSDT")},
    }
    d = {
        "spot": {
            "trades": spot.get("trades_normalized") or [],
            "book": spot.get("order_book") or {},
            "ticker_24h": _ticker_change(spot.get("ticker_24h")),
        },
        "futures": {
            "trades": fut.get("trades_normalized") or [],
            "book": fut.get("order_book") or {},
            "open_interest": fut.get("open_interest") or {},
            "oi_history": fut.get("oi_history") or [],
            "mark_price": fut.get("mark_price"),
            "global_ls": fut.get("global_ls"),
            "top_ls": fut.get("top_ls"),
            "taker_buy_sell": fut.get("taker_buy_sell"),
            "ticker_24h": _ticker_change(fut.get("ticker_24h")),
        },
        "latency_ms": None,
    }
    dx = _strict(decompose_demand, d, name="decompose_demand",
                  flow_provider=summarize)
    verdict, reasons = _strict(demand_verdict, dx, name="demand_verdict")
    climate = _strict(macro_climate, macro_in, target_change_pct, name="macro_climate")
    return {"decomposition": dx, "verdict": verdict, "reasons": reasons,
            "macro_climate": climate, "macro_inputs": macro_in,
            "cross_asset": {"symbols_seen": sorted(tickers.keys()),
                            "funding_symbols": sorted(funding_rows.keys())}}


def _adapt_regime(evidence: dict[str, Any], calc_data: dict[str, Any]) -> dict[str, Any]:
    flow = calc_data.get("flow") or {}
    fut_evidence = _fut_evidence(evidence)
    d = {
        "futures": {
            "flow": flow.get("futures_flow") or flow.get("spot_flow") or {},
            "open_interest": fut_evidence.get("open_interest") or {},
            "funding": fut_evidence.get("funding") or {},
            "mark_price": fut_evidence.get("mark_price") or {},
            "long_short_ratio": fut_evidence.get("global_ls"),
            "top_long_short_accounts": fut_evidence.get("top_ls"),
        },
    }
    verdict, reasons = _strict(regime_verdict, d, name="regime_verdict")
    return {"state": d, "verdict": verdict, "reasons": reasons}


def _adapt_stage(evidence: dict[str, Any]) -> dict[str, Any]:
    from market_service.analysis.stage import infer_stage
    fut = _fut_evidence(evidence)
    klines = fut.get("klines") or []
    closes = []
    for k in klines:
        if isinstance(k, (list, tuple)) and len(k) >= 5:
            try:
                closes.append(float(k[4]))
            except (TypeError, ValueError):
                pass
    if len(closes) < 2:
        raise C.ContractViolation(
            function="infer_stage",
            reason="need at least 2 closes to compute 4h change",
            details={"kline_count": len(klines)},
        )
    px_chg_4h = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0
    up_steps = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down_steps = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
    # Wire the L/S and funding signals the on-demand derivative fetch populates.
    funding_bps = None
    funding = fut.get("funding") or {}
    if isinstance(funding, dict):
        try:
            funding_bps = float(funding.get("lastFundingRate") or funding.get("last_funding_rate") or 0) * 10000
        except (TypeError, ValueError):
            funding_bps = None
    top_long_pct = _ls_last_pct(fut.get("top_ls"))
    glb_long_pct = _ls_last_pct(fut.get("global_ls"))
    # OI 4h change approximated from the history series if available.
    oi_series = _oi_series(fut.get("oi_history") or [])
    if len(oi_series) >= 2 and oi_series[0] > 0:
        oi_chg_4h = (oi_series[-1] - oi_series[0]) / oi_series[0] * 100
    else:
        oi_chg_4h = 0.0
    return {
        "stage": _strict(infer_stage,
                         px_chg_4h=px_chg_4h, oi_chg_4h=oi_chg_4h, up_pct_4h=50.0,
                         up_steps=up_steps, down_steps=down_steps,
                         funding_bps=funding_bps,
                         top_long_pct=top_long_pct, global_long_pct=glb_long_pct,
                         distribution_clusters=0.0, absorption_clusters=0.0,
                         name="infer_stage"),
        "kline_count": len(klines),
    }


def _accumulate_prior_walls(
    snapshots: list[dict[str, Any]],
) -> tuple[dict[float, float], str | None]:
    """Merge the FULL recorded wall history into one prior-walls map.

    ``snapshots`` are newest-first (the order both stores return). We
    iterate oldest-first so the newest known qty wins for a given level,
    but every level ever recorded is retained - so wall_migration probes
    ALL recorded wall levels, and a wall removed since an older pull is
    still surfaced (as ERODED) rather than silently dropped.
    """
    prior_walls: dict[float, float] = {}
    prior_cycle_ts: str | None = None
    for snap in reversed(snapshots or []):
        ts = snap.get("cycle_ts")
        if ts and prior_cycle_ts is None:
            prior_cycle_ts = ts
        asks = snap.get("asks")
        if not isinstance(asks, list):
            continue
        for row in asks:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                prior_walls[float(row[0])] = float(row[1])
            except (TypeError, ValueError):
                continue
    return prior_walls, prior_cycle_ts


def _oi_series(oi_hist: list[dict[str, Any]] | None) -> list[float]:
    """Extract the per-bar OI contract count from a raw OI history list."""
    out: list[float] = []
    for row in oi_hist or []:
        if not isinstance(row, dict):
            continue
        v = row.get("sumOpenInterest") or row.get("sum_open_interest") \
            or row.get("openInterest") or row.get("open_interest")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _oi_value_series(oi_hist: list[dict[str, Any]] | None) -> list[float]:
    """Extract the per-bar OI USD-notional from a raw OI history list."""
    out: list[float] = []
    for row in oi_hist or []:
        if not isinstance(row, dict):
            continue
        v = row.get("sumOpenInterestValue") or row.get("sum_open_interest_value")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _ls_last_pct(series: list[dict[str, Any]] | None) -> float | None:
    """Extract the latest long-account proportion from a topL/S or globalL/S series."""
    if not series:
        return None
    last = series[-1] if isinstance(series[-1], dict) else None
    if not last:
        return None
    for k in ("longAccount", "long_account"):
        if k in last and last[k] is not None:
            try:
                return float(last[k])
            except (TypeError, ValueError):
                continue
    return None



# ---------------------------------------------------------------------------
# Public composition API — the names BOTH planes may consume. The underscored
# originals stay importable (tests + shim); these aliases are the sanctioned
# seam for out-of-plane callers (pipeline_inference).
# ---------------------------------------------------------------------------

accumulate_prior_walls = _accumulate_prior_walls
resolve_tier_config = _resolve_tier_config
resolve_scorecard_weights = _resolve_scorecard_weights

__all__ = [
    "WINDOW_MINUTES_MAP",
    "GROUP_MAP",
    "SUBSTRATE_GRAPH",
    "substrate_for",
    "section_inputs",
    "resolve_calc_sections",
    "resolve_analysis_sections",
    "sections_for_groups",
    "run_calculations",
    "run_analysis",
    "accumulate_prior_walls",
    "resolve_tier_config",
    "resolve_scorecard_weights",
]
