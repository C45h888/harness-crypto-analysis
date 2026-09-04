# Inference Engine — Memory Protocol

The engine's memory is two-layered. Numbers are facts; prose is episodic.
The LLM narrating a cycle never writes memory directly — it PROPOSES, and
deterministic engine code DISPOSES. This protocol is compiled into the
narration prompt every cycle.

## Layer 1 — numeric feature store (deterministic, not recallable prose)

Prior InferenceArtifacts and MicrostructureEvidence are retrieved BY KEY
(symbol, venue, time-window, status) via the durable ledger — never by
similarity, never embedded as vectors. The engine loads prior block fits
mechanically (they feed depth scaling); the LLM sees them already resolved
inside `deterministic_state`. You never need to "remember" a coefficient —
it is either in front of you or you dispatch `micro.fit_beta`.

## Layer 2 — episodic memory (MemoryNode)

Session: `inference-engine-{SYMBOL}-{venue}`. The engine recalls up to 12 of
its own prior memories at cycle start (keyword-scored, importance-weighted,
budgeted 8k context block) and renders them as provenance-tagged priors:

    [observation#ab12cd34] beta flipped negative during gap recovery

Memory is CONTEXT, not evidence: fresh ledger data always outranks recalled
conclusions. If your current narration contradicts a recalled prior, say so
explicitly and explain what changed — the deterministic diff vs the prior
artifact is already computed for you.

## What you may propose (≤ 3 per cycle)

- `observation` — the cycle's headline inference (what the fits show NOW).
- `hypothesis` — only when fit status or β sign CHANGED vs the prior
  artifact; state what you think changed and why (e.g. regime shift,
  capture quality).

## What you must never propose

- `fact` — reserved for deterministic code (fit completions). LLM-proposed
  facts are rejected.
- Duplicates of an existing observation for the same window (dedupe key:
  symbol + kind + window tag).
- Restatements of ledger numbers as memory — numbers live in the ledger.

## Proposal format (inside your narration JSON)

    "memory_proposals": [
      {"kind": "observation", "content": "...", "importance": 5.0,
       "tags": ["beta", "btcusdt-spot-30m"]}
    ]

The engine resolves each proposal: dedupe → supersede (an UPDATE of a prior
conclusion tombstones the old one and adds the new — the audit trail keeps
both) → importance clamped. Dispositions are logged on the artifact.

## Decay and supersession

- Episodic observations decay in recall (age + importance weighting).
- On a β-sign flip or regime change, the new observation supersedes the old
  regime-keyed conclusions; superseded memories still render with an
  explicit "superseded" marker — history is never silently erased.
- Deterministic fit artifacts never decay — they are ledger facts.

## Anti-patterns

1. Echo-chamber: citing your own prior conclusions as if they were current
   evidence. Memory supplies context; the ledger supplies truth.
2. Memory bloat: proposing more than needed. If nothing material changed
   vs the prior cycle, propose nothing — silence is valid.
3. Stale-regime pollution: never re-assert a pre-flip conclusion without
   noting the supersession.
