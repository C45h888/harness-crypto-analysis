# NOOA Knowledge Base — Design

Status: Draft (post-brainstorm, awaiting user sign-off)
Date: 2026-08-15
Owner: Claude (with user approval)

## 1. Purpose

Establish a version-controlled NOOA knowledge base inside this repo that
records:

1. What `nooa` v0.0.6 and `nooa-cli` v0.0.6 actually contain.
2. What our integration in `market_service/nooa_harness/` expects of them.
3. What the wider community (Reddit, NVIDIA devblog, GitHub
   issues/discussions on `NVIDIA-NeMo/labs-OO-Agents`, blogs/articles)
   reports about the framework.
4. What the gap between (1)+(2)+(3) and our runtime behavior actually is
   when executed against v0.0.6.

The eventual downstream **state-assessment agent** reads this KB and
writes `state-snapshot.md` grading our integration against the truth
above.

This KB produces *findings*, not refactors. Any code change to the
integration is a follow-up task.

## 2. Scope

**Sources ingested.**
- `https://github.com/NVIDIA-NeMo/labs-OO-Agents.git` at tag `v0.0.6`,
  cloned shallow to `/tmp/nooa-monorepo/` (NOT into the repo — repo
  contains only KB artifacts, not the upstream source).
- Internet: Reddit threads, NVIDIA devblog, Hacker News, GitHub
  issues/discussions on `NVIDIA-NeMo/labs-OO-Agents`, and any blog or
  article that mentions the project. Every internet claim must cite a
  URL.

**Out of scope for this task.**
- Replacing the NOOA framework or upgrading our pin.
- Editing files in `market_service/nooa_harness/` based on KB findings.
- Adding LLM call coverage or refactoring existing strategies.
- Setting up CI for KB freshness.

## 3. Architecture

A four-stage deterministic pipeline. The pipeline's stage boundaries are
hard. Inside stage 2 the work fans out across worker lanes.

```
Stage 1 (ingest)          Stage 2 (fan-out)            Stage 3 (synthesize)
                          ┌──────────────┐
                          │ M-lane:      │ one worker per
1 agent: clone monorepo   │ monorepo     │ packages/* subpackage
to /tmp, write            │ readers      │
sources/pin.json +        ├──────────────┤
sources/inventory.md,     │ I-lane:      │ one worker per
build internet index      │ internet     │ topic (Reddit,
                          │ readers      │ devblog, GitHub
                          ├──────────────┤ issues, blogs)
                          │ B-lane:      │ one worker per call
                          │ behavior     │ site in
                          │ validators   │ market_service/nooa_harness/
                          └──────┬───────┘
                                 ▼
                          Stage 3: 1 synthesis agent
                          cross-references M + I + B
                                 ▼
                          Stage 4: 1 assessment agent
                          reads the KB, writes
                          state-snapshot.md
```

**Why four stages, not three.**
Stage 4 (state assessment) is what the user actually called out as the
deliverable for "now." Stages 1–3 build the substrate. Stage 4 reads the
substrate. Keeping them separate means the assessment agent never has
to also ingest.

## 4. File tree

All paths under `docs/nooa-kb/` and committed to git.

```
docs/nooa-kb/
  sources/
    pin.json                  # exact git SHA, ref, clone timestamp, /tmp path
    monorepo-inventory.md     # auto-discovered file index of /tmp clone
    internet-index.md         # list of URLs each I-worker used, with timestamps
  from-monorepo/
    packages-nooa/
      exports.md              # M1 output: every public name + signature
      strategies.md           # M2: PredictStrategy, CodeActStrategy
      context-blocks.md       # M3: DynamicContext semantics
      agentdoc.md             # M4: spec / Annotated / hidden
      unifiedllm.md           # M5: backends / registry
    packages-nooa-cli/
      cli.md                  # M6: commands, subcommands, config
    docs-markdown.md          # M7: any *.md at top level used as 'official docs'
  from-internet/
    reddit.md                 # I1: user experience, gotchas
    nvidia-blogs.md           # I2: announcements, design notes
    github-issues.md          # I3: open/closed issues on NVIDIA-NeMo/labs-OO-Agents
    third-party.md            # I4: blogs/articles
  behavior/
    call-sites.md             # B-summary: index of all B-* micro-test results
    B01-agents-base.md        # one file per call site or per import line
    B02-strategy-predict.md
    B03-strategy-codeact.md
    B04-dynamic-context.md
    B05-agentdoc.md
    B06-unifiedllm.md
    B07-runner-env.md
    B08-suite-composition.md
  assessment/
    import-coverage.md        # synthesized from M + B
    decorator-usage.md        # synthesized from M + B + I
    pin-drift.md              # synthesized from M vs latest tag
    security-boundaries.md    # synthesized from I + B (esp. CodeActStrategy)
    state-snapshot.md         # FINAL — written by stage 4 assessment agent
  README.md                   # how to read the KB, freshness policy, who can edit
```

## 5. Worker contracts

Each worker writes into the file at its assigned path. Workers do not
write outside their own file. Workers do not edit each other's files.

### M-lane (monorepo readers, one per `packages/*` subpackage or per
top-level markdown section)

- **Input:** `/tmp/nooa-monorepo/`, the path of the subpackage assigned.
- **Output:** `docs/nooa-kb/from-monorepo/<area>.md`.
- **Required sections in the output:** Overview · Public exports (with
  signatures) · Internal seams exposed publicly · Docstrings verbatim
  where they describe behavior · Cross-references to other M-lane files.
- **Disallowed:** inventing exports not in source, inferring runtime
  behavior not stated in source. If unsure, write `UNVERIFIED —
  requires B-lane`.

### I-lane (internet readers, one per topic area)

- **Input:** WebSearch + WebFetch URLs, the topic area assigned.
- **Output:** `docs/nooa-kb/from-internet/<topic>.md`.
- **Required sections:** Sources (full URL list with retrieval
  timestamp) · Claims (each with the supporting URL) · Confidence
  (high/medium/low with reason) · Conflicts with M-lane (if any).
- **Disallowed:** paraphrasing community speculation as fact. Speculation
  is allowed but tagged `[SPECULATION]` and the source for that
  speculation must be quoted.
- **Internet sourcing limit:** at most 8 distinct URLs per worker, each
  retrieved during this session. Workers cite retrievals, not cached
  prior knowledge.

### B-lane (behavior validators, one per call site / import line)

- **Input:** `/tmp/nooa-monorepo/`, a specific call site in
  `market_service/nooa_harness/`, the model configuration stub described
  in §6.
- **Output:** `docs/nooa-kb/behavior/B<n>-<slug>.md`.
- **Required sections:** Call site path · Imports exercised · Micro-test
  code · Observed behavior · Documented behavior (cite from M-lane) ·
  Drift (`match` / `drift` / `unverified_by_execution`).
- **Disallowed:** running a real LLM. The model layer must be stubbed.

### Stage 3 synthesis

- **Input:** every file under `from-monorepo/` + `from-internet/` +
  `behavior/`.
- **Output:** `import-coverage.md`, `decorator-usage.md`, `pin-drift.md`,
  `security-boundaries.md`, and `call-sites.md` (index).
- **Rule:** every synthesis claim must reference at least one M / I / B
  source file by relative path. No claim without a source.

### Stage 4 assessment

- **Input:** the full KB plus the integration source files in
  `market_service/nooa_harness/`.
- **Output:** `assessment/state-snapshot.md`. This document is graded
  narrative: green / yellow / red per area, with explicit confidence.
- **Rule:** the assessment agent must NOT add new facts. It only
  summarizes the existing KB and grades it. If a fact is missing, the
  agent writes "KB gap: <topic>" rather than making the gap up.

## 6. Behavior-test sandbox

LLM generations cannot be validated without a real model, so:

- B-lane tests stub the model with a deterministic callable that returns
  a known shape (e.g., a JSON object matching the strategy's expected
  output contract, or a fixed string for `PredictStrategy`).
- Anything that requires a live LLM (a real `assess()` or
  `synthesize()` call against an LLM) is marked `unverified_by_execution`
  with the reason.
- B-lane tests must run in a subprocess with a timeout, so a runaway
  v0.0.6 framework import does not corrupt the parent process.

## 7. Failure modes and confidence

| Situation | Handling |
|---|---|
| Monorepo clone fails | Stage-1 agent retries once, then writes a `FATAL` entry in `sources/pin.json` and exits; later agents do not run. |
| `nooa` import fails at v0.0.6 (despite pinning working before) | M-lane agent records the failure in `exports.md` and continues; B-lane tests are skipped. |
| Worker writes outside its file | Synthesis rejects the file; agent is re-run with file-scope reminder. |
| Two workers publish contradictory claims | Synthesis picks the higher-confidence one and records both in `assessment/state-snapshot.md` under "Conflicts." |
| Behavior drift discovered (documented ≠ observed) | Surfaced in `security-boundaries.md` or the relevant `B0n-*.md` with `drift = true`. |

Confidence ratings:
- **high** — direct evidence from v0.0.6 source, internet claim backed
  by an authoritative URL, or behavior matches docs in B-lane.
- **medium** — inference from source without direct documentation or
  community corroboration.
- **low** — one internet source only, or an `unverified_by_execution`
  cell.

## 8. Implementation order

1. Stage 1 ingest — single agent, deterministic.
2. Stage 2 fan-out — orchestrated; one agent per lane.
3. Stage 3 synthesis — single agent, reads outputs of Stage 2.
4. Stage 4 assessment — single agent, reads the full KB and writes
   `state-snapshot.md`.
5. README at `docs/nooa-kb/README.md` documenting freshness policy.

After this lands, the user reviews `state-snapshot.md`. Any remediation
work becomes a separate task with its own brainstorming cycle.

## 9. Write boundary (hard constraint)

Stage 1, Stage 2, Stage 3, Stage 4 agents, and any sub-agents they spawn
**may only create or modify files under these paths**:

```
docs/NOOA_KNOWLEDGE_BASE_DESIGN.md        # this spec, only for spec edits
docs/nooa-kb/**                            # all KB output
/tmp/nooa-monorepo/**                      # clone target only
```

These agents **must NOT** modify, create, or delete any other file
anywhere in the repository or filesystem, including but not limited to:

- `market_service/**` (the integration under audit)
- `tests/**` (the test suite)
- `db/**`, `alembic/**`, `alembic.ini` (schema + migrations)
- `Dockerfile`, `docker-compose.yml`, any container/build config
- `requirements.txt` or any dependency manifest
- `.env`, `.env.example` (secrets / config)
- Any git operation outside `git add docs/nooa-kb/` and
  `git add docs/NOOA_KNOWLEDGE_BASE_DESIGN.md`

If an agent discovers a need to change something outside the boundary
to make the KB correct, it must instead write a finding into the KB
(e.g., `assessment/state-snapshot.md` under a "Suggested remediation"
heading) so the user can decide in a follow-up task.

This constraint is enforced by:

1. A pre-flight `git status` snapshot at the start of Stage 1.
2. A post-flight `git status --porcelain` check at the end of each stage.
   Any path matching outside the allowlist above causes that stage to
   fail loudly and abort before the next stage runs.
3. Stage workers read this section before any file write.

## 10. Open questions called out for follow-up

These are explicitly NOT answered here so that work can begin; they are
resolved when stage 4 runs.

- Should `pin-drift.md` compare to `latest` or to the next stable tag
  past `v0.0.6`?
- Should stage 4 be re-run automatically when a new tag lands, or only
  on demand?
- How aggressively should the KB mask internet sources that disagree
  with the v0.0.6 source of truth?
