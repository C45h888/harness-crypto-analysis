# Nested Loop Spec — the in-depth workflow inside each loop state

Status: **workflow vocabulary and governance membrane implemented; runtime
alignment incomplete.** Vocabulary lives in `engine/loop_states.py`, legality
in `engine/fsm.py`, and semantics in `engine/controller.py` (under
`market_service/nooa_harness/`). `engine/membrane.py` has been deleted.

**Section 8 is the current P3b implementation contract.** Sections 5–7 below
record the original pass plan, not current implementation status; their
references to `run_cycle` and a deferred membrane are historical. The
canonical runtime entry point is `InferenceEngine.narrate_cycle`.

---

## 0. Where this sits

The agentic surface is two layers, deliberately separated:

| Layer | Owner | Sees | State |
|---|---|---|---|
| **1 — loop states (workflow)** | `engine/loop_states.py` | the hierarchical traversal + in-depth sub-loops | ✅ implemented |
| **2 — governance membrane (legality)** | `engine/fsm.py` | `(NestedLoop, TaskIntent, SubLoop)` observations | ✅ implemented; runtime alignment pending |

Layer 1 is now **four levels deep**, so every loop state carries an in-depth
workflow nested loop:

```
AgenticStage            the workflow position
  └── NestedLoop        the mode of work
        └── SubLoop     an in-depth workflow stage inside the loop
              └── LoopStep     a fine state inside the sub-loop
```

The **sub-loop is the primary agentic loop** the agent follows. Each
`NestedLoop` is a multi-stage workflow (2–4 sub-loops); each sub-loop is an
ordered step sequence with an optional exit condition. Iterating sub-loops
are the reason/act/observe cores, marked by `PRIMARY_SUBLOOP`.

Tasks are **intent-based** (`TaskIntent`), never P-numbers. `INTENT_LOOP`
binds an intent to its loop; `INTENT_SUBLOOP` binds it to the sub-loop that
serves it, giving the membrane in-depth visibility.

---

## 1. Terminology (binding)

- **Stage** — top-level workflow position (`AgenticStage`).
- **Nested loop** — one per stage (`NestedLoop`); the mode of work.
- **Sub-loop** — an in-depth workflow stage inside a loop (`SubLoop`); the
  primary agentic loop unit.
- **Loop step** — a fine state inside a sub-loop (`LoopStep`).
- **Task intent** — what the agent is establishing (`TaskIntent`).
- **Membrane** — Layer 2; observes `(NestedLoop, TaskIntent, SubLoop)` and
  authorizes transitions. It must **not** enumerate fine steps.

Composition rule: a sub-loop is entered from the parent loop, runs its
steps, and on exit hands control to the next sub-loop. The membrane sees
the loop + intent + sub-loop, never the internal step.

---

## 2. The in-depth loops

| Stage | Nested loop | Sub-loops (in order) | Primary |
|---|---|---|---|
| WAKE | `COMPREHENSION` | `INTAKE` → `INTERPRETATION` → `FRAMING` | `INTERPRETATION` |
| GATHER | `EVIDENCE` | `SOURCING` → `ACQUISITION` → `VERIFICATION` | `ACQUISITION` |
| REASON | `REASONING` | `HYPOTHESIS` → `ANALYSIS` → `SYNTHESIS` | `ANALYSIS` |
| CHECK | `VALIDATION` | `GATE` → `RECOVERY` | `RECOVERY` |
| FINALIZE | `OUTPUT` | `COMPOSITION` → `ARCHITECTURE` → `PLACEMENT` → `MEMORY` | `PLACEMENT` |

### COMPREHENSION (WAKE) — understand the prompt
- **INTAKE** — `RECEIVE → DECODE → NORMALIZE`. Receive the raw input.
- **INTERPRETATION** *(iterates)* — `CLASSIFY_INTENT → EXTRACT_CONSTRAINTS
  → IDENTIFY_QUESTIONS`. Exit: the intent and its questions are unambiguous.
- **FRAMING** — `DECOMPOSE → SELECT_EVIDENCE_NEEDS → SET_ACCEPTANCE`.

### EVIDENCE (GATHER) — acquire data
- **SOURCING** — `PLAN_READS → SELECT_TOOLS`.
- **ACQUISITION** *(iterates)* — `READ → CALCULATE → OBSERVE`. Exit: every
  planned read/calculation has been observed.
- **VERIFICATION** *(iterates)* — `ASSESS → REPLAN`. Exit: coverage
  satisfies the intent or budget spent.

### REASONING (REASON) — reason prompt data against tool data
- **HYPOTHESIS** — `FRAME → STATE_PRIOR`.
- **ANALYSIS** *(iterates)* — `TEST → COMPARE`. Exit: hypothesis settled or
  refinement stops adding signal.
- **SYNTHESIS** — `INTERPRET → RECONCILE`.

### VALIDATION (CHECK) — check the final
- **GATE** — `VALIDATE → CLASSIFY_FINDINGS`.
- **RECOVERY** *(iterates)* — `DIAGNOSE → STEER → REVALIDATE`. Exit: the
  final passes or the repair budget is spent.

### OUTPUT (FINALIZE) — place the output
- **COMPOSITION** — `ASSEMBLE → GROUND`.
- **ARCHITECTURE** — `UNDERSTAND_SURFACE → LOCATE_OUTPUT`. **The agent must
  understand the agentic surface plane architecture and locate where its
  output belongs inside it.**
- **PLACEMENT** — `PERSIST → CONFIRM`. Place the output.
- **MEMORY** *(iterates)* — `PROPOSE → RESOLVE → SETTLE`. Exit: every
  proposal resolved and the cycle settled.

Placing output without understanding the surface it is placed into is a
drift vector; the `ARCHITECTURE` sub-loop is structural, not advisory.

---

## 3. Why the final loop is OUTPUT, not PERSISTENCE

The final state for the agent is to **place the output where it understands
the architecture of the current agentic surface plane**. Persistence alone
would place bytes without grounding them in the surface. `OUTPUT` composes,
understands, places, and remembers — and `ARCHITECTURE` is where the agent
builds the understanding the placement depends on.

---

## 4. Invariants (enforced at import by `self_check`)

1. `STAGE_ORDER` lists every stage exactly once; `STAGE_LOOP` is injective.
2. `LOOP_SUBLOOPS` covers every loop; each loop has ≥ 2 sub-loops, no
   duplicates within a loop.
3. `SUBLOOP_SPECS` covers exactly the sub-loops used by loops; each spec has
   ≥ 2 steps, no duplicates within a spec.
4. Every `LoopStep` is reachable (appears in some sub-loop).
5. `PRIMARY_SUBLOOP` maps every loop to one of its own sub-loops.
6. `INTENT_LOOP` covers every intent; `INTENT_SUBLOOP` covers every intent
   and its sub-loop belongs to the intent's loop.
7. Every loop serves ≥ 1 intent.
8. Success and failure terminals partition `LoopTerminal`.

Every iterating sub-loop names its exit condition (also enforced).

---

## 5. What is still deferred

### 5.1 Governance membrane (Layer 2)
Observes `(NestedLoop, TaskIntent, SubLoop)` and authorizes transitions
between loop/sub-loop activations and task advances. It must not enumerate
`LoopStep`. Roadmap names (must not reuse the deleted `AgentLoopFSM` /
`LoopState` / `LoopEvent`): `AgenticLoopMembrane`, `GovernanceEvent`.

### 5.2 Runtime iteration
The vocabulary defines the loops; the runtime does not yet *drive* them.
The existing `run_cycle` control flow still runs the staged loop directly.
Wiring `LoopObservation` through the loop and the `CycleController` is the
next pass, followed by the membrane.

### 5.3 Resumability (open)
`LoopObservation` is frozen and serializable (three enum values). If the
loop must resume from persisted state, sub-loop positions must become
explicit serializable values and the membrane must be pure/deterministic.
Decision deferred to the membrane pass.

---

## 6. Pass plan (sequencing contract)

| Pass | Deliverable | Depends on |
|---|---|---|
| done | Layer-1 vocabulary + in-depth sub-loops (`loop_states.py`) + this spec + old-name deletion | — |
| next | wire Layer 1 into `run_cycle`; controller carries `LoopObservation` | done |
| next+1 | Layer 2 membrane (`AgenticLoopMembrane`) over `(NestedLoop, TaskIntent, SubLoop)` | controller pass |
| next+2 | runtime iteration of sub-loops (enter/exit, budgets, escalation) | membrane pass |

A pass is done when its exit condition exists and reproduces, not when code
merges.

---

## 7. Non-goals (this pass)

- No membrane code.
- No change to `run_cycle` control flow.
- No change to tool dispatch, budgets, or artifact contracts.
- No reintroduction of the deleted `LoopState` / `LoopEvent` /
  `AgentLoopFSM` vocabulary.

---

## 8. P3b — safe extraction and canonical runtime alignment

Status: **P3b-A implemented and verified; P3b-B pending its design gates**.
The four extracted methods are now called by `narrate_cycle`, with explicit
cycle-local handoffs. Six characterization cases pin artifacts, metadata,
prompts and ordered side effects. Stage-order and early-return tests pass.
Full-suite checkpoint: **785 passed, 1 skipped, 4 warnings, 7 subtests passed**.
All pre-existing methods outside `narrate_cycle` were verified unchanged
against a full working-file backup. This section supersedes the historical
pass sequencing above: method boundaries alone do not make nested loops
operational.

### 8.1 Authority and scope

- `loop_states.py` alone defines state vocabulary and workflow mappings.
- `fsm.py` alone decides transition legality and maps failure kinds to terminals.
- `controller.py` owns observations, semantic completion decisions, budgets and
  outcome classification, subject to the membrane.
- `core.py` executes authorized work; the LLM proposes work and interpretations.
- `_CycleContext` holds cycle-local data, not a second state machine.

Use `STAGE_ORDER` / `STAGE_LOOP` for stage-to-loop traversal,
`LOOP_SUBLOOPS` for sub-loop order, `SUBLOOP_SPECS` for steps and iteration
shape, and `INTENT_LOOP` / `INTENT_SUBLOOP` for intent ownership. Handler
registries may associate existing enum members with implementation functions;
they must not duplicate workflow order or define alternative state names.
`PRIMARY_SUBLOOP` identifies the core work, not permission to skip siblings.
In particular, OUTPUT's primary PLACEMENT sub-loop is not marked iterating:
`SUBLOOP_SPECS[sub_loop].iterates` controls iteration, not primary status.

### 8.2 Verified runtime gaps to resolve

1. COMPREHENSION is initialized but its sub-loops are not executed explicitly.
2. **Bounded fix implemented before extraction:** the runtime previously
   closed SOURCING before opening ACQUISITION, resetting the position and
   causing denial. It now advances directly from SOURCING to ACQUISITION
   before the two pre-gate reads. An E2E regression verifies the allowed
   transition and the observation at actual dispatch. General completion,
   denial handling and feedback semantics remain P3b-B work.
3. Tool rounds run inside REASONING, but some phase-derived intents belong to
   EVIDENCE. Those task transitions can be denied while dispatch continues.
4. Most REASONING and OUTPUT sub-loops are not traversed. Repeated validation
   requests also need an explicit recovery lifecycle rather than re-entering
   the same parent loop through a successor-only transition.
5. `_govern` can silently leave the observation unchanged on denial. A logged
   boundary is therefore not proof that the corresponding work was authorized.
6. SETTLE is currently requested before persistence and memory disposition.
   `_persist` returns backend success flags, but the caller ignores them.
7. Later narration/parse failures fall into fallback output and may be labelled
   budget exhaustion. This differs from first-turn failure routing.

These are behavior changes, not mechanical extraction fixes. Keep their
implementation and review separate from the parity-preserving extraction.

### 8.3 P3b-A: behavior-preserving extraction

Keep `narrate_cycle(wake, wake_meta, task=None, scenario=None)` and its return
contract unchanged. Extract four private methods:

| Method | Exact existing-code seam | Vocabulary it hosts |
|---|---|---|
| `_stage_wake` | timestamp, wake log, governed controller initialization; stop before pre-gate reads | WAKE / COMPREHENSION |
| `_stage_gather` | two pre-gate reads, ledger, deterministic state and hard-gate early return; stop before memory recall | GATHER / EVIDENCE |
| `_stage_reason_and_check` | memory recall, prompts, initial narration, tool rounds, validation/repair and final-turn bookkeeping; stop before interpretation assembly | REASON / REASONING and CHECK / VALIDATION |
| `_stage_output` | interpretation/fallback, hypothesis, calculations, terminal, artifact persistence and memory disposition | FINALIZE / OUTPUT |

The combined reason/check method is a feedback coordinator, not a new stage.
During P3b-A these associations identify implementation seams; they do not
claim that all nested sub-loops are already operational.

The orchestrator creates a context, calls gather, propagates any completed
result, calls reason/check, propagates any completed result, then calls output.
Gather and reason/check return `None` to continue or the existing artifact/meta
tuple to finish. Failure-path persistence stays in its existing branch.

`_CycleContext` contains only cross-boundary inputs, timestamp, controller,
capability log, gate evidence/status/reasons, deterministic state, result
ledgers, first/final parsed turns, counters, validation and unexecuted calls.
Prompts, raw responses, per-tool scratch values and output-assembly locals
stay local. Required inputs have no dummy defaults; mutable fields use
factories. Controller successors always rebind the context's controller.
Do not put the context on the engine instance or duplicate its observation.

Preserve exact prompts, call order, tool arguments, coverage credit, budgets,
exception boundaries, early returns, terminal values, artifact/meta shapes,
and persistence/memory ordering. This includes existing quirks in 8.2 until
explicitly changed in P3b-B.

### 8.4 P3b-B: make the declared nested loops execute real work

After extraction parity is green, bind executable work to the existing shape:

| Nested loop | Required runtime work in declared sub-loop order |
|---|---|
| COMPREHENSION | INTAKE receives/normalizes the envelope; INTERPRETATION records task, constraints and unresolved questions; FRAMING sets evidence needs and acceptance criteria. Do not invent missing user constraints. |
| EVIDENCE | SOURCING selects proposed reads; ACQUISITION executes authorized reads/calculations and records observations; VERIFICATION assesses coverage and identifies gaps. The insufficient-data gate must still consume zero LLM calls. |
| REASONING | HYPOTHESIS records the proposed hypothesis/prior; ANALYSIS tests against observed evidence; SYNTHESIS reconciles findings. Missing evidence requests must reach a legally authorized evidence activation, not dispatch under a denied intent. |
| VALIDATION | GATE validates and classifies findings; RECOVERY diagnoses, steers and revalidates until passing or a bounded stopping condition. Successful first-pass validation records recovery as unnecessary, not fictitious repair work. |
| OUTPUT | COMPOSITION assembles/grounds output; ARCHITECTURE resolves the existing artifact contract and configured Postgres/Redis destinations; PLACEMENT persists and checks receipts; MEMORY disposes proposals before final settlement. The LLM cannot choose arbitrary persistence destinations. |

Every activation needs a real handler, observable result and controller-owned
completion predicate. Step execution follows `SUBLOOP_SPECS.steps`; the
membrane continues to observe loop/sub-loop boundaries, not fine steps.
Human-readable `exit_condition` strings are specifications, not executable
predicates. Iterating handlers must have explicit progress and finite budget
checks; do not add unbounded loops or extra LLM calls merely to tick states.
An inapplicable step must have an explicit disposition, not fabricated work.

Legality is checked BEFORE governed work. Denial must prevent that dispatch
or boundary action and produce an explicit diagnostic/recovery decision.
No forced observation assignment, silent auto-advance or no-op event parade
may make a trace appear aligned.

### 8.5 Design gates before runtime-alignment implementation

The following contracts must be resolved with tests before P3b-B changes:

1. **Progress and re-entry:** the observation currently cannot distinguish a
   newly entered loop from one whose last sub-loop was closed. Define progress
   tracking and completion/skip semantics before enforcing whole-loop exit.
   The current OPEN operation can advance directly to the next sub-loop;
   closing between siblings loses that position. Do not patch this with
   repeated close/open calls. Any new state/progress vocabulary belongs in
   `loop_states.py`; legality remains in `fsm.py`.
2. **Evidence feedback:** successor-only parent traversal cannot represent
   REASONING requesting EVIDENCE again. Define bounded suspend/resume or
   explicit feedback edges in Layer 1, derive legality in Layer 2, then let
   the controller choose requests semantically. Do not loosen every edge or
   reclassify evidence intents merely to avoid denials.
3. **Completion semantics:** define an executable predicate for every
   iterating sub-loop, including ambiguous comprehension and analysis with no
   further signal. P1–P6 tool credit stays unchanged and separate from these
   predicates; it is not the loop-position authority.
4. **Settlement and durability:** choose the required persistence receipt
   policy, memory-write failure disposition and terminal-recording protocol.
   An artifact is currently written with its terminal already populated;
   moving settlement after confirmation requires an explicit final update or
   equivalent protocol. Never claim SETTLED after an unconfirmed required
   write. If all stores fail, return/report the failure honestly rather than
   claiming a durable failure artifact exists.
5. **Failure precedence:** distinguish narration, parse, unrepaired validation,
   budget and infrastructure failures through controller classification and
   membrane routing. Specify what wins when multiple conditions apply; do
   not infer budget exhaustion from every loop break.

These are required design decisions, not permission for unrelated redesign.
Legacy controllers with governance disabled retain their compatibility
contract. Transition-count expectations may change only in the reviewed
runtime-alignment gate, not in extraction.

### 8.6 Implementation and verification gates

1. Back up the COMPLETE working `core.py` with a checksum and record the diff;
   do not reconstruct WIP from HEAD or rely on a method-only snapshot.
2. Characterize artifact/meta content, prompts, tool calls, governance
   verdicts, persistence and memory order against the unextracted runtime.
   Freeze IDs/time or normalize only identified nondeterministic fields.
3. Extract output, then wake/gather, then reason/check using bounded edits.
   Inspect each diff and run characterization plus the full suite each time.
   No whole-file AST regeneration or global local-variable rewrite.
4. Resolve 8.5, then implement runtime alignment one nested loop at a time.
   Add approved behavior-change assertions without rewriting baseline tests
   just to make failures disappear.
5. Add mapping/handler coverage tests derived from Layer 1; runtime trace
   tests must couple allowed boundaries to actual work, not only enum visits.
   Cover success, zero-token refusal, repair, evidence feedback, budget,
   narration/parse failures, persistence failures and memory dispositions.
6. Verify denied transitions execute no corresponding work; verify terminal
   agreement between returned metadata and any successfully persisted final
   deterministic state; verify concurrent cycles do not share context.
7. Keep a structural guard: scratch `membrane.py` stays absent; controller and
   core use the canonical `fsm.py` authority. This guard alone does NOT prove
   runtime alignment.

**Completion:** P3b-A finishes the safe extraction. The amended P3b is complete
only when P3b-B drives real nested-loop work through the canonical mappings,
all design gates are resolved, and the full suite is green. Resumability,
arbitrary new tools and new persistence destinations remain out of scope.
