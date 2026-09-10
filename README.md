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
├── db/init/001_schema.sql   # Postgres schema
├── tests/
├── Dockerfile               # runs market_service.collector
└── docker-compose.yml
```

`market_service/` is the single runtime authority. All former legacy
exploratory scripts have been fully migrated into it (`market_service/manifest.py`
`MIGRATIONS` records provenance) and deleted — there is no legacy tree.

## Entrypoints

## Runtime paths

Docker Compose is the canonical runtime. The root `.env` is loaded by Compose;
`.env.example` is only a template and is never used directly at runtime.

For local Python-only development, use Python 3.12 through `uv`; the host
macOS Python 3.9 is unsupported:

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.harness SOLUSDT --json
```

The following Docker command is the canonical analyst entrypoint:

```bash
docker compose --profile tools run --rm harness SOLUSDT \
  --inference --inference-force --task "is short-term sell pressure exhausting?" --json
```

The harness reads canonical state and triggers inference. **It never computes
and never invokes workers.** The calculation container owns every fire-tick
(see "Authority split" below); the poller owns exchange egress.

```bash
# Start the canonical runtime (poller + calculation workers)
docker compose up -d
docker compose --profile substrates up -d calculation

# WORKER STATE — the warm plane the calculation container keeps fresh.
# This is the default surface for the terminal-based agents.
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.harness SOLUSDT --json

# One substrate only, full payload
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.harness SOLUSDT --substrate-read --substrate tape --json

# Read the canonical collated envelope (Redis-first, Postgres fallback)
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.harness SOLUSDT --read --mode snapshot --json

# Read an exact immutable run by run_id
docker compose --profile tools run --rm harness --run-id <RUN_ID> --json

# Capture health, keyed on MICROSTRUCTURE_VENUE (default futures)
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.harness SOLUSDT --microstructure-status

# Verify every canonical runtime module imports cleanly
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.run_all
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.run_all --domain calculation

# Persistent collector (needs DATABASE_URL; see docker-compose.yml)
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.collector
```

## NOOA CLI — mounted at the canonical harness mount point

The NOOA CLI is mounted at the repo root without touching the installed
`nooa-cli` package: `market_service/commands/nooa_cli.py` attaches the
repo's `market` harness group to the framework root `oo` group at import
time.

**Runtime authority split** — three planes, one rule each:

```text
poller / microstructure-capture       EGRESS
└── the only processes that call Binance

calculation container                 COMPUTATION
├── the only process that constructs substrate workers
├── fires on its own data cadence (blocking XREADGROUP, no timer)
└── control plane: GET /health, GET /status, POST /invoke
        ▲
        │  substrate_worker/control_client.py — the one sanctioned seam
        │
inference plane (nooa_harness)        SEMANTIC AUTHORITY
├── decides WHEN a calculation must run; requests it over the plane above
└── reached one-shot: harness --inference --task, or nooa market inference run

harness.py                            READ / INTERPRET
├── worker state, canonical envelope, Redis projections, poller control
└── never computes, never invokes, never touches an exchange
```

Worker identity on the Redis plane is `(substrate, symbol)` and never the
process, so a second process that builds a `SubstrateWorkerCore` overwrites
the live container's supervisor heartbeat (and the fire-dedupe state sharing
that key) and consumes its stream entries with `noack=True`. That is why
invocation is centralised — enforced by
`tests/test_worker_construction_boundary.py`.

**CLI mount points:**

```bash
# VSCode shell (uses .venv; repo .env is loaded non-clobbering)
./nooa market read SOLUSDT                         # canonical envelope
./nooa market substrate-read SOLUSDT               # warm-plane worker state
./nooa market memory recall --session-id <UUID>    # MemoryNode recall

# Task-directed inference — the only trigger; there is no autonomous firing
./nooa market inference run SOLUSDT --force --task "is sell pressure exhausting?"

# Docker runtime (tools profile, one-shot)
docker compose --profile tools run --rm nooa market read SOLUSDT
docker compose --profile tools run --rm harness SOLUSDT --substrate-read --json

# Request a fire-tick from the calculation plane (operator escape hatch).
# Needs `--profile substrates up -d calculation`; CALC_CONTROL_URL points at it.
./nooa market substrate invoke SOLUSDT density
```

The `market` group is the **sole direct caller** of the python objects in
`market_service/nooa_harness/`. The outer `harness.py` never imports anything
from `market_service/nooa_harness/` except the one-shot inference runner
behind `--inference`.

## Analyst contract objects (schema v2)

The NOOA analyst suite produces validated, schema-versioned contract objects
that cross the boundary to downstream consumers (Hermes, human reviewers,
the memory node). All objects are frozen dataclasses with `to_dict()` /
`from_mapping()` round-trips, persisted Postgres-first with Redis live
projections. See `docs/NOOA_HARNESS_ARCHITECTURE.md` for the full contract
surface.

Key objects crossing the boundary:

| Object | Purpose | Key fields |
|---|---|---|
| `SpecialistReport` | One specialist's output (delta/macro/OI/liquidation) | `summary`, `evidence: [EvidenceEntry]`, `confidence`, `limitations` |
| `EvidenceEntry` | One piece of cited evidence | `path`, `interpretation`, `value`, `metric_name` |
| `AnalystBriefing` | Controller's synthesis of all specialists | `narrative`, `consensus: Consensus`, `key_evidence: [KeyEvidence]`, `disagreements: [Disagreement]`, `specialist_reports` |
| `Consensus` | Structured market verdict | `direction`, `confidence`, `confidence_score`, `timeframe`, `magnitude` |
| `KeyEvidence` | Cross-referenced evidence claim | `path`, `claim`, `value`, `specialist` |
| `Disagreement` | Specialist disagreement | `topic`, `specialist_a`, `specialist_b`, `resolution` |
| `AgentMemory` | Durable analyst memory | `kind`, `content`, `importance`, `evidence_refs` |

The `envelope_summary` on each briefing is enriched with key market metrics
(last_price, open_interest, funding_rate, spot_cvd, futures_cvd, spot_obi,
futures_obi, keystone levels) so downstream consumers can reason over the
briefing without re-fetching the full envelope.

## Architecture: two-layer reasoning

The system operates as three layers:

1. **NOOA analyst suite** (innermost layer, subordinate runtime module)
   — domain-focused specialists (delta_orderflow, macro, open_interest,
   liquidations) + controller, running on any OpenAI-compatible backend
   (Minimax, vLLM, Ollama). Produces structured `AnalystBriefing` objects
   from canonical market envelopes. Lives in
   `market_service/nooa_harness/`.

2. **Inner NOOA CLI** (`market_service/commands/nooa_cli.py` +
   `nooa_cli_ext.py`) — the mounted `market` click group. **Sole direct
   caller of the `nooa_harness` runtime module.** Reached from the outer
   harness via `--nooa`.

3. **Outer harness CLI** (`market_service/commands/harness.py`) — the
   thin router that emits the clean aggregated market-data contract and
   bridges into the inner NOOA CLI for any analyst / briefing / memory /
   agent operation. **No direct `nooa_harness.*` imports.**

Terminal-based coding agents (pi, hermes, claude code) shell out to the
outer harness. They reach the subordinate `nooa_harness` runtime only
through the `--nooa` bridge — never directly. The contract objects are
the quality gate — every consumer's reasoning is bounded by the quality
of what crosses the contract boundary.

The harness CLI is the single mount point every terminal-based agent
uses. Reading the latest briefing or triggering a fresh analysis cycle
all flow through `harness.py --nooa market …`.

## Shared-domain map (container-split prep)

`market_service/manifest.py` classifies every canonical module into one shared
domain (`CLEAN_MODULES`). This is the single source of truth for the target
multi-container architecture.

| Domain | What | Modules |
|---|---|---|
| `data-access` | pulls raw evidence | `market_service.clients.*` |
| `calculation` | pure math over inputs | `calculations.flow`, `orderbook`, `volume_profile`, `technical`, `signals` |
| `analysis` | derived interpretation | `analysis.market`, `oi`, `liquidations`, `macro`, `auction`, `demand`, `regime`, `wall_migration`, `path_absorption`, `stage` |
| `monitor` | watch / summarise | (collector / long-running monitors) |

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
- **Python 3.12**, deps in `requirements.txt` (run via `uv run` or Docker)
- **PostgreSQL** via Docker for the persistent collector
- **Redis 7** via Docker for latest state, telemetry streams, and bounded refresh commands

## Stack (database)

`docker compose up --build` runs Redis, PostgreSQL, and the collector. The
one-shot `collator` service can be run separately for live envelope validation.
PostgreSQL
is bound to `127.0.0.1:5433`; Redis is bound to `127.0.0.1:6379` for local
inspection. Redis uses AOF persistence and capped streams. Schema:
`market_snapshot` + `signal_event` + `market_run`. Run against your own `.env` (see
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

The one-shot canonical collation seam is:

```bash
docker compose --profile tools run --rm collator SOLUSDT --json
```

It writes one immutable `market_run` envelope to PostgreSQL first, then writes
the same envelope to `marketflow:latest:SOLUSDT:collated` and
`marketflow:stream:collated:SOLUSDT`. The Docker `collator` service runs this
same SOLUSDT path once for live validation. The collated stream is intentionally
untrimmed; apply retention manually when required.

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.health
```

```bash
docker compose exec postgres psql -U marketflow -d marketflow \
  -c 'SELECT symbol, observed_at, price, spot_buy_share FROM market_snapshot ORDER BY observed_at DESC LIMIT 1;'
```

## Testing

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m unittest discover -s tests -q
```

## Docs

- `ARCHITECTURE.md` — Phase 1 architecture, operating rules, migration path
- `docs/CONTAINERIZATION_CONTRACT.md` — implementation contract for the domain
  container split
- `docs/DOCKER_RUNTIME_VERIFICATION_DOCTRINE.md` — independent Docker test and
  acceptance doctrine for the live runtime seam
- `docs/CANONICAL_RUNTIME_DOCTRINE.md` — governing authority, state, evidence,
  determinism, and agent-boundary doctrine
