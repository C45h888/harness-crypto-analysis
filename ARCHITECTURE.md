# Market Flow — Phase 1 Architecture

The governing design contract for all future runtime and agent work is
[`docs/CANONICAL_RUNTIME_DOCTRINE.md`](docs/CANONICAL_RUNTIME_DOCTRINE.md).
This document describes the current Phase 1 implementation beneath that
doctrine.

## Goal

Build a trustworthy, inspectable market-data system for a human trader. It collects and explains conditions; it does not place, size, or manage orders.

## Target flow

```text
Binance spot + USD-M futures
            |
            v
  collector (30-second snapshots)
            |
            +--> deterministic signal rules --> signal_event
            |
            v
        PostgreSQL
            |
            v
Nous/Hermes analyst chat --> evidence-backed market assessment --> human decides
```

## What gets stored

- `market_snapshot`: normalized price, depth imbalance, timed spot/futures CVD, funding, mark/index, OI, and measurement coverage.
- `signal_event`: only meaningful, deterministic state changes with their input evidence. Persistent conditions remain visible in snapshots but do not spam identical events every polling interval.

Raw every-trade storage is intentionally deferred. It is expensive, retention-heavy, and is not needed to preserve the current discretionary workflow. Add it later only for replay/backtesting or forensic tape work.

## Operating rules

1. Deterministic calculations produce facts and signals first.
2. Nous/Hermes may interpret database-backed evidence, but never invent missing evidence.
3. A human explicitly approves every trade; there is no exchange private key or execution service in this stack.
4. Each signal records the values that caused it, so chat conclusions can be audited.
5. The collector records actual trade-window coverage. A fixed number of trades must never silently be labeled a five- or fifteen-minute window.

## Migration path

1. Start the collector and compare its snapshots against the current scripts during live sessions.
2. Extract reusable calculations from the legacy scripts into tested modules.
3. Add a read-only `market briefing` query for Nous/Hermes over `latest_market_state` and recent `signal_event` rows.
4. Add dashboards, replay/backtests, and optional LLM summarization only after data quality is proven.

## Run

```bash
cp .env.example .env
# Set a strong POSTGRES_PASSWORD and keep .env private.
docker compose up --build
```

The database is bound only to `127.0.0.1:5433`. To inspect recent evidence:

```bash
docker compose exec postgres psql -U marketflow -d marketflow -c 'SELECT symbol, observed_at, price, spot_buy_share, futures_buy_share FROM latest_market_state;'
```

## Known Phase 1 limits

- One venue: Binance is useful but not the whole market.
- L2 depth is a point-in-time snapshot and can be cancelled; it is evidence, not proof of support or resistance.
- Signals are alerts for human review, not trading recommendations.

arget architecture (the shape we're building toward)
Shared domains each get their own container, so the runtime is broken along data-point boundaries, not as one giant app:


                 ┌─────────────────────────────────────────────┐
   Binance REST  │  clients/ data-access container            │  pulls raw
   CoinGecko     │  (market_service.clients + data pullers)   │  evidence
   CryptoQuant   └──────────────────┬──────────────────────────┘
                 ┌──────────────────┴──────────────────────────┐
                 │  calculations container                     │  pure math:
                 │  (flow, signals, CVD/OBI/VWAP)              │  no I/O
                 └──────────────────┬──────────────────────────┘
                 ┌──────────────────┴──────────────────────────┐
                 │  analysis container                         │  regime/OI/
                 │  (market, oi, liquidations, macro, walls…  │  liq/macro
                 └──────────────────┬──────────────────────────┘
                                    ▼
                 aggregated in  Redis  (live/collated)  +  Postgres  (durable)


Each container = one shared domain, run together, all feeding the same aggregation layer. Clean separation of concerns — a calc change doesn't touch the data path.

Phase 1 deliverable (this phase, per your directive)
"All scripts run and give clean market data to the Hermes harness." The multi-container + Redis aggregation is the target, but Phase 1 proves the foundation: every script actually executes and outputs structured, clean data the harness can consume. Nothing gets aggregated yet.

Phase 1 workstream
1. Script surface inventory + baseline — enumerate every runnable script (package modules + legacy/*.py), classify each by domain, and confirm each one runs (the syntax gap is already closed; this adds live runnability + output shape). Flag any dead/broken scripts.
2. Clean market data contract — define one consistent output shape per script so the harness gets clean data, not terminal soup: {data_source, symbols, ts, evidence, derived_metrics, status, errors} — same null ≠ 0 discipline the snapshot contract already uses. Where a script prints prose today, that moves behind the structured JSON.
3. Hermes harness ingestion — one clean entry the harness reads: python -m market_service.harness SYMBOL --json (or equivalent) that wires the running scripts' output into the structured contract. This is the "clean market data → Hermes harness" surface.
4. Domain mapping table — assign every script to a container domain now (data-access / calculation / analysis / monitor), so the container split in the next phase is mechanical, not a re-derivation.
5. Verification (phase 1 only) — RUN ALL harness that executes each script, asserts it produced clean structured output, and reports pass/fail per script.

Domain mapping (prep for the split)
Domain / container: data-access / clients
Scripts (package + legacy): market_service.clients., legacy: cryptoquant_client,
  continue_monitor, sol_monitor_alerts, deep_keystone
────────────────────────────────────────
Domain / container: calculation
Scripts (package + legacy): market_service.calculations., flow.py, flow5m, wrappers
────────────────────────────────────────
Domain / container: analysis
Scripts (package + legacy): market_service.analysis.*, oi_analysis, liquidations, macro,
  demand_diagnostic, session_regime, path_absorption, wall_analysis, wall_state_check,
  seller_wall_check, spot_fut_assess, scan_levels, long_term_flow
────────────────────────────────────────
Domain / container: monitor / exploratory
Scripts (package + legacy): sol_deep_monitor, summarize_monitor, run/scan one-offs

Deferred (next planning sessions)
- Building/connecting the analysis + calculation containers.
- Redis node with instance/session keyspaces; model reads Redis only.
- Deterministic-logic layer over Redis; Postgres as durable ledger.