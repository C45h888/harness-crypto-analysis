# Canonical Market Runtime Doctrine

## Purpose

This system transforms fragmented market telemetry into coherent, auditable
market state for a discretionary trader. It is an analytical system, not an
autonomous trading system.

## Core principle

No component may treat raw data as meaning. Raw data must be normalized,
timestamped, checked for freshness and coverage, transformed into deterministic
metrics, classified into explicit conditions, and stored with its supporting
evidence.

## System authorities

### Data nodes

Data nodes retrieve source data for one defined responsibility, such as spot
flow, futures flow, order books, open interest, or macro context. They do not
interpret market meaning.

### Redis

Redis is the operational nervous system. It carries latest ticker state,
telemetry streams, refresh commands, node health, and refresh results. Redis is
not the permanent historical authority.

### PostgreSQL

PostgreSQL is the institutional memory. It stores validated snapshots,
deterministic signals, source evidence, assessments, human decisions, replay
data, and evaluation results.

### Canonical runtime

`market_service/` is the source of truth for coherent market state. It decides
whether data is usable, whether coverage is sufficient, which deterministic
conditions are active, and whether state is healthy or degraded.

### Reasoning agents

NOOA, or any future analyst model, is a reasoning participant inside the
runtime. It may request approved refreshes, read typed state, inspect evidence,
compare conditions, produce scenarios, and identify missing evidence. It may
not access Docker directly, bypass the runtime, redefine thresholds, turn
missing data into neutral data, or execute trades.

### Canonical output

The cycle's deliverable is **the answer**: the agent's authored prose with its
gate data, composed by the chartered answer owner
(`nooa_harness/engine/core/answer.py`) and delivered to the operator through
the CLI surface. The canonical output row (`canonical_output` /
`runtime/contracts.CanonicalOutput`) is the answer OF RECORD — frozen at
delivery — plus its run record. The run record is engine bookkeeping and is
never the consumable output.

### Trader

The trader retains final decision authority. No model output is an instruction
to trade; it is an evidence-linked analytical proposal for human review.

## State doctrine

Every market state must be symbol-specific, time-bounded, schema-versioned,
source-attributed, coverage-aware, and explicit about missing values. Published
state is immutable.

State is one of:

- `healthy` — required sources are present and adequately covered.
- `degraded` — analysis is possible, but one or more sources or coverage
  requirements are incomplete.
- `invalid` — the state must not be used for analysis.

The system must never silently substitute zero, neutral, or stale values for
unavailable evidence.

## Interaction doctrine

Agents do not control infrastructure directly. They use typed runtime
capabilities such as:

- request a bounded refresh;
- read latest state;
- read recent events;
- inspect evidence;
- run a deterministic calculation;
- submit an assessment.

The runtime may implement these capabilities using Redis, PostgreSQL, or data
nodes without exposing those infrastructure details to the agent.

Across Docker boundaries, these capabilities are represented by versioned
domain contracts rather than raw database rows or hand-built prompts.

## Evidence doctrine

Every interpretation must be traceable to a snapshot identifier, deterministic
signal identifiers, source timestamps, coverage metadata, model and prompt
versions, and a runtime trace. An assessment without evidence references is
incomplete.

## Determinism doctrine

The deterministic layer establishes facts and machine-readable conditions. The
reasoning layer establishes relationships, scenarios, uncertainty, and review
questions.

The reasoning layer may challenge a deterministic conclusion only by identifying
a data-quality issue or requesting further evidence. It may not silently replace
the deterministic result.

## Change doctrine

The repository evolves in this order:

1. define domain contracts;
2. consolidate the canonical runtime;
3. establish Redis and PostgreSQL responsibilities;
4. migrate data nodes to those contracts;
5. validate replay and failure behavior;
6. introduce NOOA;
7. add governed multi-agent behavior only after the single-agent path is
   trusted.

No agent abstraction should be added until the state model and authority
boundaries are stable.

## Operating objective

The system should become more machine-like by reducing ambiguity, not by
pretending uncertainty does not exist:

```text
coherent state
+ deterministic conditions
+ preserved evidence
+ explicit uncertainty
+ human-controlled decision
```

## State charter

The runtime's state model is chartered: one canonical owner per state axis
(horizon identity → `runtime/horizons.py`; loop identity →
`nooa_harness/loop_states.py` incl. the `LoopTag` crosswalk; the
deterministic_state keyspace → `engine/core/context.py` roots registry;
schema versions → `runtime/contracts.py` registry; status families → their
per-plane owners, pairwise disjoint; **read/calc cadence →
`engine/cadence.py`** — `INTERVAL_SECONDS` {10,15,30} × `WINDOW_MINUTES`
{15,30,60}, bound by the plan, adopted by the controller; **the answer
(canonical output) → `engine/core/answer.py` format + PLACEMENT delivery,
contract `runtime/contracts.CanonicalOutput`**). Planes consume
owners by IMPORT, never by mirrored constants; silent defaults that map
unknown state identities to authorized behavior are governance errors.
Enforcement lives in `tests/test_state_charter.py` +
`tests/test_outer_cycle_charter.py` + `tests/test_governance_boundary.py`;
the full doctrine is `docs/STATE_CHARTER_SPEC.md`.

## Governance boundary (agentic plane)

The FSM (`engine/fsm.py`) is the SOLE source of legality and semantic
authority over the agentic loops:

- **Transitions** — every loop/sub-loop/intent move is a governed event;
  denials are findings, never silent auto-advance.
- **Work authorization** — every tool dispatch authorizes against the live
  `(loop, sub-loop)` observation; the tool registry's declared homes are
  FSM-audited at import (a home the board cannot express fails at load).
- **Cadence** — the CLI is the SOLE approval channel; the LLM can never
  bind cadence. The pure resolver refuses non-CLI sources and
  out-of-charter horizons (no silent defaults); the FSM adjudicates the
  verdict at adoption; agent-side cadence ARGS are validated against the
  frozen vocabulary at the dispatch seam (out-of-vocabulary values are
  denied findings carrying the supported set).
- **Budgets** — governance owns the verdict (`GateKind.BUDGET`), config
  owns the numbers; the driver consults the gate before every round and
  names everything that will not run on `unexecuted` (never a silent drop).
- **Failure** — all failure semantics live in the Failure FSM
  (`engine/failure_fsm.py`) behind the single `CYCLE_FAILED` intake edge;
  the main membrane routes, it does not classify.

The controller is the FSM's sole semantic executive (classification,
credit, tri-state, adoption); the driver is the transport (consults,
never self-authorizes). The LLM proposes; every proposal crosses a
deterministic boundary before it can touch state. The outer cycle is the
receipt-gated 7-stage traversal (wake → comprehension →
plan_bound_acquisition → evidence → reasoning → validation → output) —
no stage runs on another stage's promise, and no pre-INTAKE acquisition
exists anywhere (the fetch semantic and DATA gate are dissolved; run_wake
owns INTAKE deterministically).

## Gate substrate and the record plane (2026-10-20 consolidation)

One deterministic home for every guard and every loop close:

- `engine/gates.py` owns the gate vocabulary (`GateKind`, `GateStatus`,
  `GateDisposition`, `GateVerdict`), the worker registry (`GATE_WORKERS`,
  `evaluate`, `require`), the single refusal router (`route_refusal`), and
  verdict recording (`record_verdict`, `close_loop`). Workers take a frozen
  snapshot and return a verdict; they perform no I/O and never mutate state.
- **A loop close is a gate** (`GateKind.CLOSE`). `receipts.py`,
  `LoopReceipt`, and `ReceiptDisposition` are retired; the ledger `VERDICT`
  row is the canonical record of what ran and how it closed.
- `engine/ledger.py` is the engine bedrock: `UNDERSTANDING`, `PLAN`,
  `HANDOFF`, `OBSERVATION`, plus audit kinds `VERDICT`, `FINDING`,
  `SCENARIO`. Only the four context kinds may enter narration prompts.
- `engine/projections.py` derives the permanent artifact keys
  (`loop_receipts`, `validation_findings`, `chain_gaps`, `synthesis_note`,
  `assessment_note`, `assembly_dropped`, `evidence_salvaged`, …) from ledger
  rows at artifact write. Projections are not live authorities.
- `inference/gate.py` (the validated/provisional/insufficient trichotomy) is
  retired; data sufficiency is adjudicated by per-tool deterministic
  refusals plus the CONTENT gate.
- Agent memory has one home: `memory.MemoryNode` plus its
  `AgentMemorySurface` facade. `memory_skill.py` is deleted; the engine never
  reads agent memory.
