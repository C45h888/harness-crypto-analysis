# Analysis Workers — Deterministic Analysis Plane Spec

Status: **spec — ready for implementation**. Companion to
[`SUBSTRATE_WORKER_SPEC.md`](SUBSTRATE_WORKER_SPEC.md) (the calculation worker plane this is
orthogonal to). Governing doctrines: `CANONICAL_RUNTIME_DOCTRINE.md` (null discipline,
evidence-first), `TELEMETRY_HYGIENE_SPEC.md`, and the substrate worker core
(`substrate_worker/core/` — fire authority, reader, supervisor are REUSED, not forked).

## 0. Vision

The calculation plane (substrate workers) reduces raw evidence into twelve
single-purpose substrates. The **analysis plane** is the next reduction: nine
analysis workers that consume *substrate projections* — never raw streams, never
Binance — and run the market-semantics layer that today lives orphaned in
`market_service/analysis/` (auction, demand, regime, wall_migration, oi, path
absorption, stage, macro, liquidations).

Each analysis domain gets its own **analysis-bounded worker**: a deterministic
detection loop bounded to the Redis plane that watches the substrate state
streams, fires when a **substrate publish is the deterministic trigger** (plus
verdict-flip probes on the composed output), runs that one domain's analysis
functions, and aggregates the result into Redis (latest projection + bounded
stream) and Postgres (durable ledger). Analysis becomes continuously warm like
the substrates it reads — the reasoning plane reads it via the same projection
discipline, instead of recomputing on pull or depending on the retired
envelope path (`run_analysis` → `assemble_envelope` → `publish_run`, which now
has **zero runtime callers**).

**Orthogonality contract:** the analysis container is a sibling of the
calculation container. Both are worker planes over Redis; neither owns the
other's keys. The calculation plane publishes `substrate:*` state; the analysis
plane consumes those publishes and publishes `analysis:*` state. No worker in
either plane imports from the other's worker package. The only coupling is the
Redis keyspace (and the shared PG ledger), which is the system's existing
contract.

## 1. Detection doctrine (identical four-layer core, different input surface)

Fire = `(L1 ∧ L2) ∨ L3 ∨ L4`, then dedupe → compute → persist — **the same
`SubstrateWorkerCore` machinery, parameterized by input-stream resolver**.

| Layer | Semantics in the analysis plane |
|---|---|
| L1 arrival | New entries on the worker's **primary dependency substrate's state stream** since its high-water. A substrate fire by the calculation plane IS the arrival event. |
| L2 significance probe | Cheap deterministic check over the composed substrate latests using **thresholds imported from the analysis module** (never redefined). Asks: *could this analysis verdict have flipped?* |
| L3 time guards | Cooldown (probe fires only) + staleness heartbeat + rollovers — identical semantics; analysis cadences run slower (composite layers legitimately lag their inputs). |
| L4 liveness | Cold start (first dependency publish with no prior projection) + dependency-recovery (a dependency substrate goes stale→fresh) — same cascade as substrate workers. |

Closed deterministic loop: `last analysis state → probe → fire? → compute →
persist → new state`. Same dependency states + same prior state ⇒ same decision
⇒ same output (replay-safe).

**Input-stream resolver (the one core extension).** `SubstrateWorkerCore` binds
`self._stream` to the raw stream via `store.raw_stream(symbol)`. The analysis
plane binds it via a **resolver seam**: `INPUT_STREAM = ("substrate:<name>",)` →
`store.substrate_stream(name, symbol)`, with consumer group
`analysis:<worker>:<symbol>`. One blocking XREADGROUP on the primary dependency
stream is the wake (unchanged cadence doctrine: blocking reads are the only
cadence); secondary dependencies are read as `latest` projections with the
existing `DEPENDENCIES` staleness gate (`_attach_dependencies` — already
implements exactly this). No second stream reader is invented.

**Dependency freshness gate (strict).** Analysis workers declare
`DEPENDENCIES = (substrate names, ...)`. The existing core already refuses to
fire when any dependency is missing or older than the worker's `staleness_s`.
Analysis carries this further: dependencies are the *entire* input surface — a
worker with a missing/stale dependency never fabricates from partial state
(`missing_inputs` names the exact dependency; our `degraded`/`insufficient_data`
payload discipline from the fire-authority fix applies verbatim).

## 2. Keyspace (the analysis plane namespace)

| Object | Key | Writer |
|---|---|---|
| Latest projection | `marketflow:latest:analysis:<name>:<SYMBOL>` | analysis worker (Lua SET+XADD, same atomic publish) |
| State stream | `marketflow:stream:analysis:<name>:<SYMBOL>` (MAXLEN ~10k) | analysis worker |
| Supervisor heartbeat | `marketflow:analysis:<name>:<SYMBOL>:supervisor` | core supervisor |
| Consumer group | `analysis:<name>:<SYMBOL>` (+ `-ws` if ever needed) | core on each dependency stream |
| PG ledger | `analysis_state` table (mirror of `substrate_state`, adds `plane='analysis'`) | pg_store (new `record_analysis_state` — Phase 3 continuation) |

Read path: `read_paths.read_analysis_latest(name, symbol)` +
`read_analysis_snapshot(symbol)` — same compact-by-default projection discipline
as `read_substrate_latest` (`available:false`, never errors). The NOOA tool
becomes `analysis.read` (companion of `substrate.read`, `tools_market.py`).

**Payload schema:** reuse `SubstrateStatePayload` verbatim (schema_version=1 —
the payload shape IS the contract; `substrate` field carries the analysis
name). No schema fork.

## 3. Worker roster & probe matrix (worker-file content)

Registry lives in `market_service/analysis_worker/__init__.py`:
`ANALYSIS_WORKER_REGISTRY`. Each worker file imports **exactly one** analysis
module (+ core, runtime store, contracts). Any output needing another
analysis domain is composition — never inside a worker.

| Worker | Analysis module | Primary trigger stream (blocking) | `DEPENDENCIES` (staleness-gated) | Derivative cache inputs | Probe (fire when) | Compute (imported, one module) |
|---|---|---|---|---|---|---|
| `auction_worker` | `analysis.auction` | `substrate:density` (mid/keystone frame) | `density, tape, delta` | — | auction verdict flip; microprice sign flip vs last state; initiated-flow ratio crosses band | `microprice, initiated_flow, flow_persistence, auction_verdict` |
| `demand_worker` | `analysis.demand` | `substrate:tape` | `tape, technicals, delta` | `cross_asset.tickers_24h` (macro climate) | demand verdict flip (`demand_verdict` bands); demand-component share Δ crosses band | `decompose_demand, demand_verdict, macro_climate` |
| `regime_worker` | `analysis.regime` | `substrate:technicals` | `technicals, oi` | `funding` (current snapshot) | regime verdict flip; funding sign flip; ATR band change | `regime_verdict` |
| `wall_migration_worker` | `analysis.wall_migration` | `substrate:density` | `density, ladders, anchors, migration, oi` | — | migration verdict change (UP/DOWN/FLAT); `wall_delta` BUILT UP/ERODED on any band; keystone-wall balance flip | `wall_delta, densest_clusters, level_absorption, wall_trap_assessment, keystone_wall_balance, keystone_holds_scorecard, default_wall_band, depth_qty*` |
| `oi_analysis_worker` | `analysis.oi` | `substrate:oi` | `oi, technicals` | `oi_history` (bar classification) | new OI bar class (classify_bar) vs last; `wall_break_assessment` state change | `classify_bar, wall_break_assessment` |
| `path_absorption_worker` | `analysis.path_absorption` | `substrate:ladders` | `ladders, density, tape, oi` | — | fuel_ratio crosses 1.0; ascent/descent verdict change; wall concentration shift | `cum_ask_to, cum_bid_to, fuel_ratio, simulated_ascent, simulated_descent, fall_short_level, wall_concentration, tbr_oi_combo` |
| `stage_worker` | `analysis.stage` | `substrate:technicals` | `technicals` | `klines_4h` (**NEW cache entry** — see §6) | stage transition (markup/distribution/markdown/accumulation) | `infer_stage, vol_direction_split` |
| `macro_worker` | `analysis.macro` | `marketflow:latest:*:derivatives` publish tick (see §5 note) | — | `cross_asset.tickers_24h`, `cross_asset.funding` | macro climate change; idiosyncratic-vs-benchmark regime flip | `idiosyncratic` + cache-derived returns/corr |
| `liquidation_worker` | `analysis.liquidations` | `substrate:oi` | `oi, technicals` | `oi_history, klines` | pressure verdict flip (`_signal` grid); OI-price divergence crosses band | `_oi_price, _signal` composition |

**Book-shaped workers (`auction`, `path_absorption`)** need the live book, which
no substrate projection carries whole. Sanctioned source: the **microstructure
book snapshot** (`store.microstructure_book_key(venue, symbol)` — mutable Redis
snapshot the capture container already maintains), read-only. This keeps the
"never touch Binance" doctrine; the book key is transport-plane state like the
derivative cache.

**Purity rules carried over from `test_substrate_graph.py`** (new suite
`test_analysis_graph.py`):
1. An analysis worker imports **exactly one** analysis module.
2. No worker imports another worker, any substrate worker, or any raw-transport
   module (Binance clients are forbidden at import and call time).
3. Thresholds are imported from the analysis module — no second threshold table.
4. Outputs that need another analysis's output are read-plane derived outputs
   (composed at the harness boundary in the next cutover).

## 4. Runtime layout

```
market_service/analysis_worker/
    __init__.py        ANALYSIS_WORKER_REGISTRY (name → worker class)
    core/              — RE-USE SubstrateWorkerCore wholesale (no fork):
                         the only change is the input-stream resolver seam
                         (base.py) + dependency-stream group registration (reader.py)
    auction_worker.py  nine worker files, one analysis module each
    demand_worker.py
    regime_worker.py
    wall_migration_worker.py
    oi_analysis_worker.py
    path_absorption_worker.py
    stage_worker.py
    macro_worker.py
    liquidation_worker.py
    runner.py          build_workers(store, symbols) — ANALYSIS_WORKERS env,
                       ANALYSIS_SYMBOLS env (same _split_env pattern as runner.py)
    analysis_container.py   control-plane host (mirror of dain_container.py)
    tools.py           read_state / invoke_many — same compact/full discipline
    calc_healthcheck.py  healthcheck with analysis supervisor keys
```

**Container:** compose service `analysis`, image built from the same Dockerfile,
command `python -m market_service.analysis_worker.analysis_container`.
Control plane on port `CALC_CONTROL_PORT`-style env `ANALYSIS_CONTROL_PORT`
(default **8042**), localhost-bound endpoints `/health` `/status` `/invoke`
with identical exit-0/1 semantics and the same invoke-authority rule: only
sanctioned fire-tick entry for foreign processes (N OO A container), same
heartbeat-collision warning.

**Batching (same as the calculation plane):** one process runs every enabled
worker as a long-lived task (`build_workers` over the registry, one asyncio
task each; per-worker consumer groups + supervisor keys preserve isolation).
Dispatch order is registry order — deterministic. `SUBSTRATE_SYMBOLS`-style env
(`ANALYSIS_SYMBOLS`) fans out per symbol.

## 5. The deterministic trigger, concretely

The calculation worker's `publish_substrate_state` Lua script already
XADDs `substrate`/`symbol`/`ts`/`payload` to its state stream on every fire.
The analysis worker's consumer group on that stream receives:

```
entry = {substrate: "density", symbol: "SOLUSDT", ts: "1729...", payload: "<full state json>"}
```

L1 arrival = XREADGROUP delivered an entry. The worker then:
1. reads ALL declared dependencies as `latest` projections (fresh, staleness-guarded);
2. runs the probe over the composed view (verdict flips etc.);
3. on fire: compute (pure analysis functions) → `SubstrateStatePayload.create`
   → PG-first (`record_analysis_state`) → publish `analysis:latest` + stream.

Note on the `macro` worker: its only input is the derivative-evidence cache,
which is a SET (not a stream). Its trigger is the `oi` substrate state stream
(the poller refreshes the cache every 60s and OI fires at least every 300s), so
the trigger stays stream-borne; the worker re-reads the cache at compute time.
The poller gains `klines_4h` (one extra Binance endpoint in
`fetch_derivative_evidence` — `/fapi/v1/klines interval=4h limit=180`) for the
stage worker. That is a poller-side additive change, documented here as a
prerequisite.

## 6. Cadence defaults (per worker, env-overridable like SUBSTRATE_COOLDOWN_S)

| Worker | cooldown_s | staleness_s | Rationale |
|---|---|---|---|
| auction | 15 | 120 | follows density/tape cadence closely |
| path_absorption | 30 | 180 | composite over book-shaped inputs |
| wall_migration | 30 | 180 | widest dependency fan; verdict granularity |
| demand | 30 | 300 | flow composites move at tape cadence |
| oi_analysis | 60 | 300 | matches OI staleness (300s) |
| regime | 60 | 600 | regime is slow by definition |
| liquidation | 120 | 300 | pressure grid is slow-moving |
| stage | 300 | 900 | 4h window semantics |
| macro | 300 | 900 | cross-asset climate |

## 7. Read-plane & cutover contract

- `runtime/read_paths.py` gains `read_analysis_latest` / `read_analysis_snapshot`
  (same compact/full discipline, `available:false` for missing workers).
- NOOA gains the `analysis.read` capability alongside `substrate.read` — the
  reasoning plane prefers `analysis:latest` for verdict-level questions and
  `substrate:latest` for raw substrate facts. No double-compute: the legacy
  `canonical_state.analysis` envelope guards remain read-compat only.
- Provenance: each analysis payload carries `substrate_dependencies` names +
  `computed_at_ms` of every dependency it composed (the payload schema's
  `provenance` field), so a reader can always answer "which substrate states
  produced this verdict".

## 8. Phases

1. **P1 — core seam + skeleton**: input-stream resolver in the core (parameterized
   stream key + group registration), analysis keyspace accessors in
   `redis_store` (`analysis_stream`, `analysis_latest_key`, `analysis_supervisor_key`,
   `publish_analysis_state`, `read_analysis_latest`), `analysis_state` PG table
   (alembic), runner + container + healthcheck. One reference worker
   (`regime_worker`) end-to-end against live substrate projections.
2. **P2 — full roster**: remaining 8 workers per the probe matrix; poller gains
   `klines_4h`; graph test (`test_analysis_graph.py`) pins purity invariants.
3. **P3 — NOOA cutover**: `analysis.read` capability + tool schemas; harness
   reads analysis projections; deprecate the envelope path re-exports
   (`run_analysis`/`assemble_envelope`/`publish_run`) once no reader depends on
   `canonical_state.analysis`.

## 9. Invariants (test-pinned)

- An analysis worker never reads a raw REST stream or imports a Binance client.
- An analysis worker imports exactly one analysis module; probe constants are
  imported, never redefined.
- Dependency staleness ⇒ dormant/degraded with named `missing_inputs` — never a
  fabricated verdict.
- Fire = `(L1 ∧ L2) ∨ L3 ∨ L4`, dedupe via supervisor Lua script, PG-first
  publish, Redis SET+XADD atomic — byte-identical semantics to the calc plane.
- Consumer groups are per `(analysis-worker, dependency-substrate)` on the
  dependency's state stream — no worker steals another's publishes (state
  streams are read via groups with ack-free consumption, same `noack` posture).
- Restart convergence: consumer-group position + own `latest` projection.

## 10. Open decisions (flagged, default chosen)

1. **PG ledger shape**: new `analysis_state` table vs `plane` column on the
   existing substrate table. Default: **new table + `record_analysis_state`**
   (read paths stay disjoint; alembic is cheap).
2. **Macro trigger**: piggyback on `oi` substrate fires (default) vs a dedicated
   60s timer (violates the no-timer doctrine). Default chosen: piggyback.
3. **`klines_4h` in the derivative cache**: additive cache entry (default) vs a
   second cache key. Default: extend `fetch_derivative_evidence` (single writer,
   single TTL).
4. **Analysis-of-analysis** (e.g. scorecard composing regime + demand + wall
   migration): NOT a worker. It stays a read-plane composition (Phase 3 NOOA
   tool), preserving worker purity.