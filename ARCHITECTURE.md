# Market Flow — Phase 1 Architecture

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
