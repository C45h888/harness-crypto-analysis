# NOOA Harness Boundary

This directory is the future integration boundary for NVIDIA NOOA. It is not
the market runtime and it is not an execution engine.

The canonical runtime remains authoritative for:

- external data collection;
- freshness and coverage checks;
- deterministic calculations;
- market status and error classification;
- PostgreSQL persistence;
- Redis canonical state;
- run identity and replayability.

The NOOA harness will be responsible for reasoning over that state. It may
inspect complete market envelopes, compare runs, request approved refreshes,
form hypotheses, explain uncertainty, and prepare a human-review briefing.

## Intended package shape

```text
market_service/nooa_harness/
├── __init__.py
├── state.py          # typed views of canonical Redis state
├── capabilities.py   # narrow typed bindings to approved runtime actions
├── analyst.py        # future NOOA Agent class
├── prompts.py        # reasoning doctrine and response structure
├── backends.py       # OpenAI-compatible, Ollama, vLLM configuration
└── tracing.py        # agent/session/run observability
```

The package starts as a contract boundary. NOOA should be added only after the
state types and tests are stable.

## Direct Redis principle

The future agent may read the local Redis canonical state directly through a
read-scoped Redis client. This is intentionally not a second orchestration
plane. Typed classes describe the state; they do not force the model through a
rigid chain of reasoning.

The agent must read complete run envelopes when possible:

```text
marketflow:run:<RUN_ID>
marketflow:latest:<SYMBOL>:collated
marketflow:stream:collated:<SYMBOL>
```

`latest` is a moving projection. Exact analysis should use the immutable
run-addressed key and retain the `run_id` in the response.

## Write boundary

Agent artifacts must use a separate namespace:

```text
marketflow:agent:<SESSION_ID>:observations
marketflow:agent:<SESSION_ID>:hypotheses
marketflow:agent:<SESSION_ID>:requests
marketflow:agent:<SESSION_ID>:briefings
```

The agent must not overwrite canonical state, domain projections, runtime
records, or collated streams.
