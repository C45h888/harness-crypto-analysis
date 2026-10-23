# System Audit — Current Runtime, Authority, State, and Documentation

**As of:** 2026-10-22  
**Scope:** Static review of the current checkout, including the uncommitted working tree. Focus: semantic authority, identity/state ownership, and runtime documentation. No secrets or `.env` contents were inspected. No runtime code or existing document was changed for this audit.

## Executive assessment

The runtime has made meaningful progress toward explicit authority: `Scope` centralizes symbol/venue normalization, `STATE_CHARTER_SPEC.md` pins several vocabularies and state roots, the agentic FSM governs legality, and the engine ledger is the source for findings and verdicts. The problem is no longer simply “everything has multiple owners.” Some axes have strong owners and tests.

The current risks are **uneven enforcement and documentation drift**:

1. A requested venue can differ from the configured capture venue and still enter inference. Symbol is checked; venue is not.
2. PostgreSQL reads of canonical output/history are not venue-scoped, although Redis keys and CLI interfaces are. This can mix records from different venues for the same symbol.
3. Several prominent documents and runtime docstrings describe retired analyst-suite/collated-envelope flows as active. The current runtime is a task-directed `InferenceEngine` with a canonical `CanonicalOutput` record and live scoped Redis reads.
4. The engine has a canonical ledger and derived projections, but consumers encounter both canonical and compatibility shapes. Their authority and parity contract should be explicit rather than inferred from implementation comments.

**Overall risk: HIGH for operator/developer correctness; MODERATE for state-authority structure.** Targeted charter and identity tests pass, but they do not establish cross-venue persistence correctness or live Docker/Redis/PostgreSQL behavior.

## Current authority map

| Concern | Current authoritative home | Important distinction |
|---|---|---|
| Requested runtime identity | `Settings` resolved through `market_service/runtime/scope.py` | `Scope` normalizes identity. The configured venue is not yet enforced against an explicit per-run venue override. |
| Live evidence | Poller/capture writers and their Redis keyspaces | Evidence is not interpretation; freshness/coverage must remain visible. |
| Deterministic calculations and market state | Deterministic calculation/worker modules and their contracts | The LLM may interpret but must not replace deterministic facts. |
| Agentic legality | `engine/fsm.py`, controller, and gate owners | The driver transports proposals; it must not create authorization. Worker-plane governance is separate and plane-specific. |
| Findings/verdict record | `engine/ledger.py` (`deterministic_state["ledger"]`) | `engine/projections.py` produces compatibility fields at artifact write; those fields should remain derived. |
| Delivered inference answer | `engine/core/answer.py` and `runtime/contracts.CanonicalOutput` | PostgreSQL is durable; Redis is a live projection. A run record is diagnostic/bookkeeping, not the answer. |
| Final decision | Human trader | No model output authorizes a trade. |

The issue is not that the system has multiple authorities for distinct concerns. The issue is that several documents do not distinguish those concerns and several read paths do not carry the full `(symbol, venue)` identity.

## Findings

### A-01 — Active documentation describes retired runtime paths

**Severity: P1 — high risk of wrong operation or future changes against dead interfaces.**

Evidence:

- `README.md:152-207` describes a specialist/controller suite, `AnalystBriefing`, and `--nooa` routing as the active analyst architecture. The current package initializer says the `agents` module is retired and identifies `InferenceEngine` / `inference_runner` as the live path (`market_service/nooa_harness/__init__.py`).
- `market_service/nooa_harness/README.md` lists `agents.py`, `suite.py`, and `runner.py` as package modules; those files are absent from the current tree. The live entry point is `inference_runner.py`.
- `docs/NOOA_HARNESS_ARCHITECTURE.md:1-13` still presents the first-integration design around `MarketRunEnvelope` and an analyst object.
- `docs/RUNTIME_DATA_AUTHORITY.md:1-19, 79-120` labels itself an active contract and calls the Redis collated/run plane canonical, while also describing an autonomous wake envelope and the older `InferenceArtifact` path. Its trigger section says the autonomous loop was removed, but the preceding runtime description still describes autonomous wakes. Current inference is one-shot; current market reads use live scoped Redis projections (`market_service/nooa_harness/inference/tooling/tools_market.py`).
- `README.md:69-74, 131-150` still directs readers to collated-envelope reads and older mount behavior.

**Impact:** An engineer can select a retired command or modify a read/write plane that has no production writer. This directly creates the “conflated semantic authority” problem: the same data and analyst semantics appear to be owned by both the old envelope/suite path and the current inference path.

**Action:** Establish one short active-runtime document map. Rewrite `README.md` and `market_service/nooa_harness/README.md` against the live CLI and package tree. Mark old integration contracts as `historical/superseded` or move them under an archive; do not leave them labelled active. Keep detailed implementation history only where it has durable value.

### A-02 — Venue identity is not enforced end-to-end

**Severity: P1 — a run can proceed with an identity the configured capture does not provide.**

Evidence:

- `market_service/nooa_harness/inference_runner.py:131-155` resolves the configured scope, then accepts an explicit `venue` via `normalize_venue`. It refuses an uncaptured **symbol**, but does not compare the requested venue to `settings.venue` (or to a set of actually captured venues) before building the engine and LLM client.
- The new `tests/test_identity_scope.py` verifies uncaptured-symbol refusal and expressly verifies that `spot` is a valid/expressible venue, but does not verify that a `spot` request is refused when capture is configured for `futures`.
- `market_service/runtime/scope.py` models a single configured venue; `Settings` reads it from `MICROSTRUCTURE_VENUE`. The read tools correctly query venue-scoped keys, so a mismatched venue can become an empty/partial read rather than an early identity refusal.

**Impact:** Wrong-venue invocations can spend LLM/runtime work on incomplete evidence and produce an answer for an identity the configured capture path does not supply.

**Action:** Choose and enforce one contract: either (a) reject an explicit venue that differs from the configured capture venue before engine/LLM/store construction, or (b) define an authoritative captured-scope inventory keyed by `(symbol, venue)` and validate against it. Add a test proving mismatch refusal has zero LLM calls and zero writes. Keep venue aliases separate from canonical venue values (`perps` is input normalization, not a second stored identity).

### A-03 — PostgreSQL canonical-output reads ignore venue

**Severity: P1 — cross-venue history can be selected for the wrong request.**

Evidence:

- `market_service/commands/nooa_cli_ext.py:128-164` accepts `venue`, but the PostgreSQL branch calls `read_canonical_output(symbol, run_id=...)` without it. The Redis fallback does pass venue.
- `market_service/runtime/postgres_store.py:1043-1072` has no venue parameter on `read_canonical_output`; the latest-by-symbol query filters only on `symbol`. Exact `run_id` reads are global, which should be documented as intentional if that is the contract.
- `nooa_cli_ext.py:824-839` accepts venue for `inference history` but calls `read_canonical_history(symbol, limit=...)`; the PostgreSQL query filters only by symbol.
- `engine/core/driver.py:190-205, 250-310` reads prior canonical outputs by symbol only for wake counters, ledger recall, and prior-run comparison. Redis fallback is venue-scoped, so PostgreSQL and Redis can choose different records.

**Impact:** Prior-run state, history, and operator reads can mix venues for the same symbol. The outcome can change depending on whether PostgreSQL or Redis supplied the record.

**Action:** Add venue filters to latest/history reads and thread `(symbol, venue)` through all engine prior-record reads. Decide whether exact `run_id` is globally authoritative or must also validate the row’s symbol/venue. Add regression tests with two same-symbol rows on different venues and assert Postgres/Redis parity.

### A-04 — State authority is improved, but projection surfaces remain easy to misread

**Severity: P2 — maintainability and semantic drift risk.**

Positive evidence:

- `docs/STATE_CHARTER_SPEC.md` and `tests/test_state_charter.py` enforce canonical owners for horizon values, deterministic-state roots, schema versions, and status families.
- `engine/ledger.py` centralizes event records, while `engine/projections.py` explicitly derives legacy keys from ledger rows and carrier fields. This is a sound separation when projections stay read-only.

Remaining risk:

- A cycle contains nested working state (`plan`, `acquisition`, `understanding`, `scenario_facts`, etc.), a central ledger, and flattened compatibility keys. `build_artifact_state()` deliberately materializes multiple legacy keys from those canonical homes.
- `CanonicalOutput` also exposes selected fields such as `conformance` beside the complete `run_record` containing `deterministic_state`. This may be a useful query projection, but the authority/parity rule is not obvious from the surface alone.
- README and older specs describe `MarketRunEnvelope`, `InferenceArtifact`, `AnalystBriefing`, or Redis as canonical output, which obscures the newer state charter and record plane.

**Impact:** Maintainers may treat a projection as a write authority or assume two serialized views are independently canonical.

**Action:** Document a concise read/write matrix: canonical writer, durable record, live projection, and compatibility projection for each state family. Treat legacy fields as derived/read-only and add parity tests for duplicated query fields. Avoid adding another state registry or abstraction unless a concrete unowned axis is found; the existing charter already addresses several major axes.

### A-05 — Existing audit notes have become historical evidence, not current status

**Severity: P2 — stale audit conclusions can send work in the wrong direction.**

`docs/Planne-based-audit-finidngs.md` reports frozen `market.read`, split hardcoded identity, and several pending repairs. The current uncommitted tree adds `runtime/scope.py`, changes `market.read` to live venue-scoped projections, and adds identity tests. Those portions are no longer accurate as current findings. However, the Postgres venue-filter issue and explicit venue mismatch above remain visible in current code.

**Action:** Preserve the old note for provenance, label it superseded, and track only open items in this audit (or a future issue list). Do not append a second parallel “latest audit” register.

### A-06 — Documentation volume lacks lifecycle signals

**Severity: P2 — excess search cost and avoidable context noise.**

The repository has 50 Markdown documents under `docs/` (about 9,400 Markdown lines across root and `docs/`); the largest file is `docs/DETERMINISTIC_INFERENCE_SURFACE_PLAN.md` at 1,355 lines. Documents mix active doctrines, completed implementation logs, open plans, drafts, imported knowledge-base material, and historical audits. Some already include supersession records, but there is no compact authoritative index separating current runtime contracts from history/plans. The volume itself is not proof of a defect; the lack of lifecycle/authority labels is.

**Action:** Use a small status vocabulary (`active contract`, `implemented record`, `proposal`, `historical/superseded`) and maintain one index. Archive completed plan detail when it no longer guides operation. Keep imported/upstream research clearly segregated from this repository’s own runtime authority. Do not delete useful provenance simply to reduce the count.

## Recommended order of work

1. **Close the identity correctness gap:** venue validation and venue-scoped PostgreSQL read/history APIs, including engine prior-record reads.
2. **Prove the data path:** tests for mismatched venue refusal, same-symbol cross-venue isolation, and parity between PostgreSQL and Redis results.
3. **Reconcile documentation:** make the README and in-package runtime README match the live `InferenceEngine` / `CanonicalOutput` / live-read design; mark the old analyst-suite and collated-run contracts superseded.
4. **Clarify the state contract:** publish one concise authority/read-write matrix; preserve canonical ledger + derived projection structure, and test any duplicated query fields.
5. **Then prune/archive docs:** after status labels are in place, move completed/historical detail instead of creating more overlapping specs.
6. **Run release-grade verification:** full test suite, type/lint checks if configured, then a Docker smoke test against the actual Redis/PostgreSQL topology. This audit did not run those checks.

## Verification performed and limits

- `python -m pytest -q tests/test_identity_scope.py tests/test_state_charter.py tests/test_outer_cycle_charter.py tests/test_governance_boundary.py` — **84 passed**, with two `SyntaxWarning: invalid escape sequence` warnings from charter scans.
- `git diff --check` — clean.
- Static inspection only for Redis/PostgreSQL writer/read wiring and docs. No live services or full test suite were run.
- The checkout was already dirty before this audit: 16 tracked paths modified and 2 untracked paths. The test result applies to that working tree. This audit adds only this report and does not judge whether those changes are ready to commit.

## Acceptance criteria for closing this audit

- A run cannot analyze a venue that has not been declared captured for its symbol; refusal happens before engine/LLM/store writes.
- Canonical PostgreSQL latest/history reads and engine prior-state reads are keyed by `(symbol, venue)`; Redis and PostgreSQL select the same identity.
- The README and runtime-local READMEs describe only existing entry points and current state/output authority.
- Current documents distinguish active contracts from proposals and historical records; old audit findings are explicitly superseded.
- Ledger facts/verdicts remain canonical, derived compatibility projections are not writable authorities, and duplicated persisted query fields have a tested parity rule.
- Full suite and live-runtime smoke checks pass before calling the system verified.

---

## Resolution status — Plane A/B/E pass (2026-10-23)

| Finding | Status | Evidence |
|---|---|---|
| A-01 docs describe retired paths | **partially open** | runtime reads fixed; `README.md` / `nooa_harness/README.md` still describe collated/`AnalystBriefing` |
| A-02 venue not enforced | **closed** | `inference_runner.run_inference_once` refuses `venue_not_captured` before `_build_engine`; `tests/test_plane_abe_contracts.py::VenueRefusalTests` |
| A-03 PostgreSQL reads ignore venue | **closed** | `read_canonical_output(..., venue=)` / `read_canonical_history(..., venue=)`; all 3 call sites thread the cycle venue or the CLI `--venue`; `CanonicalReadVenueTests`, `PriorRecordVenueTests` |
| A-04 projections easy to misread | **closed** | `tests/test_record_plane_contract.py` (idempotence, derived-not-authoritative, conformance parity) |
| A-05 audit notes historical | **closed** | `Planne-based-audit-finidngs.md` retained as provenance; this file carries status |
| A-06 docs lack lifecycle signals | **open** | 28 docs, no index; 3 carry lifecycle labels |

Plane E (memory) closed in the same pass: segment identity is derived from
`(symbol, venue)` + task shape + horizon + a paraphrase-tolerant word
fingerprint (`memory.task_segment_id`), prior gate outcomes reach the prompt as
a non-citable `prior_outcomes` block (`driver._prior_outcomes`), both dispatch
seams carry the segment, and the operator can reproduce the agent's memory view
(`nooa market memory session`).

Acceptance items still unproven: the live Docker/Redis/PostgreSQL smoke run
(the stack was down for every pass) and the README reconciliation above.
