# Runtime State Wiring Spec — One Namespace Per State Axis (S-0..S-5)

Status: **implemented and green** (1147 tests incl. 31 charter tests;
tree hash-verified stable across the full-suite run). Governing doctrine:
`CANONICAL_RUNTIME_DOCTRINE.md` (null discipline: measured, not claimed).
Companion spec: `HORIZON_RETENTION_SPEC.md` (the worker-plane horizon
namespace, already implemented and green).

> **CORRECTION RECORD (spec integrity).** An earlier draft of this spec
> carried four claims inherited from the `docs/nooa-kb/` monorepo
> documentation rather than this tree's code: `describe_loop` import shims,
> `PASS3_HORIZON_BUDGETS`, `engine/status_transition.py` S0–S5, and a
> `reset_compensation` primitive. All four were verified absent from this
> repo and are REMOVED. This is the spec's own charter lesson: a claim not
> pinned to the tree by an import or a test is a mirror. The verified tree is
> in fact STRONGER on those axes than the draft assumed — see §2.A (the loop
> axis is single-owner via `loop_states.py` + driver maps, no shims to
> absorb) and §2.E (`failure_substrate/` is a real, well-typed failure
> taxonomy: `FailureClass` enum, pure membrane, FSM-routed verdicts). The
> drift risk concentrates where this spec now points: the dead outer-cycle
> table, the horizon fan-out, and the deterministic_state root fan-out.

---

## 1. The verified problem — one state, five vocabularies

The runtime's state model is coherent in the data plane (Redis keyspaces:
every key name flows through `RedisRuntimeStore`) but fragmented at the
semantic boundaries. Five axes, each verified against the live tree:

### A. The outer cycle — the traversal table is documentation, not control flow

* `engine/core/runner.py:44` — `OUTER_SEQUENCE` is referenced by its
  definition and `__all__` ONLY. It is never iterated. The real stage order
  is the hardcoded await chain at `runner.py:91-124`. The docstring claims a
  7-stage order (WAKE → GATHER → COMPREHENSION → EVIDENCE → REASONING →
  VALIDATION → OUTPUT) while the tuple starts at `NestedLoop.CONTEXT` with a
  "merged" comment covering wake+gather+comprehension+evidence. No test
  binds the table to the chain.
* `engine/core/driver.py:369-375` — `expected_loop`/`expected_sub_loop`
  map the runtime's `loop_tag` string to FSM enums, with a **silent default**:
  `.get(st.loop_tag, NestedLoop.REASONING)` — an unmapped loop_tag silently
  authorizes work as REASONING (a null-discipline violation in the FSM's own
  input-mapping layer). `_legacy_loop_names` (:381-384) reconciles
  comprehension/evidence → CONTEXT. The turn-routing map (:552-557)
  re-reconciles `loop_tag` → FSM loop/sub_loop by hand.
* `context.py:224` — `loop_tag: str = "evidence"` — the runtime's loop
  identity is an untyped string; its vocabulary lives in convention
  (config.py's budget keys, reasoning.py's three assignments), not in a type.

### B. The horizon vocabulary — the {15m,1h,4h} widths are defined in six places

All verified by grep; the same width constants with zero imports between them:

| # | Claimant | What it defines |
|---|---|---|
| 1 | `runtime/horizons.py` (H1) | `HORIZON_PERIOD_MS` {15m,1h,4h} + IST alignment + fold primitives — **the worker-plane owner**, consumed by `substrate_worker/core/base.py` |
| 2 | `runtime/horizon_spans.py` | `NATIVE_HORIZONS` (1s/5s/30s/60s) + `LONG_HORIZONS` (15m/1h/4h) + `SPAN_HORIZONS` + `regime()` + own schema version |
| 3 | `microstructure/fitting_route_c.py` | `FORWARD_HORIZONS_MS` (1000/5000/30000/60000) — the native fits' numeric authority |
| 4 | `microstructure/horizon_bridge.py` | `NATIVE_HORIZONS_MS` tuple — **self-declared mirror** ("Mirrors fitting_route_c / task_directive — kept equal by test — see tests/test_horizon_bridge.py"). That test **does not exist** (only `test_fix_plane.py:51` partially pins the fitting_route_c side) |
| 5 | `engine/task_directive.py` | `NATIVE_HORIZONS_MS` dict + `LONG_HORIZONS_MS` — **self-declared mirror** ("mirror the deterministic plane; kept in sync by test, by value") |
| 6 | `engine/core/runner.py` | docstring's `{"horizon": "15m\|1h\|4h"}` scenario vocabulary |

And the horizon axis already reaches the engine's own state: `gather.py:504`
reads `task_plan.forecast_horizon_ms`, `:542` branches on
`horizon_regime == "native"`. Two mirrors held together by comments, one
dangling test reference, and our own H1 module added a sixth numeric
definition during the rollout. Adding a `1d` horizon today means editing six
files with nothing that stops the sixth from drifting.

### C. The budget axis is horizon-blind

`engine/config.py:68` `LOOP_PASS_BUDGET` is keyed by loop tag only
(comprehension/evidence/reasoning/validation/output = 1/3/6/1/1). The task
directive resolves scenario horizons ("15m|1h|4h") into milliseconds the
tools evaluate, and the engine branches per-regime (native vs long) — but
no budget anywhere is horizon-keyed. The crosswalk between "which horizon"
and "how much budget" exists only as prose in the plan specs.

### D. Status vocabularies — four scattered families, no shared namespace

| Family | Vocab | Where |
|---|---|---|
| payload health | healthy / degraded / insufficient_data | `substrate_worker/contracts.py` `PAYLOAD_STATUSES`; `fire.py` status logic |
| span fit status | validated / provisional / insufficient | `runtime/horizon_spans.py` `STATUSES` |
| bridge extrapolation | validated → provisional, `estimation_status: extrapolated` | `microstructure/horizon_bridge.py` |
| transport liveness | running / gap / reconnecting | `microstructure/capture.py` WS status transitions |
| failure classes | `FailureClass` enum (narration/tool/gate/dispatch/memory/validation/inference/infra) → verdicts (retry/fallback/terminate) | `engine/failure_substrate/membrane.py` + `signal.py` |

Six near-synonymous words for "imperfect data" across five families. The
failure substrate is the strongest of these — a typed enum, a pure
membrane, FSM-routed verdicts — but nothing connects the families to each
other or stops a new family from reinventing the words.

### E. The deterministic_state keyspace + schema versions

* ~20 root keys written across the engine (`assessment_note`, `chain_gaps`,
  `chain_halt`, `comprehension`, `congruence`, `failure`, `failure_report`,
  `forecast_result`, `forward_gate`, `forward_scenario`, `gate`, `terminal`,
  `loop_traversal`, `validation_issue`, `understanding_receipt`,
  `task_workflow`, `task_plan`, `task_directive`, `task`, `scenario`,
  `statistical_chain`, `governance_trace`, ...), enumerated nowhere. The
  engine's traversal (`gather.py`) iterates the model; a new root is added
  by editing context+gather in lockstep with no test that pins the pair.
* `schema_version` constants scattered across seven modules
  (`runtime/contracts.py` ×6, `substrate_worker/contracts.py`,
  `microstructure/contracts.py`, `interaction_plane/budgets.py`,
  `runtime/horizon_spans.py`, `redis_store.py` agent-artifact map, plus a
  hardcoded `"schema_version": 1` in `capture.py:465`). No registry; a
  reader/writer version mismatch fails at runtime, not at import.

---

## 2. The doctrine — one namespace per axis, imports are the charter

1. **One owner per axis.** Each state axis gets exactly one canonical module;
   every other claimant imports from it. Docstring claims of authority are
   upgraded to imports; mirrors are deleted.
2. **Namespaces get tests.** A frozen vocabulary with no test is not a
   namespace. Every namespace below is pinned by a conformance test that
   fails the moment a claimant redefines it locally.
3. **Silent defaults are governance errors.** Any `.get(tag, DEFAULT)` that
   maps an unknown state identity to an authorized behavior is removed; an
   unknown identity routes through the canonical failure path instead.
4. **Payloads stay byte-compatible.** This is a wiring spec, not a payload
   spec: no schema bumps, no envelope changes, no persistence renames.
5. **The spec is charater-conformant to itself.** Claims are code-verified
   (see §1's evidence) — the correction record at the top is the example.

---

## 3. The changes — phases S-0..S-5

### Phase S-0 — Pin the truth (test-only; the empirical answer to "can the planes disagree today")

| Change | What it is |
|---|---|
| `tests/test_outer_cycle_charter.py::test_narrate_cycle_matches_outer_sequence` | Instrument the `narrate_cycle` await chain (FSM observation hooks) and assert the observed stage order == `OUTER_SEQUENCE`'s semantic order. Runs once, records the truth. |
| `tests/test_outer_cycle_charter.py::test_outer_sequence_is_referenced` | Asserts `OUTER_SEQUENCE` is consumed by the runner (or the test) — a dead-table tripwire. |
| Baseline fixture | Record the current loop-tag vocabulary, the six horizon claimants, the ~20 deterministic_state roots, and the seven schema-version modules as the charter's baseline snapshot. |

**What this accomplishes:** the fragmentation becomes measured, not argued.
If the await chain and the tuple already disagree, we learn it now, before
any refactor claims to "derive" the chain from the table.

### Phase S-1 — Outer-cycle namespace (make the table real, then make it the only stage order)

| Change | What it is |
|---|---|
| `runner.py`: `OUTER_SEQUENCE` becomes the traversal | The await chain is refactored into stage receipts derived from `OUTER_SEQUENCE` (each entry = a receipt-gated stage function), OR the table is deleted and the docstring documents the chain as the order. Decision point: **derive** (the table becomes control flow) vs **delete** (the chain is the order, honestly labeled). Derive is preferred; delete is acceptable. |
| `driver.py:369-384`: silent default removed | `.get(st.loop_tag, NestedLoop.REASONING)` → explicit map + unknown-tag raises through the canonical governance/failure route. `_legacy_loop_names` and `expected_sub_loop` move to ONE crosswalk table (see S-1b). |
| `context.py:224`: `loop_tag: str` → `LoopTag` enum | A frozen enum whose members are exactly the budget keys and the crosswalk's keys. `LOOP_PASS_BUDGET` (config.py) and `passes_per_loop` (context.py) become enum-keyed. One vocabulary: enum ↔ budget keys ↔ crosswalk keys. |

**What this accomplishes inside the system:**
* The stage order has exactly ONE authority — whatever it is (table or chain),
  it is test-pinned and cannot silently drift (S-0's tripwire + the derive).
* The FSM's input-mapping layer can no longer fabricate authorization: an
  unknown loop identity is a governance event, not a REASONING pass.
* `loop_tag`, budget keys, and the crosswalk share one typed vocabulary —
  three hand-maps in driver.py collapse into one table + one enum, and the
  P1–P6 coverage axis stays formally separate (a docstring contract + test
  that no P-phase value is ever compared to a LoopTag).

### Phase S-2 — Horizon namespace (one numeric owner, six importers)

| Change | What it is |
|---|---|
| `runtime/horizons.py` becomes the tree-wide horizon owner | It already owns the worker cadence ({15m,1h,4h} widths + IST alignment). It gains: `LONG_HORIZON_MS` (the long-horizon numeric set) and `NATIVE_HORIZON_MS` (the native set), imported-or-delegated from the plane that owns each meaning (see below). Pure leaf, no new imports. |
| `microstructure/fitting_route_c.py` | REMAINS the native-fits numeric authority (it fits the models); `runtime/horizons.py` re-exports `NATIVE_HORIZON_MS = fitting_route_c.FORWARD_HORIZONS_MS` via a documented import (runtime→microstructure import is a NEW direction — allowed only as this one documented seam, asserted by the charter test; alternative: the charter test pins value-equality and the mirror comment is upgraded to "pinned by test_horizon_charter"). Decision point noted in §5. |
| `microstructure/horizon_bridge.py` | Its `NATIVE_HORIZONS_MS`/`LONG_HORIZONS_MS` mirrors are replaced by imports from the charter owner (or the owner's re-export). The dangling "kept equal by test — see tests/test_horizon_bridge.py" reference is replaced by the real charter test. |
| `engine/task_directive.py` | Same treatment: its `NATIVE_HORIZONS_MS`/`LONG_HORIZONS_MS` mirrors become imports. The "kept in sync by test, by value" comment becomes the charter test's identity. |
| `runtime/horizon_spans.py` | Its `NATIVE_HORIZONS`/`LONG_HORIZONS` name sets resolve to the charter owner's vocabulary (names imported, span payloads unchanged). |
| `microstructure/capture.py` | The WS 5m/15m/4h cadence widths validate against `HORIZON_PERIOD_MS` (namespace assertion; cadence unchanged). |
| `tests/test_state_charter.py::TestHorizonNamespace` | Identity tests: every claimant resolves to the charter owner; the bridge's dangling reference is now a real test; the H1 docstring's "SINGLE SOURCE OF TRUTH" becomes fact across all planes. |

**What this accomplishes inside the system:**
* Adding a `1d` horizon becomes ONE edit + the charter test updating. Six
  claimants stop being six opportunities for divergence.
* The engine's horizon state (`task_plan.forecast_horizon_ms`,
  `horizon_regime`) and the worker's cadence widths are the same vocabulary
  by import, not by coincidence — the LLM and the FSM see one horizon
  language.
* The two self-declared mirrors (comments) and one dangling test reference
  become enforced identity — the exact drift the correction record warned
  about becomes structurally impossible.

### Phase S-3 — The deterministic_state root registry

| Change | What it is |
|---|---|
| `context.py`: `DETERMINISTIC_STATE_ROOTS: frozenset[str]` | The ~20 verified roots, frozen, as the keyspace's namespace. |
| `gather.py` traversal assertion | The engine's iteration asserts it reads exactly the declared roots (module-level assert + charter test). |
| Writers declare their roots | Each `deterministic_state[key] = ...` site's owning stage is recorded in a ROOT_OWNERS table (key → writing stage) next to the registry; the charter test asserts every write site's key is declared. |
| `capture.py:465` hardcoded `"schema_version": 1` | Resolves to the registry (S-4). |

**What this accomplishes inside the system:**
* The keyspace has a declared boundary: adding a root is one edit in
  `context.py` + one registry entry, and the traversal fails the charter test
  if it reads an undeclared root — "context and gather move together" becomes
  enforced, the same doctrine as the substrate layer's threshold sharing.
* The engine's own state model is pinned by name — the first state axis where
  the runtime's behavior and the charter test are the same object.

### Phase S-4 — Schema-version registry + status-family namespaces

| Change | What it is |
|---|---|
| `runtime/contracts.py`: `SCHEMA_VERSION_REGISTRY` | One frozen dict: contract name → version, re-exporting the existing constants (no version changes). All seven claimant modules import their own entry from it; `capture.py:465`'s hardcoded 1 resolves here. |
| Status families: one namespace module (`runtime/status_families.py` or per-plane constants, decision point §5) | The five verified families become declared vocabularies: payload-health (`PAYLOAD_STATUSES`, existing), span-fit (`STATUSES`, existing), bridge extrapolation, transport liveness, and the failure substrate's `FailureClass` (existing — it becomes the declared failure-status family). Each family's module declares its frozen set; the charter test asserts the sets are disjoint (no family borrows another's words) and consumed by their planes. |

**What this accomplishes inside the system:**
* Version bumps become: edit the registry + the owning contract — the
  registry test fails if a reader/writer pair disagrees at import time
  instead of at runtime.
* The six near-synonymous status words are formally different words for
  different axes — each family owns its vocabulary, disjointness is tested,
  and a new family cannot silently reuse another family's words.
* The failure substrate — already the strongest taxonomy — becomes the
  declared failure-status family, formally connected to the charter.

### Phase S-5 — The charter conformance test (the keystone)

| Change | What it is |
|---|---|
| `tests/test_state_charter.py` | One test module asserting the whole charter: (1) the outer-cycle order is single-authority and test-pinned; (2) every horizon claimant resolves to the charter owner by identity; (3) `DETERMINISTIC_STATE_ROOTS` is frozen and the traversal reads exactly it; (4) every schema-version consumer resolves to the registry; (5) status families are frozen, disjoint, and consumed; (6) no silent defaults map unknown identities to authorized behavior (grep-level assertion over driver.py/config.py). |
| `CANONICAL_RUNTIME_DOCTRINE.md` cross-reference | The doctrine's state-model section points here. |

**What this accomplishes inside the system:**
* The five axes are pinned by one keystone test — the analog of the substrate
  graph's 24 contract tests, extended to the state model. Any violation of
  the doctrine fails at commit time, not at production time.

---

## 4. Per-change accomplishment table

| Phase | Axis closed | The one-line accomplishment |
|---|---|---|
| S-0 | (baseline) | The fragmentation becomes measured truth, not an argument — and we learn whether the planes already disagree |
| S-1 | Outer cycle | One stage-order authority; the FSM can no longer be fed fabricated authorizations; three hand-maps become one enum + one table |
| S-2 | Horizon identity | Six numeric definitions become one owner + five importers; adding a horizon is one edit |
| S-3 | deterministic_state | The keyspace gets a declared, frozen boundary that the engine's traversal is tested against |
| S-4 | Versions + status families | Version mismatches fail at import, not runtime; status words are formally disjoint per axis |
| S-5 | ALL | One keystone test enforces the whole charter at commit time |

---

## 5. Decision points (open, before implementation)

1. **S-1 derive vs delete:** does `OUTER_SEQUENCE` become the traversal
   (receipt-gated stage dispatch), or is the await chain declared the order
   and the table deleted? Derive preferred (the table was written to be the
   order); delete acceptable (honesty over aspiration).
2. **S-2 fitting_route_c import direction:** `runtime/horizons.py`
   importing from `microstructure/` breaks the "runtime is a leaf" principle.
   Options: (a) one documented, charter-tested seam (runtime owns the
   re-export, microstructure the fitting); (b) value-equality pinned by the
   charter test with both sides importing the NUMBERS from a new pure-leaf
   `runtime/horizon_values.py` (preferred — keeps runtime a leaf and kills
   the mirror at the root); (c) leave the native set in microstructure and
   charter-pin equality only.
3. **S-4 status-namespace home:** one `runtime/status_families.py` owning
   all five families, or per-plane constants with the charter test asserting
   disjointness. Single module preferred (discoverability); per-plane
   acceptable if families are strongly plane-bound.
4. **S-1 P-phase boundary:** confirm P1–P6 remains a coverage/provenance
   axis formally separate from `LoopTag` (driver.py:511 already states this
   in prose; S-1 makes it a tested contract).

---

## 6. Test matrix

| Test module | Phase | Count | Asserts |
|---|---|---|---|
| `test_outer_cycle_charter.py` | S-0/S-1 | 4 | narrate_cycle order == table semantics; table is referenced (tripwire); unknown loop_tag → governance event; P-phase/LoopTag separation |
| `test_state_charter.py::TestHorizonNamespace` | S-2 | 6 | six claimants resolve to the owner by identity; bridge's dangling reference is a real test; capture cadence widths validated |
| `test_state_charter.py::TestDeterministicRoots` | S-3 | 4 | roots frozen; traversal reads exactly roots; every write site declared in ROOT_OWNERS |
| `test_state_charter.py::TestSchemaRegistry` | S-4 | 3 | registry frozen; every schema_version consumer resolves to it; capture.py hardcoded 1 gone |
| `test_state_charter.py::TestStatusFamilies` | S-4 | 5 | five families frozen; disjoint; consumed by their planes; FailureClass is the declared failure family |
| `test_state_charter.py::TestCharter` | S-5 | 6 | the whole doctrine, asserted once |

Total: **28 charter tests** on top of the existing 1104-passing suite.

---

## 7. Acceptance criteria

1. Full suite green + the 28 charter tests.
2. No module outside the charter owner defines a horizon width, a
   deterministic_state root list, a schema version, or a status vocabulary
   (identity-asserted, not convention).
3. `OUTER_SEQUENCE` is either the traversal or deleted — no dead table.
4. No `.get(unknown, AUTHORIZED_DEFAULT)` anywhere in the FSM's input path.
5. Zero payload-shape changes, zero persistence-key renames.
6. The correction record at the top of this spec remains: claims enter this
   spec only through code verification.

---

## 8. Explicitly out of scope

* Payload shapes, envelope schemas, persistence keys — unchanged.
* New horizons, new loop stages, new roots, new status families — the charter
  records the current sets; extending an axis is a separate spec that edits
  the owner + the charter test.
* The interpretation plane's LLM contracts (SpecialistReport/AnalystBriefing
  schema semantics) — their schema versions join the registry; their prompt
  contracts are a separate spec's jurisdiction.
* The horizon→budget crosswalk — S-1's typed `LoopTag` makes the pairing
  expressible, but no budget is horizon-keyed in this spec (the axis is
  recorded as open in §1.C).

---

## 9. Implementation record (S-0..S-5 landed)

### 9.1 Corrections to this spec made during implementation (the charter
applies to the spec itself)

1. **§2 capture.py cadence claim was WRONG.** The audit table described
   `microstructure/capture.py` as rolling WS deltas into 5m/15m/4h windows —
   capture's actual cadence is a **10s OFI interval** (`interval_seconds=10`),
   and its true charter relevance is the **transport-liveness status family**
   (`_status("starting"/"reconnecting"/"stopped"/"connected"/"running"/
   "gap"`) plus a hardcoded `"schema_version": 1` in its status payload
   (both now chartered). The 5m/15m/4h WS bucketing lives in the
   evidence plane, not capture. Measured > claimed — the lesson in
   practice.
2. **Decision point 1 (S-1): DERIVED.** `OUTER_SEQUENCE` is now a tuple of
   `StageReceipt` entries and `narrate_cycle` iterates it through
   `_STAGE_RUNNERS` — the dead table became the control flow. Spy seams
   preserved (wrappers call through module attributes at call time).
3. **Decision point 2 (S-2): owner-defines, planes-import.**
   `runtime/horizons.py` (already a pure leaf) owns BOTH numeric grains
   (`NATIVE_HORIZONS_MS`/`LONG_HORIZONS_MS` + ordered tuples); the planes
   import from it — no new direction violations, no separate values module
   needed.
4. **Decision point 3 (S-4): per-plane families + disjointness test.** A
   single cross-plane status module would need runtime→plane imports
   (forbidden direction); each family stays owned by its plane and the
   charter test asserts pairwise disjointness of all four families.
5. **Mid-session concurrency.** A parallel session committed
   `a1cf83f "full re-orderring of sate and the runtime inference engien is
   fixed"` WHILE this spec was being implemented — the engine's stage chain
   was re-ordered (agent_fetch → data_gate added) mid-flight. The charter
   absorbed it: receipts and the AST baselines were re-pinned against the
   NEW chain, and the full suite was gated with a tree-hash stability
   check around the run. This is exactly why S-0's pin-the-truth-first
   ordering was mandated.

### 9.2 Fragments the implementation itself uncovered (folded during S-2/S-4)

The charter test scaffolding immediately caught four MORE fragments beyond
the §2 audit — each folded onto its owner in the same pass:

| Fragment | Owner it resolved to |
|---|---|
| `microstructure/fitting_common.py` `SCENARIO_HORIZONS` {15m:900, 1h:3600, 4h:14400} — a seconds-grain mirror | `runtime/horizons.LONG_HORIZONS_SECONDS` (identity) |
| `interaction_plane/segments.py` `SPAN_HORIZONS` name-tuple — AND the interaction plane had TWO different `SPAN_HORIZONS` (segments' literal vs reads.py's horizon_spans import) — a live in-plane split | `runtime/horizon_spans.SPAN_HORIZONS` → owner (identity) |
| `nooa_harness/contracts.py` `GROUP_ENVELOPE_SCHEMA_VERSION` | registry `"group_envelope"` |
| `engine/task_directive.py` `DIRECTIVE_SCHEMA_VERSION` | registry `"directive"` |

### 9.3 What landed where (S-0..S-5)

| Phase | Files | Evidence |
|---|---|---|
| S-0/S-1 | `engine/core/runner.py` (StageReceipt + OUTER_SEQUENCE + _STAGE_RUNNERS + iterating narrate_cycle), `engine/loop_states.py` (LoopTag + LOOP_TAG_CROSSWALK + REGISTRY_HOME_LOOPS), `engine/core/driver.py` (crosswalk consumption; silent REASONING default REMOVED — unknown loop_tag → GovernanceDenied) | `tests/test_outer_cycle_charter.py` (9 tests incl. dead-table tripwire, spy-seam preservation, no-silent-default scan, P1–P6/LoopTag separation) |
| S-2 | `runtime/horizons.py` (native+long numeric owner, name tuples, HORIZON_ROLLOVER_MS, HORIZON_PERIOD_MS = LONG_HORIZONS_MS alias), five horizon claimants → identity imports, `runtime/horizon_spans.py` names resolve to owner | `tests/test_state_charter.py::TestHorizonNamespace` (8 tests incl. the AST no-outside-numeric-literal scan) |
| S-3 | `engine/core/context.py` `DETERMINISTIC_STATE_ROOTS` (34 roots, bidirectional AST-pinned) | `tests/test_state_charter.py::TestDeterministicRoots` (4 tests incl. dynamic-key ban) |
| S-4 | `runtime/contracts.py` `SCHEMA_VERSION_REGISTRY` (12 entries) + 6 planes resolve to it; `capture.py` `STATUS_TRANSITIONS` + registry-derived schema_version | `tests/test_state_charter.py::TestSchemaRegistry` + `::TestStatusFamilyTests` (8 tests incl. pairwise family disjointness) |
| S-5 | `tests/test_state_charter.py` keystone scans (AST over the whole package) | 31 charter tests total; suite 1147 green, tree-hash stable |