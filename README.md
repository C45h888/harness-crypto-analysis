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

# Persistent collector (needs DATABASE_URL; see docker-compose.yml)
.venv/bin/python -m market_service.collector

# Archived exploratory tools still run from their old home:
.venv/bin/python legacy/session_regime.py
```

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

## Stack (database)

`docker compose up --build` runs the collector against Postgres bound to
`127.0.0.1:5433`. Schema: `market_snapshot` + `signal_event`. Run against your
own `.env` (see `.env.example`) — `.env` is private and gitignored.

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
- `legacy/README.md` — classification of every archived script