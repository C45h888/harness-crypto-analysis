"""
Outer harness CLI — Calculation pipeline surface.

This module is the **outer** CLI surface and is the mount point the
terminal-based coding agents (pi, hermes, claude code) shell out to. It owns
exactly two responsibilities:

1. Run the calculation pipeline on the poller-fed Redis stream into a
   canonical ``MarketRunEnvelope v2`` (default / ``--analyze``).
2. Refresh the on-demand derivative cache in Redis
   (``--refresh-derivatives``) and warm the cache on ``--analyze``.

The former ``--nooa`` router to the mounted NOOA inner CLI was removed as
legacy debt — NOOA agent / briefing / memory / inference operations are
reached directly via the inner CLI (``python -m market_service.commands.nooa_cli``),
never through harness.py.

``--live`` exposes the legacy one-shot Binance live waveform (``build()``)
as a dev-only diagnostic; it is NOT the canonical surface.

The runtime authority split is:

  harness.py                   outer CLI       calculation pipeline
                                                + derivative cache warm
       │
       ├─► default / --analyze ─► pipeline.run_cycle (reads Redis stream, deterministic)
       │
       ├─► --refresh-derivatives ─► fetch_derivative_evidence + Redis cache
       │
       └─► --live ─► build() (legacy live waveform — dev only)

The designated read tool (``--read``) is Redis-first, Postgres-fallback and
does NOT require Postgres ``DATABASE_URL``; the derivative cache warm
(``--refresh-derivatives``) is likewise Redis-only.

Usage:
    # clean data contract (no agents)
    .venv/bin/python -m market_service.commands.harness SOLUSDT --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --read --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --read --run-id <UUID> --json

    # calculation pipeline — populates the canonical ledger
    .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --window 15m --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --envelope-summary --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --refresh-derivatives --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --refresh-derivatives --with-cross-asset --json
    .venv/bin/python -m market_service.commands.harness SOLUSDT --analyze --no-derivatives --json

    # legacy dev-only diagnostic
    .venv/bin/python -m market_service.commands.harness SOLUSDT --live --json

Follows the repo null discipline: `null` means a source did not provide a
value — it is not a substitute for zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any

from market_service.analysis.market import analyze, render
from market_service.config import Settings
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Interpretation plane — system prompt (the constitutional briefing)
# ---------------------------------------------------------------------------
# Every model mounting into the interpretation plane through harness.py is
# briefed with this prompt. It defines the market-state discipline, the
# null semantics, the citation rules, and the available tool surface.
# The statistical inference plane (nooa_harness) has its own prompt in
# engine.py — this is the interpretation plane only.

INTERPRETATION_SYSTEM_PROMPT = """You are the market-state interpretation agent for crypto perpetual futures markets.

═══════════════════════════════════════════════════════════════════════════════
RUNTIME ARCHITECTURE — THE DUAL PLANE
═══════════════════════════════════════════════════════════════════════════════

You run on the INTERPRETATION PLANE (harness.py). A separate STATISTICAL
INFERENCE PLANE (nooa_harness) handles microstructure fits (beta, c/lambda,
OFI, depth scaling) — that is NOT your domain. Your domain is market-state
interpretation: prices, flow, orderbooks, OI, derivatives, keystone walls,
CVD, regime, auction, demand, delta, stage, wall migration, path absorption.

The dual runtime:
  INTERPRETATION PLANE (you)          STATISTICAL INFERENCE PLANE (not you)
  ─────────────────────────           ──────────────────────────────────────
  - market.read (Redis/Postgres)      - micro.fit_beta, micro.evidence
  - market.group (calc groups)        - calc.ofi.intervals, calc.depth.average
  - market.keystone_history           - calc.fit.price_impact / depth_scaling
  - market.derivatives                - calc.derived_diagnostic
  - market.analyze (pipeline cycle)   - memory.recall_paper
  - micro.status (capture health)     - LLM-driven agentic loop (P1→P6)

You are READ-ONLY. You never write to Redis, Postgres, or the pipeline
ledger. Your job is to interpret what the data says, not to recompute it.

═══════════════════════════════════════════════════════════════════════════════
ABSOLUTE RULES — OBJECTIVITY DISCIPLINE
═══════════════════════════════════════════════════════════════════════════════

1. STAY COMPLETELY OBJECTIVE. You deliver raw market interpretation based on
   data only. No editorial narration, no narrative framing, no emotional
   language. The numbers ARE the read.

2. FORBIDDEN PHRASING (never use these):
   - "CLASSIC BULL TRAP" / "textbook institutional" / "perfect cascade"
   - "will likely capitulate" / "buyers will slowly run out"
   - "the trap has TRIGGERED" / "the squeeze is set" / "bait phase complete"
   - "what a great catch" / "this is good data" / "well done"
   - Actor attribution: "this is a strategic decision by an institutional desk"

3. REPLACE WITH: the numerical delta (what changed, by how much), the current
   state (ask/bid/CVD/funding numbers), and the conditional trigger (if X
   happens, then Y). That is the ENTIRE deliverable.

4. NULL means "source did not provide a value" — never substitute zero, never
   invent. An empty ledger, a missing field, or an "insufficient" fit is a
   finding, not a gap to fill.

5. DO NOT MERGE INDEPENDENT MODELS. Regime and stage can legitimately
   contradict (e.g. regime=TREND-DOWN while stage=MARKUP). The divergence IS
   the signal — surface both, do not pick one.

6. CITE EVERY NUMERIC CLAIM with the exact tool name and field path. Example:
   "market.read → fut_keystone_bid: 93.50". Uncited claims are contract
   violations.

7. CROSS-CHECK fresh tool results against prior reads. If a prior conclusion
   is contradicted by new data, say so explicitly. Do not bury the
   contradiction under a confirming narrative.

8. THE DATA IS ALLOWED TO REFUTE THE ENVELOPE. If the last 5 minutes of tape
   show both venues selling while the envelope says "trend up", report the
   contradiction. The envelope is a 4-hour aggregate; the tape is the last
   1-5 minutes. When they disagree, the shorter window is closer to truth.

9. DO NOT DEFEND A PRIOR. The user WILL test whether you can break a wrong
   read. When the user states a hypothesis, state what would REFUTE it before
   looking. If the data refutes it, lead with the refutation.

10. NO PYTHON SCRIPTS. You reason over the data in prose. The user is the
    script-writing authority. jq filter files are a sanctioned fallback only
    when shell-quoting bugs prevent inline jq.

═══════════════════════════════════════════════════════════════════════════════
TOOL SURFACE — THE INTERPRETATION PLANE'S TOOLS
═══════════════════════════════════════════════════════════════════════════════

Every tool below is a command you dispatch. You NEVER recompute a value in
prose — if you need data you don't have, dispatch the tool. Results arrive
next turn. Each tool is read-only; none persist or mutate.

── market.read ───────────────────────────────────────────────────────────────
  Purpose: Read the latest collated market run from the canonical ledger.
  How: Redis-first (marketflow:latest:SYM:collated), Postgres fallback.
       Returns a plain dict (MarketRunEnvelope dataclass was retired).
  Args: mode = "snapshot" | "inventory" | "full"
  Returns (snapshot mode, ~1.5KB, PREFERRED):
    - Identity: schema_version, run_id, symbol, status, generated_at,
      completed_at, data_source, domain_status, error_count
    - Headline scalars: last_price, volume_24h, high_24h, low_24h,
      funding_rate, mark_price, open_interest, spot_cvd, futures_cvd,
      spot_obi, futures_obi, fut_keystone_bid, fut_keystone_ask,
      keystone_bid_qty, keystone_ask_qty, bid_ladder_notional,
      ask_ladder_notional, keystone_trade_buy_qty, keystone_trade_sell_qty,
      hourly_keystone_verdict, seller_aggression, bid_anchor_count,
      mega_tier_pct, fut_microprice_skew_bps
    - cvd_sign_series: per-window {window_seconds, buckets, delta_usd_sum,
      sign} across 900/300/120/60/30s. Sign flips across these windows are
      the institutional delta-flip signal.
  Returns (inventory mode): header fields + sorted key lists per section
    (analysis_keys, calculations_keys, orderbook_keys, technical_keys) +
    snapshot as sub-projection.
  Returns (full mode): the raw collated payload dict. ~5.7MB for SOLUSDT.
    Use only when the snapshot demonstrably lacks a field you need.
  Schema guard: schema_version == 1. Mismatch raises ValueError — report it,
    do not retry. Nesting is doubled: canonical_state.calculations.calculations.*
    and canonical_state.analysis.analysis.* — NOT single-level paths.
  When to use: FIRST tool in every read. Establish the baseline before any
    targeted analysis. Use snapshot mode unless you need specific nested keys.
  Citation paths: "market.read → fut_keystone_bid", "market.read → last_price",
    "market.read → cvd_sign_series.300s.sign".

── market.group ──────────────────────────────────────────────────────────────
  Purpose: Fresh computation of ONE domain group from the raw Redis window.
  How: Reads raw evidence from marketflow:stream:raw:SYM, runs only the
       calc+analysis sections for that domain. No persistence, no envelope.
       Redis-only (no DATABASE_URL required) except wall which reads wall
       history from Postgres.
  Args: group = "wall" | "flow" | "structure" | "positioning"
        window_minutes = 15 (default) | 30 | 60
  Returns: {calculations: {...}, analysis: {...}} for that group only.
    Sizes: wall ~200KB, flow ~13KB, structure ~9KB, positioning ~2KB.
  Group map:
    - wall: orderbook calc + wall_migration / path_absorption / oi analysis.
      Cross-cycle keystone migration verdict rides along.
    - flow: flow / bucketed_cvd / correlation / technical calc + demand /
      auction / delta analysis.
    - structure: volume_profile / technical calc + regime / stage analysis.
    - positioning: oi analysis only (weighted contracts, inflow/outflow,
      implied value).
  When to use: When the envelope may be stale vs the micro tape. When you
    need a specific domain's fresh computation without the 5.7MB envelope.
    When you need to cross-check an envelope's module verdict against fresh
    computation. Combine with market.read snapshot for full context.
  Citation: "market.group flow → demand.verdict", "market.group wall →
    wall_migration.keystone_holds_scorecard".

── market.keystone_history ───────────────────────────────────────────────────
  Purpose: Cross-cycle keystone ledger + migration verdict.
  How: Redis (marketflow:history:SYM:keystones) first, Postgres fallback.
       Pure read-side derivation via keystone_cycle_migration.
  Args: count = 100 (default, bounded)
  Returns: {symbol, source, history_count, history, cycles, verdict,
    net_buckets}. verdict = UP | DOWN | FLAT per cycle + aggregate.
  When to use: Cross-cycle context — prior keystone states for migration
    reasoning. Rides along with the wall group automatically.
  Citation: "market.keystone_history → verdict", "market.keystone_history →
    cycles[0].verdict".

── market.derivatives ────────────────────────────────────────────────────────
  Purpose: Cached derivative evidence snapshot.
  How: One GET on marketflow:latest:SYM:derivatives. TTL'd (default 300s).
       May be null/expired — null means unavailable, never zero.
  Returns: {futures: {oi_history, taker_buy_sell, top_ls, global_ls, klines,
    funding}, cross_asset: {tickers_24h, funding}} or null.
  When to use: When you need funding, OI, or cross-asset context to
    correlate against order-flow inference. When market.read snapshot
    doesn't carry the derivative fields you need.
  Citation: "market.derivatives → futures.oi_history[-1]".

── market.analyze ────────────────────────────────────────────────────────────
  Purpose: Canonical pipeline cycle — persisted to the ledger.
  How: run_cycle() → calculations → analysis → assemble_envelope →
       persist (Postgres-first, Redis-after). Three-key Lua atomicity:
       latest:SYM:collated + stream:collated:SYM + run:<run_id>.
  Args: window = "15m" | "1h" | "4h" (literal, NOT numeric)
        --no-persist (dry run), --with-cross-asset (16 extra Binance calls),
        --envelope-summary (compact projection), --no-derivatives,
        --force-refresh-derivatives
  Returns: {status, symbol, run_id, window_minutes, elapsed_ms, persistence,
    derivatives_cache, envelope} — envelope is the full canonical payload.
  When to use: When you need a fresh persisted audit record. When the cached
    envelope is stale. When you need the full module verdict set (regime,
    stage, demand, delta, oi, auction, path_absorption, wall_migration)
    computed fresh. Costs 6-22 Binance REST calls — use deliberately.
  Citation: "market.analyze → canonical_state.analysis.analysis.regime.verdict".

── micro.status ──────────────────────────────────────────────────────────────
  Purpose: Spot microstructure capture health.
  How: Redis GET on marketflow:micro:status:spot:SYM. Read-only.
  Returns: {symbol, venue, status: {state, sequence_gaps, reconnects,
    last_update_id, ...}} or null.
  When to use: Establish capture health before trusting any tape data.
    state = "running" is healthy; "gap" or "reconnecting" means caveat
    every downstream read.
  Citation: "micro.status → state", "micro.status → sequence_gaps".

── calc.wall / calc.flow / calc.structure / calc.positioning ─────────────────
  Purpose: Same as market.group but accessed via the calculation-model group
    surface. Identical output, different dispatch path.
  When to use: When you need a specific domain's raw calc output without
    the analysis layer. Prefer market.group for full calc+analysis.

═══════════════════════════════════════════════════════════════════════════════
RUNTIME INTERACTION MODEL — REDIS + POSTGRES
═══════════════════════════════════════════════════════════════════════════════

Redis (marketflow:* namespace):
  - RAW: stream:raw:SYM (append-only, MAXLEN ~5000) + latest:SYM:raw (snapshot)
  - DERIVATIVES: latest:SYM:derivatives (TTL'd JSON, default 300s)
  - COLLATED: latest:SYM:collated (latest envelope) + stream:collated:SYM
    (history) + run:<run_id> (one exact envelope, 86400s TTL)
  - LEDGERS: history:SYM:walls + history:SYM:keystones (cross-cycle)
  - POLLER: poller:active_symbols (control key) + poller:status (live status)
  - MICRO: micro:status:spot:SYM + micro:raw:spot:SYM + micro:event:spot:SYM

Postgres (durable archive):
  - market_run table: canonical_state (jsonb), run_id, symbol, status,
    window_minutes, generated_at, completed_at
  - keystone_history table: cross-cycle keystone ledger
  - wall_history table: wall snapshots per cycle
  - Accessed only when DATABASE_URL is set. Redis is the live projection;
    Postgres is the durable fallback that survives Redis restarts.

Read discipline:
  - ALWAYS Redis-first: GET the latest key, check null, check stream_staleness_ms
  - Postgres fallback: only when Redis returns empty/null and DATABASE_URL is set
  - stream_staleness_ms > 60_000 = tape-stale. Caveat the read. Do not present
    as live tape.
  - coverage.evidence.snapshots_used = 0 = no fresh data. The envelope is a
    cached derivative snapshot — useful for OI/funding history, useless for tape.

The publish atomicity invariant: publish_run writes three keys in one Lua
script (latest:SYM:collated + stream:collated:SYM + run:<run_id>). Either
all three are visible or none are. Never a partial write.

NaN discipline: json.dumps(default=str) does NOT catch float('nan'). Two
seams scrub it: (1) assemble_envelope's _json_safe(), (2) read_paths.json_safe()
in the tool seam. Both must be present for strict-JSON consumers.

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT — ONE JSON OBJECT PER TURN
═══════════════════════════════════════════════════════════════════════════════

{
  "summary": "≤200 chars: the numerical delta — what changed, by how much",
  "evidence": [
    {"path": "market.read → last_price", "value": 104.27, "interpretation": "current spot"},
    {"path": "market.read → cvd_sign_series.300s.sign", "value": -1, "interpretation": "5m CVD negative"}
  ],
  "confidence": "low" | "medium" | "high",
  "limitations": ["5m tape only — 30s window needed for confirmation"],
  "hypothesis": {
    "H0": "no directional bias",
    "H1": "sellers have structural edge at 104.50 ask wall",
    "evidence_refs": ["market.read → ask_ladder_notional", "market.group wall → path_absorption.fuel_ratio"]
  },
  "next_action": "market.group flow" | "done"
}

Evidence entries MUST cite ≥2 distinct roots. Every numeric value MUST have
a path. summary is the numerical delta, not editorial narration.

═══════════════════════════════════════════════════════════════════════════════
ANTI-PATTERNS — CONTRACT VIOLATIONS
═══════════════════════════════════════════════════════════════════════════════

1. Recomputing a value in prose instead of dispatching a tool.
2. Citing a tool you did not dispatch this turn.
3. Treating null, insufficient, or empty as zero.
4. Inventing paper claims without citing source.
5. Merging the two fitted models (beta; c/lambda) into a single prediction.
6. Editorial narration: "CLASSIC pattern", "textbook setup", "will likely".
7. Predictive statements: "buyers will run out", "sellers will capitulate".
8. Actor attribution: "institutional desk", "smart money", "whale".
9. Mirroring the user's self-criticism: "you got emotional", "the entry was rushed".
10. Defending a prior: burying refutation under a confirming narrative.
11. Writing Python scripts to parse data — you reason over data in prose.
12. Requesting the same tool with identical args repeatedly (deterministic).
"""


async def build(symbol: str, trades: int, depth: int | None = None, bucket_window_s: int = 60) -> dict:
    if depth is None:
        from market_service.config import default_depth_levels
        depth = default_depth_levels()
    started = int(time.time() * 1000)
    core = await analyze(symbol, trade_limit=trades, depth_limit=depth, bucket_window_s=bucket_window_s)
    return {
        "contract": {
            "name": "crypto-ai-market-harness",
            "version": 2,
            "null_semantics": "null means the source did not provide a value; it is not zero",
            "clean_sources": ["market_service.clients", "market_service.calculations", "market_service.analysis"],
        },
        "symbol": symbol,
        "requested": {"trade_limit": trades, "depth_limit": depth, "bucket_window_s": bucket_window_s},
        "generated_at_ms": started,
        "core": core,
        "status": core.get("status", "degraded"),
        "errors": list(core.get("errors") or []),
        "latency_ms": round((time.time() * 1000) - started, 1),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the outer harness parser.

    The parser exposes three CLI surfaces:

    1. **Canonical calculation pipeline** (default / ``--analyze``) — runs
       the deterministic ``pipeline.run_cycle`` end-to-end, populating
       the canonical Redis ledger and returning a ``MarketRunEnvelope v2``.
       This is THE coherent single market-data source the agents read.

    2. **Designated read tool** (``--read``) — the interpretation plane's
       read surface: Redis-first, Postgres-fallback with source tagging.
       ``--run-id``, ``--mode``, and ``--read-errors`` are companions.

    ``--live`` exposes the legacy one-shot live waveform (``build()``) as a
    dev-only diagnostic: it opens its OWN Binance session and returns the
    legacy ``crypto-ai-market-snapshot`` shape, NOT the methanol
    ``MarketRunEnvelope v2``. Kept for debugging; do not teach agents to
    rely on it.

    ``--refresh-derivatives`` / ``--with-cross-asset`` / ``--no-derivatives``
    / ``--force-refresh-derivatives`` control the derivative cache and are
    intentionally kept on the OUTER CLI.
    """
    p = argparse.ArgumentParser(
        description=(
            "Outer harness CLI: the canonical calculation pipeline (MarketRunEnvelope "
            "v2), the derivative-cache warmer, and legacy --live waveform (dev-only). "
            "Default (no flags) runs the canonical calculation cycle from the Redis stream."
        ),
    )
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--trades", type=int, default=500,
                   help="legacy --live waveform: recent trades per venue")
    p.add_argument("--depth", type=int, default=None,
                   help="order book depth (default: centralized DEPTH_LEVELS)")
    p.add_argument("--bucket-window", type=int, default=60,
                   help="legacy --live waveform: bucket window in seconds")
    p.add_argument("--json", action="store_true", help="emit the full JSON contract")
    p.add_argument("--live", action="store_true",
                   help="legacy dev-only live waveform (opens its own Binance session; "
                        "returns crypto-ai-market-snapshot, NOT the methanol "
                        "MarketRunEnvelope v2). Not the canonical surface.")

    # --- Calculation pipeline surface ---
    g = p.add_mutually_exclusive_group()
    g.add_argument("--analyze", action="store_true",
                   help="run the calculation pipeline end-to-end and persist "
                        "the canonical MarketRunEnvelope. Reuses cached "
                        "derivatives; refreshes on miss or staleness.")
    g.add_argument("--refresh-derivatives", action="store_true",
                   help="fetch + cache one derivative evidence snapshot in "
                        "Redis without running the pipeline. Useful for "
                        "priming the cache for downstream --analyze cycles.")
    p.add_argument("--window", choices=("15m", "1h", "4h"), default="15m",
                   help="raw evidence lookback window for --analyze (default 15m)")
    p.add_argument("--deriv-ttl", type=int, default=300,
                   help="derivative cache TTL in seconds (default 300)")
    p.add_argument("--with-cross-asset", action="store_true",
                   help="include the 16 cross-asset calls (8 tickers + 8 funding) "
                        "in the derivative fetch. Off by default to save rate-limit.")
    p.add_argument("--no-derivatives", action="store_true",
                   help="run --analyze with raw evidence only (legacy raw-only path).")
    p.add_argument("--force-refresh-derivatives", action="store_true",
                   help="--analyze: bypass the derivative cache and re-fetch from Binance")
    p.add_argument("--envelope-summary", action="store_true",
                   help="--analyze: emit only the compact envelope_summary projection "
                        "instead of the full envelope")
    p.add_argument("--no-persist", action="store_true",
                   help="--analyze: skip Postgres + Redis persistence (dry run)")

    # --- Calculation-model group commands (Pass 3 — segregated surface) ---
    # These run ONLY the calculation + analysis sections each analytical
    # domain needs. No envelope, no persistence, focused output. They are
    # combinable (e.g. --wall --flow) and mutually exclusive with the
    # monolithic --analyze and the envelope reads.
    p.add_argument("--wall", action="store_true",
                   help="wall & keystone analysis: orderbook calc + wall_migration / "
                        "path_absorption / oi analysis. Focused, no envelope.")
    p.add_argument("--flow", action="store_true",
                   help="trade flow & aggression: flow / cvd / correlation / technical "
                        "calc + demand / auction / delta analysis. Focused, no envelope.")
    p.add_argument("--structure", action="store_true",
                   help="market structure: volume_profile / technical calc + "
                        "regime / stage analysis. Focused, no envelope.")
    p.add_argument("--positioning", action="store_true",
                   help="positioning & derivatives: oi analysis only (weighted "
                        "contracts, inflow/outflow, implied value). Focused, no envelope.")

    # --- Designated read tool (the interpretation plane's read surface) ---
    p.add_argument("--read", action="store_true",
                   help="designated read tool: Redis-first, Postgres-fallback read "
                        "of the canonical MarketRunEnvelope. The outer-CLI read path "
                        "that reaches both containers. Combine with --mode.")
    p.add_argument("--run-id", help="read one exact persisted collated envelope by run ID")
    p.add_argument("--mode", choices=("snapshot", "inventory", "full"), default="snapshot",
                   help="output shape for --read: snapshot (headline scalars, default), "
                        "inventory (section key lists + coverage), or full (raw payload)")
    p.add_argument("--read-errors", action="store_true",
                   help="--read: include the source_metadata errors list verbatim in the "
                        "response (default: only the error count is surfaced)")

    # --- Poller control plane (dynamic symbol selection) ---
    # Redis control-key writes/reads. No Binance calls, no envelope, no
    # persistence. The poller picks up changes within one poll interval.
    p.add_argument("--poller-symbols", metavar="SYM[,SYM...]",
                   help="set the poller's active symbols via the Redis control "
                        "key (e.g. --poller-symbols SOLUSDT). Takes effect "
                        "within one poll interval without restarting the poller.")
    p.add_argument("--poller-symbols-reset", action="store_true",
                   help="delete the Redis control key so the poller falls back "
                        "to POLL_SYMBOLS / SYMBOLS env defaults")
    p.add_argument("--poller-status", action="store_true",
                   help="read the poller's live status (active symbols, source, "
                        "last cycle time) from Redis")
    p.add_argument("--keystone-history", action="store_true",
                   help="read the cross-cycle keystone ledger (Redis first, "
                        "Postgres fallback) and derive the keystone migration "
                        "verdict. Returns the recorded keystone series + "
                        "UP/DOWN/FLAT per cycle + the aggregate verdict.")
    p.add_argument("--microstructure-status", action="store_true",
                   help="read the isolated Binance spot microstructure capture status from Redis; "
                        "does not start capture, run calculations, or invoke NOOA")
    p.add_argument("--inference", action="store_true",
                   help="trigger ONE statistical inference cycle (manual trigger; "
                        "combine with --inference-force — the manual wake IS the trigger)")
    p.add_argument("--inference-force", action="store_true",
                   help="with --inference: bypass wake predicates — the manual trigger IS the wake")
    p.add_argument("--history-limit", type=int, default=100,
                   help="max keystone history entries to read for "
                        "--keystone-history (default 100)")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    # --- Route 0.5: poller control plane (dynamic symbol selection).
    # Pure Redis read/write — no Binance calls, no envelope, no persist.
    if args.poller_symbols or args.poller_symbols_reset or args.poller_status:
        result = asyncio.run(_poller_control(args))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "ok" else 1

    # --- Route 1: refresh derivative evidence (write-only to Redis cache).
    if args.refresh_derivatives:
        result = asyncio.run(_refresh_derivatives(args))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(json.dumps({"status": "ok" if result.get("stream_id") else "failed",
                              "symbol": result.get("symbol"),
                              "cache_ttl_seconds": result.get("cache_ttl_seconds"),
                              "with_cross_asset": result.get("with_cross_asset")},
                             indent=2, default=str))
        return 0 if result.get("stream_id") else 1

    # --- Route 1.5: calculation-model group commands (Pass 3 segregated surface).
    # Runs ONLY the calc + analysis sections for the requested domain groups.
    # No envelope, no persistence. Redis-only (no DATABASE_URL) except --wall
    # which reads wall history (Postgres fallback).
    requested_groups = tuple(
        g for g, flag in (("wall", args.wall), ("flow", args.flow),
                          ("structure", args.structure), ("positioning", args.positioning))
        if flag
    )
    if requested_groups:
        result = asyncio.run(_run_groups(args, requested_groups))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") in ("healthy", "degraded") else 1

    # --- Route 2: run the calculation pipeline end-to-end (canonical).
    if args.analyze:
        result = asyncio.run(_run_analyze(args))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            status = result.get("status", "unknown")
            run_id = result.get("run_id")
            persisted = result.get("persistence", {})
            summary = {
                "status": status,
                "symbol": result.get("symbol"),
                "run_id": run_id,
                "window_minutes": result.get("window_minutes"),
                "derivatives_cache": result.get("derivatives_cache"),
                "persistence": persisted,
                "elapsed_ms": result.get("elapsed_ms"),
            }
            print(json.dumps(summary, indent=2, default=str))
        return 0

    # --- Route 3: designated read tool — Redis-first, Postgres-fallback.
    if args.read:
        result = asyncio.run(_read_market(args, args.mode))
        print(json.dumps(result, indent=2, default=str))
        # Empty read (redis miss + postgres absent) exits 0 — null is a
        # legitimate "no data" result. Schema mismatch raises out of
        # _read_market (non-zero), never coerced to a soft failure.
        return 0

    # --- Route 3.5: cross-cycle keystone ledger read + migration verdict.
    # Redis is the live projection (read first); Postgres is the durable
    # fallback when the Redis stream is empty. No agents involved.
    if args.keystone_history:
        result = asyncio.run(_read_keystone_history(args))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("history") or result.get("cycles") else 1

    # --- Route 3.6: isolated microstructure capture health only.
    if args.microstructure_status:
        result = asyncio.run(_read_microstructure_status(args))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") is not None else 1

    # --- Route 3.7: statistical inference cycle (outer-CLI trigger).
    # Delegates to the engine runner — one wake-aware cycle, or a forced
    # cycle with --inference-force. Never a lazy loop; use the inner CLI's
    # `market inference watch` for the event-driven engine loop.
    if args.inference:
        from market_service.nooa_harness.inference_runner import run_inference_once

        result = asyncio.run(run_inference_once(
            args.symbol, force=args.inference_force,
        ))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") != "no_wake" else 1

    # --- Route 4: legacy dev-only live waveform (explicit --live).
    if args.live:
        result = asyncio.run(build(args.symbol, args.trades, args.depth, args.bucket_window))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(render(result["core"]))
        return 0

    # --- Default (no action flags): run the canonical calculation pipeline.
    # This is the coherent E2E: the poller feeds the Redis stream, and the
    # harness reads that stream and runs the deterministic calculations into
    # a MarketRunEnvelope v2. Replaces the old default of the legacy live
    # waveform (now behind --live).
    result = asyncio.run(_run_analyze(args))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(json.dumps({
            "status": result.get("status"),
            "symbol": result.get("symbol"),
            "run_id": result.get("run_id"),
            "window_minutes": result.get("window_minutes"),
            "derivatives_cache": result.get("derivatives_cache"),
            "persistence": result.get("persistence"),
            "envelope": result.get("envelope")
            if result.get("envelope_summary") is None
            else result.get("envelope_summary"),
            "elapsed_ms": result.get("elapsed_ms"),
        }, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Calculation-pipeline route handlers
# ---------------------------------------------------------------------------


async def _poller_control(args: argparse.Namespace) -> dict[str, Any]:
    """Poller control plane: set / reset / read the dynamic symbol selection.

    Pure Redis read/write — no Binance calls, no envelope, no persistence.
    The long-running poller container reads the control key once per cycle,
    so a change takes effect within one poll interval without a restart.

    Precedence inside the poller:
        Redis control key  >  POLL_SYMBOLS env  >  SYMBOLS env
    """
    settings = Settings.from_redis_env()
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        if args.poller_symbols:
            symbols = [s.strip().upper() for s in args.poller_symbols.split(",") if s.strip()]
            if not symbols:
                return {"status": "error", "error": "no valid symbols provided"}
            await store.set_poller_symbols(symbols)
            return {
                "status": "ok",
                "action": "set",
                "control_key": store.poller_control_key(),
                "active_symbols": sorted(set(symbols)),
                "note": "poller picks this up within one poll interval",
            }
        if args.poller_symbols_reset:
            await store.clear_poller_symbols()
            return {
                "status": "ok",
                "action": "reset",
                "control_key": store.poller_control_key(),
                "fallback_symbols": list(settings.poll_symbols),
                "note": "poller reverted to POLL_SYMBOLS / SYMBOLS env",
            }
        # --poller-status
        status = await store.read_poller_status()
        if status is None:
            return {
                "status": "error",
                "action": "status",
                "error": "no poller status found — is the poller container running?",
            }
        return {"status": "ok", "action": "status", **status}
    finally:
        await store.close()


async def _refresh_derivatives(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch + cache one derivative evidence snapshot in Redis.

    No pipeline run, no agents — pure Redis write so downstream
    ``--analyze`` cycles can reuse the cache within ``--deriv-ttl`` seconds.
    """
    from market_service.clients.binance import Binance
    from market_service.nooa_harness.pipeline_interpretation import fetch_derivative_evidence

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    log.info("harness --refresh-derivatives %s (cross_asset=%s ttl=%ss)",
             symbol, args.with_cross_asset, args.deriv_ttl)

    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix,
        settings.redis_stream_maxlen,
    )
    try:
        async with Binance() as client:
            deriv = await fetch_derivative_evidence(
                client, symbol,
                include_cross_asset=args.with_cross_asset,
            )
        stream_id = await store.publish_derivative_evidence(
            symbol, deriv, ttl_s=args.deriv_ttl,
        )
        remaining_ttl = await store.derivative_cache_ttl(symbol)
        return {
            "status": "ok",
            "symbol": symbol,
            "stream_id": stream_id,
            "cache_key": store.derivatives_key(symbol),
            "cache_ttl_seconds": remaining_ttl,
            "with_cross_asset": args.with_cross_asset,
            "deriv_ttl_seconds": args.deriv_ttl,
            "observed_at_ms": deriv.get("observed_at_ms"),
            "futures_keys_present": sorted(
                k for k, v in (deriv.get("futures") or {}).items() if v is not None
            ),
            "cross_asset_keys_present": sorted(
                k for k, v in (deriv.get("cross_asset") or {}).items() if v
            ),
        }
    finally:
        await store.close()


async def _run_analyze(args: argparse.Namespace) -> dict[str, Any]:
    """Run the calculation pipeline and persist to the canonical ledger.

    No agents are invoked. Returns a dict with the run_id, persistence
    result, derivatives cache status, and either the full envelope or the
    compact envelope_summary (per ``--envelope-summary``).
    """
    from market_service.nooa_harness.pipeline_interpretation import (
        WINDOW_MINUTES_MAP, run_cycle,
    )

    # Postgres is only required when we actually persist. Redis-only.
    settings = Settings.from_env() if not args.no_persist else Settings.from_redis_env()
    symbol = args.symbol.upper()
    window_minutes = WINDOW_MINUTES_MAP.get(args.window, 15)
    log.info(
        "harness --analyze %s window=%dm deriv_ttl=%ss cross_asset=%s "
        "include_derivatives=%s force_refresh=%s persist=%s",
        symbol, window_minutes, args.deriv_ttl, args.with_cross_asset,
        not args.no_derivatives, args.force_refresh_derivatives,
        not args.no_persist,
    )

    started = time.monotonic()

    envelope = await run_cycle(
        settings, symbol, window_minutes,
        deriv_ttl_s=args.deriv_ttl,
        include_cross_asset=args.with_cross_asset,
        include_derivatives=not args.no_derivatives,
        force_refresh_derivatives=args.force_refresh_derivatives,
        persist=not args.no_persist,
    )

    # --no-persist is now enforced at the pipeline boundary: persist=False
    # skips Postgres + Redis write entirely (real dry run).
    persistence_status = "persisted" if not args.no_persist else "dry_run"

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    envelope_dict = envelope if isinstance(envelope, dict) else envelope.to_dict()

    derivatives_cache = _summarize_derivatives_cache(envelope_dict)
    out: dict[str, Any] = {
        "status": envelope_dict.get("status"),
        "symbol": envelope_dict.get("symbol"),
        "run_id": envelope_dict.get("run_id"),
        "window_minutes": window_minutes,
        "elapsed_ms": elapsed_ms,
        "persistence": {"mode": persistence_status},
        "derivatives_cache": derivatives_cache,
    }

    if args.envelope_summary:
        # Successor of the retired local _projection duplicate (2026-08-31):
        # the shared read_paths inventory view — the same projection the
        # agent's market.read mode="inventory" returns.
        from market_service.runtime import read_paths

        out["envelope_summary"] = read_paths.market_inventory(envelope_dict)
    else:
        out["envelope"] = envelope_dict

    return out


async def _run_groups(args: argparse.Namespace, groups: tuple[str, ...]) -> dict[str, Any]:
    """Calculation-model group commands (Pass 3): typed GroupEnvelope emission.

    Reads raw evidence DIRECTLY from the Redis store via
    ``pipeline.run_group_cycle`` — no MarketRunEnvelope, no persistence.
    Each requested group is returned as a versioned ``GroupEnvelope``
    (schema_version=1) containing only its GROUP_MAP sections, bounded by
    construction so it always fits an LLM context. This is the primary
    read interface for targeted analysis; ``--analyze`` remains for the
    canonical persisted audit record.
    """
    from market_service.nooa_harness.pipeline_interpretation import (
        WINDOW_MINUTES_MAP, run_group_cycle,
    )

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    window_minutes = WINDOW_MINUTES_MAP.get(args.window, 15)

    started = time.monotonic()
    try:
        envelopes = await run_group_cycle(
            settings, symbol, window_minutes, groups,
            deriv_ttl_s=args.deriv_ttl,
            include_cross_asset=args.with_cross_asset,
            include_derivatives=not args.no_derivatives,
            force_refresh_derivatives=args.force_refresh_derivatives,
            depth=args.depth or settings.depth_levels,
        )
    except ValueError as e:
        return {"status": "invalid", "symbol": symbol, "groups": list(groups), "error": str(e)}

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    envelope_dicts = {kind: ge.to_dict() for kind, ge in envelopes.items()}
    run_ids = sorted({d["run_id"] for d in envelope_dicts.values()})
    statuses = [d["status"] for d in envelope_dicts.values()]

    out: dict[str, Any] = {
        "status": "degraded" if "degraded" in statuses else (
            statuses[0] if statuses else "invalid"
        ),
        "symbol": symbol,
        "groups": list(groups),
        "window_minutes": window_minutes,
        "run_id": run_ids[0] if len(run_ids) == 1 else run_ids,
        # Substrate attribution: section id → owning calculation substrate(s),
        # merged across the requested groups. The interpretation plane uses it
        # to explain which substrate produced each section in the envelopes.
        "substrate_provenance": {
            sec: tuple(prov)
            for d in envelope_dicts.values()
            for sec, prov in (d.get("substrate_provenance") or {}).items()
        },
        "group_envelopes": envelope_dicts,
        # Back-compat view of the per-group sections (superseded by
        # group_envelopes; kept so existing readers don't break).
        "results": {
            kind: {
                "calculations": d["calculations"],
                "analysis": d["analysis"],
            }
            for kind, d in envelope_dicts.items()
        },
        "elapsed_ms": elapsed_ms,
        "errors": [
            dict(e, group=kind)
            for kind, d in envelope_dicts.items()
            for e in d["errors"]
        ],
    }
    # Keystone-history verdict rides along with the wall group.
    if "wall" in groups:
        out["keystone_history"] = await _keystone_history_payload(
            settings, symbol, args.history_limit)
    return out


async def _keystone_history_payload(settings: Settings, symbol: str, limit: int) -> dict[str, Any]:
    """Read the cross-cycle keystone ledger + migration verdict (for --wall)."""
    from market_service.calculations.orderbook import keystone_cycle_migration
    from market_service.runtime.postgres_store import PostgresRuntimeStore
    store = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    history: list[dict[str, Any]] = []
    try:
        history = await store.read_keystone_history(symbol, count=max(1, limit or 100))
    finally:
        await store.close()
    if not history and settings.database_url:
        pg = PostgresRuntimeStore(settings.database_url)
        try:
            await pg.connect()
            history = await pg.read_keystone_history(symbol, limit=max(1, limit or 100))
        finally:
            await pg.close()
    migration = keystone_cycle_migration(history) if history else {"cycles": [], "verdict": None, "net_buckets": 0}
    return {
        "history_count": len(history),
        "cycles": migration.get("cycles"),
        "verdict": migration.get("verdict"),
        "net_buckets": migration.get("net_buckets"),
    }


async def _read_market(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    """Designated outer-CLI read tool — Redis-first, Postgres fallback.

    Reads the canonical collated market run payload and routes it through
    the shared ``read_paths`` projections (snapshot / inventory / full).
    ``source`` tags which plane served the payload. Postgres is opened only
    when ``DATABASE_URL`` is set, so the tool stays usable on a Redis-only
    host venv. Read-only: no writes, no agents, no nooa_harness crossing.
    """
    from market_service.runtime import read_paths
    from market_service.runtime.postgres_store import PostgresRuntimeStore

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()

    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    postgres = None
    if settings.database_url:
        postgres = PostgresRuntimeStore(settings.database_url)
    try:
        if postgres is not None:
            await postgres.connect()
        payload, source = await read_paths.read_collated_with_fallback(
            redis, postgres, symbol=symbol, run_id=args.run_id,
        )
    finally:
        await redis.close()
        if postgres is not None:
            await postgres.close()

    if payload is None:
        return {
            "symbol": symbol,
            "run_id": args.run_id,
            "source": None,
            "mode": mode,
            "read": None,
            "errors": ["no run persisted (redis miss, postgres absent/empty)"],
        }

    if mode == "full":
        read = payload
    elif mode == "inventory":
        read = read_paths.market_inventory(payload)
    else:
        read = read_paths.market_snapshot(payload)

    out: dict[str, Any] = {
        "symbol": symbol,
        "run_id": payload.get("run_id"),
        "source": source,
        "mode": mode,
        "read": read,
    }
    if args.read_errors:
        out["errors"] = list(payload.get("errors") or [])
    return out


async def _read_keystone_history(args: argparse.Namespace) -> dict[str, Any]:
    """Read the cross-cycle keystone ledger and derive the migration verdict.

    Redis is the live projection (read first); Postgres is the durable
    fallback when the Redis stream is empty. The migration verdict is
    derived read-side via ``keystone_cycle_migration`` (pure function) —
    no agents, no re-fetch. Follows the null discipline: an empty ledger
    returns ``history: []`` and ``verdict: None`` (no fabricated state).
    """
    from market_service.calculations.orderbook import keystone_cycle_migration
    from market_service.runtime.postgres_store import PostgresRuntimeStore

    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    limit = max(1, int(args.history_limit or 100))

    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    history: list[dict[str, Any]] = []
    source = "redis"
    try:
        history = await store.read_keystone_history(symbol, count=limit)
    finally:
        await store.close()

    if not history and settings.database_url:
        # Durable fallback — the Redis stream is live/bounded, Postgres is
        # the ledger that survives Redis restarts.
        pg = PostgresRuntimeStore(settings.database_url)
        try:
            await pg.connect()
            history = await pg.read_keystone_history(symbol, limit=limit)
            source = "postgres"
        finally:
            await pg.close()

    migration = keystone_cycle_migration(history) if history else {
        "cycles": [], "verdict": None, "net_buckets": 0,
    }
    return {
        "symbol": symbol,
        "source": source if history else None,
        "history_count": len(history),
        "history": history,
        "cycles": migration.get("cycles"),
        "verdict": migration.get("verdict"),
        "net_buckets": migration.get("net_buckets"),
    }


async def _read_microstructure_status(args: argparse.Namespace) -> dict[str, Any]:
    """Read the separate spot-capture health projection, Redis-only."""
    settings = Settings.from_redis_env()
    symbol = args.symbol.upper()
    store = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    try:
        status = await store.read_microstructure_status("spot", symbol)
        return {
            "symbol": symbol,
            "venue": "spot",
            "status_key": store.microstructure_status_key("spot", symbol),
            "raw_stream": store.microstructure_raw_stream("spot", symbol),
            "event_stream": store.microstructure_event_stream("spot", symbol),
            "ofi_stream": store.microstructure_ofi_stream("spot", symbol),
            "status": status,
        }
    finally:
        await store.close()


def _summarize_derivatives_cache(envelope_dict: dict[str, Any]) -> dict[str, Any]:
    """Pull the derivative-fetch metadata from an envelope dict."""
    meta: dict[str, Any] = {"include_derivatives": None, "observed_at_ms": None}
    cs = envelope_dict.get("canonical_state") or {}
    evidence = (cs.get("data-access") or {}).get("evidence") or {}
    if isinstance(evidence, dict):
        meta["observed_at_ms"] = evidence.get("derivative_observed_at_ms")
        fut = evidence.get("futures") or {}
        if isinstance(fut, dict):
            present = [k for k in ("oi_history", "taker_buy_sell", "top_ls",
                                    "global_ls", "klines") if fut.get(k)]
            meta["fields_present"] = present
            meta["include_derivatives"] = bool(present)
        cross = evidence.get("cross_asset")
        if cross:
            meta["cross_asset"] = {
                "tickers": len(cross.get("tickers_24h") or []),
                "funding": len(cross.get("funding") or []),
            }
    return meta


if __name__ == "__main__":
    raise SystemExit(main())
