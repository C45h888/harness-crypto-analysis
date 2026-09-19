# Task Directive Spec — Prompt Parsing as a Plan Variable

Status: SPEC (shaped against the current deterministic surface; no code yet).
Resolves the prompt-thinness finding (2026-09-20 audit): the user prompt is
today an LLM-steering hint with keyword routers; the deterministic plane
computes from hardcoded defaults. This spec binds the prompt into the plan
architecture without moving authority away from the controller.

Source contracts this spec consumes (all already landed):

- `FORWARD_HORIZONS_MS = (1s, 5s, 30s, 60s)` — native event-grain horizons.
- `SCENARIO_HORIZONS = {15m: 900, 1h: 3600, 4h: 14400}` — long-regime
  scenario extrapolation (empirical OFI exceedance, linear accumulation
  labelled).
- `calc.forward.forecast` — canonical ForecastResult (engine pre-acquired).
- `calc.forward.scenario` — horizon-native P(T)/P(S) curves with bands.
- `calc.scenario.evaluate` — long-regime empirical exceedance.
- Probability gate: probabilities null unless OOS calibration passed AND the
  fit is validated. **Immutable. Never bypassed by the prompt.**

---

## 0. Immutable laws (the shape that must not move)

1. **The LLM runs the track; the engine decides the track.** The agent is
   never allowed to choose state, pick a plan, skip a position, or re-order
   links. The plan is frozen at intake from the directive; the agent walks
   it and cites links. This is the existing controller doctrine extended to
   the plan, not a new authority split.
2. **The prompt is parsed, never trusted.** Extraction is a deterministic
   pure function with refusal semantics. What the parser cannot extract is
   `null` — it is never guessed, and the LLM may not fill it.
3. **The LLM assesses the prompt; the engine disposes the plan.** At the
   primitive stages of the loop the LLM gets ONE bounded assessment turn
   over the prompt (propose). The engine validates every proposal against
   the frozen vocabulary and binds only what survives (dispose). A rejected
   proposal is recorded, not applied.
4. **The probability gate is prompt-proof.** A user asking "what is the
   probability?" can never pull `p_ge/p_le` out of a refused state. The
   band-membership verdict and sigma-distance are model-implied and always
   available; probabilities stay gated on calibration evidence.
5. **Null discipline survives.** An unparseable value or unsupported
   horizon is a recorded finding ("directive refused: reason"), the plan
   degrades to `general`, and the cycle continues — never a fabricated
   number, never a cycle abort.

## 1. The directive variable (plan factor)

One frozen object, computed once at intake, persisted on
`deterministic_state["task_directive"]`, consumed by the plan builder:

```json
{
  "schema_version": 1,
  "source": "cli | prompt_parse | llm_proposal",
  "kind": "price_target | hypothesis | general",
  "horizon": {"value": "5m", "regime": "unsupported", "resolved_ms": null,
               "reason": "5m is between native 60s and long 15m"},
  "targets":  [{"value": "220.00", "kind": "target",       "provenance": "prompt:pos3"}],
  "invalidations": [{"value": "210.00", "kind": "stop",    "provenance": "prompt:pos4"}],
  "hypothesis_seed": null | {"H0": "...", "H1": "..."},
  "parse_note": "what was extracted / refused, with reasons"
}
```

Precedence (higher wins per field; engine disposes):

```text
CLI scenario dict (--target/--horizon)
  > deterministic prompt parse (pure regex/number extraction)
  > LLM assessment proposal (validated against the frozen vocabulary)
```

The LLM proposal can only ever propose values the deterministic surface
already understands: a horizon from the frozen vocabularies, Decimal
prices, an intent kind from the enum. It cannot add chain links, change
gates, or introduce horizons the plane refuses.

## 2. Placement: primitive stages of the agentic loop

Intake becomes two phases inside the existing comprehension sub-loop
(`run_comprehension`), before the plan is built:

```text
Phase A — deterministic parse (pure, zero LLM):
    task text + scenario dict → TaskDirective (with parse_note/refusals)
Phase B — LLM assessment (ONE bounded turn, propose/dispose):
    prompt: task + parse result + frozen vocabularies + assessment contract
    LLM emits: {intent_assessment, proposed_horizon?, proposed_targets?,
                proposed_invalidations?, hypothesis_seed?, confidence}
    engine validates every field → binds or rejects with reason
    (rejects ride parse_note; the track is never amended by them)
```

The comprehension loop is currently deterministic-template
(`default_comprehension`). Phase B replaces the template with a real,
single-turn assessment — the only place the LLM is ever allowed to shape
what the prompt *means*. What runs afterwards remains engine-owned.

## 3. Plan binding (the variable as a plan factor)

`build_task_workflow(task, scenario)` becomes `build_plan(directive)`:

```text
plan = {
  kind: directive.kind,
  horizon_regime: native | long | none,
  resolved_horizon_ms: directive.horizon.resolved_ms | null,
  targets / invalidations: directive lists,
  required_links: from kind + regime (TASK_CHAINS generalized),
  pre_acquire: ["calc.forward.forecast"
                 + ("calc.forward.scenario" if targets and regime==native)
                 + ("calc.scenario.evaluate"  if targets and regime==long)],
  directives_refused: [...]   # findings, not amendments
}
```

Rules:

- Native regime + targets → the engine pre-acquires the forward scenario
  **before interpretation** (same pattern as the canonical forecast).
- Long regime + targets → the engine pre-acquires the legacy exceedance
  path at the question's horizon (already horizon-native per frozen rule).
- `resolved_ms` is chosen from the frozen vocabularies only:
  `1s|5s|30s|60s` → native; `15m|1h|4h` → long; anything else →
  `regime: "unsupported"` with the refusal reason recorded in the
  directive and the plan degrades to `general` (findings ride along).
- The track itself stays exactly as the chain module defines it. The plan
  selects required-ness; it never invents links.

## 4. The use case: "user gives a value and a horizon — is it in the forecast distribution?"

End-to-end binding on the existing surface:

```text
"can SOL reach 220 in the next 60 seconds"
    → Phase A: target=220.00, horizon=60s (native, resolved_ms=60_000)
    → plan: kind=price_target, pre_acquire=[forecast@60s, forward.scenario]
    → gather: calc.forward.forecast(horizon_ms=60_000)   [canonical object]
              calc.forward.scenario(target=220, spot resolved internally)
    → agent: walks positions, cites fit_id → distribution → scenario
    → final: the task-directive verdict block (below)
```

Deterministic verdict contract (always model-implied, no calibration
needed):

```json
{
  "verdict_kind": "forecast_band_membership",
  "target": "220.00",
  "spot_price": "...", "expected_quote": "...",
  "expected_ticks": "...", "interval_lo_95": "...", "interval_hi_95": "...",
  "band_verdict": "above_upper_band | inside_band | below_lower_band",
  "sigma_distance": "...",            // pure arithmetic of published fields
  "horizon_regime": "native", "horizon_ms": 60000,
  "fit_id": "...", "validation_state": "...", "input_hash": "..."
}
```

Probability block (gated — null unless calibration passed):

```json
{ "p_ge": "...", "p_lo/p_hi band": "...",
  "probability_status": "validated | refused",
  "probability_reason": null | "calibration_not_passed | ..." }
```

Long-regime answers state their semantics verbatim: *"empirical OFI
exceedance under the linear accumulation scenario"* — never an
unqualified probability.

**Semantic decision being frozen here:** "is the value in the forecast
distribution" = band membership + sigma distance (always answerable,
model-implied, honest about its 95% interval assumption) — while "what is
the probability it hits" = calibrated `P(T)` only. The prompt can ask
either; the plane refuses only the second when calibration evidence is
absent.

## 5. Output contract (what the user reads)

The final composition prompt (`run_output`) gains the task + directive +
comprehension block so the synthesis re-anchors to the question:

- restates the task and the directive as parsed (including refusals);
- cites the directive verdict block and its deterministic provenance;
- when probabilities are refused, states the band verdict and the refusal
  reason verbatim — never a paraphrased probability.

`validate_final_turn` gains one task-conformance check: a task-bearing
cycle must cite the directive verdict (or record its refusal) before the
gate passes; repair steer names the missing citation.

## 6. Nested agentic loops (deferred, feasibility statement)

The use case above is closed on the current single-track loop. Where
nested loops would later attach, without moving authority:

- **per-target inner loop**: one inner iteration per parsed target/stop
  (multi-target prompts), each citing the same canonical forecast.
- **horizon-sweep inner loop**: the decay report's finalized horizons drive
  a bounded inner sweep (native horizons only), still engine-sequenced.
- Both remain propose/dispose: the LLM may never spawn loops; the plan
  declares them, the controller sequences them.

## 7. Non-goals (this pass)

- No free-form prompt→SQL/search extraction; only the frozen vocabularies.
- No prompt-driven model selection, no gamma, no distribution changes.
- No agent-chosen horizons: only frozen vocab values survive disposal.
- No change to the track, positions, gates, or null discipline.

## 8. Implementation order

1. `TaskDirective` contract + `parse_task_directive()` (pure, versioned,
   refusal semantics) — `engine/task_directive.py` (new, no I/O).
2. Phase B assessment turn in `run_comprehension` (bounded, propose/dispose).
3. `build_plan(directive)` + gather pre-acquisition wiring (horizon,
   scenario targets).
4. Output contract + `validate_final_turn` task-conformance check.
5. Tests: parser vectors (numbers, horizons, refusals), assessment-disposal
   (proposal rejected outside vocab), plan binding per regime, verdict
   block end-to-end, probability gate untouched.

Definition of done: a task-bearing cycle persists the directive, the plan,
and the verdict block; a task without parseable value/horizon degrades to
`general` with findings; every final answer to the use-case question cites
`fit_id → distribution → scenario` with regime and gate statuses intact.
