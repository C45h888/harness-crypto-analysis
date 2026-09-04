# Calculation-Substrate Decomposition — Completion + Wiring Plan

Status: **implemented and green** (515 tests + 12 subtests, incl. 24 new
substrate-graph contract tests). This document records what was done, how the
individual calculation substrates are wired into the `harness.py` runtime and
the interpretation plane, and the remaining forward steps.

## 1. The problem

The calculation modules were coupled in four ways:

1. **Layer violation** — `analysis/demand.py` and `analysis/market.py` imported
   calculation functions directly at module scope (`calculations.flow.summarize`),
   so the analysis layer re-derived flow internally instead of consuming output.
2. **Blurred substrate boundaries** — one file held many concerns:
   `flow.summarize` computed book metrics (OBI, spread, microprice skew);
   `technical.py` mixed time-series math (EMA/ATR/trend) with tape classifiers
   (tiered large flow, seller aggression); `orderbook.py` held seven distinct
   concerns; `wall_migration.py` (an analysis file) owned order-book tier/anchor
   primitives.
3. **Coupling hub** — `nooa_harness/bedrock.py` (src of truth, ~2000 lines) mixed
   70+ pure-function imports with per-adapter evidence marshalling.
4. **Implicit contracts** — inputs were shape-based dicts, so nothing stopped two
   adapters from interpreting the same dict differently.

## 2. What was done (the mechanical path, completed)

### Phase 1 — substrate decomposition (move-don't-rewrite)

`market_service/calculations/substrates/` — twelve single-responsibility pure-math
modules. **Hard rules: no cross-substrate imports, no I/O, no runtime/clients.**

| Substrate | Owns (pulled from) |
|---|---|
| `tape` | summarize flow, bucketed_cvd, correlation, turnover share, price-bucketed flow, microprice skew (from `flow`) |
| `density` | rolling density, find_keystone, zone grids, significant levels, keystone intensity (from `orderbook`) |
| `ladders` | absorption ladder, keystone bid stack, ask-wall ladder (from `orderbook`) |
| `migration` | hourly + cross-cycle keystone migration (from `orderbook`) |
| `anchors` | derive/compute round anchors (from `orderbook` + `wall_migration`) |
| `tiers` | TierConfig, bid-tier buckets, institutional balance, mega-at-keystone (from `wall_migration`) |
| `technicals` | EMA, ATR%, OLS slope/drift (series half of `technical`) |
| `large_print` | tiered large flow, seller aggression (tape half of `technical`) |
| `volume_profile` | POC/value-area profile (from `volume_profile`) |
| `delta` | DELTA variable + band grid (from `delta`) |
| `signals` | deterministic signals (from `signals`) |

Legacy module paths (`calculations.flow`, `.orderbook`, `.technical`,
`.volume_profile`, `.delta`, `.signals`, and `analysis.wall_migration` tier
functions) remain as **re-export seams** — they resolve to the SAME objects as the
substrates (pinned by `LegacySeamIdentityTests`, identity not copy). New code
imports from the substrate package.

### Phase 2 — cut the analysis→calculations layer violation

- `analysis/demand.py`: `decompose_demand(d, *, flow_provider=None)` — the
  analysis layer consumes calculation OUTPUT via an injected provider port
  (`bind_flow_summary` + lazy default). Lazy import inside the resolver keeps the
  module-scope import calculation-free.
- `analysis/market.py`: documented COMPOSITION ROOT (same class as bedrock: it
  assembles a full product from raw evidence). Imported substrates directly and
  the docstring says so.

### Phase 3 — bedrock hub on the substrate graph

- `SUBSTRATE_GRAPH` (in `nooa_harness/bedrock.py`) is the single source of truth
  for section → substrate ownership, with declared `consumes` inputs.
- `run_calculations` / `run_analysis` now emit **`substrate_provenance`** —
  section id → owning substrate(s), restricted to sections that actually ran.

## 3. Wiring into the harness runtime + interpretation plane (completed)

The substrates were unwired dead math because the runtime called them through
`market_service.calculations.*` seams. That covalent path is gone:

```
nooa_harness/bedrock.py  (COMPOSITION GRAPH, imports substrates directly)
   │  run_calculations / run_analysis  →  each section's substrate(fn)
   │  SUBSTRATE_GRAPH, substrate_for(), section_inputs(), _substrate_provenance()
   ▼
pipeline_interpretation.py   (INTERPRETATION PLANE)
   │  run_cycle            → canonical envelope (substrate_provenance rides
   │     canonical_state.calculations / .analysis via dict(calculations))
   │  run_group_cycle      → GroupEnvelope(kind, ..., substrate_provenance={sec: (...)})
   │
   ▼
contracts.GroupEnvelope    (schema_version=1, additive field → to_dict/from_mapping)
   ▼
commands/harness.py        (the CLI runtime — --wall/--flow/--structure/--positioning
   │  via _run_groups → run_group_cycle)
   │  top-level result includes "substrate_provenance": {section: (substrate,...)}
   │  _keystone_history_payload still imported through the seam (works)
   ▼
INTERPRETATION plane readers (specialists/controller, engine, read_paths)
```

Concretely: every group command now reports WHICH substrate produced each section
of its GroupEnvelope, so the interpretation plane can explain
"`orderbook` came from density+ladders+migration+anchors",
"`delta` came from the delta substrate", etc.

## 4. Contract suite (`tests/test_substrate_graph.py`, 15 tests)

- substrates never import siblings/analysis/runtime/clients (AST scan)
- analysis modules never import calculations at module scope (with the two
  documented exemptions: composition roots + compat seams)
- every `GROUP_MAP` section ∈ `SUBSTRATE_GRAPH`; every graph substrate exists
- `run_calculations`/`run_analysis` provenance restricted to ran sections, keyed
  by section id
- `GroupEnvelope` provenance round-trips `to_dict`/`from_mapping`
- legacy seams resolve to the same objects as the substrates (identity)

## 5. Verification

- `python -m pytest tests/` → **515 passed, 12 subtests passed** (baseline before
  run: 491/12 — only additive).
- Ruff on code I own: clean (pre-existing WIP debt in the user's uncommitted
  engine/interpretation work was left untouched).
- Demo wiring end-to-end (canonical envelope + GroupEnvelope with provenance)
  runs green without Redis.

## 6. Remaining forward steps (next implementation candidates)

1. **GROUP_MAP amount = graph iteration**: `run_calculations`/`run_analysis`
   currently iterate a section registry with `want()`; refactor them to iterate
   `SUBSTRATE_GRAPH` directly so the graph is the runtime engine, not a
   declaration.
2. **Typed `EvidenceInput` per substrate** — frozen contracts (the
   `microstructure/contracts.py` pattern): `validate()` + `to_dict()`, so no
   adapter trusts dict shape.
3. **Rate the seams**: drop `calculations/flow.py → ...` re-export shims once the
   last external importers move to substrates; delete the DEPRECATED raw-qty tier
   fallback and legacy `_ROUND_ANCHORS` fallback paths that the substrates now
   supersede.
4. **Optional (per the vision): per-substrate addresses / namespaces** — e.g.
   each substrate emits under its own Redis key graph (`substrate:tape`,
   `substrate:density`, ...) or is importable as its own package, if the vision
   wants the interpretation plane to read per-substrate state independently.
5. **Schema honesty** — if provenance becomes a hard-read field, bump
   `GROUP_ENVELOPE_SCHEMA_VERSION` to 2 with a migration note (additive today, so
   v1 readers tolerate it).