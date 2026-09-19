# Deterministic Inference Surface Plan

## Purpose

This document defines the deterministic surface that must change so the current statistical model matches the requirements described in the forward-inference review:

- long-horizon calculations must be labelled as linear accumulation scenarios;
- short-horizon forecasts must be native/event-level forecasts;
- Gaussian probabilities must be treated as model-implied and must not become production probabilities without calibration evidence;
- Route A and Route B must remain separate and preserve disagreement;
- missing features must remain missing and must participate in model identity;
- fitting sufficiency must be separated from OOS validation sufficiency;
- the inference plane must receive one canonical deterministic forecast object rather than a collection of semantically mixed raw tool outputs.

This is an implementation plan and contract assessment. It does not introduce a concavity model, select a winning model, or ask the LLM to repair statistical deficiencies.

The priority here is the **actual deterministic computation and authority surface**. Tests are secondary. Tests should be added after the deterministic contracts are settled, but passing tests alone must not be used as evidence that the contracts are correct.

---

## 1. Current repository state

### 1.1 Where the current statistical model lives

The main statistical implementation is:

```text
market_service/microstructure/fitting.py
```

The current model stack contains:

```text
fit_price_impact()
fit_depth_scaling()
derive_price_delta()
evaluate_scenario()
build_feature_vector()
build_forward_observations()
fit_forward_ols()
predict_distribution()
calibration_report()
evaluate_forward_scenario()
assemble_evidence_v2()
```

The core contracts are in:

```text
market_service/microstructure/contracts.py
```

The dispatch and inference-plane wrappers are in:

```text
market_service/nooa_harness/inference/dispatch.py
market_service/nooa_harness/inference/capability.py
market_service/nooa_harness/engine/
```

### 1.2 Current uncommitted work

The current uncommitted work is principally in the inference engine and dispatch routing:

```text
market_service/nooa_harness/engine/
market_service/nooa_harness/inference/dispatch.py
market_service/runtime/postgres_store.py
```

There is currently no uncommitted change under:

```text
market_service/microstructure/
```

Therefore, the statistical contract changes described in this document still need to be made in the deterministic microstructure layer before the inference-plane integration can be considered complete.

### 1.3 Current good foundations

The following foundations should be retained:

- Decimal arithmetic with high precision;
- deterministic replay from persisted events;
- canonical input hashes;
- explicit model versions;
- separate `PriceImpactFit` and `DepthScalingFit` objects;
- refusal on insufficient legacy fits;
- no zero-filling at feature construction;
- explicit tick-size handling in the pure fitting functions;
- event-grain forward observations for short horizons;
- an agent that is intended to read deterministic evidence rather than recompute it.

These foundations are useful, but they do not by themselves prove that the forward validation and probability semantics are correct.

---

## 2. The required target architecture

The deterministic inference path must become:

```text
raw event tape
    ↓
validated replay and operational-quality state
    ↓
versioned feature construction
    ↓
versioned forward target construction
    ↓
train-only model fit
    ↓
time-ordered OOS evaluation
    ↓
probability calibration evaluation
    ↓
Route A / Route B diagnostics
    ↓
forecast-regime classification
    ↓
canonical ForecastResult
    ↓
hard deterministic validation gate
    ↓
inference agent reads and explains the result
```

The agent must not:

- decide whether the linear model or another model is better;
- convert an uncalibrated Gaussian number into a production probability;
- treat a long-horizon scenario as a native future-price forecast;
- infer a missing feature as zero;
- merge Route A and Route B into an unlabelled average;
- promote a fitted model to validated status merely because coefficients exist.

---

## 3. Forecast regimes that must become explicit

### 3.1 Native forecast regime

For a supported horizon below 15 minutes, the system must use a forward target built directly from future event-grain prices:

\[
Y_t(h) = P_{t+h} - P_t
\]

The current supported native horizons are:

```text
1 second
5 seconds
30 seconds
60 seconds
```

These are the horizons currently represented by `FORWARD_HORIZONS_MS`.

If a caller requests a horizon below 15 minutes that is not in the fitted native horizon set, the system must refuse with an explicit unsupported-horizon result. It must not silently fall back to the long-horizon scenario model.

Required metadata:

```json
{
  "forecast_type": "native_forecast",
  "horizon_regime": "native",
  "horizon_ms": 5000,
  "assumptions": {
    "linear_impact_scaling": false,
    "forward_target_directly_observed": true
  }
}
```

### 3.2 Long-horizon scenario regime

For 15 minutes, 1 hour, and 4 hours, the existing required-flow scenario remains available:

\[
\Delta_{req} = n\alpha + \beta OFI_{total}
\]

\[
OFI_{req} = \frac{\Delta_{req} - n\alpha}{\beta}
\]

This must be explicitly classified as:

```text
forecast_type = scenario_extrapolation
horizon_regime = long
```

Required metadata:

```json
{
  "forecast_type": "scenario_extrapolation",
  "horizon_regime": "long",
  "horizon": "1h",
  "assumptions": {
    "linear_impact_scaling": true,
    "scale_invariance_proven": false,
    "native_forward_target_used": false
  },
  "probability_semantics": "empirical_ofi_exceedance"
}
```

The result is not a structural law and must not be represented as:

```text
P(price reaches target | current state)
```

It is:

```text
fraction of observed horizon-length OFI sums that reach the required flow
under a linear accumulation scenario
```

No `gamma` or concavity parameter is to be added during this implementation pass.

---

## 4. Deterministic contract changes required

## 4.1 `FeatureVector` must expose explicit schema identity

Current contract:

```python
FeatureVector(
    symbol,
    venue,
    ts_ms,
    vector_version,
    fields,
    def_versions,
    quality,
    input_hash,
)
```

Required additions:

```python
feature_keys: tuple[str, ...]
feature_schema_hash: str
```

The schema must be deterministic and ordered. For example:

```text
("ad_10s", "dmu", "ofi_10s", "obi_top", "skew_bps", "spread_bps")
```

The schema hash must include:

- vector version;
- ordered feature keys;
- each feature definition version;
- symbol and venue;
- null/quality policy version.

The feature vector must continue to omit unavailable fields. It must never convert missing values to zero.

`FeatureVector.from_dict()` must validate or explicitly preserve the schema hash. It must not simply accept a mismatched `fields` / `def_versions` pair.

### Required file

```text
market_service/microstructure/contracts.py
```

### Required pure construction changes

```text
market_service/microstructure/fitting.py
```

Functions:

```python
feature_input_hash()
build_feature_vector()
```

`build_feature_vector()` must produce an explicit schema representation every time.

---

## 4.2 `ForwardFit` must identify the model schema and validation state

Current `ForwardFit` contains coefficients and a single status. That is insufficient.

Required additions:

```python
feature_keys: tuple[str, ...]
feature_schema_hash: str
feature_definition_versions: dict[str, str]
training_start_ts_ms: int | None
training_end_ts_ms: int | None
oos_start_ts_ms: int | None
oos_end_ts_ms: int | None
n_train: int
n_oos: int
split_method: str
train_r2: str | None
oos_r2: str | None
oos_mae: str | None
oos_rmse: str | None
baseline_oos_r2: str | None
baseline_oos_mae: str | None
estimation_status: str
validation_status: str
probability_status: str
```

The existing `status` field may be retained for compatibility, but it must become a projection of the more explicit fields rather than the only source of truth.

Recommended meanings:

```text
estimation_status:
    insufficient
    fitted

validation_status:
    unvalidated
    provisional
    validated

probability_status:
    not_requested
    model_implied
    validated
    refused
```

A coefficient fit is not automatically a validated forecast.

---

## 4.3 Missing-feature prediction must refuse incompatible models

Current prediction behavior iterates over fields present in the live vector. This means that a trained feature missing from the live vector can be silently omitted.

That is not acceptable.

The model must distinguish these cases:

### Valid prediction

```text
live schema == fit schema
all required features present
all definition versions match
```

### Refused prediction

```text
live vector is missing a feature required by the fit
```

### Different model

```text
live vector has a different explicit feature schema
```

A model fitted on:

```text
OFI + AD + OBI
```

is not automatically the same model as:

```text
OFI + AD + OBI + CVD
```

The fit identity must include the schema. The prediction function must check the schema before calculating `expected_ticks`.

Required function change:

```python
predict_distribution()
```

It should return a structured refusal or raise a deterministic compatibility error. It must not treat the missing feature as a zero contribution.

---

## 4.4 Forward feature construction must match the documented definitions

The documented `xt-v2` meanings are:

```text
ofi_10s = closed 10-second OFI interval value
ad_10s = closed 10-second average-depth value
dmu = current-event microprice displacement
spread_bps = current-event spread
obi_top = current-event top-of-book imbalance
cvd_slope_60s = trailing trade-flow feature
skew_bps = current-event microprice skew
```

The current dispatcher constructs forward vectors from individual event contributions:

```python
ofi=e.contribution
average_depth=None
```

That does not match the documented meaning of `ofi_10s` and `ad_10s`.

Required deterministic association:

```text
event timestamp t
    → current event quote features
    → enclosing OFI interval
    → that interval's ofi_10s and ad_10s
```

This association must be implemented in the deterministic feature/observation construction layer, not improvised by the agent or dispatcher.

The dispatcher should call a single pure builder over a replayed, typed input object rather than manually assembling semantically incomplete vectors.

---

## 4.5 Forward target construction must reject gap-spanning observations

Current `build_forward_observations()` handles venue mismatch and end-of-window cases, but it does not have enough operational-quality input to identify every gap or halt between `t` and `t+h`.

Required forward target inputs should include quality spans or an equivalent replay-quality object:

```python
ForwardQualitySpan(
    start_ts_ms,
    end_ts_ms,
    quality,
    reason,
)
```

A target must be null when its interval crosses:

- a book sequence gap;
- a reconnect gap;
- a halt or unavailable state;
- a stale-price span;
- an invalid quote span.

Required output:

```json
{
  "y_ticks": null,
  "excluded": {
    "5000": "sequence_gap"
  }
}
```

The system must never bridge a gap by taking the next available price and pretending it is the true `t+h` observation.

Required function change:

```python
build_forward_observations()
```

Required supporting changes may be made in:

```text
market_service/microstructure/contracts.py
market_service/microstructure/orderbook.py
market_service/microstructure/ofi.py
```

The target builder must remain pure and replayable.

---

## 5. Genuine OOS fitting surface

## 5.1 Current problem

The current `fit_forward_ols()` calculates coefficients using the complete usable dataset and then evaluates the last 30% using those full-sample coefficients.

That is not genuinely OOS because the test observations influenced the coefficients used to score the test.

Current conceptual behavior:

```text
fit beta on train + test
score beta on test
```

Required behavior:

```text
split ordered observations
fit beta on train only
predict test using train beta
score test predictions
```

Mathematically:

\[
\hat\beta_{train} = OLS(X_{train},Y_{train})
\]

\[
\widehat{Y}_{test}=X_{test}\hat\beta_{train}
\]

\[
R^2_{OOS}=1-
\frac{\sum(Y_{test}-\widehat{Y}_{test})^2}
{\sum(Y_{test}-\bar{Y}_{train})^2}
\]

The same split must be used for the univariate OFI comparator.

## 5.2 Required pure functions

A small deterministic validation module should be introduced rather than keeping every concern in one large function.

Preferred new file:

```text
market_service/microstructure/validation.py
```

Pure functions:

```python
time_order_split()
fit_train_design()
predict_design()
evaluate_oos_predictions()
compare_oos_baseline()
```

The exact names may differ, but the responsibilities must remain separate.

`fit_forward_ols()` should orchestrate these functions and place all provenance and metrics into `ForwardFit`.

## 5.3 Validation status rule

The deterministic plane must not use only in-sample `r2` to promote a model.

The promotion decision must consider at least:

```text
fit exists
training sample sufficient
OOS sample sufficient
OOS metric available
baseline comparison available
feature schema valid
no target leakage
```

A model can have:

```text
estimation_status = fitted
validation_status = unvalidated
```

That is a valid result and must be preserved.

---

## 6. Probability and calibration surface

## 6.1 Mean and probability are separate outputs

The forward model calculates:

\[
\mu = \hat E[Y\mid X]
\]

and a residual scale:

\[
\sigma = \hat\sigma(Y\mid X)
\]

A Gaussian-implied probability is then:

\[
P(Y>\theta\mid X)=
\Phi\left(\frac{\mu-\theta}{\sigma}\right)
\]

This is allowed to exist as a model calculation, but it is not automatically a production probability.

The output must distinguish:

```text
expected delta
model-implied probability
validated probability
```

## 6.2 Current calibration implementation is not sufficient

The current `calibration_report()` compares predicted and realized conditional means in quantile bins. That is not a complete reliability report for `P(Y>0)` or `P(Y>theta)`.

A probability calibration report must operate on pairs like:

```text
forecast probability p_i
realized event z_i = 1[Y_i > theta]
```

and report per-bin:

```text
n
mean predicted probability
observed event frequency
absolute calibration error
```

It must be built from OOS predictions, not predictions produced by a model fitted on the same rows.

## 6.3 Required pure calibration functions

Preferred new file:

```text
market_service/microstructure/validation.py
```

Required responsibilities:

```python
build_oos_probability_predictions()
probability_reliability_report()
calibration_gate()
```

The calibration gate must produce a deterministic result such as:

```json
{
  "status": "passed|failed|insufficient",
  "n_oos": 180,
  "n_bins": 5,
  "bins": [...],
  "reason": null
}
```

The calibration rule must be chosen and frozen before looking at production results. It must not be changed by the agent after seeing the data.

## 6.4 Probability refusal rule

`predict_distribution()` may return mean and interval fields when a fit is usable.

If calibration has not passed, it must return:

```json
{
  "p_positive": null,
  "p_above_theta": null,
  "probability_status": "refused",
  "probability_reason": "calibration_not_passed"
}
```

`evaluate_forward_scenario()` must not default to:

```python
calibrated=True
```

The caller must provide deterministic calibration evidence, or the function must refuse/null probabilities.

The dispatcher must run or load calibration evidence. It must not assume calibration by default.

No Student-t, bootstrap, or other alternate distribution should be introduced during this phase.

---

## 7. Route A and Route B surface

## 7.1 Route A

Route A remains:

\[
\widehat{\Delta P}_A=\alpha+\beta OFI
\]

Its output should explicitly state what its interval means.

The current `band_95_ticks` is based on slope uncertainty:

\[
1.96\cdot SE_\beta\cdot |OFI|
\]

That is not automatically a full predictive interval containing residual uncertainty, intercept uncertainty, and estimation uncertainty.

Required output metadata:

```json
{
  "uncertainty_type": "slope_contribution_band",
  "prediction_interval": false
}
```

If a full predictive interval is later added, it must have a different field and definition.

## 7.2 Route B

Route B remains:

\[
\widehat{\beta}(AD)=cAD^{-\lambda}
\]

\[
\widehat{\Delta P}_B=\alpha+\widehat{\beta}(AD)OFI
\]

Route B must remain separate from Route A. No averaging is permitted.

Route B must report:

```text
fit status
n_blocks
AD used
beta_implied
c
lambda
uncertainty status
```

Until second-stage uncertainty propagation exists, the output must say:

```text
uncertainty_status = not_propagated
```

## 7.3 Agreement

The existing `agreement_ticks` value is valuable:

\[
AgreementTicks
=
|\widehat{\Delta P}_A-
\widehat{\Delta P}_B|
\]

The common output should preserve it and eventually add:

```text
absolute_difference_ticks
relative_difference
status
```

A threshold must not be invented yet. Agreement is a diagnostic, not a model-selection decision.

Route A versus the forward multivariate forecast should normally be marked `not_comparable` unless the horizon, target, units, and information set match.

---

## 8. Long-horizon scenario surface

Required change to:

```text
market_service/microstructure/fitting.py
evaluate_scenario()
```

The calculation itself may remain unchanged for now. The output contract must become structured.

Required fields:

```json
{
  "forecast_type": "scenario_extrapolation",
  "horizon_regime": "long",
  "probability_semantics": "empirical_ofi_exceedance",
  "assumptions": {
    "linear_impact_scaling": true,
    "scale_invariance_proven": false
  },
  "native_forward_comparison": {
    "status": "not_available|available|not_comparable"
  }
}
```

The existing `scale_assumption` string may remain for compatibility, but the structured fields must be authoritative.

Required change to the interaction/inference layer:

The engine must not present `exceedance` as an unqualified probability. The correct wording is:

```text
empirical OFI exceedance under the linear accumulation scenario
```

The scenario verdict may still answer whether the required-flow regime was observed, but it must not be labelled as a validated future-price probability.

---

## 9. Canonical `ForecastResult`

The inference plane needs one deterministic object that collapses the proven evidence without collapsing distinct estimands.

Preferred location:

```text
market_service/microstructure/forecast.py
```

or, if a new module is not desired, a dedicated section with an explicit composer in `fitting.py`.

The preferred approach is a separate module so model fitting and evidence composition remain distinct.

Conceptual shape:

```json
{
  "schema_version": "forecast-result-v1",
  "symbol": "BTCUSDT",
  "venue": "spot",
  "generated_at_ms": 0,
  "horizon_ms": 5000,
  "horizon_regime": "native",
  "forecast_type": "native_forecast",

  "information_set": {
    "vector_version": "xt-v2",
    "feature_schema_hash": "...",
    "fields": ["ofi_10s", "dmu", "spread_bps"],
    "quality": "exact_feed"
  },

  "route_a": {
    "status": "diagnostic_only",
    "expected_ticks": "...",
    "interval_lo_95": null,
    "interval_hi_95": null,
    "uncertainty_type": "slope_contribution_band",
    "fit_id": "..."
  },

  "route_b": {
    "status": "unavailable",
    "reason": "insufficient_depth_blocks",
    "expected_ticks": null,
    "uncertainty_status": "not_available"
  },

  "multivariate": {
    "status": "provisional",
    "expected_ticks": "...",
    "variance_ticks": "...",
    "interval_lo_95": "...",
    "interval_hi_95": "...",
    "p_positive": null,
    "probability_status": "refused",
    "probability_reason": "calibration_not_passed",
    "fit_id": "..."
  },

  "agreement": {
    "route_a_vs_route_b": {
      "status": "diagnostic",
      "absolute_difference_ticks": "...",
      "relative_difference": null
    },
    "route_a_vs_multivariate": {
      "status": "not_comparable",
      "reason": "different_estimands"
    }
  },

  "diagnostics": {
    "heteroskedasticity": false,
    "n_train": 0,
    "n_oos": 0,
    "oos_skill": null,
    "baseline_oos_skill": null,
    "calibration_status": "not_run",
    "gap_exclusions": 0
  },

  "assumptions": {
    "gaussian_probability": true,
    "linear_impact_scaling": false
  },

  "validation_state": "provisional",
  "model_version": "forward-ols-v2",
  "input_hash": "..."
}
```

This object must be deterministic and composed by Python. The LLM should receive it as evidence and never create it from separate raw values.

For a long-horizon scenario, the object should instead contain:

```json
{
  "forecast_type": "scenario_extrapolation",
  "horizon_regime": "long",
  "forward_native_forecast": null,
  "linear_scenario": {
    "required_ofi": "...",
    "empirical_ofi_exceedance": "...",
    "status": "provisional"
  },
  "assumptions": {
    "linear_impact_scaling": true
  }
}
```

---

## 10. Deterministic dispatcher surface

The current dispatcher exposes many individual tools. Those diagnostic tools may remain, but the inference plane needs one canonical deterministic orchestration path.

Preferred new capability:

```text
calc.forward.forecast
```

or:

```text
calc.statistical.forecast
```

This capability should:

1. resolve tick size from the frozen registry;
2. read and replay the event tape;
3. build validated feature vectors;
4. build forward observations;
5. fit the requested native horizon using a train-only fit;
6. evaluate OOS predictions;
7. evaluate calibration if probability output is requested;
8. calculate Route A when the inputs are comparable;
9. calculate Route B when depth history is sufficient;
10. preserve Route A/B disagreement;
11. classify native versus scenario regime;
12. compose one `ForecastResult`;
13. return structured refusal reasons rather than partial semantic guesses.

The existing low-level tools can remain useful for diagnostics:

```text
calc.feature.build
calc.forward.join
calc.forward.fit
calc.forward.distribution
calc.forward.scenario
calc.price.delta
calc.scenario.evaluate
calc.decay.report
calc.discipline.audit
```

However, the engine should not require the agent to call these independently and then infer their relationships. That relationship belongs in the deterministic orchestrator.

---

## 11. Runtime gate changes

The current engine gate is primarily based on the legacy price-impact fit. That is insufficient for the forward plane.

The runtime needs capability-specific gates.

### Native forecast gate

A native forecast may expose a mean and residual interval only when:

```text
fit status is fitted/provisional/validated according to policy
feature schema matches
forward target is valid
no gap-spanning target was used
```

### Probability gate

A probability may be non-null only when:

```text
native fit exists
OOS probability predictions exist
calibration gate passed
```

Otherwise:

```text
p_positive = null
p_target = null
p_invalidation = null
probability_status = refused
```

### Long-horizon scenario gate

A long-horizon scenario may expose:

```text
required_ofi
empirical_ofi_exceedance
linear scaling assumption
```

It must not be promoted to a native forecast or validated probability.

### Global artifact status

The existing top-level artifact status can remain for compatibility, but the deterministic state must include the more precise statistical statuses. The engine must not use a legacy `validated` status as proof that the forward probability is calibrated.

---

## 12. Exact implementation order

This is the order that should be followed to complete the inference plane without introducing semantic shortcuts.

### Step 1 — Add the deterministic contract types

Modify:

```text
market_service/microstructure/contracts.py
```

Add or extend:

```text
FeatureVector schema identity
ForwardFit validation metadata
OOS evaluation record
CalibrationReport
ForecastResult
```

Do not yet change the agent or prompts.

### Step 2 — Correct feature and target construction

Modify:

```text
market_service/microstructure/fitting.py
market_service/microstructure/contracts.py
```

Add or refactor:

```text
feature schema construction
interval-to-event feature association
forward quality spans
gap and halt exclusion reasons
```

This step establishes what the data actually means.

### Step 3 — Refactor the forward fit into train and OOS stages

Modify:

```text
market_service/microstructure/fitting.py
```

Prefer a pure validation helper module:

```text
market_service/microstructure/validation.py
```

The fit must be trained only on the training segment. The last segment must be scored using fixed training coefficients.

The comparator must use the same split.

### Step 4 — Add explicit model-schema compatibility

Modify:

```text
market_service/microstructure/contracts.py
market_service/microstructure/fitting.py
```

Prediction must refuse mismatched or incomplete schemas.

### Step 5 — Implement real OOS calibration

Modify or add:

```text
market_service/microstructure/validation.py
market_service/microstructure/fitting.py
```

The Gaussian formula remains. Only the promotion of its probability output changes.

### Step 6 — Add structured horizon and assumption metadata

Modify:

```text
market_service/microstructure/fitting.py
market_service/microstructure/contracts.py
```

`evaluate_scenario()` must produce structured long-horizon metadata. Native forward functions must produce structured native metadata.

### Step 7 — Formalize Route A/B diagnostics

Modify:

```text
market_service/microstructure/fitting.py
```

Preserve both routes, add uncertainty-status fields, and preserve agreement without averaging.

### Step 8 — Compose `ForecastResult`

Add:

```text
market_service/microstructure/forecast.py
```

This becomes the deterministic authority object for the inference plane.

### Step 9 — Add one canonical dispatch path

Modify:

```text
market_service/nooa_harness/inference/dispatch.py
market_service/nooa_harness/inference/capability.py
```

The dispatcher should invoke deterministic composition, not manually rebuild model objects and pass partial results between tools.

### Step 10 — Connect the engine to the canonical object

Modify:

```text
market_service/nooa_harness/engine/core/gather.py
market_service/nooa_harness/engine/core/reasoning.py
market_service/nooa_harness/engine/core/output.py
market_service/nooa_harness/engine/controller.py
```

The engine should:

- acquire the deterministic forecast object;
- gate it before interpretation;
- persist it under deterministic state;
- allow the agent to cite and explain it;
- prevent the agent from promoting null or refused probability fields.

### Step 11 — Update the output contract

Only after the deterministic object is stable, update:

```text
market_service/nooa_harness/engine/interpreter.py
market_service/nooa_harness/engine/narration.py
market_service/nooa_harness/engine/kb.py
```

The output contract must require the agent to preserve:

```text
forecast_type
horizon_regime
probability_status
validation_state
feature_schema
assumptions
route disagreement
```

The agent should not be asked to reconstruct these semantics from prose.

### Step 12 — Add offline model comparison later

Only after the linear path is correctly scored OOS should a validation-only comparison be added:

```text
linear model
versus
candidate concave model
```

No concavity parameter belongs in the runtime before that comparison exists.

---

## 13. What must not happen during this implementation

The following changes must not be made as shortcuts:

1. Do not add `gamma` merely because concavity is theoretically plausible.
2. Do not mark a probability valid because the Gaussian formula returned a number.
3. Do not use the agent to decide which route is correct.
4. Do not average Route A and Route B.
5. Do not use the existing `calibration_report()` as proof of probability calibration without changing its estimand and OOS population.
6. Do not use full-sample coefficients to calculate OOS skill.
7. Do not zero-fill missing features.
8. Do not silently omit a feature at prediction time when the fit requires it.
9. Do not let a long-horizon scenario masquerade as a native forecast.
10. Do not let the dispatcher default unknown tick sizes to `0.01`.
11. Do not make the engine call multiple raw tools and then infer their relationship in the prompt.
12. Do not let an overall legacy `validated` status promote a forward probability that has not passed its own gates.

---

## 14. Completion definition for the deterministic inference plane

The inference plane can be considered complete for this scope when all of the following are true:

### Data semantics

- feature definitions are versioned;
- feature schema is part of model identity;
- missing values remain missing;
- forward targets cannot bridge quality gaps;
- `ofi_10s` and `ad_10s` have their documented meanings.

### Model semantics

- Route A, Route B, and forward OLS remain separate;
- Route A/B disagreement is preserved;
- genuine OOS coefficients and metrics are used;
- training and OOS populations are explicit;
- no model is called validated merely because it fitted.

### Probability semantics

- Gaussian probabilities are labelled model-implied;
- OOS reliability is measured;
- calibration failure nulls probabilities;
- no alternate distribution is introduced by the agent.

### Horizon semantics

- short supported horizons are native forward forecasts;
- 15m/1h/4h results are scenario extrapolations;
- linear accumulation is a structured assumption;
- long-horizon exceedance is not an unqualified price probability.

### Runtime semantics

- one canonical deterministic `ForecastResult` is produced;
- the inference engine consumes that object;
- the agent only interprets it;
- refused fields remain null;
- route and probability statuses survive persistence and output composition.

### Validation semantics

- the deterministic plane can report insufficient, fitted, provisional, and validated states distinctly;
- model selection is performed in validation, not in runtime interpretation;
- the current linear model is evaluated before any concavity model is introduced.

---

## 15. Immediate recommended next action

The first implementation pass should not touch prompts or agent wording.

It should implement the deterministic foundation in this order:

```text
1. ForwardFit and FeatureVector schema contracts
2. Correct event/interval feature construction
3. Gap-aware forward target construction
4. Train-only fit and genuine OOS evaluation
5. OOS probability calibration gate
6. Structured horizon/assumption metadata
7. Canonical ForecastResult composer
8. One deterministic dispatcher entry point
9. Engine gate and persistence integration
10. Agent/output contract update
```

After these steps, the inference plane will have a deterministic authority surface that can be completed and consumed by the engine without requiring the agent to perform statistical interpretation or model selection.

---

## 16. Implementation status — current pass

The first implementation pass has now been started in the existing monolithic modules, as intentionally chosen for speed before later decomposition.

Implemented in the current working tree:

- `FeatureVector` now carries explicit `feature_keys` and `feature_schema_hash`.
- `ForwardFit` now carries schema identity, train/OOS counts, OOS metrics, validation status, probability status, and train-only OOS coefficients.
- `FEATURE_VECTOR_VERSION` is now `xt-v2`.
- `FORWARD_MODEL_VERSION` is now `forward-ols-v2`.
- `fit_forward_ols()` now evaluates OOS performance using coefficients fitted only on the time-ordered training segment.
- The univariate OFI comparator uses the same OOS split.
- `predict_distribution()` refuses probabilities unless explicit calibration has passed and the forecast itself is OOS-validated.
- `calibration_report()` now evaluates held-out probability reliability rather than only in-sample conditional-mean bins.
- Forward vectors in the dispatcher now attach closed interval OFI/AD values to event-grain quote features instead of using one event contribution as `ofi_10s`.
- Forward replay can refuse observations spanning persisted sequence-gap or degraded-quality spans.
- Long-horizon scenarios now expose structured `forecast_type`, `horizon_regime`, `probability_semantics`, and linear-scaling assumptions.
- Route A and Route B remain separate, preserve disagreement, and mark Route B uncertainty as not propagated.
- A deterministic `ForecastResult` contract and `calc.forward.forecast` canonical dispatcher have been added.
- The engine now acquires the canonical forward result before interpretation and persists its model version/input hash when available.
- Dispatcher tick handling now resolves against the frozen symbol/venue registry instead of defaulting unknown instruments to `0.01`.
- The agent-facing guidance now treats the canonical forecast object as primary and the lower-level statistical tools as diagnostic sub-steps.

Remaining deterministic work before decomposition:

1. refresh the engine's final-output schema so every final answer cites `ForecastResult` fields directly;
2. make long-horizon scenario results flow through the same canonical result path in every engine branch;
3. expose calibration reports and refusal reasons in the persisted evidence projection, not only in the tool result;
4. make the top-level artifact status distinguish legacy-fit status from forward validation/probability status without losing compatibility;
5. perform a final end-to-end inspection of the monolithic surface, then split fitting, validation, forecasting, and dispatch into independent planes.

The current failing characterization tests are stale against the new deterministic authority shape: they encode the previous multi-tool traversal and previous in-sample probability semantics. They should be refreshed only after the deterministic contract is accepted, rather than used to drive the statistical design.
