# NOOA Harness Architecture

Status: design contract for the first integration phase

This document defines how the NOOA harness will enter the existing canonical
runtime without replacing its deterministic market pipeline.

## Design position

The harness is a reasoning layer over canonical state, not another market
orchestration layer. The model should have broad freedom to inspect evidence,
make connections, challenge conclusions, and develop scenarios. The system
should constrain authority and unsafe actions, not the model's reasoning.

```text
                         human trader
                              ▲
                              │ review
                              │
                     NOOA analyst object
                              │
              typed state views + approved requests
                              │
                   local Redis canonical state
                              │
      ┌───────────────────────┴───────────────────────┐
      │ immutable run envelopes and domain evidence   │
      │ deterministic outputs, coverage, errors       │
      └───────────────────────┬───────────────────────┘
                              │
                    canonical market runtime
```

## What the agent can do

The agent may:

- read a complete `MarketRunEnvelope`;
- inspect raw evidence alongside deterministic calculations;
- compare exact runs and recent history;
- identify contradictions between data domains;
- request a bounded refresh for an approved symbol and scope;
- ask for missing evidence;
- form hypotheses and alternative scenarios;
- explain uncertainty and data limitations;
- produce an evidence-linked human briefing.

These permissions are intentionally permissive at the reasoning level. The
model can reach its own conclusions instead of being forced through a fixed
decision tree.

## What the agent cannot do

The boundary is defined by authority, not by restrictions on thought. The
agent must not:

- place, size, cancel, or manage trades;
- access exchange private endpoints or credentials;
- access Docker, the host filesystem, or arbitrary subprocesses;
- delete, overwrite, or backdate canonical Redis state;
- write to domain projections or collated streams;
- change deterministic thresholds or calculations;
- turn `null`, stale, or missing evidence into certainty;
- publish a reasoning output as canonical market state.

Agent outputs are advisory artifacts. The trader retains final authority.

## State model

The implementation uses typed, validated, schema-versioned frozen dataclasses
for all analyst-contract objects. Schema version 2 (2026-08-16) tightens
evidence entries, consensus, and key_evidence from loose dicts to typed
sub-objects with required fields and validation:

```python
# Canonical runtime envelope (unchanged v1)
class MarketRunEnvelope:
    run_id: str
    symbol: str
    status: str
    generated_at: str
    completed_at: str
    coverage: dict
    canonical_state: dict
    domain_outputs: dict
    errors: list[dict]
    source_metadata: dict

# Typed sub-contracts (v2 — from contracts.py)
class EvidenceEntry:
    path: str              # required — envelope path
    interpretation: str    # required — what the analyst concluded
    value: Any | None      # optional — raw value at path
    metric_name: str | None  # optional — human-readable label

class Consensus:
    direction: str                    # required — up|down|flat|unknown
    confidence: ConfidenceLevel       # required — low|medium|high
    confidence_score: float | None    # optional — 0.0-1.0 numeric
    timeframe: str | None             # optional — intraday|session|swing
    magnitude: str | None             # optional — marginal|moderate|strong

class KeyEvidence:
    path: str              # required — envelope path
    claim: str             # required — what the evidence shows
    value: Any | None      # optional — raw value
    run_id: str | None     # optional — source run
    specialist: str | None # optional — sourcing specialist

class Disagreement:
    topic: str             # required — subject of disagreement
    specialist_a: str | None
    specialist_b: str | None
    resolution: str | None

# Specialist output (v2 — validated from LLM text)
class SpecialistReport:
    name: str
    run_id: str
    summary: str
    evidence: tuple[EvidenceEntry, ...]
    confidence: ConfidenceLevel
    limitations: tuple[str, ...]
    extra: dict[str, Any]  # specialist-specific fields

# Controller briefing (v2 — validated from LLM text)
class AnalystBriefing:
    session_id: str
    run_id: str
    schema_version: int
    model_provider: str
    model_name: str
    generated_at: str
    narrative: str
    consensus: Consensus
    disagreements: tuple[Disagreement, ...]
    key_evidence: tuple[KeyEvidence, ...]
    limitations: tuple[str, ...]
    uncertainty_sources: tuple[str, ...]
    parse_errors: tuple[dict[str, Any], ...]
    envelope_summary: dict[str, Any]  # enriched with market metrics
    extra: dict[str, Any]
    specialist_reports: dict[str, dict | None]

# Durable memory (v1 — unchanged)
class AgentMemory:
    session_id: str
    kind: str
    content: str
    memory_id: str
    run_id: str | None
    title: str | None
    importance: float
    tags: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    forgotten: bool
```

The canonical envelope remains read-only. All analyst artifacts are
schema-versioned, validated on construction, and persisted Postgres-first
with Redis live projections. The `AnalystBriefing` and `SpecialistReport`
are the ONLY sanctioned boundaries that turn LLM text into typed state —
nothing reaches the durable stores without crossing them.

## Redis integration

The agent connects to the local Redis service using a read-scoped identity.
The implementation may use the existing Redis adapter internally, but the
agent receives typed state objects rather than infrastructure administration.

Read surfaces:

```text
marketflow:run:<RUN_ID>
marketflow:latest:<SYMBOL>:collated
marketflow:stream:collated:<SYMBOL>
marketflow:runtime-run:<RUN_ID>
marketflow:latest:<SYMBOL>:raw              # poller firehose (point-in-time)
marketflow:stream:raw:<SYMBOL>              # bounded
marketflow:latest:<SYMBOL>:derivatives      # on-demand batch (TTL 300s)
marketflow:stream:domain:derivatives:<SYMBOL>
marketflow:latest:<SYMBOL>:{data-access|calculations|analysis}
marketflow:stream:domain:{data-access|calculations|analysis}:<SYMBOL>
```

Agent-owned write surfaces:

```text
marketflow:agent:<SESSION_ID>:observations
marketflow:agent:<SESSION_ID>:hypotheses
marketflow:agent:<SESSION_ID>:requests
marketflow:agent:<SESSION_ID>:briefings
```

The split between poller (point-in-time) and run-cycle (historical) data is
documented in `docs/RUNTIME_DATA_AUTHORITY.md`. The analyst never calls
Binance itself; the on-demand derivative fetch is owned by the runtime and
exposed to the analyst via the outer `harness.py --refresh-derivatives`
command and implicit fetching inside the `harness.py --analyze` cycle.

The NOOA inner CLI is **calculation-agnostic**: its `analyst` command runs
the agent suite and reads the canonical ledger that `harness.py --analyze`
populates. Calculation flags (`--deriv-ttl`, `--with-cross-asset`,
`--no-derivatives`, `--force-refresh-derivatives`) live on `harness.py`,
not on `nooa market analyst`. NOOA itself will be reworked in a future
pass to be driven by mathematical / statistical derivations defined in a
later specification; the calculation pipeline remains the single source of
truth for canonical numbers.

The model should prefer exact run reads. The `latest` key is useful for live
orientation but can advance while the autonomous runner continues producing
cycles.

## Request semantics

Refresh requests remain typed and bounded:

```text
symbol: SOLUSDT
scope: all | order_book | trades | funding | open_interest | tickers
reason: human-readable evidence request
session_id: agent session identifier
```

The request enters the existing harness/orchestrator path. NOOA does not call
exchange clients directly and does not create a second collection pipeline.

## CLI mount (current integration state)

The NOOA CLI is mounted at the canonical harness mount point without
mutating the installed `nooa-cli` package. The runtime authority split is:

```text
harness.py                          OUTER CLI
├── clean market-data contract      (no direct nooa_harness.* imports)
└── --nooa ─► nooa_cli.py           INNER CLI (mounted into the framework
            │                       ``oo`` group at import time)
            └─► nooa_cli_ext.py     the ``market`` click group
                                    (envelope, briefing, memory, analyst)
                                    ─── sole direct caller of
                                        market_service/nooa_harness/*
```

Concretely:

- `market_service/commands/nooa_cli_ext.py` — the `market` harness group
  (`envelope`, `briefing`, `memory`, `analyst`), backed by the canonical
  runtime stores. **Sole direct importer of `market_service/nooa_harness/*`.**
- `market_service/commands/nooa_cli.py` — repo-root mount: attaches the
  `market` group to the framework root `oo` group at import time and
  delegates to the normal CLI entry. One mount covers the VSCode-shell
  wrapper (`./nooa`), the `nooa-market` console script, the Docker `nooa`
  compose service, and the harness passthrough (`harness --nooa market ...`).
- `market_service/commands/harness.py` — the OUTER canonical harness CLI.
  Emits the clean aggregated market-data contract and routes every analyst
  / briefing / memory / agent operation through `--nooa` into the inner
  NOOA CLI. **No direct `nooa_harness.*` imports.**

`nooa`/`nooa-cli` are pinned at `0.0.8` from PyPI in `requirements.txt`.
The agent classes (`agents.py`) are validated against the 0.0.8 API
surface (`Agent`, `Context`, `spec`, `strategy`, `DynamicContext`,
`CodeActStrategy`, `PredictStrategy`, `agentdoc.hidden`).

Terminal-based coding agents (pi, hermes, claude code) shell out to the
outer `harness.py` for clean market data, and to
`harness.py --nooa market …` for any analyst / briefing / memory / agent
operation. They never reach the subordinate `nooa_harness` runtime module
directly — that path is reserved for the inner NOOA CLI.

## Model backend seam

Model configuration belongs inside the NOOA package, not inside market data
nodes:

```text
NOOA_MODEL_PROVIDER=openai|ollama|vllm
NOOA_MODEL_NAME=<provider model>
NOOA_MODEL_BASE_URL=<optional compatible endpoint>
```

The initial implementation should support one backend at a time and keep the
agent state contract independent of the backend. Model prompts, model name,
temperature, and session identifiers must be recorded with each briefing.

## Observability

Every agent result should record:

- `session_id`;
- `run_id` or run IDs inspected;
- model provider and model name;
- prompt or doctrine version;
- requested refreshes;
- evidence references;
- claims and uncertainty;
- completion status and errors;
- timestamp and latency.

This makes the analyst layer auditable without treating its interpretation as
canonical truth.

## Implementation phases

### Phase A — contract-only boundary

- keep the package isolated and importable without NOOA installed;
- define typed state and agent artifact objects;
- define Redis key and write namespaces;
- add serialization and null-semantics tests.

### Phase B — read-only state access

- add the read-scoped Redis client;
- load exact run envelopes;
- load bounded recent history;
- reject unsupported schema versions;
- add replay fixtures from recorded envelopes.

### Phase C — controlled refresh requests

- connect typed request objects to the existing harness request stream;
- enforce symbol and scope validation;
- return the exact resulting `run_id`;
- keep refresh requests separate from canonical writes.

### Phase D — NOOA analyst object

- add the NOOA dependency in an optional analyst environment;
- instantiate one read-only analyst class;
- require evidence-linked observations and hypotheses;
- test with recorded runs before live data.

### Phase E — live analyst validation

- run against `SOLUSDT` only;
- require the Docker verification doctrine to pass;
- compare analyst outputs against the same immutable run;
- preserve human review as the final step.

## Known issues — analysis register (pre-fix-pass state)

This section is the current analysis of issues observed in the runtime. It
is tracked here so the next fix pass has one register to act against. It is
**analysis only** — nothing here has been changed inside the system yet.

### NOOA-SECURITY-1 — ControllerAgent runs code via `CodeActStrategy` (critical) **— FIXED**

**Status: resolved.** `ControllerAgent.synthesize` previously ran model-emitted
Python via `@strategy(CodeActStrategy(), ...)` inside the harness container
(the only code-executing agent). It now follows the same non-code-executing,
JSON-only path as the 4 specialists (`_call_model_once` → one `llm.acall`,
output parsed by `AnalystBriefing.from_controller_text`). No model-supplied
code is executed; the model only returns JSON text. Local behavior is
unchanged — same inputs (bounded envelope + specialist reports) and the same
JSON narrative output contract. The worked-out rationale (recorded below)
remains as the analysis that drove the fix.

`ControllerAgent.synthesize` (`market_service/nooa_harness/agents.py`, the
`@strategy(CodeActStrategy(), ...)` decorator) is the **only** agent that lets
the model emit raw Python that is executed inside the harness container. The
worked-out rationale (verbatim TODO in the source):

> `CodeActStrategy` lets the model emit Python that executes inside the
> harness container. This is acceptable while the only envelope-side
> capability is the read-only `MarketRunEnvelope`, but the doctrine
> explicitly flags the container boundary as security-critical before live
> credentials or broader capabilities are exposed. Replace with a
> non-code-executing strategy (e.g. `PredictStrategy` + strict JSON-only
> response contract) or sandbox generated code before any capability
> expansion lands.

**Surface:** the 4 specialists (`DeltaOrderflowAgent`, `MacroAgent`,
`OpenInterestAgent`, `LiquidationAgent`) use the safe one-shot path
(`_call_model_once` + JSON parse). The controller is the only code-executing
path. Its prompt literally instructs the model to "read the envelope and
specialist reports, then produce the JSON above".

**Why this is a security boundary:** that code runs inside a container that
holds exchange credentials (`BINANCE_BASE_URL`), `CRYPTOQUANT_API_KEY`,
`NOOA_API_KEY`, plus Postgres + Redis network access and the in-memory
envelope. Today the code only has read access to the envelope, so it is
"acceptable"; the moment broader capabilities are exposed (Redis/Postgres
writes, exchange/fetch access, env/secrets reads) this becomes **arbitrary
code execution** inside that boundary.

**Recommended path (primary):** switch the controller to `PredictStrategy`
+ strict JSON-only response contract — one-shot text completion like the
specialists. The deterministic pipeline already did the math, so the model
does not need to compute programmatically. Simpler and safer.

**Alternate path:** keep `CodeActStrategy` but sandbox execution — a
restricted runtime with no import / file / network / DB access, exposing only
a read-only envelope view. Preserves model computation at the cost of a
sandbox to maintain.

**Deferred-until-fixed:** capability expansion, live credentials, or any
non-read-only surface must not land while `CodeActStrategy` remains.

### RUNTIME-1 — non-atomic poller latest/stream write (latent) **— FIXED**

**Status: resolved.** `poller.poll_symbol` previously called `set_raw_latest`
(SET) then `append_raw_evidence` (XADD) as two round-trips, leaving a ~1 RTT
window where a reader could see a latest with no matching stream entry. These
methods are replaced by a single atomic `RedisRuntimeStore.publish_raw_evidence`
(Lua script that SETs latest + XADDs the stream in one step), so no reader can
observe a latest snapshot whose stream entry is missing. Verified live against
the running Redis.

### RUNTIME-2 — dead orchestrator command surface (dead code) **— FIXED**

**Status: resolved.** Removed the entire orphaned control surface from the
pruned orchestrator architecture, which published into streams nothing read
(`stream:harness:requests` held 24 orphaned entries):
- `redis_store`: `request_refresh`, `request_harness_run`, `consume_commands`,
  `consume_harness_requests`, `publish_result`, `read_results`, `wait_for_result`,
  `write_runtime_run`, `read_runtime_run`, `run_state_key`, and the
  `command_stream`/`result_stream`/`harness_request_stream` properties.
- contracts: `RefreshCommand`, `HarnessRunRequest`, `RuntimeRunState`, `MarketEvent`
  (and `RuntimePhase`); re-exported symbols removed from `runtime/__init__`.
- `commands/harness.py`: dropped `--trigger`/`--domain`/`--scope`/`--timeout` and
  `trigger_domain`/`trigger_full_cycle`; `commands/nooa_cli_ext.py`: dropped the
  `refresh` subcommand; deleted the last orphan `market_service/nodes/`.
The pipeline now runs directly via `--analyze` — no dead control plane remains.

### RUNTIME-3 — always-on `nooa` compose service crash-loops **— FIXED**

**Status: resolved.** The compose `nooa` service is now on-demand only
(`profiles: ["tools"]`, `restart: "no"`) and invoked with explicit args
(`docker compose --profile tools run --rm nooa market ...`), so it no longer
runs `nooa_cli.main([])` with no subcommand (the source of the
`NoArgsIsHelpError` crash-loop).

### RUNTIME-4 — no idempotency on `append_raw_evidence` (moot today) **— FIXED**

**Status: resolved.** The atomic `publish_raw_evidence` adds a per-snapshot
idempotency guard (keyed on `observed_at_ms`, TTL) so a re-delivered snapshot
from a concurrent poller is deduped (no duplicate XADD, no double-append).
Verified live.

## Readiness condition

NOOA is ready for live read-only analysis only when:

1. exact run IDs are stable across Redis and PostgreSQL;
2. scope requests preserve null and unavailable semantics;
3. the agent can read complete envelopes without infrastructure access;
4. agent artifacts cannot overwrite canonical state;
5. recorded-run tests pass;
6. model, prompt, evidence, and uncertainty are observable.

The first code should therefore implement Phase A and Phase B. It should not
yet add trading capabilities, multi-agent orchestration, memory mutation, or
autonomous execution.
