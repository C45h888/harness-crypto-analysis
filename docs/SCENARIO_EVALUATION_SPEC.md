# Scenario Evaluation Spec — Phases 1 + 2

Status: **Phases 1 + 2 + 3 INTEGRATED** (636 green). Open: live-fire
validation (real `--task + --target` cycle against the tape).

Interaction plane question: **"according to the statistical inference model,
is it possible for price to hit X?"**

Probability semantic (frozen, Phase 0): **empirical OFI exceedance rate** —
the fraction of observed horizon-length OFI sums reaching the required flow.
No model-based probability; the fitted model supplies the *requirement*,
the tape supplies the *probability*.

Horizon rule (frozen): the OFI distribution is built at the **question's
horizon** — `15m | 1h | 4h` (default `1h`). A 10s distribution is rejected
by design: required flow for a real target always sits far outside 10s
flow, collapsing every answer to "0%". Horizons arrive via `--horizon`;
`--target` carries the price. Both ride the existing `task` threading
pattern — no new invocation model.

Scale assumption (stated, not smuggled): β is fitted per base interval
(10/15/30s) and applied linearly over the horizon —
`Δ_req = n·α + β·OFI_total`, `n = horizon / interval_seconds`.
Impact scale-invariance is an *assumption*, recorded on every output and
as a standing limitation. Challenging it is future work, not this change.

---

## Phase 1 — Deterministic backbone (no LLM, no prompt changes)

### 1.1 `fitting.evaluate_scenario()` (pure, `microstructure/fitting.py`)

```python
def evaluate_scenario(
    *,
    target_price: Decimal,
    current_price: Decimal,
    tick_size: Decimal,
    price_fit: PriceImpactFit,          # fitted α/β/SE + status
    intervals: list,                    # replayed OFI base intervals, time-ordered
    interval_seconds: int,              # 10 | 15 | 30
    horizon: str,                       # "15m" | "1h" | "4h"
    depth_fit: DepthScalingFit | None = None,
    average_depth: Decimal | None = None,
) -> dict[str, Any]
```

Computation (all Decimal, all outputs stringified per repo convention):

1. `delta_req_ticks = (target − current) / tick`, signed. Zero → raise
   `ValueError("target equals current price")`.
2. `direction = "up" if delta > 0 else "down"`.
3. `n = HORIZON_SECONDS[horizon] / interval_seconds`
   (`{"15m": 900, "1h": 3600, "4h": 14400}`; all divisible by 10/15/30 —
   non-divisible combos raise rather than round).
4. `drift = n · α`; `OFI_req = (Δ_req − drift) / β`.
   Guard: `|β| < 1e-18` → `required_ofi: None`,
   `reason: "beta_zero"` (forces H0 downstream, never a fabricated number).
   Decimal `InvalidOperation` on division → same guard, never propagates.
5. Empirical distribution: rolling sums of OFI over `n`-interval windows,
   step 1, over the supplied intervals (cap 50_000 for safety; 4h @10s is
   1_440 sums — the cap never binds in practice).
   Quality: a window is *usable* iff every interval in it is
   `quality == "exact_feed"`; degraded windows are counted, never scored.
   Usable `< 30` → raise `ValueError("insufficient usable windows: k")`.
6. Exceedance (one-sided, direction-matched):
   up → `#{s ≥ OFI_req} / N`; down → `#{s ≤ OFI_req} / N`.
7. Band range: recompute step 4 with `β ± 1.96·SE`, order by value into
   `[required_ofi_lo, required_ofi_hi]`, exceedance evaluated at both ends
   → `exceedance_range`. SE null → range null + reason.
8. Route B (only when `depth_fit` validated/provisional, c/λ non-null,
   AD > 0): `β_implied = c·AD^(−λ)`, `OFI_req_b`, `exceedance_b`, else
   `status: unavailable + reason` (same shape as `derive_price_delta`).
9. Output: `{direction, current_price, target_price, delta_req_ticks,
   horizon, n_intervals, required_ofi, required_ofi_range,
   exceedance, exceedance_range, n_windows_usable, n_windows_degraded,
   fit_id, fit_status, r2, heteroskedasticity_flag, tick_size,
   scale_assumption_note, route_b}`.
   Refusals raise `ValueError("<code>: <detail>")` — the dispatcher
   converts to `refused`, mirroring `derive_price_delta` (null, never zero).

Refusal codes: `insufficient_fit` (fit status gate, same rule as ΔP),
`beta_zero`, `target_eq_current`, `bad_horizon`, `no_intervals`,
`insufficient_windows`, `no_market_price` (dispatch-level).

### 1.2 `dispatch_calc_scenario_evaluate()` (`inference/dispatch.py`)

```python
async def dispatch_calc_scenario_evaluate(
    store, symbol, venue, *,
    target_price: Any, horizon: str = "1h",
    interval_seconds: int = 10, window_minutes: int = 30,
    tick_size: str = "0.01", postgres=None,
) -> tuple[dict | None, dict]
```

- Scope-validate via `CAPABILITIES["calc.scenario.evaluate"]`.
- Fits via existing `_tool_fit_beta` (same `window_minutes` fit window —
  the *fit* window stays 15/30/60m; only the *distribution* horizon is
  15m/1h/4h).
- Intervals via the established replay pattern (`read_microstructure_events`
  → `replay_events_from_payloads` → `replay_intervals`).
- Current price resolved **inside the tool** from `read_paths.read_collated`
  snapshot: `mark_price ?? last_price`; absent/unparseable → refused
  `no_market_price`. The agent never supplies prices (NEVER-recompute rule).
- `horizon` validated against the frozen set; `target_price` parsed to
  Decimal or refused.
- Returns `(result, capability_log_entry(...))` with
  `ofi_source`-style provenance (`scenario_arg` target, price source named).

### 1.3 Registry (4 touchpoints, existing convention)

- `capability.py`: `CAPABILITIES["calc.scenario.evaluate"]` (bounded
  symbols/venues = `_INITIAL_*`, description names the refusal discipline).
- `dispatch.py`: `TOOL_NAMES["calc.scenario.evaluate"]`,
  `TOOL_PHASE[...] = "P5"`, `execute_tool` branch forwarding
  `target_price/horizon/interval_seconds/window_minutes/tick_size`.
- No `_normalize_tool_name` change (dotted canonical name, same as peers).

### 1.4 Phase 1 tests (all LLM-free)

- Pure math: upside + downside hand-computed cases; band ordering with
  negative β; `beta_zero` guard; zero-delta / bad-horizon refusals.
- Windows: usable/degraded counting, `< 30` refusal, rolling-sum correctness
  on a synthetic series.
- Dispatch (fake store): `no_market_price`, `insufficient_fit`,
  unparseable-target refusals; registry resolves + phase is P5.

---

## Phase 2 — Invocation plumbing (state echo only, no semantics)

No `_output_format`, validator, or guidance changes — those are Phase 3.
Phase 2 makes `--target/--horizon` reach `deterministic_state` and
byte-identical behaviour without them.

- `harness.py`: `--target <price>` (Decimal-parsed, `> 0`) +
  `--horizon {15m,1h,4h}` (default `1h`); both require `--inference
  --inference-force` (same gate as `--task`); passed as
  `scenario={"target_price": ..., "horizon": ...}`.
- `nooa_cli_ext.py` `inference run`: same two options, same threading.
- `inference_runner.run_inference_once(..., scenario=None)`: forwards to
  `acquire_manual_wake` (echo on manual predicates) and
  `run_cycle(scenario=...)`.
- `engine.run_cycle(..., scenario=None)`: persists
  `deterministic_state["scenario"]`, echoes a `SCENARIO:` block in the TASK
  prompt section, records on the wake capability detail + `cycle_meta`
  (including the gate-refused early return, per the `task` precedent).
- Tests: CLI parse (valid/invalid horizon, non-numeric target),
  threading (scenario echo in state/meta/detail), target-less parity
  (no scenario keys, validator untouched).

## Phase 3 — Agent semantics (INTEGRATED)

- `_output_format` carries the `scenario{...}` contract key (required at
  final only when a SCENARIO block was given); P5 tool list names
  `calc.scenario.evaluate`.
- `_validate_final_turn(parsed, coverage, scenario=None)`: conditional
  clause — complete scenario block (verdict/probability/requirement
  fields), `calc.scenario.evaluate → …` evidence root, and non-empty H0+H1
  reachability pair. Target-less cycles unchanged.
- `_scenario_verdict(tool_result, capability_log, gate, reasons)`: band-edge
  rule (max-edge 0 → invalidated, min-edge ≥ 0.5 → validated, else
  inconclusive), provisional ceiling with reason-carried read, refusals and
  missing evaluations → inconclusive with cause named. Scenario cycles take
  their `hypothesis_verdict` from this helper; all other cycles keep the
  gate mapping byte-identical.
- Steering: per-cycle P5 suffix naming the exact target/horizon args +
  repair-prompt scenario line when unevaluated + scenario reminder beside
  the task reminder.
- `interpretation["scenario"]` persisted (JSON column, no migration).
- Tests: 10 semantics units (validator × 4, verdict × 6) in
  `tests/test_scenario.py::ScenarioSemanticsTests`.

## Explicitly deferred (live-fire + beyond)

P5 guidance + `_output_format` `scenario{...}` block, `_validate_final_turn`
scenario clause (conditional on scenario presence), H0/H1 reachability
framing, verdict mapping, multi-interval horizon compounding, per-symbol
tick registry, docs + live-fire.
