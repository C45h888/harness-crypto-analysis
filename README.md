# Crypto AI Analytics

Live crypto market-data system for a discretionary SOL/ETH/BTC perp trader. It
collects evidence, computes deterministic metrics, and renders an auditable
market snapshot for a human (or an analyst model) to interpret. It does **not**
place, size, or manage orders.

## Runtime layout (single coherent state)

```text
crypto-ai-anal/
├── market_service/          # canonical package (one authority)
│   ├── clients/             #   Binance / CoinGecko / CryptoQuant
│   ├── calculations/        #   flow + deterministic signals
│   ├── analysis/            #   market / oi / liquidations / macro
│   ├── commands/snapshot.py #   harness snapshot CLI
│   ├── collector.py         #   persistent DB collector
│   └── config.py
├── legacy/                  # archived one-off tools (see legacy/README.md)
│   └── data/                #   historical runtime logs
├── db/init/001_schema.sql   # Postgres schema
├── tests/
├── Dockerfile               # runs market_service.collector
└── docker-compose.yml
```

`market_service/` is the single runtime authority. **No code inside
`market_service/` imports from `legacy/`.** All legacy scripts are tucked under
`legacy/` so the root stays clean.

## Entrypoints

```bash
# Live harness snapshot (JSON contract with raw evidence + derived metrics)
.venv/bin/python -m market_service.commands.snapshot SOLUSDT --json

# Same data, pretty text
.venv/bin/python -m market_service.analysis.market SOLUSDT

# CLEAN AGGREGATED MARKET DATA for the model — the single harness surface.
# All clean data for a symbol in one contract (core snapshot + signals + OI +
# liquidation + macro). The model reads THIS, not scattered scripts.
.venv/bin/python -m market_service.commands.harness SOLUSDT --json
.venv/bin/python -m market_service.commands.harness SOLUSDT --json --with-scripts

# Verify every manifest script runs (Phase 1 gate)
.venv/bin/python -m market_service.commands.run_all
.venv/bin/python -m market_service.commands.run_all --domain analysis

# Persistent collector (needs DATABASE_URL; see docker-compose.yml)
.venv/bin/python -m market_service.collector

# Archived exploratory tools still run from their old home:
.venv/bin/python legacy/session_regime.py
```

## Shared-domain map (container-split prep)

`market_service/manifest.py` classifies every runnable script into one shared
domain. This is the single source of truth for the target multi-container
architecture and drives `harness` / `run_all`.

| Domain | What | Members |
|---|---|---|
| `data-access` | pulls raw evidence | `market_service.clients.*`; legacy: `continue_monitor`, `sol_deep_monitor`, `sol_monitor_alerts`, `cryptoquant_client` |
| `calculation` | pure math over inputs | `market_service.calculations.*`; legacy: `flow5m` |
| `analysis` | derived interpretation | `market_service.analysis.*`; most legacy tools |
| `monitor` | watch / summarise | legacy: `summarize_monitor` |

## Live JSON contract

`python -m market_service.commands.snapshot SYMBOL --json` returns:

- contract metadata + version
- request parameters + timestamps + coverage spans
- source status (`healthy` / `degraded`) and per-endpoint errors
- raw evidence (spot/futures order books, tickers, trades, funding, OI)
- deterministic flow metrics (CVD, OBI, VWAP, buy/sell, spread) — as *figures*
- spot/futures CVD correlation
- deterministic signals with the rule + input evidence that fired them
- OI, liquidation-pressure, and macro analyses (when `--window` path used)
- CryptoQuant on-chain context

`null` means a source did not provide a value — it is never substituted with
zero. Use `--trades N` / `--depth N` to cap raw payload size.

## Stack

- **Binance** public REST (Spot + USD-M futures) — no API key for public data
- **CoinGecko** public — global/market-cap/dominance overlay
- **CryptoQuant** via MCP bridge — on-chain metric descriptions (basic plan:
  numerics locked, descriptions/interpretations still flow)
- **Python 3.12**, `.venv/`, deps in `requirements.txt`
- **PostgreSQL** via Docker for the persistent collector
- **Redis 7** via Docker for latest state, telemetry streams, and bounded refresh commands

## Stack (database)

`docker compose up --build` runs Redis, PostgreSQL, and the collector. PostgreSQL
is bound to `127.0.0.1:5433`; Redis is bound to `127.0.0.1:6379` for local
inspection. Redis uses AOF persistence and capped streams. Schema:
`market_snapshot` + `signal_event`. Run against your own `.env` (see
`.env.example`) — `.env` is private and gitignored.

The infrastructure adapters are in `market_service/runtime/`:

- `RedisRuntimeStore` owns latest-state keys, telemetry streams, and refresh commands.
- `PostgresRuntimeStore` owns the durable ledger connection and read boundary.
- `market_service.commands.health` checks both services.

Redis key conventions are versioned at the adapter boundary:

```text
marketflow:latest:<SYMBOL>:<SOURCE>   latest state projection
marketflow:stream:market:<SYMBOL>     append-only telemetry
marketflow:stream:commands             bounded refresh requests
marketflow:stream:results              node results
```

The collector publishes the validated snapshot to the `collector` latest-state
projection and its symbol telemetry stream after the PostgreSQL transaction
commits. Redis is therefore a live projection and command bus; PostgreSQL
remains the durable ledger.

```bash
.venv/bin/python -m market_service.commands.health
```

```bash
docker compose exec postgres psql -U marketflow -d marketflow \
  -c 'SELECT symbol, observed_at, price, spot_buy_share FROM market_snapshot ORDER BY observed_at DESC LIMIT 1;'
```

## Testing

```bash
.venv/bin/python -m unittest discover -s tests -q
```

## Docs

- `ARCHITECTURE.md` — Phase 1 architecture, operating rules, migration path
- `docs/CANONICAL_RUNTIME_DOCTRINE.md` — governing authority, state, evidence,
  determinism, and agent-boundary doctrine
- `legacy/README.md` — classification of every archived script
