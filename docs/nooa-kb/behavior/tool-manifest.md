# Inference Engine — Tool Manifest

The statistical inference engine commands deterministic calculation modules
as TOOLS. The LLM never recomputes a value: it dispatches a tool, receives
deterministic output, and cites it. Every dispatch is scope-validated
(SOLUSDT / BTCUSDT / ETHUSDT × spot / futures / perps), audit-logged to the artifact's `capability_log`,
and bounded in output size.

Citation rule: every numeric claim in an interpretation MUST name the tool
and the field path it came from (e.g. `micro.fit_beta → price_impact_fit.beta`).
Uncited numeric claims are contract violations.

## T1 — Microstructure tools (paper stack)

### `micro.capture_status`
- When: always first — establish capture health before trusting any tape.
- Returns: `state` (running/gap/reconnecting/...), `sequence_gaps`,
  `reconnects`, `last_update_id`.
- Interpretation: `sequence_gaps > 0` or state != running → caveat every
  downstream fit; never silently ignore.

### `micro.events`
- When: you need the raw best-quote transition tape (with paper `e_n`
  contributions) for a window.
- Args: `count` (default 200, bounded).
- Returns: ordered transition records (`previous`/`current` quotes,
  `contribution`).

### `micro.ofi_intervals`
- When: you need completed OFI interval rows (paper `OFI_k` + event-average
  depth) rather than raw transitions.
- Args: `count` (default 200, bounded).
- Returns: interval rows with `ofi`, `average_depth`, `mid_start`/`mid_end`,
  `quality`, `depth_estimator`.

### `micro.replay`
- When: you must verify that interval aggregation reproduces from raw
  events (determinism check), or rebuild intervals at a different cadence.
- Args: `event_payloads`, `interval_ms`.
- Returns: `(events, intervals, dropped_count, log)` — pure, no I/O.

### `micro.fit_beta`
- When: you need the CURRENT fitted state: β (price impact), the
  tautology-caveat sensitivity fit, and the cross-block depth scaling
  (c, λ) over prior evidence.
- Args: `interval_seconds` (10|15|30), `window_minutes` (15|30|60),
  `tick_size` (default 0.01).
- Returns: full MicrostructureEvidence: `price_impact_fit` (α, β, stderr,
  r², status), `sensitivity_fit`, `depth_scaling_fit` (c, λ or nulls),
  `block_average_depth`, `coverage`.
- Interpretation discipline (constitution): the two models are NEVER
  merged into a point prediction — the combined expression carries a
  heteroskedastic ν·OFI term and is at most a derived diagnostic. Respect
  `status`: `insufficient` fits must not be interpreted at all;
  `provisional` fits must be caveated.

### `micro.evidence`
- When: you need the last PERSISTED immutable evidence object (the fit at
  last cycle), not a fresh fit.
- Returns: the stored MicrostructureEvidence dict, or null.

## Staged inference cycle (P1→P6)

Coverage is measured from tool families you EXECUTE, not phases you declare.
A FINAL turn (`tool_calls=[]`, phase P6) is rejected for repair unless P1+P2+P3+P5
all have ≥1 executed tool, a P6 synthesis turn was declared, `hypothesis.H0` is set,
`summary` is ≥200 chars (the P4 why-now explanation), `confidence` is low|medium|high,
every evidence entry carries a non-empty `interpretation`, and `evidence` cites ≥2 distinct roots
including ≥1 fresh tool result plus a `calc.price.delta → …` ΔP path. Budgets: 5 tool rounds (≤3 calls each),
8 LLM turns per cycle.

The two fitted models are NEVER merged. Call OFI and AD as SEPARATE tools,
then join via `calc.observation.build`. The combined formula is a derived
diagnostic only (`calc.derived_diagnostic`).

### `calc.ofi.intervals`
- When: you need deterministic OFI per interval (paper Cont `OFI_k` = sum e_n
over half-open clock-bound `[t_{k-1}, t_k)`), with AD explicitly excluded.
- Args: `interval_seconds` (default 10), `window_minutes` (default 30).
- Returns: OFI-only interval rows (`ofi`, `event_count`, `quality`).

### `calc.depth.average`
- When: you need deterministic AD per block (paper `AD_i` = event-average
  `(qB+qA)/2`), with OFI explicitly excluded.
- Returns: `ad_per_interval`, `mean_ad`, `depth_estimator`.

### `calc.observation.build`
- When: you need the joined observations (ΔP ticks vs OFI) that feed the fits.
- Returns: observation rows (`ofi`, `delta_ticks`, `average_depth`, `quality`)
  plus excluded count.

### `calc.fit.price_impact` / `calc.fit.depth_scaling`
- When: you need the OLS ΔP=α+β·OFI fit (HC0 SE) or the log-log depth-scaling
  fit recomputed as an independent check on `micro.fit_beta`.
- Depth scaling needs ≥3 distinct-AD blocks; a single window yields
  `insufficient` by design — that is a correct null, not an error.

### `calc.price.delta` (alias `calc.derived_diagnostic`)
- When: P5 derivation — you have an OFI value (scenario arg, or latest
  closed interval by default) and need the NUMERIC derived ΔP.
- Args: `ofi` (optional; default = latest interval OFI, `ofi_source` echoed),
  `interval_seconds` (10/15/30), `window_minutes` (15/30/60).
- Returns: `route_a_direct` (ΔP ticks + quote + 95% band from α+β·OFI),
  `route_b_depth_scaled` (depth-scaled ΔP when c/λ identify, else
  `unavailable` with reason), `agreement_ticks`, hetero warning.
- Refuses (`status: refused`, result null) on gate-failed fits, empty tape,
  or bad OFI — a refusal is a finding, report it, never substitute zero.
  Cite as `calc.price.delta → route_a_direct.delta_ticks`.

### `memory.recall_paper`
- When: FIRST step of every validation workflow — pull Cont-Kukanov-Stoikov
  paper facts (OFI definition, β regression, depth scaling, heteroskedastic
  caveat) from the real MemoryNode paper KB to ground H0/H1.
- Args: `query` (default "Cont OFI AD beta").
- Returns: paper fact entries (`content`, `tags`, `importance`). Empty means
  the KB is unseeded — report it, do not invent paper claims.

## T2 — Market correlation tools (canonical pipeline)

### `market.read`
- When: you need the latest collated market run — prices, funding, OI,
  keystone/wall, flow, technical classification, CVD sign series.
- Args: `mode` (optional): `snapshot` (default — bounded headline view),
  `inventory` (section keys + snapshot), `full` (raw payload deep-dive;
  only when the snapshot demonstrably lacks the field you need — prefer
  citing what the snapshot has).
- Returns (snapshot): run identity (`schema_version`, `symbol`, `status`,
  `run_id`, `generated_at`, `completed_at`, `data_source`,
  `domain_status`, `error_count`), headline scalars (`last_price`,
  `volume_24h`, `high_24h`, `low_24h`, `funding_rate`, `mark_price`,
  `open_interest`, `spot_cvd`, `futures_cvd`, `spot_obi`, `futures_obi`,
  `fut_keystone_bid`, `fut_keystone_ask`, `keystone_bid_qty`,
  `keystone_ask_qty`, `bid_ladder_notional`, `ask_ladder_notional`,
  `keystone_trade_buy_qty`, `keystone_trade_sell_qty`,
  `hourly_keystone_verdict`, `seller_aggression`, `bid_anchor_count`,
  `mega_tier_pct`, `fut_microprice_skew_bps`), and `cvd_sign_series` —
  per-window `{window_seconds, buckets, delta_usd_sum, sign}` across
  900/300/120/60/30s. Sign flips across these windows are the
  institutional delta-flip signal.
- Citation paths use this tool name and its field names:
  `market.read → fut_keystone_bid`.
- `null` means not-computable/absent — never zero, never invent. A
  `schema_mismatch` error means the writer is on a different contract:
  report it, do not retry.

### `market.derivatives`
- When: you need funding / OI / cross-asset context to correlate against
  order-flow inference.
- Returns: cached derivative evidence (may be null/expired — null means
  unavailable, never zero).

### `market.keystone_history` / `market.wall_history`
- When: cross-cycle context — prior keystone/wall states for migration
  reasoning.
- Args: `count` (default 100, bounded).
- Returns: newest-first ledger rows.

## T3 — Substrate worker plane (always-fresh warm projections)

Tool-first invocation (replaces the removed run_cycle): agents invoke workers
for bounded fire-ticks, then read the warm projections. P3 coverage credits
any `substrate.*` family execution.

### `substrate.read`
- When: you need the always-fresh worker snapshot (compact by default) or one
  substrate (`substrate` arg, `mode` compact|full).
- Returns: per-substrate `{available, status, trigger_source, age_ms}` in compact
  mode; raw payloads in full mode. Missing workers are `available:false`, never errors.
- Citation: `substrate.read → substrates.tape.status`.

### `substrate.invoke` / `substrate.<worker>`
- When: P3 correlation — fire a bounded tick before reading, so the projection
  is fresh relative to the micro tape.
- Workers (12): `tape, density, delta, ladders, anchors, tiers, volume_profile,
  technicals, migration, oi, signals, large_print`. `substrate.invoke` with no
  substrate fires all registered workers.
- Returns: `{invoked, fired, trigger_source, status, available}` per worker.
  Cooldowns still gate inside the core; `invoked:false` is a structured report, never an error.
- Workflow: invoke `substrate.tape`/`density`/`delta`/… then `substrate.read`;
  name agreements AND contradictions vs P1/P2 explicitly.
- Ordering: dispatches run sequentially in the listed order — put invoke BEFORE
  read in the same `tool_calls` array so the read sees the fresh projection.
  Never request the same worker twice in one cycle (deterministic — anti-pattern 5).
- Freshness: compact projections carry `age_ms` — judge staleness from it.
  `available:false`, `fired:0`, or `invoked:false` (cooldown-dormant) are FINDINGS:
  cite them (`substrate.read → substrates.<name>.available`) and move on.
- Empty upstream (no projections at all — capture down) is itself the finding:
  report `substrate.read → substrates.*.available:false` as the P3 verdict
  instead of forcing correlation against absent data.

## Anti-patterns (contract violations)

1. Recomputing any value in prose instead of dispatching the tool.
2. Citing a tool you did not dispatch this cycle.
3. Treating `insufficient` fits, null evidence, or empty ledgers as zeros.
4. Merging the two fitted models into a single prediction.
5. Requesting the same tool with identical args repeatedly in one cycle
   (deterministic — the answer will not change).
