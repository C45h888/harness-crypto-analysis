# Agentic Authority Spec — Deterministic Cycle Controller for the OO Agent

Status: SPEC (no code changes yet). Resolves Vector 3 (validator/refusal
contradiction) and the authority vacuum behind it. Written against the
post-decomposition `engine/` package (config/schemas/kb/llm/narration/core/
interpreter) as verified 2026-09-10.

---

## 0. Pre-existing state (what already landed)

| Item | State |
|------|-------|
| `engine/` package decomposition (8 modules) | ✓ landed, 679 tests green |
| Round 1 prompt consolidation (SystemPromptCache, TURN_CONTRACT_LINE, follow-up dedup, manifest budget note) | ✓ landed this session, verified |
| `scenario_verdict` refusal handling (narration.py:288-298) | ✓ correct — reads capability_log, refuses-as-inconclusive |
| `validate_final_turn` refusal awareness | ✗ ABSENT — the Vector-3 defect |
| Repair steer correctness (core.py scenario_steer) | ✗ ABSENT — keyed on key-presence, blind to None value |
| Phase-coverage credit from tool outcomes | ⚠ inline in core.py loop (core.py:638-644), not centralized |

## 1. Problem statement (confirmed)

Three actors disagree on what "the scenario was evaluated" means:

1. **dispatch_calc_scenario_evaluate** (inference/dispatch.py:719-843) —
   deterministic; refusal shape is `(None, {result: "ok", detail: {status:
   "refused", reason: …}})`. Null-discipline: refusal is a finding, not an
   exception.
2. **scenario_verdict** (engine/narration.py:288-329) — handles refusals
   correctly: `(inconclusive, "scenario unevaluated (…cause…)")`. Sound.
3. **validate_final_turn** (engine/narration.py:252-260) — demands a
   `calc.scenario.evaluate → …` evidence root unconditionally. Cannot
   distinguish called-and-refused from never-called.

**The live failure (4× scenario.evaluate thrash):** tool refuses
deterministically → loop does not credit P5 (correct) but DOES populate
`accumulated_tool_results["calc.scenario.evaluate"] = None` → model's final
is rejected for "missing citation" → repair steer never fires (key IS
present, value None) → model has no actionable guidance except re-calling a
tool that deterministically refuses → loop burns LLM turns until
`AGENTIC_MAX_LLM_TURNS`. Every wasted turn is ~34KB system prompt + state +
max_tokens budget spent on an unwinnable repair.

**Root cause (constitutional, not local):** no single actor owns the loop's
decisions. Coverage credit lives inline in the loop; refusal classification
lives in `scenario_verdict`; final-gate semantics live in
`validate_final_turn`; repair steering lives in prompt assembly. Three
semantic authorities, zero coordination. The LLM is structurally invited to
override a deterministic refusal.

## 2. Constitutional principle

**The deterministic controller is the cycle's sole authority. The LLM is a
subordinate executor: it proposes turns; the controller disposes.**

This mirrors the existing doctrine already in the codebase — memory
proposals are "LLM proposes; engine disposes" (core.py:850). Vector 3 is the
same principle, un-applied to tool outcomes. The spec extends
propose/dispose to the WHOLE loop:

| Concern | Current owner | Authority after this spec |
|---------|--------------|--------------------------|
| Tool-outcome classification (ok / refused / denied / error) | implicit, inline loop | **CycleController** (sole classifier) |
| Phase-coverage credit | inline loop (core.py:638-644) | **CycleController** (sole crediter) |
| "was the scenario evaluated?" | ambiguous (3 actors) | **CycleController** (tri-state, single answer) |
| Repair vs finalize vs degrade | inline loop | **CycleController.next_action()** |
| What a refused tool means for the final gate | nobody | **CycleController** → passed INTO the validator |
| Turn/round budget accounting | inline loop | **CycleController** (unchanged semantics, moved ownership) |
| Narration text / evidence / hypothesis | the LLM (proposed) | unchanged — proposals, never decisions |
| Deterministic verdict from tool payload | scenario_verdict | unchanged (it already agrees with the controller) |

`validate_final_turn` and `scenario_verdict` become **consultants**: pure
functions that receive controller-classified facts and return (passed,
missing[]). Neither interprets raw dispatch shapes again.

## 3. The contract

### 3.1 ToolOutcome ledger (engine/authority.py — new module)

```python
@dataclass(frozen=True)
class ToolOutcome:
    canonical: str            # registry name
    result: str               # capability_log result: 'ok' | 'denied' | 'error'
    refused: bool             # detail.status == 'refused' (null-discipline refusal)
    refusal_reason: str | None
    payload: Any              # result payload; None when refused/denied/error
```

Classification rules (the ONLY place these exist):
- `result == 'error'` → error outcome, not credited, payload None
- `result == 'denied'` → denied outcome, not credited, payload None
- `result == 'ok'` and `detail.status == 'refused'` → **refused**: not
  credited, payload None, refusal_reason carried
- `result == 'ok'` otherwise → data outcome, credited if TOOL_PHASE maps it,
  payload carried (null payload is still data — absence is information)

### 3.2 ScenarioEvalStatus tri-state

```python
class ScenarioEvalStatus(Enum):
    NOT_CALLED = "not_called"     # no dispatch this cycle
    REFUSED    = "refused"        # dispatched; deterministic refusal (terminal)
    EVALUATED  = "evaluated"      # dispatched; payload present
```

The controller answers `scenario_state()` from the ledger — one source,
identical answer for scenario_verdict, the validator, and the repair steer.

### 3.3 Controller API (pure over injected state; no I/O)

```python
class CycleController:
    def __init__(self, scenario: dict | None): ...
    def record_outcome(self, canonical: str, tool_log: dict, result: Any) -> ToolOutcome
    def credit(self, canonical: str) -> None          # phase coverage, controller-owned
    def scenario_state(self) -> ScenarioEvalStatus
    def scenario_refusal_reason(self) -> str | None
    def next_action(self, parsed_turn) -> Action       # DISPATCH | REPAIR(missing, steer) | FINALIZE | DEGRADE
```

`core.run_cycle` delegates: every dispatch goes through
`controller.record_outcome`; every coverage decision goes through
`controller.credit`; the loop's exit decision is `controller.next_action()`.
No new data sources — the controller owns state the loop already held
(capability_log entries, accumulated results, coverage sets, counters).

## 4. Semantic fixes (what changes in behavior)

### 4.1 Validator accepts refusal-citation (narration.py)

`validate_final_turn(parsed, coverage, scenario=None, *, scenario_status=None,
scenario_refusal_reason=None)`:

- `EVALUATED` (or legacy None → current behavior): citation requirement
  unchanged (`calc.scenario.evaluate → …` data path).
- `REFUSED`: the citation requirement is **satisfied by a refusal finding** —
  evidence entry with path `calc.scenario.evaluate → refusal`,
  interpretation naming the refusal reason. Additionally `scenario.verdict`
  must be `unevaluable` (reachable/not_reachable are FORBIDDEN without tool
  data — the LLM cannot override the null discipline). `required_ofi`,
  `exceedance`, `fit_status`, `n_windows_usable` echo requirements are
  waived (nothing to echo) and replaced by: rationale (≥80 chars) must name
  the refusal reason.
- `NOT_CALLED`: unchanged demand + the repair steer fires (see 4.2).

### 4.2 Repair steer correctness (core.py)

Current bug: `if scenario and "calc.scenario.evaluate" not in
accumulated_tool_results` — True-presence-with-None-value silences the
steer. Replaced by the controller:

- `NOT_CALLED` → steer: "call calc.scenario.evaluate with SCENARIO …"
- `REFUSED` → steer: "calc.scenario.evaluate ALREADY DISPATCHED and refused
  deterministically (reason: …). Do NOT re-call it. Cite the refusal as
  `calc.scenario.evaluate → refusal`, set verdict=unevaluable, name the
  reason in the rationale."
- `EVALUATED` → current behavior (echo the numbers).

### 4.3 Token economy — no LLM turns on unwinnable repairs

Controller rules that bound the thrash:

1. **Refusal is terminal per tool per cycle.** After `REFUSED`, no repair
   prompt ever requests that tool again; `next_action` classifies further
   identical dispatch requests as redundant (logged, not dispatched — the
   same anti-pattern-5 discipline the prompt teaches, now enforced
   structurally).
2. **No repair round whose missing items are all refusal-satisfied.** If the
   only gap is "cite the refusal", the controller sends AT MOST ONE repair
   turn (the refusal-citation steer above) and then finalizes on the next
   parseable turn even if the model's citation is imperfect — the
   validation record carries the honest `missing` list.
3. **Budget exhaustion with REFUSED** → the deterministic
   `scenario_verdict` (inconclusive, cause named) still lands, exactly as
   today; the difference is the controller stopped paying for repairs at
   rule 2 instead of burning to `AGENTIC_MAX_LLM_TURNS`.

### 4.4 What does NOT change

- The two-call gate, budgets, `_REQUIRED_PHASES`, TOOL_PHASE mapping,
  capability_log shapes, artifact contract, `scenario_verdict` cut points
  (narration.py:272-327), PG-first persistence, memory-proposal resolution.
- Scenario success path: `EVALUATED` behaves exactly as today.
- Non-scenario cycles: controller is transparent (scenario_state is
  NOT_CALLED-irrelevant; coverage credit rules identical).

## 5. File plan

| Pass | File | Change |
|------|------|--------|
| 1 | `engine/controller.py` (NEW) | ToolOutcome, ScenarioEvalStatus, CycleController |
| 1 | `engine/__init__.py` | export the three names (+ shims if tests want private forms) |
| 2 | `engine/narration.py` | `validate_final_turn` gains keyword-only `scenario_status` / `scenario_refusal_reason`; refusal-citation branch (4.1) |
| 2 | `engine/core.py` | loop adopts controller: record_outcome at dispatch sites, credit via controller, next_action for exit decisions, steer synthesis from controller (4.2, 4.3) |
| 3 | `tests/test_engine.py` | new AuthorityTests: refused→cite-passes, refused→no-re-call-steer, not-called→steer-fires, redundant-dispatch-suppressed, budget-bound |
| 3 | `tests/test_scenario.py` | refusal-path validator cases (unevaluable required, echoes waived) |

Estimated: 1 new module (~150 ln) + 2 file edits + 1 export block + 2 test
files. No contract changes, no artifact-schema changes, no dispatch changes.

## 6. Test matrix (acceptance)

1. `test_refused_scenario_cited_as_finding_passes` — dispatch refused once;
   final cites `calc.scenario.evaluate → refusal`, verdict=unevaluable →
   `final_validation.passed` True, `repairs` ≤ 1.
2. `test_refused_scenario_never_re_dispatched` — model re-calls after
   refusal; controller logs redundant, does not dispatch; capability_log has
   exactly ONE scenario.evaluate entry.
3. `test_not_called_steer_fires` — scenario never dispatched; repair prompt
   contains the CALL steer (regression for the silent-steer bug).
4. `test_refused_steers_citation_not_recall` — repair prompt after refusal
   contains "Do NOT re-call" and the refusal reason.
5. `test_scenario_success_unchanged` — existing test_scenario.py suite stays
   green without modification (EVALUATED path).
6. `test_budget_exhaustion_refusal_terminal` — refusal + model keeps
   re-calling; cycle ends ≤ 5 LLM calls (vs 8-12 live), verdict inconclusive
   with cause, `unexecuted_tool_calls` empty (controller suppressed them).
7. `test_non_scenario_cycle_identical_meta` — cycle_meta keys unchanged for
   autonomous cycles.

## 7. Pitfalls (from this codebase's history)

- **Do not credit refused calls into phase coverage.** The null-discipline
  rule (core.py:634-638 comment) is correct and stays: refusals are
  findings, not coverage. Only the FINAL-GATE semantics change (citation
  satisfied by the finding), not coverage credit.
- **Keep `scenario_verdict` untouched.** It already reads the
  refusal cause from capability_log and is the deterministic verdict
  authority. The controller feeds it the same ledger, not a new one.
- **The `_refused` classification must move verbatim** (detail.status ==
  "refused" on result "ok") — do not "fix" the shape; the dispatcher's
  ok-with-refused-detail contract is deliberate null discipline.
- **Preserve legacy signature compatibility**: `validate_final_turn` gains
  keyword-only args with None defaults; all 44 existing engine tests +
  scenario tests must pass unmodified (they exercise the None → legacy
  branch).
- **Do not let the controller parse LLM text.** narration.py keeps
  extract/coerce. The controller classifies TOOL outcomes only.
- **Gather-block interplay (deferred)**: pre-gather credit (Vector 4) will
  seed the controller's ledger with the six spine dispatches — the
  controller is the natural home for that seeding, but this spec does NOT
  include it; sequence after Vector 3 lands.

## 8. Out of scope (this spec)

- Vector 4 (pre-gather credit) — sequenced after, seeds the controller.
- Round-4 prompt-list dedup (kb.py ↔ narration.py PHASE_GUIDANCE).
- Any change to dispatch.py tool behavior, budgets, or the model contract in
  build_output_format.
