# Scenario Contract Integration — Runtime Surface Report

Status: **integrated, full suite green (1333 passed, 10 skipped)**.
Scope: one frozen price-target question from the interaction plane through
carry, dispatch, validation, artifact, and output — plus the prompt-authored
permission to answer a different question.

This report describes the surface as built. It does not replace
`docs/SCENARIO_EVALUATION_SPEC.md` (evaluation math) or
`docs/TASK_DIRECTIVE_SPEC.md` (directive parsing); it is the ownership and
flow map between them.

---

## 1. The single authority

`engine/scenario_contract.py` — `ScenarioContract` (frozen, schema-versioned,
registry-backed).

It owns:

| Field | Meaning |
|---|---|
| `asked_target` / `asked_horizon` | the question verbatim |
| `target_canonical` | Decimal-normalized value (comparison only) |
| `allowed_targets` | targets the prompt/directive/plan named |
| `divergence` | `forbidden` / `declared` / `free` — **prompt-authored** |
| `divergence_basis` | the prompt phrase that authorized divergence |
| `contract_id` | hash of asked target + horizon + source |

Derivation is deterministic and conservative: CLI `--target` with no task is
`forbidden`; only explicit comparison/alternative language in the task makes
it `declared`. The model cannot raise its own permission.

`ctx.scenario` is now a **compat projection** (`to_scenario_dict()`); the
controller carries the contract itself. `deterministic_state["scenario_contract"]`
is the artifact projection.

---

## 2. Surface map (who reads what)

| Plane | File | Contract surface |
|---|---|---|
| Intake | `commands/nooa_cli_ext.py`, `interaction_plane/trigger.py` | raw `{target_price, horizon}` |
| Wake | `core/wake.py:187,218,234` | `build_contract` → controller + ctx + projection |
| Comprehension | `core/wake.py:359,477-478` | plan-bound `allowed_targets`, opening `SCENARIO CONTRACT` block, reminder, P5 guidance, understanding receipt |
| Acquisition | `core/gather.py:171-219` | plan's long-horizon `pre_acquire` honored as `engine.scenario.digest` (digest authority only; never accumulated, never credited) |
| Declaration | `engine/schemas.py` (`scenario_intent`), `controller.py:413` | model-authored intent, engine-disposed (`as_asked` / `revision` / `refuse`) |
| Carry | `controller.py:707`, `core/driver.py:567-579` | independent target+horizon fill, invalid-explicit replacement, finding → `capability_log` + `st.scenario_surface` |
| Redundancy | `controller.py:769` | numeric equivalence (`104.8` == `104.80`); declared revision is new work |
| Prompt state | `core/driver.py:814`, `kb.py:887,976,1116` | live `SCENARIO STATUS` in follow-up, repair, forced-final |
| Validation | `core/validation.py:606` | one oracle bound to the last genuine `ToolOutcome` |
| Artifact | `core/validation.py:468`, `output.py:154` | `scenario_contract`, `scenario_evaluation`, `scenario_divergence` |

Roots are chartered in `core/context.py:244-245`.

---

## 3. Invariants now enforced

1. **One question.** Every plane reads the same contract; no plane re-derives
   the asked target from prose.
2. **Absent args are bound.** Carry fills target and horizon independently.
3. **Invalid explicit is not explicit.** `0`, `"abc"`, blank/null → the bound
   target is carried and the raw value is named on the finding.
4. **Valid explicit wins execution, not validation.** The tool runs the
   model's number; validation compares it to the bound number.
5. **One outcome.** Validation reads the last genuine dispatch
   (`scenario_outcome()`), never a capability-log scan and never "any entry
   matched once".
6. **A refusal answers only its own question.** Refusing a different target
   is a finding, not an `unevaluable` escape.
7. **No dispatch, no verdict.** A verdict without a real dispatch is named as
   fabrication.
8. **Divergence is declared and disposed.** A permitted revision changes the
   effective bound only via `scenario_intent` with reason + basis; the final
   must surface `scenario.divergence`. Forbidden divergence is rejected by
   the controller before it can bind.
9. **Numeric, not textual.** Target/horizon equality and redundancy use
   Decimal canonicalization.
10. **Digest is not proof.** The pre-acquired scenario digest is
    envelope-only; the agent still earns the evaluation with its own call.

---

## 4. Holes closed (empirically probed)

| Case | Before | Now |
|---|---|---|
| Evaluate asked target, then evaluate 100.00, finalize on 100.00 | passed (any-match scan) | fails: latest outcome ≠ bound |
| Explicit wrong target → refusal → `unevaluable` | passed (REFUSED bypass) | fails: refusal ≠ bound target |
| `104.8` vs `104.80` | rejected as mismatch | passes (numeric) |
| Right target, wrong horizon | passed | fails: horizon ≠ bound |
| No dispatch, fabricated `reachable` | passed | fails: never dispatched |
| No dispatch, fabricated `unevaluable` | passed | fails: never dispatched + structural refusal rule |
| Revision without prompt permission | silently allowed | rejected by `with_scenario_intent` |
| Revision declared, final omits divergence | passed | fails: divergence block required |

Probe output (real controller/contract objects) confirms each line.

---

## 5. Tests

- New: `tests/test_scenario_contract_surface.py` — **34 tests**: contract,
  carry, invalid explicit, numeric redundancy, revision disposal, validation
  oracle (all hole cases), prompt blocks (opening/follow-up/repair/forced
  final/digest), evaluation root, and a WAKE+COMPREHENSION integration test
  proving the opening loop binds and renders the contract.
- Extended: `tests/test_engine.py` e2e refused-scenario asserts
  `scenario_contract` + `scenario_evaluation` on the artifact.
- Updated: `tests/test_structural_v6.py` (horizon carry on explicit target),
  `tests/test_synthesize.py` (aligned to the parallel relaxed-synthesis
  semantics), `tests/fixtures/cycle_characterization.json` (only the two new
  deterministic roots and the expected prompt lines changed).
- Full suite: **1333 passed, 10 skipped, 7 subtests passed** (the
  skip count reflects the parallel workstream's gated tests).

Verification commands:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_scenario_contract_surface.py -q
```

---

## 6. Deliberate boundaries

- **Legacy plane retained.** Direct construction with no contract and no
  outcome keeps the pre-contract verdict-equality behavior (tests/older
  callers). The runtime always takes the contract plane when a scenario
  exists.
- **Chain required-ness unchanged.** `chain.py` still requires
  `calc.forward.scenario` when the task bears horizons; the contract adds
  no new required link. The contract's own required tool is enforced by the
  structural scenario block + equality oracle.
- **Pre-acquire is digest-only.** The plan now does the work it advertises,
  but the digest never satisfies proof.
- **Latest declaration wins.** A later `as_asked` intentionally reverts a
  prior revision.
- **Parallel workstream.** `synthesize.py`'s relaxed-loop rewrite is not
  ours; only missing imports were restored and its stale test was aligned.
  Three `pi` processes share this cwd — serialize or use a worktree for
  further concurrent edits.
