# Inference Engine — Tool Packages

The statistical inference engine commands deterministic calculation modules
as TOOLS. The LLM never recomputes a value: it dispatches a tool, receives
deterministic output, and cites it. Every dispatch is scope-validated
(SOLUSDT / BTCUSDT / ETHUSDT × spot / futures / perps), audit-logged to the artifact's `capability_log`,
and bounded in output size.

Citation rule: every numeric claim in an interpretation MUST name the tool
and the field path it came from (e.g. `micro.fit_beta → price_impact_fit.beta`).
Uncited numeric claims are contract violations. `null` means not-provided —
never zero, never invent.

NOTE: loop pass budgets and dispatch ceilings are NOT stated here — the engine
injects the live numbers from `engine/config.py` at runtime. The model reads
the LOOP STATE block on every turn for its current loop, passes spent/left,
and traversal so far.

## Package micro — capture / replay / persisted evidence reads

Purpose: establish tape health, replay determinism, and read the persisted
same-window fit state. Reads only — never a prediction. Gate-quality signals
(state, gaps, fit status) gate the cycle; everything else is audit material.

### `micro.capture_status`
- First read, every cycle: `state`, `sequence_gaps`, `reconnects`.
- Gaps or non-running state caveat every downstream fit; never ignore.

### `micro.events`
- Raw best-quote transition tape with paper `e_n` contributions. Args: `count` (bounded).
- Ordered `previous`/`current` quotes + `contribution` per transition.

### `micro.ofi_intervals`
- Completed OFI interval rows (`ofi`, `average_depth`, boundary mids, `quality`, estimator label).
- Args: `count` (bounded). Deterministic replay of the tape.

### `micro.replay`
- Pure determinism check: events + `interval_ms` → `(events, intervals, dropped)`. No I/O.
- Use to verify aggregation reproduces or to rebuild at another cadence.

### `micro.fit_beta`
- Current same-window fitted state (β impact, sensitivity fit, c/λ depth scaling, coverage).
- Same-window impact never substitutes for a forward target (doctrine). Respected `status` only.

### `micro.evidence`
- Last PERSISTED immutable evidence object (prior cycle), not a fresh fit. May be null.

## Package calc-base — split OFI/AD inference + derived diagnostic

Purpose: the paper stack split into separately-cited steps (OFI and AD never
merged), ending in the derived ΔP diagnostic. Baseline path: forward-native
tools upgrade it; agreement AND disagreement between paths are both reported.

### `calc.ofi.intervals`
- Deterministic OFI per interval (Cont `OFI_k`), AD excluded. Args: cadence/window.
- OFI-only rows (`ofi`, `event_count`, `quality`).

### `calc.depth.average`
- Deterministic AD per block (event-average `(qB+qA)/2`), OFI excluded.
- `ad_per_interval`, `mean_ad`, estimator label.

### `calc.observation.build`
- Joins intervals + AD + mids → observations (`ofi`, `delta_ticks`, `average_depth`) + excluded count.
- The join the fits consume; excluded rows are findings, not errors.

### `calc.fit.price_impact` / `calc.fit.depth_scaling`
- OLS ΔP=α+β·OFI (HC0 SE) and log-log depth scaling as independent checks.
- Depth scaling needs ≥3 distinct-AD blocks; single-window `insufficient` is a correct null.

### `calc.price.delta` (alias `calc.derived_diagnostic`)
- NUMERIC derived ΔP for one OFI value (arg, else latest interval): route A direct + band, route B depth-scaled when c/λ identify.
- Refuses (null, never zero) on gate-failed fits / empty tape / bad OFI. Cite `→ route_a_direct.delta_ticks`.

### `calc.scenario.evaluate`
- Legacy flow-requirement path: target + 15m|1h|4h → required horizon flow vs empirical OFI distribution, band-aware, route-B cross-check.
- Current price resolved inside the tool (never agent-supplied). Baseline for forward comparison, never a short-horizon verdict.

## Package calc-forward — horizon-native forward stack (primary for horizon questions)

Purpose: event-grain forward targets, per-horizon fits, conditional
probabilities, formal tests, typed events, decay, discipline. For any
question bearing a horizon, threshold, target, or hypothesis, this package
outranks calc-base; calc-base remains its comparator.

### `calc.forward.forecast`
- Canonical deterministic `ForecastResult`: schema-checked feature vector, forward target, train-only OOS evaluation, calibration-gated probabilities, native/long-horizon semantics, assumptions, and preserved Route A/Route B disagreement.
- This is the primary forward tool. The lower-level feature/join/fit/distribution tools are diagnostic sub-steps.

### `calc.feature.build`
- Versioned xt-v2 feature vector from the latest window (OFI/AD/Dmu/spread/OBI/skew/CVD + definition versions + schema hash).
- Floats rejected; unproven fields NULL. Cite `vector_version` + fields.

### `calc.forward.join`
- True forward join Y(h)=P_{t+h}−P_t at event grain (1s/5s/30s/60s) + per-horizon exclusion log.
- Gaps never bridged; exclusions counted. No fitting here.

### `calc.forward.fit`
- Per-horizon multivariate OLS Y(h)~X (Decimal, train-only time-ordered OOS) + univariate Cont comparator. Args: `horizon_ms`.
- Carries feature-schema identity, train/OOS counts, OOS metrics, and separate estimation/validation/probability status. No-skill is a valid stop, never overruled.

### `calc.forward.distribution`
- E[Y(h)] + 95% PI + P(>0)/P(>theta) from a fitted fit. Threshold arrives with the query, never embedded.
- Normal-approx stated on output; miscalibrated/insufficient → NULL + refusal.

### `calc.forward.scenario`
- Horizon-native P(T)/P(S) curves with bands from forward fits. Args: `horizon_ms`, `targets`, `invalidations`.
- Compares against the legacy path (agreement + disagreement). No optimality claim.

### `calc.hypothesis.test`
- Independent post-fit test ONLY (never auto-called): effect/SE/CI/p + n/split/OOS + multiplicity. Args: `hypothesis_id` (pre-registered, required), `horizon_ms`, `m_tests`.
- p<0.05 is evidence, never an execution predicate. No signal/action field exists.

### `calc.events.absorption` / `calc.events.walls`
- Typed Absorption detector + 10-field wall lifecycle (Decimal, event grain) + replay-agreement stats.
- No institutional attribution, structurally. E[ΔP|event] runs through the forward fit.

### `calc.decay.report`
- Per-horizon skill-decay (OOS skill), regime splits, finalized horizon set. Nulls listed — nulls are results.

### `calc.discipline.audit`
- Nine-lock audit (leakage/time-order/horizon/regime/baseline/cost/calibration/multiplicity/pins) → go/no-go memo.
- Any fail → no-go. Phase-12 opens only on go.

## Package market — canonical pipeline correlation context

Purpose: regime context for correlation (P3), subordinate to fresh
calculation-plane evidence. Snapshot-first; deep-dives only on demonstrated need.

### `market.read`
- Latest collated run. Modes: `snapshot` (bounded headline + CVD sign series — default), `inventory`, `full` (explicit deep-dive only).
- Cite `market.read → <field>`. Schema mismatch = stale writer: report, never coerce.

### `market.derivatives`
- Funding / OI / cross-asset cache. Null/expired means unavailable, never zero.

### `market.keystone_history` / `market.wall_history`
- Bounded cross-cycle ledgers for migration reasoning. Args: `count`.

## Package substrate — always-fresh worker plane (primary P3 evidence)

Purpose: warm projections computed on the calculation plane. Invoke for a
bounded fire-tick, then read; judge freshness from `age_ms` against each
worker's own cadence.

### `substrate.read`
- Snapshot over workers (compact default) or one substrate (`substrate`, `mode` compact|full).
- Missing workers are `available:false`, never errors. Cite `→ substrates.<name>.available`.

### `substrate.invoke` / `substrate.<worker>`
- Bounded fire-tick request (12 workers: tape, density, delta, ladders, anchors, tiers, volume_profile, technicals, migration, oi, signals, large_print).
- Cooldowns gate inside the core; `invoked:false` is a finding. Same worker never twice per cycle; invoke BEFORE read in one array.

## Package memory — paper grounding + episodic priors

Purpose: Cont-Kukanov-Stoikov facts for H0/H1 grounding (evidence home) and
provenance-tagged priors subordinate to fresh ledger data. Contradicting a prior is allowed, stated explicitly.

### `memory.recall_paper`
- Paper facts from the real MemoryNode KB (kind=fact, paper session). Args: `query`.
- Empty = unseeded KB: report it, never invent paper claims. `fact` kind is LLM-forbidden in proposals.

## Loop walk (runtime values injected per turn — see LOOP STATE block)

Comprehension (understand once, no tools) → Evidence (gather: micro + calc-base + reads + memory) → Reasoning (test + compare: calc-forward) → Validation (discipline audit + gate; one bounded retry, then terminal with findings preserved) → Output (non-agentic composition, tools dropped). Closed loops are never revisited; refusals and nulls travel forward as findings.

## Anti-patterns (contract violations)

1. Recomputing any value in prose instead of dispatching the tool.
2. Citing a tool you did not dispatch this cycle.
3. Treating `insufficient` fits, null evidence, or empty ledgers as zeros.
4. Merging the two fitted models into a single prediction.
5. Requesting the same tool with identical args repeatedly in one cycle.
6. Concluding or testing hypotheses inside Evidence passes; gathering inside Reasoning passes (loops own their work).
7. Placing tools in the Output pass (dropped unread) or revisiting a closed loop.
