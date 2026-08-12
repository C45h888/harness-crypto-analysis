"""
Canonical runtime manifest for the crypto-ai-anal system.

The legacy root scripts have been fully migrated into the canonical
``market_service`` package (see ``MIGRATIONS`` for provenance) and removed.
This module is the single source of truth for:

  1. The domain-split container architecture (target state): data-access,
     calculation, and analysis each map to their own container/domain.
  2. The clean-market-data surface (`market_service.commands.harness`): the
     canonical modules the model reads.
  3. The run-all verifier (`market_service.commands.run_all`): confirming the
     canonical modules import cleanly.

Domains:
  data-access  -> pulls raw evidence (clients, market-data pullers)
  calculation  -> pure math over normalized inputs (flow, signals, orderbook)
  analysis     -> derived interpretation (regime, OI, liquidation, macro, walls)
  monitor      -> long-running / exploratory watch + summarise
"""

from __future__ import annotations

DOMAIN_DATA_ACCESS = "data-access"
DOMAIN_CALC = "calculation"
DOMAIN_ANALYSIS = "analysis"
DOMAIN_MONITOR = "monitor"
DOMAINS = (DOMAIN_DATA_ACCESS, DOMAIN_CALC, DOMAIN_ANALYSIS, DOMAIN_MONITOR)

# The canonical package modules that produce the "clean" market-data contract.
# key = contract section, value = package module path (all import-checked).
CLEAN_MODULES: dict[str, str] = {
    "clients": "market_service.clients",
    "flow": "market_service.calculations.flow",
    "orderbook": "market_service.calculations.orderbook",
    "volume_profile": "market_service.calculations.volume_profile",
    "technical": "market_service.calculations.technical",
    "signals": "market_service.calculations.signals",
    "market": "market_service.analysis.market",
    "oi": "market_service.analysis.oi",
    "liquidations": "market_service.analysis.liquidations",
    "macro": "market_service.analysis.macro",
    "auction": "market_service.analysis.auction",
    "demand": "market_service.analysis.demand",
    "regime": "market_service.analysis.regime",
    "wall_migration": "market_service.analysis.wall_migration",
    "path_absorption": "market_service.analysis.path_absorption",
    "stage": "market_service.analysis.stage",
}

# Feature-complete migrations: legacy script -> canonical module(s). The legacy
# files have been deleted; this map records where each feature landed (provenance
# + audit trail so no feature is lost and no history is ambiguous).
MIGRATIONS: dict[str, str] = {
    "auction_dynamics": "analysis/auction.py",
    "demand_diagnostic": "analysis/demand.py",
    "sol_futures": "analysis/regime.py",
    "flow5m": "calculations/orderbook.py + flow.py (price_bucketed_flow)",
    "deep_keystone": "calculations/orderbook.py",
    "keystone_scan": "calculations/orderbook.py",
    "long_term_flow": "calculations/orderbook.py + volume_profile.py + analysis/stage.py + clients/binance.py (fut_agg_trades_paginated)",
    "sol_deep_monitor": "calculations/technical.py",
    "continue_monitor": "calculations/flow.py (microprice_skew + spot_turnover_share)",
    "spot_fut_assess": "calculations/flow.py (price_bucketed_flow)",
    "macro": "analysis/macro.py (idiosyncratic)",
    "oi_analysis": "analysis/oi.py (walls, wall-break, implied value)",
    "oi_analysis_run": "analysis/oi.py (OI-weighted contracts, inflow/outflow)",
    "path_absorption": "analysis/path_absorption.py",
    "seller_wall_check": "analysis/wall_migration.py",
    "wall_analysis": "analysis/wall_migration.py",
    "wall_state_check": "analysis/regime.py (regime) + analysis/oi.py",
}
