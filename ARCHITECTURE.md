# Market Flow — Phase 1 Architecture

The governing design contract for all future runtime and agent work is
[`docs/CANONICAL_RUNTIME_DOCTRINE.md`](docs/CANONICAL_RUNTIME_DOCTRINE.md).
The implementation contract for the next containerization task is
[`docs/CONTAINERIZATION_CONTRACT.md`](docs/CONTAINERIZATION_CONTRACT.md).
The Docker acceptance procedure is defined in
[`docs/DOCKER_RUNTIME_VERIFICATION_DOCTRINE.md`](docs/DOCKER_RUNTIME_VERIFICATION_DOCTRINE.md).
The NOOA integration boundary is defined in
[`docs/NOOA_HARNESS_ARCHITECTURE.md`](docs/NOOA_HARNESS_ARCHITECTURE.md), with
the isolated package boundary in `market_service/nooa_harness/`.
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

## Runtime topology (current reality)

The four-node pipeline (data-access → calculations → analysis → collator
sequenced by a timer orchestrator over `stream:commands`) has been **replaced
by a harness-owned pipeline**:

* `market_service.poller` — 5-second firehose; the ONLY process that touches
  the live Binance API for core data. Normalizes trades at the client
  boundary and atomically appends raw evidence to the Redis stream
  (`SET latest` + `XADD` in one Lua script, deduped on `observed_at_ms`).
* `market_service.nooa_harness.pipeline.run_cycle` — the canonical
  collation seam. Reads the raw window from Redis (deduping trades by
  aggregate id), merges on-demand derivative evidence (cache-first, 5-min
  TTL), runs the deterministic calculations and analysis sections, and
  collates one immutable `MarketRunEnvelope` (Postgres first, then Redis).
  One Redis connection + one Postgres connection per cycle, shared by every
  persistence seam.
* `market_service.nooa_harness.suite` — the interpretation plane. Four
  specialists + one controller read a bounded LLM projection of the
  envelope; raw model text crosses back into typed state only via
  `SpecialistReport.from_llm_text` / `AnalystBriefing.from_controller_text`.
* `market_service.microstructure.capture` — isolated WebSocket depth-delta
  capture (spot), feeding its own ledger namespace; never touches the
  poller path. Also appends ONE status-transition entry to
  ``marketflow:stream:microstructure:status:{venue}:{SYMBOL}`` ONLY on
  state change (running -> gap -> reconnecting -> ...), the event-driven
  companion to its latest-key status payload.
* `market_service.nooa_harness.wake_worker` — the EVENT-DRIVEN wake worker
  (Slice 1). Bounded ENTIRELY on the Redis plane: it blocks on the
  microstructure event stream (XREADGROUP, one consumer per scope) +
  status-transition stream, evaluates the deterministic trigger matrix
  (``inference.evaluate_triggers``) against the artifact high-water
  (``deterministic_state.coverage.events_total``), and on a fire
  materializes a typed ``WakeEnvelope`` IN MEMORY and dispatches the engine
  cycle as an async task. The retired ``publish_wake``/``read_pending_wakes``
  envelope transport is gone — the envelope is an assertion object, never
  a transported artifact. Dedupe is a supervisor-lua script on
  ``marketflow:state:inference:wake:...:supervisor``; liveness is a
  TTL-bounded heartbeat on the same key.

### Wake plane (deterministic trigger -> engine dispatch)

The inference engine is event-driven, never lazily polled. Durable
position lives on the consumer group's advanced `>` marker (crash-resume
for free); the artifact ledger (``coverage.events_total``) is the
restart-safe high-water. Predicates: ``event_delta`` (≥ threshold new
events since the last artifact), ``cold_start`` (established capture, no
artifact yet), ``capture_recovery`` (status stream records a
gap/reconnecting -> running transition). Every fire passes: status-
established, artifact cooldown (60s default), timestamp-water (fresh data
only), then the atomic dedupe (identical conditions collapse). Fires log
and, by default, record an informational journal entry on the inference
stream; ``WAKE_ENGINE_DISPATCH=1`` routes the envelope straight to
``engine.run_cycle`` (the Slice-2 closed loop, no LLM at worker import).

### Calculation-model groups (Pass 3 pivot)

`nooa_harness.pipeline.GROUP_MAP` is the single source of truth for the
segregated command surface (`--wall` / `--flow` / `--structure` /
`--positioning` in `commands/harness.py`). Each group runs only the
calculation/analysis sections it needs:

| Group | Calculations | Analysis |
|---|---|---|
| `wall` | orderbook | wall_migration, path_absorption, oi |
| `flow` | flow, bucketed_cvd, correlation, technical | demand, auction, delta |
| `structure` | volume_profile, technical | regime, stage |
| `positioning` | — | oi |

Section dependencies resolve automatically (`turnover`/`signals` → `flow`;
`correlation` shares `bucketed_cvd` with the bucketed section; `regime` →
`flow`; `wall_migration` → `orderbook`). The monolithic run-cycle
(`--analyze`) stays the canonical persisted-envelope path.

### Specialized group envelopes — the interpretation read plane

The canonical `MarketRunEnvelope` is the **persisted audit record**; what
analysts READ are specialized `GroupEnvelope` contracts
(`nooa_harness/contracts.py`, schema_version=1):

* `pipeline.run_group_cycle` reads raw evidence **directly from the Redis
  store** (no monolithic envelope, no persistence), runs only the requested
  group's sections, and emits one typed `GroupEnvelope` per group with its
  own `run_id`.
* `pipeline.build_group_envelopes` deterministically projects a persisted
  canonical envelope into the four group envelopes, preserving its
  `run_id` so every specialist claim stays audit-linked to one run.
* `pipeline.build_controller_view` gives the controller a compact
  cross-group block (bounded headlines per group) instead of the
  monolithic envelope.
* `SPECIALIST_GROUP_KINDS` assigns each NOOA specialist its group:
  delta_orderflow→flow, macro→structure, open_interest→positioning,
  liquidations→wall.

Every group envelope carries `evidence_headlines` (compact snake_case
scalars: price, funding, OI) and arrays bounded at emission (128-item cap
with explicit `__truncated__` markers). Group envelopes fit the LLM context
**by construction** — the monolithic envelope's `bounded_envelope_view`
collapse path (realistic envelopes degraded to identity-only views) no
longer participates in the read path.

### Time & coverage semantics

- `evidence.observed_at` is the source snapshot time, not the harness read
  time.
- `coverage.evidence` (per envelope) records MEASURED window coverage:
  actual trade span, dedupe effectiveness, snapshot count, and stream
  staleness — never just the requested window.
- `derivatives_meta` records the 5-minute bar period and per-series bar
  counts so derivative series horizons (N × 5 min) are explicit regardless
  of the requested 15m/1h/4h window.

## Deferred (next phases)

- Add Redis node instance/session keyspaces for future model reads.
- Deterministic-logic layer over Redis; Postgres as durable ledger.
