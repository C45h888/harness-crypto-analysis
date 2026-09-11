evelopment theory specification — deliberately independent of integration semantics

1. Purpose

This document defines what the existing Object-Oriented Microstructure Agent should be able to do in theory. It is not an implementation plan, execution design, Redis schema, or state-mutation specification. Those integration semantics remain intentionally open so the development workforce can fit the theory into the existing system without prematurely constraining the architecture.

2. Theoretical Boundary

The agent is a market-intelligence and conditional-prediction component. Its theoretical responsibility is to transform observed order-book and trade-flow state into measurable microstructure features, identify microstructure events, estimate conditional future price behavior, and provide statistical evidence for hypotheses. It should not equate statistical evidence with an automatic trading instruction.

3. Core Question

The central question is not 'Is the market bullish or bearish?' It is:

Given the current microstructure state X_t, what can be statistically inferred about future price displacement over a defined horizon h?

4. Microprice Theory

For a best bid/ask book, define the midprice as:

M_t = (P_bid,t + P_ask,t) / 2

A common volume-sensitive microprice formulation is:

P_μ,t = (P_ask,t × Q_bid,t + P_bid,t × Q_ask,t) / (Q_bid,t + Q_ask,t)

where Q_bid and Q_ask are the displayed quantities at the best bid and ask. The microprice shifts toward the side with comparatively less displayed liquidity, providing a compact representation of short-horizon directional pressure.

The primary derived feature is microprice displacement:

D_μ,t = P_μ,t − M_t

D_μ is a feature, not a guaranteed forecast. Its predictive value must be established empirically.

5. Microstructure Feature Space

The agent should be able to construct a feature vector X_t from measurable market state, including:

microprice displacement

OFI (order-flow imbalance)

bid/ask depth imbalance

signed trade-flow or volume imbalance

bid-ask spread

recent returns and short-horizon price velocity

short-horizon volatility

liquidity-zone distance

large-order size and persistence

order additions and cancellations

replenishment / absorption measurements

other empirically justified microstructure variables

Feature definitions should be explicit, timestamped, and versioned. The theoretical model must preserve the distinction between an observed feature and an interpretation of that feature.

6. Prediction Target

For a prediction horizon h, define the future price-change target:

Y_t(h) = ΔP_t(h) = P_{t+h} − P_t

The agent should support multiple horizons because microstructure information is expected to decay with time. Candidate horizons can include 1s, 5s, 30s, and 60s, but the actual horizon set should be determined by empirical validation and feed resolution.

7. Conditional Prediction

The theoretical object is the conditional distribution:

ΔP_t(h) | X_t

The question becomes: historically, what future price behavior is associated with states resembling the current X_t? This is the bridge between deterministic microstructure measurement and predictive analysis.

8. Simple Linear Regression Baseline

The first statistical model should be deliberately transparent:

Ŷ_t(h) = β₀ + β₁X₁,t + β₂X₂,t + … + β_kX_k,t

This baseline allows the system to determine whether variables such as microprice displacement and OFI carry incremental predictive information, what direction their estimated relationships have, how uncertain those estimates are, and how the model behaves out of sample.

Complex models should only be introduced after this baseline is understood. A more complex model that cannot be compared against a transparent baseline makes it harder to distinguish genuine predictive information from overfitting.

9. Probability Theory

A point estimate of expected price change is insufficient for trading-oriented reasoning. The agent should also reason about the conditional distribution of future price displacement.

E[ΔP | X_t] = μ(X_t)

with uncertainty represented through an estimated distribution, variance, prediction interval, or another validated probabilistic representation.

This enables questions such as:

P(ΔP > 0 | X_t)

P(ΔP > 30 | X_t)

P(ΔP < −10 | X_t)

The numerical threshold should be supplied by the hypothesis/query rather than embedded as an arbitrary trading rule.

10. Target and Invalidation Probability

For a candidate target price T, the theoretical capability is:

P(P_{t+h} ≥ T | X_t)

Likewise, for an invalidation level S:

P(P_{t+h} ≤ S | X_t)

This allows candidate targets to be compared probabilistically. It does not mean that a target with the highest probability is automatically optimal; reward, loss, time horizon, transaction costs, and execution conditions belong to subsequent reasoning.

11. Hypothesis Testing

Directional hypotheses can be expressed formally. For example, for a bullish conditional-price hypothesis:

H₀: E[ΔP | X] ≤ 0

H₁: E[ΔP | X] > 0

The statistical component should calculate the appropriate evidence: estimated effect, standard error, confidence interval, p-value or equivalent model-specific evidence, sample size, and out-of-sample behavior.

A statistical threshold such as p < 0.05 is not itself a trading command. Statistical significance is evidence about a hypothesis; the reasoning layer must consider whether the evidence is economically and operationally meaningful.

12. Microstructure Reversal Hypothesis

A central use case is testing whether identifiable liquidity behavior is associated with subsequent reversal.

Example observed pattern:

large resting bid

repeated replenishment

aggressive selling

limited downward price response

OFI begins to recover

microprice displacement turns upward

price fails to establish a new low

The agent can construct a structured hypothesis:

Bullish absorption → subsequent positive ΔP

The historical test then asks whether comparable absorption states have a statistically distinguishable positive future price response.

H₀: E[ΔP | Absorption] ≤ 0

H₁: E[ΔP | Absorption] > 0

13. Liquidity and Wall Semantics

A 'wall' should not be treated as simply 'order size exceeds threshold'. The theoretical object should capture behavior over time.

price and side

displayed size

distance from midprice

persistence / lifetime

additions

cancellations

executions

replenishment

interaction with aggressive flow

resulting price response

This permits hypotheses about persistence, absorption, exhaustion, and liquidity failure. The system should avoid claiming that an observed wall is definitively 'institutional' unless there is an independently defensible identification mechanism.

14. Historical + Live Conditional Reasoning

The existing memory capability provides the conceptual ingredients for conditional prediction:

LIVE STATE → WebSocket/current market state → current X_t

HISTORICAL STATE → PostgreSQL observations → historical X_t and subsequent ΔP

The theoretical question is therefore whether the current state belongs to a historical population whose conditional future behavior can be estimated. This is stronger than simply asking an agent to 'predict price'.

15. Multiple Prediction Horizons

Prediction should be horizon-specific:

predict ΔP over 1s

predict ΔP over 5s

predict ΔP over 30s

predict ΔP over 60s

The agent should be able to discover that a feature has predictive value at one horizon but weak or absent value at another. This horizon decay is itself a useful empirical result.

16. Theoretical Output

The agent should theoretically be able to return a structured analytical result containing:

current feature vector / feature-set version

prediction horizon

expected ΔP

uncertainty / variance / standard error

confidence or prediction interval

probability of positive movement

probability of candidate target

probability of candidate invalidation

hypothesis being tested

effect estimate

statistical evidence

sample size

model/version information

out-of-sample validation information

relevant detected microstructure events

This is an analytical evidence object in theory. Its concrete storage, API, orchestration, and state-mutation representation are intentionally outside this document.

17. Statistical Discipline

No look-ahead leakage.

Time-ordered train/validation/test methodology.

Separate evaluation by prediction horizon.

Regime-aware evaluation where appropriate.

Out-of-sample testing before interpreting a relationship as predictive.

Comparison with simple baselines.

Explicit feature/model versions.

Transaction costs and execution effects considered when moving from statistical prediction to economic evaluation.

Probability calibration checked if probabilistic outputs are used.

Multiple-hypothesis/data-mining effects considered when many candidate patterns are tested.

18. What This Agent Should NOT Claim

Microprice alone predicts the next price tick with certainty.

A statistically significant coefficient guarantees profitability.

p < 0.05 means a trade should be placed.

A large resting order is necessarily institutional.

A historical relationship will remain stable indefinitely.

A predicted target is guaranteed to be reached.

19. Capability Boundary

At the end of this theoretical stage, the Microstructure Agent should be capable of answering questions such as: What is the current microstructure state? What liquidity event is occurring? What does the fitted model estimate for future ΔP at a specified horizon? How uncertain is that estimate? What is the probability of reaching a candidate target or invalidation? Does the current state provide statistical evidence for a specified hypothesis? Has a reversal pattern historically preceded measurable price displacement?

It should not, by theoretical necessity, decide the final trade, select an execution mechanism, or mutate the broader trading system's state in a prescribed way. Those concerns are deliberately left for a later integration design.

20. Development Interpretation

The development task is therefore not 'add an AI trading strategy'. It is to make the existing OO Microstructure Agent mathematically capable of producing a reproducible chain:

OBSERVATION

→ MICROSTRUCTURE MEASUREMENT

→ MICROSTRUCTURE EVENT

→ FEATURE VECTOR

→ CONDITIONAL MODEL

→ EXPECTED ΔP + UNCERTAINTY

→ TARGET / INVALIDATION PROBABILITIES

→ HYPOTHESIS TEST

→ STATISTICAL EVIDENCE

That chain is the theoretical contract. Integration details should be designed only after the development workforce has confirmed that this capability can be implemented cleanly within the existing system.


23. Final Doctrine

The Microstructure Agent is a conditional market-state inference system. Microprice, OFI, liquidity behavior, and related order-flow variables form measurable evidence. Statistical fitting converts those measurements into conditional estimates of future price displacement. Probability theory converts point estimates into distributions and target/invalidation probabilities. Hypothesis testing determines whether observed relationships are statistically supported. Historical data establishes the empirical population against which the live state can be evaluated. The agent's output is evidence and hypothesis support—not an automatic trade.
---

PART B — INFERENCE ASSESSMENT (OO AGENT SURFACE PROBE, 2026-09-10)

Status of this part: assessment only. No interface, schema, Redis, Postgres,
or behavior change is authorized by this appendix. It records where the
existing system stands relative to Part A so the phased plan in Part C starts
from evidence, not assumption.

24. What was probed

- `market_service/microstructure/` (contracts, ofi, orderbook, capture, fitting)
- `market_service/nooa_harness/engine.py` + `inference/` (capability, dispatch, gate)
- `market_service/runtime/contracts.py` (InferenceArtifact trichotomy)
- `market_service/calculations/` + `substrates/` (tape, auction microprice,
  ladders, density, absorption)
- `tests/test_microstructure.py`, `tests/test_inference_engine.py`

25. What the OO agent already is

A Cont-paper OFI inference stack with strong deterministic discipline: exact
best-quote event reconstruction (`e_n`), frozen depth estimator
(`event_mean_best_bid_ask_v1`), Decimal-50 OLS `dP_k = alpha + beta*OFI_k`
with HC0 SE, log-log depth scaling (`beta = c*AD^-lambda`), sensitivity
(queue-only) fit, same-window price join, required-flow scenario evaluation
(`OFI_req = (d_req - n*alpha)/beta` vs empirical rolling-sum exceedance,
horizons 15m/1h/4h), SHA-256 `input_hash` reproducibility,
`BookGapError`-never-bridged replay, trichotomy gate
(`validated/provisional/insufficient`, NULL interpretation + zero LLM tokens
on `insufficient`), dual-model no-merge rule (heteroskedastic `nu*OFI`
caveat), and an agentic P1-P6 narration plane (OFI -> AD ->
substrate/market correlation -> explanation -> paper-grounded derivation ->
output) with bounded tool registry. Estimated coverage of Part A: ~25-30%.

26. Clause-by-clause gap summary

- S4 microprice: COMPUTED ELSEWHERE, UNWIRED. `substrates/tape.py:
  microprice_skew_bps` and `analysis/auction.py:microprice` exist on poller
  snapshots; `microstructure/` never computes `D_mu = P_mu - M_t`, never
  stores it on intervals, never fits on it.
- S5 feature vector `X_t`: 1 of ~12 families present (OFI + AD only). Spread
  derivable but never materialized; imbalance/CVD/absorption/walls/
  volatility/returns/adds/cancels/replenishment live in adjacent workers,
  never joined as versioned features. No `X_t` object, no feature-set version.
- S6 forward target `Y_t(h) = P_{t+h} - P_t`: NOT IMPLEMENTED. Current `dP_k`
  is same-window impact (`mid_end - mid_start` inside `[start,end)`). No
  forward join exists.
- S7 conditional `dP(h)|X_t`: plumbing present (Redis live + PG history +
  deterministic replay seam), population absent (30-min rolling tape, single
  regressor, contemporaneous).
- S8 multivariate baseline: MISSING. Univariate OFI only; no
  `Y = b0 + b1*X1 + ...` incremental-information test.
- S9 distribution: PARTIAL. SE band + exceedance exist; no predictive
  variance / prediction interval / `P(dP>0)` / query-threshold `P(dP>theta)` /
  calibration.
- S10 target/invalidation: PARTIAL, WRONG HORIZON. Scenario shape is right
  (one-sided, band-aware, up/down symmetric) but at 15m/1h/4h under a
  recorded-not-proven linear scale-invariance assumption, not fitted 1-60s
  horizons.
- S11 hypothesis test: SCAFFOLD. LLM H0/H1 + deterministic band-aware verdict
  exist; no formal effect/SE/CI/p-value/n/out-of-sample evidence object.
- S12 absorption-reversal: INGREDIENTS ONLY. `path_absorption`, ladders,
  large-print, delta workers exist; no typed `Absorption` event, no
  `E[dP|Absorption]` test, `D_mu` leg unmeasurable.
- S13 walls: THIN. Migration/ladders/density track levels/stacks, not the
  10-field wall lifecycle (price/side/size/distance/persistence/adds/cancels/
  executions/replenishment/flow-interaction/response). No
  institutional-attribution claims made (good).
- S14 history+live: PLUMBING YES, POPULATION NO. Capture -> Redis -> PG ->
  replay is correct; versioned feature/observation store keyed by `X_t`
  regime does not exist.
- S15 horizon decay: NOT DISCOVERABLE. `interval_seconds` varies measurement
  cadence, not forecast horizon.
- S16 output object: ~40%. Has model/version/hash/status/coverage/scenario;
  lacks `X_t`+version, forecast `h`, forward `E[dP]`, predictive variance,
  `P(positive)`, short-h target/invalidation, formal evidence,
  out-of-sample, event taxonomy.
- S17 discipline: STRONG on determinism (hashes, frozen estimator, gap
  refusal, sensitivity check, hetero flag), WEAK on validation (no
  train/val/test, no horizon-separated or regime-aware eval, no baseline
  comparison, no cost-aware or calibration or multiplicity control). Latent:
  hardcoded `tick_size="0.01"` in calc tools.
- S18/S19 boundaries: ALIGNED. Evidence-not-trade, no-merge, no-guarantee,
  no-institutional-claim discipline already structural.

27. Boundary verdict (present capability)

- "Current microstructure state?" Partial (OFI/AD/beta/status, no `D_mu`,
  no event taxonomy).
- "What liquidity event is occurring?" No.
- "Forward `E[dP]` at horizon h + uncertainty?" No (diagnostic only).
- "P(target)/P(invalidation)?" Yes with asterisk (long-h flow exceedance
  under unproven scaling).
- "Statistical evidence for hypothesis?" Weak yes (verdict + fit stats,
  no formal test).
- "Did reversal patterns precede displacement?" No.

---

PART C — LONG-HORIZON PHASED INTEGRATION PLAN

Status of this part: sequencing only. Phases define entry criteria,
theoretical exit evidence, and explicit non-goals. No phase prescribes
files, schemas, APIs, worker topology, or state mutation. The development
workforce owns all integration semantics; phases are gated so later work
never starts on an unproven earlier claim. Codebase work begins only after
the phase it belongs to is opened; earlier phases are measurement and proof.

28. Plan principles (binding on all phases)

1. Theory first, integration after: each phase must demonstrate its
   statistical object exists and reproduces before any system wiring is
   designed.
2. Inherit the discipline: Decimal determinism, canonical input hashes,
   frozen versioned definitions with refusal on mismatch, gap-never-bridged
   replay, trichotomy gating, no-merge rule for heteroskedastic models,
   NULL-means-not-provided.
3. One new claim per phase: a phase that proves two things proves neither;
   split it.
4. Baselines before complexity: no model enters a later phase without beating
   (or tying with documented reason) the transparent comparator from its own
   phase.
5. Forward-only evidence counts: same-window impact never substitutes for
   `Y_t(h)`.
6. Stop rules are first-class: every phase names the result that would halt
   or redirect the program (e.g. no horizon shows signal, `D_mu` adds
   nothing, absorption states do not separate).

29. Phase 0 — Baseline map and frozen inventory (no new math)

Goal: agree what exists so later phases cannot silently redefine it. Entry:
Part A + Part B accepted as governing. Work (assessment-grade): inventory
every live microstructure-adjacent computation (OFI/AD/fits/scenario/
microprice-skew/CVD/ladders/density/absorption/walls/memory) with its
definition owner, unit, cadence, and version status; list every hardcoded
assumption found (tick size, window lengths, horizon constants); record the
exact current chain OBSERVATION -> EVIDENCE with hashes of representative
outputs. Exit evidence: a separate integration-design note (owned by the
development workforce) confirms the map is complete enough to place later
phases without renaming existing objects. Non-goals: no refactors, no new
features, no schema changes.

30. Phase 1 — Microprice displacement as a versioned feature (measurement only)

Goal: `D_mu,t` becomes a first-class observed feature with the same
provenance discipline as OFI. Work: freeze the `M_t / P_mu,t / D_mu,t`
definitions (handling zero/negative quantities, crossed/locked books, tick
representation), timestamp and version them, attach them to the same
event/interval grain as OFI without altering any existing OFI/AD value or
fit. Exit evidence: replayed fixtures yield bit-identical OFI/AD plus new
deterministic `D_mu` series with its own version label and
mismatch-refusal rule. Non-goals: no predictive claim, no fit on `D_mu`,
no threshold or signal.

31. Phase 2 — Feature vector `X_t` (observation, not interpretation)

Goal: one explicit, timestamped, versioned `X_t` per decision grain. Work:
promote, one family at a time, the S5 list (OFI, `D_mu`, depth imbalance,
signed flow/volume imbalance, spread, returns/velocity, short volatility,
liquidity-zone distance, large-order size/persistence, adds/cancels,
replenishment/absorption, plus justified additions) from adjacent workers
into versioned feature definitions with units, denominators, and quality
flags; preserve the observed-vs-interpreted split (features carry
measurements; regimes/verdicts live downstream). Exit evidence: a
feature-set version whose replay is deterministic and whose every field
traces to a named definition version. Non-goals: no model, no selected
"best" subset (selection is a Phase 4 matter with multiplicity control).

32. Phase 3 — Forward targets `Y_t(h)` with leakage control

Goal: replace contemporaneous `dP_k` with true forward displacement as the
prediction target. Work: define the forward join (`P_{t+h} - P_t` at
candidate h including 1s/5s/30s/60s, price source and timestamping rules,
handling of gaps/halts/missing prints), prove no look-ahead (features at `t`
never see `[t, t+h]`), keep same-window impact as a separate diagnostic (not
renamed, not removed). Exit evidence: replay fixtures produce deterministic
`(X_t, {Y_t(h)})` pairs per horizon with a documented exclusion log for
unformable targets. Non-goals: no fitting yet; horizon-set finalization
waits for Phase 8 decay results.

33. Phase 4 — Transparent multivariate baseline per horizon

Goal: the S8 object `Y^_t(h) = b0 + b1*X1 + ...` fitted and reported
separately per horizon. Work: OLS (or documented equivalent) with effect,
SE, CI, per-feature incremental information, out-of-sample behavior by
time-ordered split; univariate Cont fit retained as comparator; decision
rule for what "carries information" written before results are viewed. Exit
evidence: per-horizon baseline report showing direction, magnitude,
uncertainty, and out-of-sample skill (or documented absence of skill — a
valid stop signal). Non-goals: no nonlinear/complex models; no feature
selection beyond pre-registered incremental tests.

34. Phase 5 — Conditional distribution and threshold probabilities

Goal: S9 capability `E[dP|X_t]` plus query-supplied `P(dP>theta|X_t)`. Work:
residual-based variance / prediction intervals per horizon, `P(dP>0)`,
arbitrary-theta queries (threshold arrives with the query, never embedded),
calibration checks of probabilistic outputs. Exit evidence: calibration
report per horizon showing predicted vs realized frequencies (or a
documented refusal to use probabilities at horizons where calibration
fails). Non-goals: no target/invalidation economics; no trading thresholds.

35. Phase 6 — Target and invalidation probabilities at fitted horizons

Goal: S10 `P(P_{t+h}>=T|X_t)` / `P(P_{t+h}<=S|X_t)` derived from Phase 4-5
fits, replacing the long-horizon scale-extrapolation for short-h questions.
Work: horizon-native probability curves over candidate T/S supplied per
query; long-horizon scenario path retained but relabeled as flow-requirement
analysis under its stated scale assumption (not a short-h prediction). Exit
evidence: per-horizon target/invalidation tables with bands, alongside a
documented comparison against the legacy extrapolation (agreement and
disagreement both reported). Non-goals: no optimality claim (reward/loss/
cost/execution stay downstream).

36. Phase 7 — Formal hypothesis testing

Goal: S11 evidence objects (effect, SE, CI, p-value or model-specific
equivalent, n, out-of-sample) for directional hypotheses including
`H0: E[dP|X]<=0`. Work: pre-registered hypotheses, per-horizon tests,
multiplicity accounting for the number of patterns tried, separation of
statistical from economic/operational significance. Exit evidence:
hypothesis ledger where every claimed rejection names its test, sample,
split, and multiplicity adjustment. Non-goals: no automatic trade mapping;
`p<0.05` never becomes an execution predicate.

37. Phase 8 — Events and walls as conditioning states

Goal: S12/S13 typed microstructure events (`Absorption`, wall lifecycle with
the 10 behavioral fields) usable as `E[dP|event]` conditions. Work: event
detectors with persistence/adds/cancels/replenishment/flow-interaction/
response semantics; wall object capturing price/side/size/distance/lifetime/
adds/cancels/executions/replenishment/aggressive-flow interaction/price
response; institutional-attribution prohibition retained (identification
requires an independent mechanism). Exit evidence: event-labeled history
with inter-annotator or replay agreement stats plus per-event conditional
response estimates from Phase 4 machinery. Non-goals: no real-time alerting
contract; detection quality gates must pass before any live use is designed.

38. Phase 9 — Historical population and horizon decay

Goal: S14/S15 answered empirically: does the live `X_t` belong to a
historical population with estimable conditional behavior, and at which
horizons does information survive? Work: versioned observation store
semantics (what constitutes membership in a conditioning population),
regime-aware splits, per-horizon skill-decay curves with confidence bands.
Exit evidence: decay report naming which features work at which horizons
(including null results), and the finalized horizon set with
feed-resolution justification. Non-goals: no retention or storage design
prescribed; population definition is statistical, not architectural.

39. Phase 10 — Unified analytical evidence object

Goal: the S16 object assembled from proven parts only. Work: compose feature
vector + version, horizon, expected dP, uncertainty/variance/SE, intervals,
`P(positive)`, `P(target)`/`P(invalidation)`, hypothesis + effect +
evidence + n, model/version, out-of-sample info, and relevant events —
every field backed by its phase's exit evidence; any unproven field stays
null (never zero, never imputed). Exit evidence: schema-agnostic field
specification with nullability rules and worked examples from replay
fixtures. Non-goals: no storage/API/orchestration/state-mutation
specification (explicitly deferred).

40. Phase 11 — Statistical-discipline hardening and stop/go review

Goal: S17 fully discharged before any integration design begins. Work:
leakage audit, time-ordered methodology lock, horizon-separated and
regime-aware evaluation lock, baseline-comparison lock,
cost/execution-effects statement template, calibration lock,
multiplicity-control lock, version-pinning of every feature/model. Exit
evidence: a discipline checklist signed against replayed artifacts plus a
program-level stop/go memo (continue to integration design, redirect, or
halt on absence of signal). Non-goals: no integration design inside this
phase.

41. Phase 12 — Integration design (opens only after Phase 11)

Goal: fit the proven theoretical chain into the existing system without
premature constraint. Entry: Phase 11 stop/go = go, with named proven
objects only. Work (owned entirely by the development workforce at that
time): storage, API, orchestration, Redis/PG representation, worker
topology, trigger and cadence design, execution-plane separation. This
document contributes only the constraint that integration must preserve
every phase's refusal, gating, versioning, and no-merge rules. Non-goals of
this document: prescribing any of those designs now.

42. Working agreement

Parts A and B are the governing specification; Part C is the sequencing
contract. A phase is done when its exit evidence exists and reproduces, not
when code is merged. Skipped phases require a written reason naming which
later claim is thereby weakened. The chain OBSERVATION -> MEASUREMENT ->
EVENT -> FEATURE VECTOR -> CONDITIONAL MODEL -> EXPECTED dP + UNCERTAINTY ->
TARGET/INVALIDATION PROBABILITIES -> HYPOTHESIS TEST -> STATISTICAL EVIDENCE
remains the acceptance spine: any integration that cannot walk it
end-to-end on replayed fixtures has not met the doctrine, regardless of
architectural elegance.
