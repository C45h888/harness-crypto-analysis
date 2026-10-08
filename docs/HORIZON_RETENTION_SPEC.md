# Horizon Retention & Worker Cadence Spec — Time-Horizon Segmentation (Phase H1)

Status: **implemented and green — H1 AND the A/B/C rollout landed**
(1104 tests incl. 48 new horizon/retention/rollout contract tests).
Companion to
`SUBSTRATE_WORKER_SPEC.md` (worker doctrine) and `CANONICAL_RUNTIME_DOCTRINE.md`
(null discipline, evidence-first). Governs two change vectors decided in the
H1 assessment:

1. **Part 1 — widen raw retention** (time-based trim + per-entry slimming).
2. **Part 2 — horizon segmentation inside worker cadence logic** (per-worker
   `HORIZONS`, incremental bucket fold, per-horizon probes/fire economics).

Approved decisions (assessment round 2):

- Retention horizon: **12 hours** of raw stream history.
- Segment alignment: **IST-aligned** (Indian Standard Time, UTC+05:30).
- Horizon logic lives in **worker cadence**, not the parent harness; substrate
  math modules stay horizon-agnostic.

## 0. The problem (restated precisely)

The current runtime serves exactly one window per worker process
(`SUBSTRATE_WINDOW_MINUTES=15`). Every tier that could answer "what happened
over the last 1h/4h" is shaped against it:

| Tier | Today | Why it fails at ≥1h |
|---|---|---|
| Raw stream | MAXLEN ~10 000 count-trim | time horizon varies with market activity (unbounded environment); entry bodies ~344KB → widening by count explodes memory |
| Substrate state | one latest projection + count-bounded fire log | no horizon-stratified memory; `last_state` is single-generation |
| Derivative cache | single key, 5-min TTL, 5-min bars | `derivatives_meta` explicitly records "N × 5 min, regardless of requested window" — cannot serve 4h honestly |
| Rollover contract | `hour` / `bar_5m` / `bar_1h` | ceiling is the hour; no 4h boundary |

The fix is a two-part change that keeps the existing architecture's
boundaries intact: storage becomes time-predictable (Part 1), and horizon
*interpretation* becomes a per-worker cadence concern (Part 2).

## 1. Part 1 — raw retention widening

### 1.1 Time-based trim replaces count-based trim as primary policy

`publish_raw_evidence` Lua (runtime/redis_store.py) gains an `XTRIM` on
`MINID` after the XADD:

```
minid = now_ms - RAW_RETENTION_MS
XTRIM KEYS[2] MINID '~' minid
```

* `RAW_RETENTION_MS` default **43 200 000 (12h)**, env-overridable
  (`RAW_RETENTION_MS`) via `config.Settings`.
* The existing `stream_maxlen` MAXLEN is **kept as a pathological-burst
  guardrail only** (memory ceiling, not the retention policy). Docs on the
  method change accordingly: count is the safety cap, MINID is the contract.
* Trim uses approximate (`~`) mode — exact-mode XTRIM is O(n) and would add
  per-publish latency at 5s cadence.

Retention margin rationale: a 4h window + 3× margin absorbs poller outages,
rate-limit backoff gaps, and cold-start backfill slack without ever serving a
silently truncated horizon.

### 1.2 Per-entry slimming (the memory enabler)

Current stream entries duplicate the entire snapshot (~344KB trimmed) but
`build_raw_window` reads **only `trades_normalized`** from the stream — book,
funding, OI, tickers all come from the single `latest` snapshot. The stream
body therefore shrinks to what stream consumers actually read:

```python
slimmed = {
    "observed_at": ..., "observed_at_ms": ...,
    "depth_levels": ...,
    "coverage": {...},                # kept: audit + dedupe stats
    "spot":   {"trades_normalized": [...]},
    "futures": {"trades_normalized": [...], "funding": {...}, "open_interest": {...}},
}
```

* Dropped from stream body: `order_book` (the bulk of the bytes),
  `ticker_24h`, `trades_raw` (already dropped).
* `latest` projection keeps the FULL payload unchanged — point-in-time
  readers (`read_raw_latest`) are unaffected.
* `read_raw_window` needs no shape change: it json-loads entries; slimmed
  entries simply have no book. Any consumer that assumed book-in-stream must
  move to `latest` (audit: `read_recent_events` diagnostics only).
* **Compatibility note:** old-format entries surviving the transition are
  harmless (readers ignore extra keys).

### 1.3 Memory budget (the sizing table)

| Item | Value |
|---|---|
| Cadence | 5s → 720 entries/hour → 8 640 entries / 12h |
| Slimmed entry (est.) | 20–60KB (trade-window dependent) |
| Raw stream / symbol | ~170–520MB (est.) |
| 3 symbols (current set) | ~0.5–1.5GB |
| Guardrail MAXLEN | raise to 20 000 entries as burst ceiling |
| Required | `maxmemory` raised accordingly (docker-redis compose), OR retention shortened via env — deployment decision recorded at rollout |

The pre-change budget note in `publish_raw_evidence` (1.2GB @ 3 symbols) is
replaced by this table.

### 1.4 Coverage honesty extends to retention

`build_raw_window` (runtime/raw_window.py) reports two new coverage fields:

* `retention_horizon_ms`: the effective oldest-observable timestamp
  (stream's first entry id time, or `now − RAW_RETENTION_MS` when unknown).
* `horizon_degraded: bool` + `missing_inputs` entry
  `"retention_exhausted: requested=Xh, oldest=Y"` — set when
  `window_minutes*60_000 > observed available span`. The window is NOT
  silently truncated; a degraded window is honest or it is not returned.

### 1.5 New store accessor

`RedisRuntimeStore.read_raw_oldest_ms(symbol) -> int | None` — XREAD first
entry (1 item) and parse the entry-id ms. Used by 1.4 and by Part 2 backfill
planning.

## 2. Part 2 — horizon segmentation in worker cadence logic

### 2.1 Architectural basis

Every worker already receives **every** raw stream entry through its consumer
group (XREADGROUP, noack). The cadence loop is therefore the natural fold
point: horizon buckets are maintained incrementally on the read tick —
**no re-scan of history per fire**. The base 15m window keeps today's
`build_raw_window` path (evidence-cached) unchanged.

### 2.2 Worker declaration

```python
class SubstrateBase:
    HORIZONS: tuple[str, ...] = ()   # () = legacy single-window behavior
```

* `()` (default): worker behaves byte-for-byte as today. No regression
  surface for the 8 workers that do not opt in.
* e.g. `HORIZONS = ("15m", "1h", "4h")` on the pilot worker.
* The env `SUBSTRATE_WINDOW_MINUTES` remains the BASE horizon width only.

### 2.3 Horizon spec (segments, ring lengths, alignment)

New module `market_service/runtime/horizons.py` — single source of truth:

```python
IST_OFFSET_MS = 5.5 * 3_600_000        # 19_800_000 — UTC+05:30

HORIZON_SPEC: dict[str, HorizonSpec] = {
    "15m": HorizonSpec(segment_ms=300_000,  segments=3,  rollover="bar_15m"),
    "1h":  HorizonSpec(segment_ms=300_000,  segments=12, rollover="bar_1h"),
    "4h":  HorizonSpec(segment_ms=900_000,  segments=16, rollover="bar_4h"),
}
```

* Segments are internal fold accumulators; **bucket identity** (which
  horizon bucket an entry belongs to) is IST-aligned:
  `bucket_start = floor((t_ms - IST_OFFSET_MS) / horizon_ms) * horizon_ms + IST_OFFSET_MS`.
* IST has no DST — alignment is stable forever. 4h divides 24h, so IST-midnight
  aligned 4h bars are clean (00:00/04:00/…/20:00 IST = xx:30 UTC boundaries).
* `ROLLOVER_PERIOD_MS` gains `"bar_15m"` (900_000) and `"bar_4h"`
  (14_400_000). The core's rollover guard compares against IST-aligned
  boundaries for these periods (aligned periods only; 5m/1h are already
  offset-clean since 19_800_000 is a multiple of both — the helper makes this
  explicit rather than implicit).

### 2.4 Incremental fold (cadence layer, core/reader.py)

On every successful read tick, the reader invokes the worker's fold hook with
the newly arrived rows **before** the probe:

```python
def fold(self, rows: list[dict], horizon_state: dict[str, Any]) -> None
```

* Default impl (base class): no-op. Opted-in workers override with a fold
  that updates per-horizon ring segments (trade counts, CVD delta, notional,
  per-segment aggregates) from `trades_normalized` in the rows.
* **Cross-snapshot dedupe inside the fold:** each snapshot's
  `trades_normalized` is a rolling window; snapshots overlap. The fold keeps
  a per-venue monotonic trade-id high-water (persisted in horizon state) and
  skips `id <= high_water`. Trade ids arrive in order per venue, so this is
  exact — no set growth, O(1) memory.
* Segments outside the ring window are expired by bucket identity, not by
  arrival order (a burst does not shorten the horizon; a gap leaves the
  segment marked `gap=True` and the coverage honesty of 1.4 applies at
  compute time).

### 2.5 Cold-start backfill (the Part 1 payoff)

On `cold_start` / `recovery` (L4 guards, existing), before the first probe:

* Backfill = read the retained raw window once
  (`read_raw_window(symbol, now − max(HORIZONS))`) and fold it through the
  same fold path. Bounded by 12h retention ≈ 8 640 entries, once, not per
  fire.
* If the PG ledger holds horizon checkpoints newer than the stream's oldest
  entry (burst-eviction case), rebuild from PG first, then fold the delta.
* Backfill sets `freshness.backfilled_horizons` in the next payload — the
  audit record shows when a horizon was rebuilt rather than folded live.

### 2.6 Contract changes (substrate_worker/contracts.py)

* `SUBSTRATE_STATE_SCHEMA_VERSION = 2`.
* `SubstrateStatePayload` gains an optional `horizons` block:
  `{ "<hz>": {"bucket_start_ms", "segments", "aggregates", "gap_flags"} }`.
  v1 payloads (no block) are read as a single implicit base horizon —
  analysis plane and GroupEnvelope projections keep working unchanged.
* `TriggerDecision.predicates` may be horizon-tagged: `"4h:keystone_moved"`.
  Fire source stays `probe`; horizon rollovers fire as `rollover` with the
  horizon in predicates.
* `CadenceProfile` gains `horizons: tuple[str, ...] = ()` and per-horizon
  staleness: `horizon_staleness_s: dict[str, int]` — a 4h horizon does not
  heartbeat at 120s; its staleness is hours.

### 2.7 Probe / compute signatures

```python
def probe(self, window, last_state, now_ms) -> TriggerDecision
# becomes (base signature preserved; horizon data rides inside the dicts):
#   window:      {"base": <15m window>, "horizons": {hz: view}}
#   last_state["horizons"]: per-horizon prior state (None on cold start)
def compute(self, evidence, depth) -> dict
# output gains "horizons": {hz: <per-horizon verdict>} alongside
# the unchanged base output.
```

Signatures stay dict-based (the codebase's implicit-contract lesson is
acknowledged but a full typed rewrite is out of H1 scope; the substrate
purity rule is the guard: thresholds still come from the substrate module —
a probe may never invent a horizon threshold that the math module doesn't
own).

### 2.8 Substrate math: unchanged

`calculations/substrates/*` remain pure, horizon-agnostic functions. The fold
produces bucketed populations (e.g. 4h-segmented trade rows) that are handed
to the SAME functions — `hourly_keystone_migration` already proves this
pattern (hour buckets in, verdict out). No substrate module changes; no
substrate purity violation.

### 2.9 Fire economics per horizon

* Cooldown remains global (one fire = one payload = all horizons refreshed).
* Rollover fires: existing L3 machinery, extended per 2.3.
* Staleness: per-horizon (2.6); base-horizon staleness unchanged.
* L2 probes read per-horizon state: "verdict flipped on the 4h grid" is now
  expressible — the entire point of the change.

## 3. Files touched (complete change list)

| File | Change |
|---|---|
| `runtime/redis_store.py` | 1.1 MINID trim in publish Lua; 1.2 slimmed stream body; 1.5 `read_raw_oldest_ms`; guardrail maxlen config |
| `runtime/raw_window.py` | 1.4 retention coverage + horizon degradation |
| `runtime/horizons.py` | **new** — IST offset, `HORIZON_SPEC`, bucket-identity helper |
| `config.py` | `RAW_RETENTION_MS` (default 43 200 000), guardrail maxlen |
| `substrate_worker/contracts.py` | 2.3 ROLLOVER_PERIOD_MS additions; 2.6 schema v2, CadenceProfile |
| `substrate_worker/core/base.py` | 2.2 `HORIZONS` resolution + default no-op `fold` |
| `substrate_worker/core/reader.py` | 2.4 fold invocation on read tick |
| `substrate_worker/core/fire.py` | 2.5 backfill on cold_start/recovery; 2.7 horizon evidence assembly; payload horizons block |
| `substrate_worker/migration_worker.py` | **pilot** — declare `HORIZONS`, per-horizon probe (hour-bucket logic it already owns, lifted to the contract) |
| `docker-compose.yml` / redis config | maxmemory raise per 1.3 table |
| `runtime/postgres_store.py` | horizon checkpoint persistence (2.5 rebuild path) — same `record_substrate_state` JSONB, no schema migration |

## 4. Test plan

1. **Retention trim**: MINID trim evicts by time under backfill burst; MAXLEN
   guardrail still caps; duplicate guard unaffected.
2. **Slimming**: stream body excludes book/tickers; `latest` keeps full
   payload; `read_raw_window` unchanged behavior on slim entries; old-format
   entries readable during rollout.
3. **Coverage honesty**: requested window > retention → `horizon_degraded`,
   never silent truncation.
4. **Fold**: overlap dedupe via trade-id high-water (replayed/duplicate
   snapshots never double-count); burst doesn't shift bucket identity; gaps
   flagged.
5. **IST alignment**: boundary math across the UTC+05:30 offset (4h buckets
   land at IST 00/04/08/…; UTC boundary times at :30 offsets); no-DST
   stability pinned by test.
6. **Backfill**: cold start rebuilds buckets from retained raw; PG-checkpoint
   rebuild path when stream evicted.
7. **Schema v2**: v1 payload read path (implicit base horizon) unchanged;
   GroupEnvelope projection back-compat.
8. **Pilot worker**: migration_worker fires on `bar_4h` rollover with
   horizon-tagged predicates; probe purity preserved (thresholds imported
   from substrate module only).

## 5. Implementation order (all steps COMPLETE)

1. Part 1: slimming + MINID trim + `read_raw_oldest_ms` + coverage honesty
   (independently shippable, prerequisite for everything else).
2. `runtime/horizons.py` + contracts v2 (pure additions, no behavior change).
3. Core fold + backfill machinery (default no-op — zero effect until a
   worker opts in).
4. Pilot: `migration_worker` on `("1h", "4h")`.
5. **Phase A — rollout COMPLETE**: `tape` (fold-native per-horizon CVD/
   buy-share + horizon-tagged band-cross predicates), `technicals`
   (fold-candle EMA/trend/ATR — the horizon leg is **ema9**, the only EMA
   resolving on the ≤14/18-segment rings; base ema21 stays on klines),
   `density` (window-native 4h keystone intensity over the core-attached
   deduped horizon window); `large_print` stays base-horizon only.
6. **Phase B — PG fold rebuild COMPLETE**: `_backfill_horizons` is
   PG-first via the existing `read_substrate_history` reader — restore
   seeds hwm + segments, then the raw backfill folds on top; the monotonic
   trade-id dedupe folds every already-counted trade as zero, so the
   merge is free. v1/absent/failed ledger rows → raw-only fallback.
   `freshness.horizons_rebuilt_from` records the source ("pg"|"raw").
7. **Phase C — budget guard COMPLETE**: poller boot check estimates the
   raw budget (symbols × RAW_STREAM_MAXLEN × ~60KB) against
   `CONFIG GET maxmemory` and warns loudly >70% (never blocks); docker-redis
   maxmemory raised 1500mb → 3000mb per §1.3.

### 5a. Implementation correction — guardrail separation

The raw stream's count guardrail is a SEPARATE cap (`RAW_STREAM_MAXLEN`,
default 20 000 ≈ 28h at 5s cadence) — the state-stream
`REDIS_STREAM_MAXLEN` (default 1200 ≈ 100 min) would silently defeat the
12h MINID contract if reused as the raw burst cap. A count cap below the
time retention MUST NOT be allowed.

## 6. Explicit non-goals (H1)

* No roll-up bar-builder workers (superseded by the approved widened-retention
  decision; revisit only if the 1.3 budget proves untenable in production).
* No WS microstructure horizons — WS surface remains a 15m overlay; the WS
  stream's retention model is a separate concern.
* No typed horizon contracts (dataclass horizon views) — dict-based in H1;
  the substrate purity rule is the guard until a follow-up spec types them.
* Fold horizon semantics: trailing-horizon rings (pruned by segment time
  behind the newest folded trade), NOT IST-bucket-aligned spans — bucket
  identity governs rollovers; the fold ring covers the trailing window.
