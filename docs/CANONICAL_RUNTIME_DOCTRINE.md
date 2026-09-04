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
