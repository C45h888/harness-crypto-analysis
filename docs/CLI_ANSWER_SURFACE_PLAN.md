# CLI Answer Surface (C2) — integration plan

Status: **LANDED (S1–S4 + the canonical-output pivot).** The answer is
composed by the pure composer, delivered at PLACEMENT (deliver + confirm),
and kept as the frozen answer of record in the `canonical_output` row
(contract `runtime/contracts.CanonicalOutput`, migration 0017). The old
`inference_artifact` **as-output** logic is deprecated: writes stopped,
legacy rows read-only, every engine reader migrated.

> **SUPERSEDING DECISION (canonical output plane).** Decision 1 evolved:
> the answer does not ship as a file produced FOR the consumer, but it IS
> persisted as the **canonical output of record** in Postgres (frozen text +
> structured variant) and given to the CLI surface — the deliverable is
> still the answer inside the CLI; the row is its immutable record. §8
> gates resolved: (1) run-record retention — KEEP (frozen text + structured);
> (2) delivery channel — CLI stdout at close; (3) `delivery_failed` —
> added as `FailureKind.DELIVERY_FAILED` + `LoopTerminal.DELIVERY_FAILED`
> (INFRA/PERMANENT → TERMINATE), flagged to the failure-FSM owner.
Builds on the relaxed OUTPUT loop (frame / authored gate / correction turn)
that landed green (full suite 1355 passed). Companion to
`NESTED_LOOP_SPEC.md` §2 (OUTPUT loop) and the authored-output seam in
`market_service/nooa_harness/engine/core/{output,synthesize}.py`.

> **DECISION RECORD (binding).** The six next-surface decisions are settled:
>
> 1. **C2 — answer-only.** The cycle's deliverable is the agent's authored
>    answer returned **inside the CLI**. It does NOT ship as an output
>    file/artifact. This is a doctrine change to PLACEMENT/SETTLE (§3).
>    (C1 — artifact-as-internal-record — was assessed and rejected: the
>    answer is the product; a persisted output file is not.)
> 2. **CLI surface: both ends.** Render at cycle close (the operator sees
>    the answer when the run settles) AND a `nooa` subcommand re-render for
>    past runs.
> 3. **Answer format: plain prose with embedded structured blocks.**
>    Narrative in the agent's own words; the gate-data compulsion rendered
>    as compact structured blocks. The machine JSON contract remains
>    available as the run record, never as the deliverable.
> 4. **Memory window: M1, recall-only in the collapse turn.** The exact
>    guard at ASSEMBLE: `memory.*` paths are never citable evidence.
>    **(LANDING-READY — already implemented and tested.)**
> 5. **Flagged-ship shape: a footer block, never inline.** `OUTPUT
>    CONFORMANCE` footer carries `hard_misses` / `conflicts` /
>    `quality_findings` / `correction_used` from
>    `deterministic_state["conformance"]`. Inline flags would touch the
>    agent's prose; a footer keeps "nothing rewritten" literally true.
> 6. **ΔP conditionality trigger: `task_directive.targets/invalidations`.**
>    **(LANDING-READY — already implemented and tested.)**

**Methodology (the one line):** the strict validation loop decides what is
true; the frame carries it; the agent, using its own memory, authors the
answer around it; the relaxed loop compels the gate data and returns the
answer to the CLI — determinism underneath, agent voice on top, nothing
rewritten behind the agent's back.

---

## 1. Goal

The cycle ends with **the answer**, delivered to the operator through the
CLI surface — plain, authored, and compelled to carry all gate data. No
output file is produced for the consumer. The engine's bookkeeping
(receipts, ledger, capability log) remains engine-internal.

## 2. The answer object

Composed **from the authored output that ships** (already gate-checked and
recorded by the relaxed loop) — the composer renders, it never authors.

```
┌─ ANSWER (plain prose, agent's words verbatim) ────────────────────────┐
│ summary                    — the agent's P4 narrative, as written     │
│ evidence interpretations   — each reading's meaning, as written       │
│ scenario rationale         — as written                               │
│ model_separation / limitations — as written                           │
├─ GATE DATA (structured blocks; the compulsion list) ──────────────────┤
│ H0 / H1                    — hypothesis framing (carried)             │
│ adjudication               — hypothesis_probabilities H0/H1          │
│                            — probability + basis_path, chosen, tiebreak│
│ scenario                   — verdict + target/horizon + the cited     │
│                            — calc.scenario.evaluate / forward paths   │
│ directive answer           — targets/invalidations verdict or refusal │
│ ΔP block                   — calc.price.delta → … (ONLY when the      │
│                            — directive bears targets/invalidations)   │
│ refusals                   — findings with reasons (never repaired)   │
│ confidence                 — low|medium|high                          │
├─ OUTPUT CONFORMANCE (footer; never inline) ───────────────────────────┤
│ hard_misses[]   — what the relaxed gate could not compel (flagged ship)│
│ conflicts[]     — carried-semantics mismatches (recorded, not rewritten)│
│ quality_findings[] — thin summary / single-root / thin rationale …    │
│ correction_used — whether the agent fixed its own output this cycle   │
│ terminal        — settled | flagged | …                               │
└───────────────────────────────────────────────────────────────────────┘
```

Completeness rule: the answer composer's contract IS the compulsion list —
every hard-tier item of `check_authored_output` must appear as a block or
as an explicit refusal/missing line in the footer. A block the agent left
empty renders as `missing (flagged)` — the renderer never fills it.

## 3. C2 doctrine change (PLACEMENT / SETTLE)

Today (`core/output.py::run_output`): PLACEMENT = `InferenceArtifact.create`
→ `_persist` (PG/Redis) → `_finalize_persisted` receipts → SETTLE gated on
durable writes. The artifact is the deliverable.

Under C2:

| Concept | Today | C2 |
|---|---|---|
| Deliverable | persisted `InferenceArtifact` | the **answer**, emitted to the CLI/interaction-plane sink |
| PLACEMENT | persist + confirm | **deliver + confirm** (the answer reaches the sink) |
| SETTLE precondition | durable PG/Redis receipt | answer-delivery confirmation + engine receipts recorded |
| `InferenceArtifact` | output contract | **run record** — engine-internal bookkeeping only (receipts/ledger/capability), never the consumable output |
| Failure mapping | `infra_failed` on missing durable receipt | delivery failure is terminal (`delivery_failed`), honest and distinct from persistence failures |
| Memory-less / store-less deployment | cannot settle | **can settle** (the answer is the product); the run record persists only when stores exist |

Doctrine ripples to update in the same pass:
- `NESTED_LOOP_SPEC.md` §2/§3 — OUTPUT sub-loops: PLACEMENT semantics
  ("place the output") become "deliver the answer"; ARCHITECTURE resolves
  the *surface* (CLI/interaction plane), not Postgres/Redis destinations.
- `LOOP` receipt wording — PLACEMENT/CONFIRM steps record delivery, not
  persistence.
- `STATE_CHARTER` — the run record's keyspace is unchanged (conformance,
  loop_receipts, ledger, …); only its ROLE changes (internal, not output).

**Explicit non-goal:** deleting the persistence layer. PG/Redis remain the
run-record stores; what stops is treating their row as *the output*.

## 4. CLI surface points (decision 2)

1. **Cycle close** — `nooa market inference run …` prints the answer block
   when `narrate_cycle` returns. The command's stdout becomes the answer
   surface (today it prints metadata).
2. **Re-render subcommand** — `nooa market inference answer <symbol|run>`
   renders the answer from the run record of a past cycle (the run record
   still carries the authored interpretation + conformance, so re-rendering
   is exact).

Both render from ONE pure composer (single source of truth for format).

## 5. Seams and files (plan-level)

| Seam | File | Change |
|---|---|---|
| answer composer (pure) | `nooa_harness/…/answer.py` (new, pure) | `compose_answer(authored_interpretation, gate_data, conformance, run_meta) -> str` + structured variant |
| cycle-close emission | `engine/core/output.py::run_output` | after settlement logic: deliver the answer to the sink; delivery result feeds SETTLE |
| delivery sink | `engine/…` transport + `interaction_plane` | CLI stdout channel; confirmation = the render returned without error |
| persistence re-scope | `engine/core/driver.py::_persist / _finalize_persisted` | demote to run-record writes; their failure no longer blocks settlement when delivery succeeded (recorded as `run_record_errors`) |
| CLI | `market_service/commands/nooa_cli.py` | print at close + `answer` subcommand |
| terminals | `engine/failure_fsm.py` + `loop_states.LoopTerminal` | add `delivery_failed` (only new vocabulary item — a FAILURE terminal, reviewed with the FSM owner) |

## 6. Implementation slices (each independently green)

1. **S1 — answer composer (pure) + unit tests.** No behavior change. Pins
   the format: prose blocks verbatim, gate-data blocks complete against the
   compulsion list, footer from `conformance`, missing = explicit.
2. **S2 — cycle-close render + `answer` subcommand.** Additive: the answer
   prints beside the existing behavior; C1-shaped on the inside, C2-shaped
   for the consumer. Verifier: CLI tests (stdout shape), re-render parity.
3. **S3 — the C2 switch.** PLACEMENT → deliver-and-confirm; SETTLE
   precondition changes; persistence demoted to run record; `delivery_failed`
   terminal. This is the doctrine slice: characterization fixture migration
   (the pinned `["postgres","redis","redis","redis"]` effect sequence and
   artifact-shape assertions change BY DESIGN), receipt wording, spec edits.
4. **S4 — cleanup + doctrine docs.** NESTED_LOOP_SPEC §2/§3 amendment,
   receipt semantics, retire any answer-as-artifact paths in tests.

Gate between slices: full suite green; S3 additionally requires the
characterization fixture to be regenerated and reviewed line-by-line (it is
the behavior-change ledger for this repo).

## 7. Test / verification plan

New:
- composer unit tests (format, compulsion completeness, footer honesty,
  verbatim prose — no engine-authored text anywhere).
- delivery failure → `delivery_failed` terminal, answer preserved in the
  run record, honest `final_validation`.
- store-less deployment settles (answer-only).
- re-render parity: CLI close output == `answer` subcommand output for the
  same run.

Changed (by design, in S3):
- `test_cycle_characterization` — effect sequence + artifact role.
- `test_loop_receipts` — PLACEMENT/CONFIRM receipt payload wording.
- persistence tests — run-record semantics.

Unchanged (must stay green):
- the whole VALIDATION loop surface (strictness untouched),
- the relaxed OUTPUT loop tests (`test_output_authoring.py`,
  `test_synthesize.py`) — the answer layer renders what they pin.

## 8. Open decision gates (resolve at S3 review, not before)

1. **Run-record retention of the answer:** keep the authored interpretation
   inside the run record (audit + re-render parity; recommended) or strip it
   (purist C2: answer exists only at delivery). Recommendation: keep — the
   `answer` subcommand needs it, and it is internal either way.
2. **Delivery channel:** stdout at CLI close vs interaction-plane channel
   first. Recommendation: stdout (the CLI IS the surface named in the
   decision), interaction-plane wiring later.
3. **`delivery_failed` vocabulary:** new `LoopTerminal`/`FailureKind` members
   — confirm names with the failure-FSM owner (the parallel workstream is
   active in `failure_fsm.py` territory).

## 9. Non-goals

- No changes to the strict VALIDATION loop, the frame, the authored gate,
  or the correction turn (landed this pass).
- No new agentic states: the answer layer is composer + delivery + receipt
  wording on the EXISTING OUTPUT sub-loops (PLACEMENT's `PERSIST→CONFIRM`
  becomes `DELIVER→CONFIRM` semantically; the vocabulary owner decides
  whether the step names move).
- No format for machine consumers beyond the run record JSON (already
  exists).
