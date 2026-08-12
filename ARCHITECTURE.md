# Market Flow — Phase 1 Architecture

The governing design contract for all future runtime and agent work is
[`docs/CANONICAL_RUNTIME_DOCTRINE.md`](docs/CANONICAL_RUNTIME_DOCTRINE.md).
The implementation contract for the next containerization task is
[`docs/CONTAINERIZATION_CONTRACT.md`](docs/CONTAINERIZATION_CONTRACT.md).
The Docker acceptance procedure is defined in
[`docs/DOCKER_RUNTIME_VERIFICATION_DOCTRINE.md`](docs/DOCKER_RUNTIME_VERIFICATION_DOCTRINE.md).
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
2. ✅ DONE — reusable calculations extracted from the legacy scripts into tested canonical modules; legacy scripts migrated and deleted (provenance: `market_service/manifest.MIGRATIONS`).
3. Add a read-only `market briefing` query for Nous/Hermes over the collated
   Redis/PostgreSQL run envelope, `latest_market_state`, and recent
   `signal_event` rows.
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

## Target architecture (the shape we're building toward)

Shared domains each get their own container, so the runtime is broken along
data-point boundaries, not as one giant app:

```text
                 ┌─────────────────────────────────────────────┐
   Binance REST  │  clients/ data-access container            │  pulls raw
   CoinGecko     │  (market_service.clients + data pullers)   │  evidence
   CryptoQuant   └──────────────────┬──────────────────────────┘
                 ┌──────────────────┴──────────────────────────┐
                 │  calculations container                     │  pure math:
                 │  (flow, orderbook, volume, technical)       │  no I/O
                 └──────────────────┬──────────────────────────┘
                 ┌──────────────────┴──────────────────────────┐
                 │  analysis container                         │  regime/OI/
                 │  (market, oi, liq, macro, auction, demand,  │  walls/stage
                 │   regime, wall_migration, path_absorption)  │
                 └──────────────────┬──────────────────────────┘
                                    ▼
                 aggregated in  Redis  (live/collated)  +  Postgres  (durable)
```

Each container = one shared domain, run together, all feeding the same
aggregation layer. Clean separation of concerns — a calc change doesn't touch
the data path.

## Migration status (completed)

All legacy exploratory scripts have been fully migrated into `market_service`
(provenance in `market_service/manifest.MIGRATIONS`) and deleted. There is no
`legacy/` tree, and nothing inside `market_service` imports from it.

| Domain | Modules |
|---|---|
| data-access | `market_service.clients.*` |
| calculation | `calculations.flow`, `orderbook`, `volume_profile`, `technical`, `signals` |
| analysis | `analysis.market`, `oi`, `liquidations`, `macro`, `auction`, `demand`, `regime`, `wall_migration`, `path_absorption`, `stage` |

## Current implementation status

"All scripts run and give clean market data to the Hermes harness." Verified:
every canonical module imports, the `run_all` gate is green, and the canonical
test suite is green. The live collation seam is also operational: a SOLUSDT
`MarketRunEnvelope` can be persisted to PostgreSQL and published to Redis with
the same `run_id`.

## Domain container split (in progress)

Per `docs/CONTAINERIZATION_CONTRACT.md` the unified runtime is split into
independently-runnable domain services:

  * `data-access`     - reads external APIs, publishes raw evidence
  * `calculations`    - runs pure deterministic math over evidence
  * `analysis`        - produces interpretation over calculations
  * `orchestrator`    - timer-driven; sequences the three via `stream:commands`
  * `collector`       - unchanged snapshot + signal-event path
  * `collator`        - stable entrypoint `python -m market_service.commands.collate`

The orchestrator mints a `run_id`, sends one `RefreshCommand` per domain
keyed on that `run_id`, then invokes the collator with
`--from-domain-state --run-id <run_id>`. The collator reads the three
domain envelopes from Redis, **verifies** they all carry the orchestrator's
`run_id`, and persists a `MarketRunEnvelope` whose `run_id` matches. The
orchestrator also asserts the persisted `run_id` matches the one it tracked
so a cycle is never silently attributed to a different run.

Adapter discipline: every canonical function call goes through
`market_service.nodes.contracts.strict_call` with shape adapters that raise
`ContractViolation` on wrong input shape. The handler captures violations as
structured `errors` on the envelope and produces `degraded` or `invalid`
status - never silently swallows them.

## Deferred (next phases)

- Add Redis node instance/session keyspaces for future model reads.
- Deterministic-logic layer over Redis; Postgres as durable ledger.
