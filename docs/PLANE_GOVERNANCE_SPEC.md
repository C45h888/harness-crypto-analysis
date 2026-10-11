# Plane Governance Spec — Two Deterministic Legality Engines

Status: **implemented**. Companion to
[`SUBSTRATE_WORKER_SPEC.md`](SUBSTRATE_WORKER_SPEC.md) and
[`ANALYSIS_WORKER_SPEC.md`](ANALYSIS_WORKER_SPEC.md) (the two planes this
governs). Governing doctrines: `CANONICAL_RUNTIME_DOCTRINE.md` (null
discipline, evidence-first) and `STATE_CHARTER_SPEC.md` (one owner per axis;
silent defaults that map unknown state to authorized behavior are governance
errors).

## 0. What this is

Two SEPARATE governance engines — one for the calculation worker plane, one
for the analysis worker plane. Each is the **sole source of its plane's
legality**. They decide whether a worker may fire, whether its payload may
enter the plane's keyspace, and whether the fire may be durably recorded in
Postgres.

```
CalculationGovernanceFSM ──owns──▶ calculation substrate manifests ──▶ 12 calc workers
AnalysisGovernanceFSM ────owns──▶ analysis substrate manifests   ──▶  6 analysis workers
```

## 1. Doctrine (non-negotiable)

1. **Entirely deterministic. No ticker logic at all.** There is no timer, no
   sleep, no polling loop, and no clock-as-authority. A wake is a Redis
   stream arrival or an explicit invoke — the same doctrine as
   `substrate_worker/core/supervisor.py` ("blocking reads are the only
   cadence; no timer task"). A timestamp is only ever an *input to a
   predicate*.
2. **The FSM is the sole legality authority.** A fire, a publish and a PG
   write are legal only if the plane's FSM says so.
3. **Authority flows FSM → substrate manifest → worker binding.** A
   "substrate" is not a runtime object — it is a pure calculation module. The
   thing the FSM actually controls is the **manifest**: the declarative
   authority record (evidence layer, dependencies, keyspace, cadence) derived
   from the plane's own declarations. The manifest parameterizes the worker's
   binding; the FSM never touches the worker.
4. **No contractual connection to the workers.** Governance speaks only in
   `(plane, substrate, symbol, payload_dict)` primitives. It never imports a
   worker class, never instantiates one, never references
   `SubstrateWorkerCore` or `RedisRuntimeStore`. The worker calls the
   governor by duck-typed method name; the governor never calls the worker.
5. **The FSM checks the outputs inside Redis.** Every decision is made over a
   `GovernanceObservation` that carries both the proposed payload and the
   projection currently in the plane's own `latest` key — so the FSM can
   refuse an out-of-order write that a worker alone would produce.
6. **PG writes are gated.** The `PG_WRITE` gate is the sanity gate that runs
   immediately before `record_substrate_state`. A denial suppresses both the
   durable row and the Redis projection: an ungoverned fire must not become
   visible state.
7. **Rational boundary.** Every verdict carries a non-empty `reason` and a
   `rule` id. A denial without a named rule is impossible by construction
   (`GovernanceVerdict.__post_init__` raises).

## 2. Package layout

```
market_service/governance/
    contracts.py       typed surface (manifest, observation, event, verdict)
    fsm.py             the shared PURE mechanism + common rule set
    calculation_fsm.py CalculationGovernanceFSM — all calc-plane legality
    analysis_fsm.py    AnalysisGovernanceFSM    — all analysis-plane legality
    manifest.py        the ONE derivation seam (reads plane declarations)
    gates.py           GovernanceGovernor — the I/O adapter
    ledger.py          GovernanceLedger   — Redis + PG audit
```

`manifest.py` is the only module in the package that touches a plane, and it
shares less: it reads **class-level declarations** only, never instantiates or
calls a worker. This is pinned by `tests/test_plane_governance.py::
GovernancePurityTests`.

## 3. Contracts

| Object | Meaning |
|---|---|
| `SubstrateManifest` | the authority record: `primary_stream` (the primary evidence layer), `input_streams`, `dependencies`, `derived_inputs`, `writes_keyspace`, `allowed_sections`, `schema_version`, `status_family`, cadence |
| `GovernanceObservation` | what the FSM sees: the proposed payload's facts + the plane's current `latest` projection (previous status / schema / computed_at) |
| `GovernanceEvent` | `(gate, substrate, payload)` |
| `GovernanceVerdict` | `(allowed, reason, rule, findings, remedy)` — reasoned, never raises |

## 4. The rule set

Common rules (inherited by both planes):

| Rule id | Denies when |
|---|---|
| `unknown-substrate` | the substrate is not registered for this plane |
| `plane-mismatch` | a registered manifest belongs to the other plane |
| `manifest-invalid` | the authority record has no evidence layer / keyspace / status family |
| `keyspace-trespass` | the payload targets a keyspace the substrate does not own |
| `status-invalid` | the status is outside the declared family |
| `schema-drift` | `schema_version` disagrees with the registry |
| `output-fabricated` | `insufficient_data` without named `missing_inputs` |
| `trigger-missing` | a durable row would record no WHY |
| `out-of-order-write` | the proposed `computed_at_ms` precedes the projection already in Redis |
| `binding-drift` | the worker's RUNTIME input surface diverges from the authority record (symmetric: extra OR missing) |
| `observation-unavailable` | the plane's current projection could not be read — the FSM cannot prove the output is in order |

Plane rules:

| Rule id | Plane | Denies when |
|---|---|---|
| `section-out-of-graph` | calculation | a manifest claims a section absent from `SUBSTRATE_GRAPH` |
| `analysis-no-substrate-root` | analysis | an analysis worker declares no `substrate:*` input (an analysis worker must be a reduction of substrate state, never a raw re-derivation) |

### On `binding-drift` (why it is not theater)

The FIRE gate compares the declaration the **running worker reports** against the
manifest the FSM holds, in **both** directions. Because the manifest is derived
once at governor-build time from the plane's registrations, this catches
exactly the failures a self-reported check can catch — and only those, which is
why it is stated narrowly:

- a worker whose input surface changed after the manifest was derived;
- a governor attached to the wrong worker class;
- a stale or mismatched manifest.

It does **not** claim to observe which bytes the worker actually read. That is a
future, stronger check (a read-log surface), and pretending otherwise would be a
false invariant.

## 5. The three gates and where they attach

```
_fire:
  ┌─ authorize_fire      (FIRE)      — may this substrate compute?
  │    ↓ denied → return (nothing burned)
  └─ dedupe → _fired += 1 → _last_fire_ms
       └→ _fire_guarded:
            compute → payload
            ├─ authorize_publish   (PUBLISH)   — may this payload enter the keyspace?
            ├─ authorize_pg_write  (PG_WRITE)  — may this fire be durably recorded?
            └─ record_substrate_state → _publish_own_state
```

**Gate order is the contract.** The FIRE gate runs *before* the dedupe
high-water, the cooldown stamp and the fired counter. A denial must not burn the
dedupe slot — otherwise a denied condition would permanently suppress its own
retry — must not burn the cooldown window, and must not be counted as a fire.

**One ledger row per fire cycle.** A denial is recorded at the gate that denied
(it is evidence). An allowance is recorded only at the **last** gate the cycle
reaches: `PG_WRITE` when a durable ledger is attached, otherwise `PUBLISH`.
This keeps the audit one-row-per-fire so real denials are never buried in
allow-noise. The worker implements this with `evaluate` (many) + `record` (once).

The core accepts a `governor` keyword (`SubstrateWorkerCore.__init__`).
`governor=None` is **byte-identical legacy behaviour** — the entire existing
suite is unchanged. A governor error fails **closed** (an unaskable FSM must
not authorize) and is logged loudly; a denial records
`_last_dormant_reason` so dormancy is observable.

Enable/disable per plane via env (`SUBSTRATE_GOVERNANCE`,
`ANALYSIS_GOVERNANCE`; default ON in the containers/runners).

**Analysis plane reuse.** The analysis plane (`ANALYSIS_WORKER_SPEC.md`) reuses
this same core and therefore the same seam; its manifest carries the
`substrate:*` evidence layer and the analysis keyspace.

## 6. Observability contract

Redis keys, written by the FSM's ledger only (a worker plane never touches
them):

| Object | Key |
|---|---|
| Latest decision | `marketflow:latest:governance:<plane>:<substrate>:<SYMBOL>` |
| Decision stream | `marketflow:stream:governance:<plane>:<SYMBOL>` (MAXLEN ~10k) |
| Denials-only stream | `marketflow:stream:governance:denials:<plane>:<SYMBOL>` |
| Supervisor heartbeat | `marketflow:governance:<plane>:<SYMBOL>:supervisor` (TTL) |

The latest + stream writes are one atomic Lua `SET`+`XADD` (same
ordering-hole closure as `publish_substrate_state`); denials are additionally
appended to the denials-only stream.

**The supervisor heartbeat is decision-driven, not timer-driven.** Every
decision refreshes the TTL key in the same Lua script. A plane that keeps
deciding is alive; one that stops is not. There is no timer anywhere.

`read_governance_latest` with **no substrate** reads the HEAD of the decision
stream (never a literal `*` key, which nothing writes) — so a plane-wide query
works.

HTTP: both containers expose `GET /governance?symbol=&substrate=` returning
`{plane, symbol, alive, available, latest, denials[], decisions}`; `GET /health`
additionally reports `governance: {plane, alive}`.

## 7. Persistence contract

```sql
CREATE TABLE governance_ledger (
    id BIGSERIAL PRIMARY KEY,
    plane TEXT NOT NULL, substrate TEXT NOT NULL, symbol TEXT NOT NULL,
    gate TEXT NOT NULL, allowed BOOLEAN NOT NULL,
    rule TEXT NOT NULL, reason TEXT NOT NULL,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    observation JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version INTEGER NOT NULL DEFAULT 1
);
```

A DENIED decision is durable too — a denial is evidence about the system and
must survive the process that produced it. Migration:
`alembic/versions/0014_governance_ledger.py` + `db/init/001_schema.sql`.

**Ledger identity (the join key).** The governance ledger is keyed
`(plane, substrate)` and keeps the bare substrate. The payload ledger
(`substrate_calculation`) has no plane column, so the analysis plane's rows are
namespaced `analysis:<name>` by the single owner
`substrate_worker.contracts.ledger_substrate_name()`. Join the two on
`(plane, substrate)` vs `(substrate_calculation.substrate)` with that helper —
the derivation exists once, so the two cannot drift.

**Failures are counted, never silent.** `GovernanceLedger.stats()` reports
`decisions / allowed / denied / redis_written / redis_failures / pg_written /
pg_failures / last_error / durable`. A ledger write failure increments a counter
instead of vanishing; `Governor.inspect()` and `GET /governance` surface it.

## 8. Invariants (test-pinned, `tests/test_plane_governance.py`)

1. No governance module imports a worker class, `SubstrateWorkerCore`,
   `redis_store` or `postgres_store` — **no contractual connection**.
2. `manifest.py` is the documented derivation seam and reads declarations
   only.
3. A verdict without a non-empty `reason` and `rule` is impossible.
4. `insufficient_data` without named `missing_inputs` is denied.
5. Calculation denies any non-`substrate` keyspace; analysis denies any
   non-`analysis` keyspace.
6. Analysis denies any binding with no `substrate:*` input.
7. No sleep, scheduled task, `Timer`, or `create_task` exists anywhere in the
   package (AST scan) — no ticker logic.
8. `self_check()` proves every plane manifest is valid and every declared rule
   id is real.
9. `governor=None` leaves the existing worker suite byte-identical.
10. The status vocabulary is **imported from its owner**, never mirrored
    (`plane_status_vocabulary() == substrate_worker.contracts.PAYLOAD_STATUSES`).
11. The FIRE gate precedes dedupe/cooldown/counter, and a fire cycle writes
    exactly one ledger row.

## 9. Non-goals

- No change to the reasoning plane ("cyclical controlling node"). The
  `nooa_harness` `AgenticLoopMembrane` is untouched and independent.
- No timer-driven supervisor heartbeat for governance; liveness rides the
  decision write.
- No cross-plane message bus. The two FSMs are independent legality engines;
  their only shared surface is the ledger and their disjoint keyspaces.
- The analysis roster (auction / path_absorption / macro) is not grown here.

### Declared ungoverned surfaces (out of bounds as of now)

The seam is complete over the **long-running worker loop**. These surfaces are
deliberately NOT governed yet, and are listed so the gap is visible rather than
silent:

| Surface | Why out of bounds |
|---|---|
| `substrate_worker/tools.py::invoke` (used by the container `POST /invoke`) | the refinement vector / NOOA invoke path needs better working semantics first |
| `analysis_worker/tools.py::invoke_one` | same |
| `runner.run_once()` | the runner's semantic boundary is being reworked |

The capture/transport plane (`publish_raw_evidence`) is outside governance by
design — it is upstream of every substrate manifest.

The process-global `_EVIDENCE_CACHE` shares one evidence window across workers
of a symbol. Governance sees the window's *content* (`freshness.input_fingerprint`)
but not the sharing; per-worker input divergence inside one process is invisible
to the FSM. Declared, not hidden.
