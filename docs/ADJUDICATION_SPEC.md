# Adjudication Spec — probabilistic hypothesis weighing in validation (slices 1–3)

Status: SPEC (not yet implemented). Combines contract + prompts + checker with the
reasoning-close memory write that feeds them. Slices 4 (output injection) and 5
(trace tests) follow separately.

## 0. Objective

Validation must do more than gate citations: the agent assigns a probabilistic value
to each competing hypothesis, chooses by a deterministic rule, and records the
verdict. The flow is memory-mediated end to end:

```
reasoning close → verdict-basis memory write (frame + truth + stage ids)
  → validation reads the write, infers against receipts + observations
  → verification reads (memory.recall over the task segment + accumulated re-read)
  → probabilistic analysis vector (per-hypothesis probabilities + choice)
  → deterministic checker gates coherence (never truth)
```

Probabilities INFORM, never EXECUTE — same standing as `p<0.05` today. No probability
value may gate a tool, a retry, or a terminal.

## 1. Candidates (fixed set, no invention)

- The frame's **H0 / H1** (`chain.build_hypothesis_frame`, basis logged on
  `engine.hypothesis.frame` at reasoning entry).
- The **scenario verdicts** (`reachable / not_reachable`) when a scenario is bound.
- `unevaluable` (refused tool) **bypasses weighing** by construction: no distribution
  exists, nothing to weigh. Keeps the current refusal path byte-identical.

## 2. Slice 1 — contract (`engine/schemas.py` + prompt shapes)

New optional turn field `hypothesis_probabilities`:

```json
"hypothesis_probabilities": {
  "H0": {"probability": "low|medium|high", "p": 0.0–1.0 | null,
         "basis_path": "<tool> → data.<field>", "basis_value": "…"},
  "H1": {"probability": "low|medium|high", "p": 0.0–1.0 | null,
         "basis_path": "<tool> → data.<field>", "basis_value": "…"},
  "chosen": "H0|H1",
  "tiebreak": "… | null"
}
```

- `probability` (enum) REQUIRED for every live candidate; `p` (numeric) OPTIONAL.
- `basis_path` must resolve inside `accumulated` (same resolution as the echo check:
  head + field segments; `comparator.*` vs top-level kept disjoint).
- `chosen` REQUIRED. `tiebreak` required iff both enums equal.
- `NarrationTurn` gains the field (contract parity — the structured probe must carry
  every key the validator reads, per the existing drift test).
- Prompt shapes (`FINAL_SHAPE_BLOCK`, `build_output_format`) gain the block; the stale
  `ground in recalled paper facts` sentence is replaced with grounding in the
  distribution + frame.

## 3. Memory write at reasoning close (feeds the reads)

At `run_reasoning` exit (alongside the refined-observation store), the engine writes a
**verdict-basis** observation to the task segment (best-effort, same transport
pattern as base/refined stores):

```json
{"kind": "observation", "tags": ["reasoning", "verdict-basis"],
 "segment": "<task_id>",
 "content": {"frame": {"H0","H1","basis","prompt_bearing"},
             "truth": {"forecast_y_pred","forecast_regime","expected_delta",
                       "pi_95","p_ge_zero","fit_status","mean_ofi","mean_ad","n_pairs"},
             "stage_ids": [...], "directive_kind": "..."}}
```

- Reuses `observation` + tags (no MemoryNode kind migration).
- `engine.hypothesis.frame` (entry, prompt basis, empty data) is its provenance
  ancestor; this write is the data-filled successor.
- New chartered carrier field carries its id (`st` already charters
  `base_summary`-style fields; same pattern). Failure to write never blocks:
  validation falls back to recomputing the frame from task + accumulated
  (pure `build_hypothesis_frame`), and the fallback is logged.

## 4. Validation reads + inference (no new dispatches)

On validation entry (first `run_validation` pass, before judging):

1. `memory.recall(session_id, segment=task_id)` — picks up understanding / plan /
   handoff / base / refined / verdict-basis. Rendered as a bounded
   `ADJUDICATION MEMORY` block (ids cited, full text early only — same
   early-full/late-ids pattern as the reasoning entry block).
2. Accumulated re-read — proof payloads already on the carrier. **No tool
   re-calls**: identical-args re-dispatch is structurally suppressed
   (`is_redundant`), so verification reads are receipt/observation reads by
   design, not a limitation to work around.
3. The agent infers the verdict-basis against receipts and observations and emits
   the probabilistic vector on its repair/forced-final turn.

`memory.remember/recall` are already authorized from `validation/gate` and
`validation/recovery` (`_MEMORY_HOMES`, charter-audited) — no governance change.

## 5. Slice 2 — prompts (repair + forced-final + shapes)

New `ADJUDICATION BLOCK` composer (pure, `kb.py`, beside the DATA CHECKS builder):

```
ADJUDICATION (weigh, then choose — probabilities inform, never execute):
  candidates: H0 / H1 (+ scenario verdicts when bound; unevaluable bypasses)
  evidence pointers: distribution P(≥θ)/P(≥0) + PI95 | forecast Y/calibration/regime
    | fit status/r2/OOS | decay finalized horizons | base means/n_pairs
  memory: <ADJUDUCATION MEMORY block: verdict-basis + stage ids + receipts>
  rules: enum per live candidate (low|medium|high) + optional numeric P;
    basis_path must be the exact tool+field read (comparator.* = baseline,
    top-level = multivariate — never swap); chosen = argmax, ties need tiebreak;
    deviation from argmax needs a named refused-tool finding.
```

Injected into `compose_repair_prompt` and `compose_forced_final_prompt` (which also
gains DATA CHECKS + frame + digest/proof legend — the already-owed repair upgrade),
plus the `FINAL_SHAPE_BLOCK` replacement from §2. No new turns, no budget change
(validation keeps 1 + 2 bounded retries; backstop 30 untouched).

## 6. Slice 3 — deterministic checker (`validate_final_turn`)

After the echo check, over `hypothesis_probabilities` + `accumulated`:

1. **Presence**: enum for every live candidate (scenario-bound adds the verdict
   pair; refused paths exempt).
2. **Basis resolution**: every `basis_path` resolves (head + field segments,
   comparator-disjoint); `basis_value` equals the payload (Decimal-normalized —
   same `_norm`, same tolerance as echo).
3. **Bucket coherence**: enum against evidence buckets derived deterministically
   from accumulated (distribution `P(≥θ)` bands, forecast calibration state, fit
   status tier, decay horizons). A `high` on H1 beside `P(≥θ)=0.02` fails.
   Buckets are coarse by design (3 enums × broad bands) — the checker rejects
   gross incoherence, never close calls.
4. **Choice rule**: `chosen` == argmax of (enum rank, numeric tiebreak); ties
   require `tiebreak` citing evidence; deviation requires naming a refused-tool
   finding already on the ledger.
5. Failures join `missing[]` with field-named messages and ride the repair steer.
   The checker verifies COHERENCE (choice follows probabilities follows tools),
   never truth — a wrong-but-coherent vector passes, by explicit scope decision.

`GATE` spec gains `LoopStep.ADJUDICATE` between `VALIDATE` and `CLASSIFY_FINDINGS`
(membrane-blind steps — spec-clarity edit, zero legality change). No new sub-loop,
no FSM matrix change.

## 7. Invariants (must hold at every slice)

- I1: probabilities never gate tools/retries/terminals (grep-able: no reader outside
  the checker + prompts).
- I2: no new validation turns, no budget movement (1 + 2 retries; 30 backstop).
- I3: no FSM/charter-crosswalk change (memory homes already authorize gate/recovery).
- I4: no new memory kinds, no new `deterministic_state` roots without charter edit
  (verdict-basis rides `observation` + tags; any frozen summary joins the charter).
- I5: unevaluable bypass stays airtight (refused paths never enter weighing).
- I6: suite green at every slice (checker matrix + walk updates + fixture regen as needed).

## 8. Explicit non-goals (this spec)

- Output injection of the verdict receipt (slice 4) and trace-shape tests (slice 5).
- Numeric-required probabilities; cross-loop probability reuse; probability-driven
  retries or terminals.
- Tape re-reads in validation (structurally suppressed — §4 is the final word).

## 9. Build order

1 → contract + shapes (schemas, prompt-shape text, parity test).
2 → verdict-basis write at reasoning close + validation recall block (carrier field,
  transport pattern, fallback recompute).
3 → adjudication prompt block + repair/forced-final injection (+ owed repair upgrade).
4 → checker + GATE `ADJUDICATE` step + missing-message wording.
5 → green suite (unit matrix: incoherent / tied / refused-bypass / swapped-fields;
  walk: probabilities + choice + settled).
