# Substrate Workers — Deterministic Detection & Aggregation Spec (Phases 1–3)

Status: **spec — not yet implemented**. Companion to
[`SUBSTRATE_DECOMPOSITION_PLAN.md`](SUBSTRATE_DECOMPOSITION_PLAN.md) (the substrate
layer this builds on). Governing doctrines: `CANONICAL_RUNTIME_DOCTRINE.md`
(null discipline, evidence-first), the two-plane split in `nooa_harness/`
(transport vs interpretation), and the proven `wake_worker` pattern
(blocker-only, XREADGROUP, dedupe-supervisor, in-memory envelope).

## 0. Vision

Each calculation substrate gets its own **substrate-bounded worker**: a
deterministic detection loop bounded to the Redis plane that watches the raw
data streams, fires when a **market-semantic boundary is crossed**, runs that
one substrate's calculation, and aggregates the result into Redis (latest
projection + bounded stream) and Postgres (durable ledger). The harness stops
being the thing that *computes* — it reads always-fresh substrate state. The
information pipeline is continuously warm instead of computed on pull.

## 1. Detection doctrine (the deterministic core)

Every worker runs the same four-layer fire decision. Fire =
`(L1 ∧ L2) ∨ L3 ∨ L4`, then dedupe → compute → persist.

| Layer | Name | Owner | Semantics |
|---|---|---|---|
| L1 | Arrival gate | core | ≥ N new entries on the worker's consumer group since its high-water. Anti-idle only; never sufficient alone. |
| L2 | **Significance probe** | **worker file** | A cheap deterministic check computed from the arriving window + the worker's own last state, using **thresholds imported from the substrate module** (never redefined). The probe asks: *could this substrate's verdict have flipped?* |
| L3 | Time guards | core | Cooldown (min seconds between fires) + **max-staleness heartbeat** (fire anyway with `trigger.source="staleness"` when the latest projection is older than `staleness_s`) + window rollovers. |
| L4 | Liveness guards | core | Cold start (no prior `substrate:latest`) + capture recovery (status-transition stream) — lifted from `wake_worker`. |

Closed deterministic loop: `last state → probe → fire? → compute → persist →
new state`. Same stream entries + same prior state ⇒ same decision ⇒ same
output (replay-safe). Worker restarts converge: consumer-group position
(crash resume) + own `latest` (probe state).

**Probe/calculator share constants.** The delta worker imports the
`delta_state` boundaries; the density worker imports keystone `width` and
`wall_delta`'s `direction_threshold`. When a market semantic is tuned,
detection and calculation move together — there is no second threshold table.

**Substrate purity (extends the decomposition contract):** a worker file
imports **exactly one** substrate module (+ core, runtime store, contracts).
Any output that would need another substrate's output is a **read-plane
derived output** (composed by the harness in Phase 4), never computed by the
worker. Composition lives at the boundary, never inside a worker.

## 2. Per-substrate probe matrix (worker-file content)

Probes use ONLY the substrate's own module constants:

| Worker (file) | Substrate module | Probe | Fire when |
|---|---|---|---|
| `density_worker` | `substrates.density` | keystone center on new book vs last recorded; per-level qty on significant levels; level-set identity | keystone moved > `width` (0.20); level qty Δ > `direction_threshold` (1.15) — the BUILT UP/ERODED semantics; significant set changed |
| `migration_worker` | `substrates.migration` | latest keystone vs prior cycle; hour-bucket id | bucket rollover; keystone Δ > `width` (0.20) (UP/DOWN vs FLAT scale) |
| `ladders_worker` | `substrates.ladders` | cumulative bid stack below price; ask-wall ladder totals vs last | cum-qty Δ > threshold at any rung; top rung notional Δ > threshold |
| `anchors_worker` | `substrates.anchors` | anchor qty distribution vs last (anchors re-derived from price) | any anchor qty Δ > threshold; anchor grid shifted (center moved) |
| `tiers_worker` | `substrates.tiers` | mega-tier bid/ask totals vs last | balance crosses `bias_threshold` (1.2) — verdict flip |
| `tape_worker` | `substrates.tape` | CVD delta over trailing window; buy_share | buy_share crosses 0.55/0.45 (demand bands); window CVD Δ > threshold |
| `large_print_worker` | `substrates.large_print` | per-print qty scan (WS events ideal) | any print ≥ tier (50/200/500); `seller_aggression` ratio crosses 1.5/0.8 |
| `volume_profile_worker` | `substrates.volume_profile` | POC bucket id; VAH/VAL | POC bucket changed; VA boundary moved ≥ 1 bucket |
| `technicals_worker` | `substrates.technicals` | last close vs EMA levels; new bar | EMA ABOVE/BELOW flip; new 5m bar close (klines via derivative cache) |
| `delta_worker` | `substrates.delta` | `flow_alignment` + wall imbalance vs last | Δdelta crosses a `delta_state` boundary (±0.25/±1.0); any band sign flip; TBR crosses 50±5 |
| `signals_worker` | `substrates.signals` | `deterministic_signals(new_snapshot, last_snapshot)` | any rule trips (this worker IS the signal producer) |

Input surfaces: all workers consume the **raw REST stream** (poller, 5s).
`tape`, `large_print`, `delta` additionally consume the **microstructure WS
event stream** (Phase 2) — separate consumer groups on the shared stream, so
no worker steals another's messages. Derivative-dependent inputs
(`taker_buy_sell`, `oi_history`, klines) are read **cache-only** from the
derivative evidence key; workers never touch Binance. Stale/missing
dependent inputs → worker stays dormant (heartbeat continues); if it does
compute, missing inputs are reported `missing_inputs` — never fabricated.

## 3. Runtime layout

```
market_service/substrate_worker/
    __init__.py        WORKER_REGISTRY (name → worker class)
    contracts.py       SubstrateStatePayload, TriggerDecision (typed, validate/to_dict)
    core.py            SubstrateWorkerCore — transport + fire orchestration ONLY
    density_worker.py  SUBSTRATE_NAME + probe() + compute()   ← Phase 1 flag bearer
    tape_worker.py ... one file per substrate (Phase 2)
    runner.py          process entrypoint (env-selected workers, --once smoke mode)
```

### 3.1 `core.py` — what the core owns (and nothing else)

Adapted from `WakeSupervisor` (same discipline, substrate-generic):

- Consumer group per `(substrate, symbol)` on `stream:raw:{SYM}`
  (`XREADGROUP BLOCK 1000ms`, `count 500`, `noack=True` — at-most-once like
  wake; compute is idempotent, next event re-triggers).
- L1 arrival gate, L3 cooldown/staleness/rollover, L4 cold-start/recovery.
- Dedupe: the `_DEDUPE_LUA` pattern on the substrate supervisor key
  (same-condition collapse across redundant worker instances).
- Supervisor heartbeat: `{prefix}:substrate:{name}:{SYM}:supervisor`
  (TTL-bounded, carries pid/last-fire/last-error — observability, never control).
- Evidence window build via `runtime/raw_window.build_raw_window(store,
  symbol, window_minutes)` (extracted from `bedrock.read_raw_window`; bedrock
  re-exports so nothing breaks — substrate_worker never imports nooa_harness).
- Persistence seam: atomic `publish_substrate_state` (Lua SET latest + XADD
  stream); Phase 3 adds Postgres-first.
- Dispatch: `asyncio.create_task` per fire (loop stays responsive), error
  backoff on recoverable Redis exceptions.

### 3.2 What each worker file implements (and nothing else)

```python
SUBSTRATE_NAME = "density"
INPUT_STREAMS = ("raw",)                 # + "microstructure" in Phase 2

def probe(window: dict, last_state: dict | None, now_ms: int) -> TriggerDecision:
    ...  # L2 only — imports constants from calculations.substrates.density

def compute(evidence: dict, depth: int) -> dict:
    ...  # imports substrate functions; pure; returns the output dict
```

The core drives everything else. `TriggerDecision` carries
`fired`, `predicates` (auditable values, e.g. `{"keystone_delta": {"from":
148.52, "to": 148.76, "threshold": 0.20}}`), `source`
(`"probe" | "staleness" | "cold_start" | "recovery" | "rollover"`).

### 3.3 Payload contract (`contracts.py`, schema_version=1)

```json
{
  "schema_version": 1,
  "substrate": "density",
  "symbol": "SOLUSDT",
  "status": "healthy | degraded | insufficient_data",
  "observed_at_ms": 1757000000000,
  "computed_at_ms": 1757000000123,
  "trigger": {"source": "probe", "predicates": {"keystone_delta": {...}}},
  "freshness": {"window_minutes": 15, "input_fingerprint":
                {"first_trade_ms": ..., "last_trade_ms": ..., "trade_count": ...,
                 "raw_entry_high_water": "1719-0"}},
  "missing_inputs": [],
  "output": { ... substrate-specific, arrays bounded (128 cap, __truncated__) ... },
  "provenance": {"substrates": ["density"]}
}
```

`missing_inputs` + `status="insufficient_data"` preserve null discipline:
a worker that cannot compute honestly writes that fact, never a zero.
Failed compute → latest stays at previous state (atomic publish; no
half-writes), error recorded on the supervisor key.

### 3.4 Redis namespaces (`RedisRuntimeStore` additions)

| Key | Shape |
|---|---|
| `{prefix}:latest:substrate:{name}:{SYM}` | STRING — the always-fresh projection |
| `{prefix}:stream:substrate:{name}:{SYM}` | bounded stream (replay/audit) |
| `{prefix}:substrate:{name}:{SYM}:supervisor` | TTL heartbeat + dedupe state |

`publish_substrate_state` = single Lua script (SET+XADD atomic — same
ordering-hole closure as `publish_raw_evidence`). Reads:
`read_substrate_latest`, `read_substrate_history(count)`,
`read_substrate_history_count`.

### 3.5 Emits vs read-plane derived (Phase 4 composition)

| Worker | Emits (own substrate only) | Delegated to read-plane |
|---|---|---|
| density | keystones (fut+spot), density windows, zone grids, significant levels, keystone_trade_intensity (keystone is its own output) | — |
| ladders | absorption_ladder, ask_wall_ladder | `keystone_bid_stack` (needs density's keystone) |
| anchors | anchor grid + aggregates | — |
| tiers | tier buckets, tier balance, mega-at-* (mega-at-keystone needs density keystone → read-plane) | |
| migration | hourly + cycle migration rows | — |
| all others | their substrate's full output | — |

## 4. Phase 1 — Core worker instantiation + density flag bearer

**Goal:** one substrate, full loop end-to-end, proven machinery, tests.

1. `market_service/runtime/raw_window.py` — extract `read_raw_window`'s
   window/dedupe/coverage logic from `bedrock.py`; `bedrock` re-exports
   (shim, zero behavior change).
2. `RedisRuntimeStore`: substrate namespaces + `publish_substrate_state`
   (Lua) + the three reads.
3. `substrate_worker/contracts.py` — payload + trigger contracts.
4. `substrate_worker/core.py` — `SubstrateWorkerCore` (L1/L3/L4 + dedupe +
   heartbeat + persistence + dispatch; probe/compute are abstract hooks).
5. `substrate_worker/density_worker.py` — probe per §2 row 1; `compute`
   runs `find_keystone` (fut+spot) → density windows → zone grids →
   significant levels → `keystone_trade_intensity` (own-keystone composed).
6. `substrate_worker/__init__.py` (registry) + `runner.py`
   (`python -m market_service.substrate_worker.runner [symbol] [--once]`,
   `SUBSTRATE_WORKERS` env selection).
7. `docker-compose.yml`: `substrate-workers` service, profile `substrates`,
   env: `SUBSTRATE_WORKERS=density`, `SUBSTRATE_WINDOW_MINUTES=15`,
   `DEPTH_LEVELS`, `SUBSTRATE_COOLDOWN_S=30`, `SUBSTRATE_STALENESS_S=120`,
   `LOG_LEVEL`; `depends_on: redis`.
8. Tests: `test_substrate_worker_core.py` (FakeRedis pattern from
   `test_wake_worker.py`: group create, arrival gate, dedupe collapse,
   heartbeat, staleness fire, cold start); `test_density_worker.py`
   (probe determinism: displacement > 0.20 fires / quiet book doesn't;
   payload shape; bounded arrays); extend `test_substrate_graph.py` —
   worker files import exactly one substrate + core/runtime/contracts, no
   cross-worker imports.

**Acceptance:** seeded raw stream → `runner --once` writes
`latest:substrate:density:{SYM}` + stream entry with auditable trigger;
quiet market → no fire until staleness override; full suite green; poller/
wake_worker/engine untouched.

## 5. Phase 2 — Onboard all substrates + WS inputs + registry

1. Ten remaining worker files (§2 matrix), each probe importing its own
   substrate constants; registry + runner selection make them loadable
   individually (`SUBSTRATE_WORKERS=tape,delta`).
2. Core: microstructure WS input surface (`INPUT_STREAMS += ("microstructure",)`)
   — second consumer group per worker on the event stream + status-transition
   stream for L4 recovery (reusing wake's status plumbing).
3. Per-worker env overrides: `SUBSTRATE_{NAME}_COOLDOWN_S`,
   `SUBSTRATE_{NAME}_STALENESS_S`, `SUBSTRATE_{NAME}_THRESHOLD_*` (probe
   knobs only; semantic constants stay imported from substrates).
4. `read_paths.py`: `read_substrate_latest(store, name, symbol)`,
   `read_substrate_snapshot(store, symbol)` (multi-substrate composite with
   per-entry `age_ms`).
5. Tests per worker (probe truth tables + compute shapes); a registry
   conformance test (every substrate with a standalone output has a worker).

**Acceptance:** all workers run in one process against one seeded stream,
independent consumer-group positions, per-substrate latest keys populated.

## 6. Phase 3 — Postgres ledger + multi-symbol + compose hardening

1. Alembic `0010_substrate_calculation` (mirrors `0002_wall_snapshot`
   discipline): `substrate_calculation(symbol, substrate, observed_at_ms,
   computed_at_ms, status, trigger JSONB, freshness JSONB, payload JSONB,
   schema_version CHECK = 1, created_at)` + index `(symbol, substrate,
   computed_at_ms DESC)`; additive block in `db/init/001_schema.sql`.
2. `postgres_store.py`: `record_substrate_state` + `read_substrate_history`.
   Write path **Postgres-first then Redis** (run_cycle discipline).
3. Multi-symbol: `SUBSTRATE_SYMBOLS` env — runner spawns one worker task per
   `(substrate, symbol)`; consumer group + supervisor key are per-pair.
4. Compose: `substrate-workers` gains `DATABASE_URL`, `depends_on: postgres,
   db-init`; healthcheck = supervisor-key liveness; `restart: unless-stopped`.
5. Correctness gate: worker-aggregated output vs pull-computed output on the
   same input fingerprint must be identical (proves "steady completion").

**Acceptance:** kill -9 a worker → restart resumes from consumer-group
position, replays, converges to same state; PG has every fire; Redis latest
matches the newest PG row per (substrate, symbol).

## 7. What does NOT change (until Phase 4)

Poller, microstructure capture, wake_worker, engine, `run_cycle`,
`run_group_cycle`, and the harness group commands keep working exactly as
today — they remain the pull path and the audit path. Phase 4 (separate spec
after 1–3 land) cuts `--wall/--flow/--structure/--positioning` over to
`read_substrate_*` with synchronous pull-compute fallback + `age_ms`
surfacing in GroupEnvelope.

## 8. Test matrix summary

| Suite | Covers |
|---|---|
| `test_substrate_worker_core.py` | L1/L3/L4 orchestration, dedupe Lua, heartbeat, persistence atomicity (FakeRedis) |
| `test_<substrate>_worker.py` ×11 | probe truth tables (deterministic), compute output shapes, missing-input discipline |
| `test_substrate_graph.py` (extended) | worker purity: exactly one substrate import per worker, no cross-worker imports |
| `test_substrate_pg_ledger.py` (Phase 3) | migration, PG-first writes, read-back |
| `test_substrate_cutover.py` (Phase 4) | worker vs pull-compute parity on same fingerprint |