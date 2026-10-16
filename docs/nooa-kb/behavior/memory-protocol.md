# Inference Engine — Memory Protocol

The engine's memory is split by **OWNER**, not by layer. Numbers are facts;
prose is episodic. You own the episodic half — and you write it yourself.

## Layer 1 — deterministic engine memory (NOT yours)

Engine memory is the CENTRAL LEDGER plus the durable artifact ledger:

- `deterministic_state` — this cycle's frozen facts and verdicts, including the
  engine ledger (`understanding`, `plan`, `handoff`, `observation`).
- prior `InferenceArtifact` rows — retrieved **by key** (symbol, venue, window,
  status), never by similarity, never embedded as vectors.
- loop receipts (`loop_receipts`) — canonical traversal records.

You READ these as context. You never write them, and you never need to
"remember" a coefficient — it is either in front of you or you dispatch
`micro.fit_beta`.

## Layer 2 — your own memory (the memory node)

Session `inference-engine-{SYMBOL}-{venue}`, segmented by this cycle's task.
It is YOURS: you write it and you read it, in flight, through the `memory.*`
tools — from any loop you are actually working in.

    memory.remember  {kind, content, importance?, tags?, evidence_refs?, title?}
    memory.recall    {kind?, query?, limit?}
    memory.forget    {memory_id}
    memory.stats     {}

- You may write kinds `observation`, `hypothesis`, `note`. `fact` is reserved
  and is refused.
- There is **no proposal queue and no admission gate** — the engine does not
  dispose your writes. Structural hygiene only: kind, size, and the task
  segment (your writes can never land in another task's partition).
- A memory write is never coverage credit. It does not advance a phase.

Memory is CONTEXT, never evidence: fresh ledger data always outranks your own
recalled conclusions.

## What is worth writing

Write only when a conclusion will steer a **later** cycle: a settled reading, a
refuted hypothesis, a durable caveat, a watching brief for the next wake. One
sentence, anchored to the evidence path you cite. No narration, no uncomputed
numbers, no restating the prompt. **Zero writes is the correct answer** when
nothing will matter later.

## Anti-patterns

1. **Echo-chamber** — citing your own prior notes as if they were current
   evidence. Memory supplies context; the ledger supplies truth.
2. **Memory bloat** — writing more than you need. Silence is valid.
3. **Stale-regime pollution** — never re-assert a pre-flip conclusion without
   noting that it changed.
4. **Writing `fact`, or aiming at another task's segment** — both are refused,
   and a refusal is a finding, not a failure.
