# NOOA order-flow / microstructure integration

Architecture map and semantic contract for a soft-test implementation of the
three-model framework in Cont, Kukanov and Stoikov, *The Price Impact of Order
Book Events* (arXiv:1011.6402, v3). Source: [arXiv HTML/PDF landing
page](https://arxiv.org/abs/1011.6402).

This document is an inspection result and integration proposal. It does not
change production code or authorize trading automation.

## Executive finding

The repository has a sound measurement → deterministic calculation → analysis
agent separation, plus a replayable Redis raw stream and immutable
`MarketRunEnvelope`. It does **not** currently capture the event stream needed
for the paper's literal event-level OFI. Binance data is polled through REST:
the current order book is an L2 snapshot and trades are a rolling set of
aggregated trade prints. Therefore:

- current CVD/OBI/delta metrics are usable deterministic facts, but are not
  paper OFI;
- a literal `e_n` requires an ordered Binance depth-diff feed (or an explicitly
  labelled approximation from consecutive snapshots);
- fitting `ΔP_k = β_i OFI_k + ε_k` and
  `β_i = c / AD_i^λ + ν_i` belongs in an offline/replay inference layer first;
- NOOA should receive validated fit objects and evidence references, not
  calculate OFI or silently turn it into a strategy signal.

## Evidence-backed current architecture

### Data polling and parsing

`market_service.poller.fetch_binance_evidence` is the current firehose. It
fetches spot and USD-M futures L2 depth, 24-hour tickers, futures funding/mark
and current OI, and spot/futures `aggTrades` in parallel. The loop defaults to
one cycle every five seconds. Endpoint failures are retained in an `errors`
array and do not silently become healthy values.

The only external API client is `market_service.clients.binance.Binance`.
`spot_book` and `fut_book` return `{lastUpdateId, bids, asks}` snapshots.
`normalize_spot_trade` and `normalize_fut_trade` map both recent-trade and
aggregate-trade shapes to `{ts, id, price, qty, quote_qty,
is_buyer_maker, side, venue}`. `side=buy` means an aggressive taker buy
(`is_buyer_maker=False`); `side=sell` means an aggressive taker sell.

There is no Binance websocket/depth-diff consumer in the inspected tree, no
sequence-gap recovery path, and no event-time order-book state machine.

### Raw storage and windowing

`RedisRuntimeStore.publish_raw_evidence` atomically writes the latest raw
payload and an entry to `marketflow:stream:raw:<SYMBOL>`; a Lua idempotency key
deduplicates on `observed_at_ms`. The raw stream is bounded (currently a
5,000-entry MAXLEN) and the latest key is point-in-time.

`nooa_harness.pipeline.read_raw_window` reads the latest book/ticker/funding/OI
and accumulates normalized trades from the raw stream. Because each poll
contains a rolling trade window, it deduplicates by Binance aggregate trade
`id`. This is appropriate for CVD/volume aggregation, but it cannot recover
quote updates that were never captured.

Raw every-trade/depth-event PostgreSQL storage is intentionally deferred in the
current architecture. PostgreSQL instead stores the durable `market_snapshot`,
`signal_event`, and immutable `market_run` JSONB envelope (plus wall/keystone
history and analyst artifacts). Redis is operational/replay-window storage;
PostgreSQL is the durable ledger.

### Deterministic feature calculation

`market_service.calculations.flow` computes buy/sell volume, CVD, VWAP, OBI,
spread, microprice skew, time-bucketed CVD, and spot/futures alignment.
`calculations.orderbook`, `delta`, `volume_profile`, `technical`, and
`signals` add depth windows, ladders, wall imbalance, and deterministic alerts.
All are pure functions over normalized inputs.

`nooa_harness.pipeline.run_calculations` is the canonical adapter. It resolves
calculation-section dependencies, validates input shapes with
`nooa_harness.contracts.strict_call`, retains contract errors, and marks the
calculation domain degraded rather than fabricating a value.

### Analysis and agent/object boundaries

`run_analysis` applies deterministic interpretation modules (`auction`, `oi`,
`wall_migration`, `path_absorption`, `demand`, `regime`, `stage`, `delta`).
`assemble_envelope` separates `data-access`, `calculations`, and `analysis`
inside a versioned `MarketRunEnvelope` with run id, coverage, status, errors,
and source metadata. `persist_envelope` writes PostgreSQL first and then the
matching Redis projection.

The NOOA suite (`nooa_harness/agents.py` and `suite.py`) reads one complete
canonical envelope. Specialists make one non-code-executing model call and
must cite envelope paths; the controller reconciles reports. `DeltaOrderflowAgent`
is the existing order-flow interpretation surface. The NOOA inner CLI is
calculation-agnostic; the outer harness owns collection/collation.

`market_service.analysis.market` still exposes a direct live REST one-shot
analysis/debug path. The repository documents the outer harness/canonical
ledger as authoritative, so this legacy-style path should not be used as the
OFI integration boundary without an explicit migration decision.

### Observability

Current observability includes endpoint errors, latency, source/coverage
metadata, healthy/degraded/invalid status, Redis stream IDs, idempotent run
IDs, contract-violation records, container health checks, and NOOA parse/
timeout errors. The telemetry hygiene service cleans runtime artifacts.

Missing microstructure-specific observability includes depth update sequence
gaps, exchange-to-receive lag, duplicate/out-of-order event counts, book
reconstruction status, event-level coverage, OFI interval event counts, and
fit diagnostics. These are requirements for the proposed soft-test path.

## Paper semantics and proposed contracts

The paper defines best-bid/ask state
`(P^B_n, q^B_n, P^A_n, q^A_n)` and the event contribution:

```text
e_n = 1{PB_n >= PB_{n-1}} qB_n
    - 1{PB_n <= PB_{n-1}} qB_{n-1}
    - 1{PA_n <= PA_{n-1}} qA_n
    + 1{PA_n >= PA_{n-1}} qA_{n-1}
```

The interval boundary is explicit and half-open:

```text
OFI_k = sum(e_n for event_ts_n in [t_{k-1}, t_k))
```

The empirical models are:

```text
ΔP_k = β_i OFI_k + ε_k
β_i  = c / AD_i^λ + ν_i
```

### Correct use of the three equations

The equations are a hierarchy, not three independent indicators:

1. The mechanical model is derived under a stylised constant-depth book. It
   gives `ΔP ≈ OFI / (2D) + rounding error`; it motivates the direction and
   depth dependence of impact, but is not the fitted live rule.
2. The empirical model fits a **separate** `β_i` over a stable estimation block
   `i`, using many shorter intervals `k` within it:
   `ΔP_k = α_i + β_i OFI_k + ε_k`.
3. The depth model then estimates the relation of the fitted coefficients to
   average depth: `β_i = c AD_i^-λ + ν_i`.

Substitution is useful as a derived hypothesis:

```text
ΔP_k = α_i + c OFI_k / AD_i^λ + (ν_i OFI_k + ε_k)
```

It is **not** simply `c OFI / AD^λ + independent error`: the term `ν_i OFI_k`
depends on OFI, so its variance changes with order flow. The fitter should
retain both regressions and report the combined relation only as a derived
diagnostic. This preserves the paper's methodology and makes residual checks
meaningful.

### Current five-second stream: exact mapping

| Required quantity | Current source | Status and limitation |
|---|---|---|
| `P^B, q^B, P^A, q^A` | First bid/ask in each Binance REST depth snapshot | Available every nominal five seconds, with 500 configured L2 levels. Captured at client observation time, not a complete quote-event history. |
| `e_n` | Not present | Cannot be reconstructed exactly: unobserved intermediate quote updates can make a five-second endpoint-to-endpoint difference differ from the sum of true event contributions. |
| `OFI_k` | Not present | Can be an explicitly approximate snapshot-OFI only; it is not paper OFI. Literal OFI needs an ordered diff-depth reconstruction. |
| Mid price `P_k` | Derivable from snapshot best bid/ask | Available only at poll resolution today; exact price-change timing inside the five-second gap is unavailable. |
| `AD_i` | Derivable from recorded best quantities | Available as a sampled proxy, not the paper's event-average depth. Record `depth_estimator=sampled_snapshot_mean`. |
| Aggressive trade flow | `aggTrades`, normalized and deduped by aggregate ID | Available and useful as a separate CVD/trade-imbalance control. It must not be substituted for OFI. |
| `β_i`, `c`, `λ` | Not implemented | Requires frozen interval blocks, fitting data retention, and deterministic inference code. |

With the default configuration, the poller has `POLL_SECONDS=5`, requests a
300-second `aggTrades` lookback (`FLOW_WINDOW_SECONDS=300`), and retains a
bounded raw Redis stream. The harness can assemble 15-minute, one-hour, or
four-hour windows by deduplicating aggregate-trade IDs, but it only retains the
latest book snapshot as the book input to its present calculations. That is
adequate for its existing CVD/OBI/wall workflow; it is inadequate for literal
event-level OFI estimation.

### Required authority split

The desired object-oriented NOOA design is strongest when it has four layers:

```text
Binance data → deterministic measurement objects → deterministic statistical fitter
             → immutable microstructure evidence object → NOOA interpretation
```
this
The first three stages are Python/runtime authority. The NOOA agent is an
object-oriented consumer of typed `MicrostructureEvidence`, able to explain
fit quality and combine it with the existing regime objects. It must not be
the authority that recomputes values or fits parameters from prompt context.
That division is what makes agent output less probabilistic while preserving
an auditable inference process.

For Binance, define the following transport-safe objects before writing a
calculator:

### `BestQuoteState`

```text
symbol, venue, instrument_type, exchange_ts_ms, received_ts_ms,
sequence_id, best_bid_price, best_bid_qty, best_ask_price, best_ask_qty,
tick_size, quantity_unit, source, schema_version
```

`sequence_id` must be a Binance depth-update sequence (or a synthetic,
monotonic replay sequence for fixtures). `quantity_unit` is explicit because
spot base quantity and futures contract quantity are not interchangeable.

### `OrderBookEvent`

```text
event_id, symbol, venue, exchange_ts_ms, received_ts_ms, sequence_id,
previous: {bid_price, bid_qty, ask_price, ask_qty},
current:  {bid_price, bid_qty, ask_price, ask_qty},
event_kind: best_quote_change | best_quote_unchanged | snapshot_approximation,
e_n, source_quality, gap_before, schema_version
```

The event builder applies depth diffs in sequence, reconstructs the book, and
emits an event whenever the best quote state changes. `e_n` is calculated only
from the preceding and current best state using the formula above. A REST
snapshot-to-snapshot estimate must use `event_kind=snapshot_approximation` and
must never be reported as literal event OFI.

### `OFIInterval`

```text
interval_id, symbol, venue, start_ts_ms, end_ts_ms,
event_count, first_sequence_id, last_sequence_id,
ofi, sum_abs_e, mean_ad_best,
exchange_coverage_ms, receive_coverage_ms,
missing_sequence_count, duplicate_count, out_of_order_count,
quality: exact | approximate | insufficient, schema_version
```

The aggregation boundary is the interval clock, not the poll cycle. Every
event belongs to exactly one `[start, end)` interval. `mean_ad_best` is the
time/event-average of `(qB + qA)/2` under the chosen quantity unit; the exact
estimator and denominator must be recorded in metadata.

### `PriceImpactObservation` and `PriceImpactFit`

Each observation joins one `OFIInterval` to the same-window mid-price change:

```text
mid_start, mid_end, delta_price, price_unit: ticks | log_return | quote,
ofi, interval_id, depth_regime_id, quality
```

`PriceImpactFit` is an inference artifact, not a measurement:

```text
fit_id, symbol, venue, fit_window_start/end, interval_seconds,
alpha, beta, stderr_beta, robust_se_method, n_observations,
r2, residual_mean, residual_std, heteroskedasticity_flag,
excluded_observations, input_hash, model_version, validation_split,
status: provisional | validated | insufficient
```

`DepthScalingFit` contains `c`, `lambda`, uncertainty/fit diagnostics,
positive-depth exclusions, and the set of `PriceImpactFit.fit_id` inputs. No
fit is valid without minimum-sample, coverage, and out-of-sample checks.

## What matches the paper, and what does not

| Paper assumption/choice | Binance/NOOA status |
|---|---|
| Best bid/ask Level I state | Matches conceptually; current REST L2 contains best levels, but not a complete update history. |
| Limit orders, market orders, cancellations all represented | Does not match current capture. AggTrades represent executions; REST snapshots do not identify adds/cancels or intermediate events. |
| Ordered event times and complete quote updates | Does not match. Five-second REST polling can miss arbitrary changes; aggregate trade rows can combine executions. |
| One consolidated equity market | Does not match. Spot and USD-M futures are separate books and must be fitted separately before any cross-venue comparison. |
| Queue size in shares | Requires explicit Binance base-quantity/contract-unit handling and symbol filters. |
| 10-second uniform intervals and half-hourly stable β | A possible initial configuration, not a fact. Crypto is 24/7 and cadence/liquidity are regime-dependent; test several interval and fit windows. |
| Mid-price move in ticks | Matches only after using the instrument tick size; log return or quote units must be labelled alternatives. |
| Linear OFI impact with noise from deeper levels/rounding | Testable, but not assumed. Binance depth truncation, hidden/iceberg liquidity, latency, and book reconstruction gaps add error. |
| Depth scaling `β=c/AD^λ` | Testable as a statistical cross-window relation; `AD` definition and units must be frozen before fitting. |

## Phased integration plan (soft testing only)

### Decision record: protect the five-second poller

**Proposed decision:** leave `market_service.poller` unchanged. It remains the
five-second REST source for the existing canonical workflow. Do not add a
websocket connection, book reconstruction, or fitting workload to its loop.

Literal OFI requires new data acquisition, so it belongs in a separate
`microstructure-capture` data-access component, not in
`calculations`. The component owns its Binance diff-depth connection,
snapshot bootstrap, sequence validation/recovery, and append-only event
publication. Its failure must only degrade the optional microstructure domain;
it must never slow, block, or change the status of the existing raw poller.

The deterministic calculator then consumes captured event records only:

```text
microstructure-capture → raw quote/diff event stream
                       → OFI + sampled/event-average depth calculator
                       → reproducible fit runner
                       → immutable MicrostructureEvidence
                       → canonical envelope → NOOA interpretation
```

The outer harness owns the command surface. A NOOA agent may submit a bounded
request for a microstructure calculation/fit, but a runtime capability validates
the requested symbol, venue, lookback, data-quality prerequisites, retention,
and rate limits before dispatch. The inner NOOA CLI cannot start a Binance
connection or fit from its prompt context.

Suggested soft-test commands (names are proposals, not yet implemented):

```text
harness SYMBOL --microstructure-capture-status
harness SYMBOL --microstructure-replay --from <fixture-or-event-range>
harness SYMBOL --microstructure-fit --venue spot --interval 10s --fit-window 30m
harness SYMBOL --microstructure-evidence --latest
```

The first command creates no market interpretation. The second validates
deterministic mechanics. The third produces a statistical artifact only when
coverage gates pass. The fourth is the read-only input handed to NOOA.

1. **Contract freeze and fixtures.** Add no runtime behavior. Review this
   contract, choose venue scope (spot first is simplest), tick/quantity units,
   interval clock, minimum coverage, and fit windows. Create hand-worked event
   fixtures covering bid/ask price moves, queue adds/removes, ties, duplicates,
   and gaps.
2. **Event capture/reconstruction seam.** Add an isolated replayable adapter
   for Binance depth diffs, with REST snapshot bootstrap and sequence-gap
   invalidation/recovery. Keep the existing poller path unchanged; persist raw
   fixture/event records only in a bounded soft-test stream or files. Do not
   place orders or feed decisions.
3. **Deterministic measurement.** Implement pure `BestQuoteState`,
   `OrderBookEvent`, and `OFIInterval` objects. Verify the exact `e_n` equation,
   half-open interval assignment, duplicate/gap handling, and deterministic
   serialization/hash. Keep snapshot approximation visibly separate.
4. **Replay inference.** Build a batch-only fitter for
   `PriceImpactObservation`, `PriceImpactFit`, and `DepthScalingFit`. Use
   train/validation splits, minimum event/interval counts, robust standard
   errors, residual/heteroskedasticity diagnostics, and explicit
   `provisional/validated/insufficient` status. Never expose a provisional fit
   as a trading signal.
5. **Canonical envelope integration.** Once replay checks pass, add an optional
   versioned `microstructure` domain output to `MarketRunEnvelope`, preserving
   raw event references, coverage, input hashes, model versions, and errors.
   Persist PostgreSQL-first and publish the identical Redis projection, just
   like other domains. Existing envelopes remain backward-compatible.
6. **NOOA shadow read.** Extend the order-flow specialist's typed view to cite
   the microstructure paths. The agent may summarize fit quality, caveats, and
   regime context; it may not recalculate OFI, alter β/λ, request execution, or
   override a deterministic status. Compare agent statements with exact
   envelope values on replay runs.

## Validation and replay gates

- Equation tests: every paper event case has an expected `e_n`; compare exact
  decimal/tick-normalized values.
- Book tests: apply diffs in order, reject crossed/negative/zero-invalid books,
  detect sequence gaps, and ensure replay is idempotent under duplicate input.
- Boundary tests: events at `start` are included, events at `end` are excluded;
  no event is double counted across adjacent intervals.
- Coverage tests: missing depth updates, stale snapshots, and insufficient
  trade/book coverage produce `approximate`, `insufficient`, `degraded`, or
  `invalid`—never zero-filled healthy output.
- Determinism: identical fixture bytes, configuration, and model version yield
  identical event rows, interval rows, input hash, and fit result.
- Statistical gates: minimum `n`, finite/positive depth, nonzero OFI variance,
  stable sign of β, confidence intervals, residual checks, and held-out error.
- Cross-check: on periods with both feeds, compare reconstructed best quotes to
  REST snapshots and quantify lag/gap/price disagreement before trusting fits.
- Replay provenance: every result carries source sequence range, fixture/raw
  identifiers, interval configuration, and code/model version.

## Data-quality and operational caveats

- REST depth snapshots are point-in-time evidence, not a complete event tape;
  cancellations and market orders cannot be separated from a size decrease.
- Aggregate trades are not a substitute for quote events and may aggregate
  multiple executions; trade IDs are suitable for deduplication, not event
  sequence reconstruction.
- Spot and futures have different liquidity, contract semantics, and price
  formation. Do not pool them into one β without a deliberate model.
- Binance is one venue; cross-venue routing, hidden liquidity, and liquidation
  feeds are not represented by the current canonical evidence.
- The current raw Redis retention is bounded and PostgreSQL has no raw event
  ledger. Long-horizon model fitting requires an approved retention design.
- Time must distinguish exchange timestamp, local receipt timestamp, and
  interval clock. Clock skew and reconnects affect both coverage and causality.
- The paper notes possible tautology when price-changing events enter OFI. The
  validation report should include a sensitivity fit excluding price-changing
  events, with the limitation stated explicitly.

## Explicit non-goals

- No order placement, private exchange keys, sizing, execution, or autonomous
  decision automation.
- No claim that current CVD/OBI/delta equals paper OFI.
- No production websocket migration or retention expansion before contract and
  replay gates pass.
- No live parameter adaptation, threshold invention, or model-generated code.
- No strategy interpretation mixed into event measurement or statistical fit.
- No replacement of PostgreSQL durable authority with Redis.
- No deletion or rewriting of the existing collector, harness, or legacy
  analysis modules in this architecture phase.

## Recommended ownership

| Layer | Proposed owner | Allowed output |
|---|---|---|
| Measurement/mechanics | `market_service` deterministic microstructure module | quote states, `e_n`, `OFIInterval`, quality/coverage |
| Empirical inference | batch/replay fitter | β, c, λ, residuals, uncertainty, validation status |
| Regime/strategy interpretation | existing deterministic analysis + NOOA specialists/controller | evidence-linked scenarios and limitations only |
| Storage/observability | runtime adapters | immutable envelope, input hashes, sequence/gap/latency diagnostics |
