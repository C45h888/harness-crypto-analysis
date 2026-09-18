# Track D (D1–D5) Technical Spec — Deterministic Base Only

Source of truth: `docs/agenitc-theory.md` Part A (§§4–9) + Part C (§§29–34).
Scope: `market_service/microstructure/` pure math + frozen types + replay tests.
Non-goals (explicit): no `dispatch.py` / `engine/` / `controller.py` / `fsm.py` / Redis / PG / API / worker changes in this spec. Those are Phase-12.

Status: **SPEC ONLY — zero code diff**. Code opens per-vector after spec approval.

---

## 0. Current base (frozen, must keep replaying)

| File | Owns today | Invariants |
|---|---|---|
| `contracts.py` | `BestQuoteState`, `DepthDelta`, `OrderBookEvent{previous,current,contribution}`, `OFIInterval{ofi,average_depth,mid_start,mid_end,depth_estimator}`, `PriceImpactObservation` (same-window ΔP_k), `PriceImpactFit`, `DepthScalingFit`, `MicrostructureEvidence`, `FIT_MODEL_VERSION="ofi-depth-v1"`, `MIN_OBSERVATIONS=30` route via fitting | `BookGapError` never bridged; crossed/locked refuses via `validate()`; NULL = not-provided |
| `ofi.py` | `event_contribution()` (Cont e_n), `OFIAggregator` (10s close, `DEPTH_ESTIMATOR="event_mean_best_bid_ask_v1"`), `_mid()` private | hash embeds estimator label; changing def invalidates fits |
| `orderbook.py` | `OrderBookReconstructor` + `pu`-chain (futures) / contiguous (spot) | gap → raise, caller marks `insufficient` |
| `fitting.py` | SOLE producer: `input_hash`, `replay_*`, `build_observations` (same-window), `fit_price_impact` (univariate OLS/HC0, trichotomy), `fit_depth_scaling`, `derive_price_delta`, `evaluate_scenario` (15m/1h/4h, scale-assumption labelled), `assemble_evidence` | Decimal-50; no I/O; no LLM; engine never recomputes |
| `capture.py` | tape only | no math |

External feeders (read-only, never authority): `calculations/substrates/*` (float, 5s poller), `market.read` snapshot, `read_raw_window`.

Known debt this spec must fix: `dispatch.py` hardcodes `tick_size=Decimal("0.01")` at 3 sites — D3/D4 force it to an explicit per-symbol/venue parameter (spec the contract here, apply there only in Phase-12).

---

## D1 — Microprice displacement `D_μ,t` (theory §4, plan §30)

**Why first:** every `OrderBookEvent` already carries both quotes; zero capture change; unlocks first validated measurement.

### D1.1 Frozen definitions (new `microstructure/microprice.py`, `ofi.py` untouched)

```python
MICROPRICE_ESTIMATOR = "microprice-v1"
PRECISION = 50  # reuse fitting.PRECISION

def mid(q: BestQuoteState) -> Decimal               # (bid+ask)/2
def microprice(q: BestQuoteState) -> Decimal | None # (ask*qBid + bid*qAsk)/(qBid+qAsk); None if qBid+qAsk==0
def displacement(q: BestQuoteState) -> Decimal | None  # P_mu - M; None on zero-queue
def displacement_bps(q) -> Decimal | None           # D_mu / M * 1e4
```

Edge rules (frozen): `BestQuoteState.validate()` already refuses crossed/locked (`bid>=ask` raises) — `displacement` therefore never sees a crossed book via constructor path; dict-replay path that bypasses validation returns NULL. Zero-queue (`qBid+qAsk==0`) → NULL, never 0. Tick representation preserved (quote diff, no float).

### D1.2 Change surface

| File | Change | Risk |
|---|---|---|
| `microstructure/microprice.py` | NEW, pure, no imports from engine/dispatch/capture | none — additive |
| `contracts.py` | ADD optional `mid`, `microprice`, `displacement` fields on a NEW `QuoteMeasurement` dataclass (do NOT widen `BestQuoteState` — keeps old hashes stable); `to_dict/from_dict` round-trip | low — additive optional fields |
| `ofi.py`, `orderbook.py`, `capture.py` | NO CHANGE | zero regression surface |
| `fitting.py` | NO CHANGE (no fit on `D_mu` yet) | — |
| tests `tests/test_microstructure.py` | ADD `test_microprice_*`: known quotes→known D_mu; zero-queue NULL; crossed refuses; replay old fixtures → identical OFI/AD + new series present | — |

**Effect on system:** none observable downstream (no dispatch/engine read of new fields in Track D). Old `input_hash` inputs untouched — new measurements carry their own hash domain. Old fits replay bit-identical (exit gate).

**Exit (per §30):** replay fixtures → bit-identical OFI/AD; deterministic `D_mu` series with version label persisted in test vectors.

---

## D2 — Feature vector `X_t` (theory §5, plan §31)

### D2.1 Frozen contract (extends `contracts.py`)

```python
FEATURE_VECTOR_VERSION = "xt-v1"

@dataclass(frozen=True)
class FeatureVector:
    symbol: str; venue: str
    ts_ms: int                 # event exchange_ts_ms (event grain — see D3)
    vector_version: str         # == FEATURE_VECTOR_VERSION
    fields: dict[str, str]      # Decimal canonical strings, fixed-point (no exponent)
    def_versions: dict[str, str]# field -> frozen def label that produced it
    quality: str                # exact_feed | degraded | insufficient
    input_hash: str             # SHA-256 over (symbol,venue,ts,fields,def_versions,vector_version)
```

`xt-v1` field table (small, all computable now; everything else = NULL + `quality` note):

| Field | Source stream (current data) | Decimal def (frozen) | NULL when |
|---|---|---|---|
| `ofi_10s` | OFI stream (`read_microstructure_intervals`) | existing `interval.ofi`, label `event_contribution-v1` | quality≠exact_feed |
| `ad_10s` | same | existing `average_depth`, label `event_mean_best_bid_ask_v1` | None (→NULL) |
| `dmu` | event stream (`read_microstructure_events`) | D1 `displacement`, label `microprice-v1` | zero-queue / failed validate |
| `spread_bps` | recomputed from event quotes `(ask-bid)/mid*1e4` (NOT poller float) | `spread-v1` | never (validate guarantees two-sided) |
| `obi_top` | recomputed `(qBid-qAsk)/(qBid+qAsk)` from event quotes | `obi-top-v1` | zero-queue → NULL |
| `cvd_slope_60s` | raw window trades, Decimal sum buy−sell over trailing 60s | `cvd-slope-v1` | no trades → NULL (never 0.5-fill) |
| `skew_bps` | recomputed Decimal (same formula as `tape.microprice_skew_bps`, re-implemented Decimal) | `skew-v1` | empty side → NULL |

Deferred to `xt-v2` (recorded NULL in v1, never imputed): adds/cancels, replenishment, absorption flag, wall-distance (need D8 detectors).

### D2.2 Pure builder (new code in `fitting.py`, no new imports)

```python
def build_feature_vector(
    *, symbol, venue, ts_ms, ofi, average_depth,
    quote: BestQuoteState, trades_60s: list[dict] | None,
    quality: str,
) -> FeatureVector
```

Rules: Decimal-only; floats from substrates/poller NEVER enter (recompute from raw); observed-vs-interpreted split — no regime/verdict fields.

### D2.3 Change surface

| File | Change | Effect |
|---|---|---|
| `contracts.py` | ADD `FeatureVector` + `FEATURE_VECTOR_VERSION` | additive; no existing dataclass touched |
| `fitting.py` | ADD `build_feature_vector` + `feature_input_hash` | additive; existing `build_observations`/`fit_*` untouched |
| `capture.py`, `ofi.py`, `orderbook.py`, dispatch, engine | NO CHANGE | zero downstream effect in Track D |
| tests | ADD `test_feature_vector_*`: determinism (same inputs→same hash), NULL propagation, float-rejection (passing poller float raises or is ignored — spec: raise `TypeError`) | — |

**Exit (per §31):** one `xt-v1` version; replay test proves every field traces to a named def version; no model, no "best subset" claim.

---

## D3 — Forward target `Y_t(h)` (theory §6, plan §32)

**Grain decision (binding):** 10s intervals CANNOT resolve 1s/5s horizons → `X_t` stamped at **event grain** (`exchange_ts_ms`), `Y_t(h)=mid(t+h)−mid(t)` via event mid time-series (Decimal, exact). Interval grain allowed only for 30s/60s with logged rule (spec documents which grain per horizon).

### D3.1 Pure join (in `fitting.py`)

```python
FORWARD_HORIZONS_MS = (1_000, 5_000, 30_000, 60_000)

@dataclass(frozen=True)
class ForwardObservation:
    x: FeatureVector
    y_ticks: dict[int, str | None]   # horizon_ms -> Decimal str or NULL
    y_quote: dict[int, str | None]
    price_source: str                # "microstructure_mid" | "mark_fallback_degraded"
    excluded: dict[int, str]         # horizon_ms -> reason (gap | halt | end_of_window | venue_mismatch)

def build_forward_observations(
    vectors: list[FeatureVector],
    mids: list[tuple[int, Decimal]],  # (exchange_ts_ms, mid) sorted; from events
    *, tick_size: Decimal,            # EXPLICIT param (kills hardcoded "0.01")
    venue: str,
) -> tuple[list[ForwardObservation], dict[str, int]]  # (pairs, exclusion_log)
```

Rules (frozen): half-open `[t, t+h)`; price source = microstructure mid; collated `mark_price` fallback allowed ONLY with `price_source="mark_fallback_degraded"` + counted exclusion-adjacent log; gaps/halts (`BookGapError` span, status≠running) → NULL + reason, never bridged; venue tag must match (`spot X` + `futures Y` → `venue_mismatch` exclusion); same-window `ΔP_k` retained as separate diagnostic (not renamed).

Tick-size contract (specs the Phase-12 dispatch fix): `tick_size` is a required argument everywhere; `dispatch.py`'s 3 hardcoded `Decimal("0.01")` sites become `resolve_tick_size(symbol, venue)` lookups — specced here, applied there later.

### D3.2 Change surface

| File | Change | Effect |
|---|---|---|
| `contracts.py` | ADD `ForwardObservation` | additive |
| `fitting.py` | ADD `build_forward_observations` + `FORWARD_HORIZONS_MS` | additive; `build_observations` (same-window) untouched |
| `ofi.py`/`orderbook.py`/`capture.py` | NO CHANGE | — |
| tests | ADD `test_forward_*`: no-lookahead shift test (features at `t` identical with/without future data), gap exclusion counted, venue-mismatch refused, tick-size required (`TypeError`/`ValueError` on ≤0) | — |

**Exit (per §32):** deterministic `(X_t, {Y(h)})` per horizon + exclusion log. No fitting yet.

---

## D4 — Multivariate OLS baseline (theory §§7–8, plan §33)

### D4.1 Pure fit (in `fitting.py`, mirrors `fit_price_impact` machinery)

```python
FORWARD_MODEL_VERSION = "forward-ols-v1"

@dataclass(frozen=True)  # in contracts.py
class ForwardFit:
    fit_id: str; symbol: str; venue: str
    horizon_ms: int
    betas: dict[str, str]        # field -> Decimal str (includes "intercept")
    stderr: dict[str, str | None]
    r2: str | None; resid_std: str | None
    hetero_flag: bool
    n_obs: int; n_excluded: int
    oos_skill: str | None        # out-of-sample metric (e.g. OOS R2), NULL if not run
    comparator: dict[str, str]   # univariate-Cont beta/R2 on same split
    input_hash: str; model_version: str  # == FORWARD_MODEL_VERSION
    status: str                  # validated | provisional | insufficient (same trichotomy)

def fit_forward_ols(
    pairs: list[ForwardObservation],
    *, symbol, venue, horizon_ms, min_observations=30,  # inherit MIN_OBSERVATIONS
) -> tuple[ForwardFit, list[ForwardObservation]]
```

Method (frozen): OLS/HC0 under `localcontext(prec=50)`; per-feature effect/SE/CI + incremental-info vs univariate comparator; time-ordered train/val/test + OOS skill; decision rule for "carries information" written in test BEFORE viewing results (spec records the rule: e.g. OOS R2>0 + CI excludes 0 on val AND test). `n<min` or zero-variance design → `insufficient` (numerics present but MUST NOT be interpreted — same discipline). Univariate Cont fit retained as comparator on identical split.

### D4.2 Change surface

| File | Change | Effect |
|---|---|---|
| `contracts.py` | ADD `ForwardFit` + `FORWARD_MODEL_VERSION` | additive |
| `fitting.py` | ADD `fit_forward_ols` (+ private OLS/HC0 helpers shared with `fit_price_impact` by extraction, no behavior change to existing path) | existing `PriceImpactFit`/`DepthScalingFit`/`evaluate_scenario` untouched; old tests must pass unmodified |
| everything else | NO CHANGE | no dispatch/engine surface in Track D |
| tests | ADD `test_forward_ols_*`: synthetic known-β recovery; NULL-horizon skipped+counted; `insufficient` gate; comparator present; OOS split enforced (shuffled-split helper REJECTED — time-order only) | **stop rule:** no horizon shows OOS skill → halt, do not open D5 |

**Exit (per §33):** per-horizon report (direction/magnitude/uncertainty/OOS skill); documented absence of skill is a valid stop.

---

## D5 — Distribution + calibration (theory §9, plan §34)

### D5.1 Pure predict (in `fitting.py`, read-only over D4)

```python
def predict_distribution(fit: ForwardFit, x: FeatureVector, *, theta_ticks: Decimal | None = None) -> dict[str, str | None]
# -> {expected_ticks, variance_ticks, interval_lo_95, interval_hi_95,
#     p_positive, p_above_theta (NULL unless theta given), resid_std, fit_id, horizon_ms}
# Raises ValueError on insufficient fit (mirrors derive_price_delta fabrication guard).

def calibration_report(fit: ForwardFit, pairs: list[ForwardObservation]) -> dict
# -> per-bin {predicted, realized, n} + reliability summary; miscalibrated horizon -> refusal flag
```

Rules: residual-variance → 95% PI per horizon; `P(ΔP>0|X)` Normal-approx with stated assumption (documented, same honesty as scenario scale-assumption); arbitrary-θ `P(ΔP>θ|X)` where θ arrives with query, never embedded in fit; calibration bins recorded; failing horizon → documented refusal to quote probabilities (trichotomy discipline, mirrors `evaluate_scenario` thin-tape raise).

### D5.2 Change surface

| File | Change | Effect |
|---|---|---|
| `contracts.py` | NO CHANGE (dict output, same pattern as `derive_price_delta`) — or ADD `ForwardDistribution` dataclass if reviewer prefers frozen type (decision at implementation) | minimal |
| `fitting.py` | ADD `predict_distribution` + `calibration_report` | read-only over `ForwardFit`; no existing function modified |
| everything else | NO CHANGE | — |
| tests | ADD `test_forward_dist_*`: interval coverage on synthetic Gaussian; calibration bins sum to n; `insufficient` fit raises; θ-absent → `p_above_theta` NULL (never 0) | — |

**Exit (per §34):** calibration report per horizon, or documented refusal where calibration fails. No economics/thresholds (those are D6).

---

## Cross-cutting change-surface summary (D1–D5 total)

| File | Touched? | Nature |
|---|---|---|
| `microstructure/microprice.py` | NEW (D1) | pure, ~60 lines |
| `microstructure/contracts.py` | ADD `QuoteMeasurement`, `FeatureVector`+`FEATURE_VECTOR_VERSION`, `ForwardObservation`, `ForwardFit`+`FORWARD_MODEL_VERSION` | additive only; zero modifications to existing dataclasses |
| `microstructure/fitting.py` | ADD `build_feature_vector`, `build_forward_observations`, `fit_forward_ols`, `predict_distribution`, `calibration_report` (+ shared OLS helper extraction, behavior-preserving) | existing `fit_price_impact`/`fit_depth_scaling`/`evaluate_scenario`/`assemble_evidence` byte-behavior unchanged |
| `microstructure/ofi.py`, `orderbook.py`, `capture.py`, `__init__.py` (except export) | NO | — |
| `dispatch.py`, `engine/*`, `controller.py`, `fsm.py`, Redis, PG, APIs, workers | NO (Track D) | tick-size fix specced, applied in Phase-12 |
| `tests/test_microstructure.py` | ADD-only new test fns | `785 passed` baseline must stay green unmodified |

Grain/venue/Decimal rules bind all five vectors: event grain for 1s/5s; venue tag match or refuse; Decimal-only promotion (substrate/poller floats recomputed, never copied); gaps never bridged; unproven field = NULL.

## Implementation order + gates

D1 → D2+D3 (may parallelize after D1) → D4 (highest-value unlock: first forward `E[ΔP|X]`) → D5.
Each vector opens only after prior exit evidence is recorded. D4 no-skill → halt before D5.
