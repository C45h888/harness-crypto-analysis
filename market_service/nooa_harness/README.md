# NOOA Harness Boundary — subordinate runtime module

This directory is the **subordinate** NOOA analyst runtime. It is not the
market runtime and it is not an execution engine.

The canonical runtime remains authoritative for:

- external data collection;
- freshness and coverage checks;
- deterministic calculations;
- market status and error classification;
- PostgreSQL persistence;
- Redis canonical state;
- run identity and replayability.

The NOOA harness is responsible for reasoning over that state. It receives
a complete canonical envelope from `market_service/commands/nooa_cli_ext.py`
(the inner NOOA CLI), and may form hypotheses, explain uncertainty, and
prepare a human-review briefing.

## Runtime authority split

```text
harness.py                          outer CLI
├── clean market-data contract (no agents)
└── --nooa ─► nooa_cli.py           inner CLI (mounted into the framework
            │                       ``oo`` group at import time)
            └─► nooa_cli_ext.py     the ``market`` click group
                                    (envelope, briefing, memory, analyst)
                                    ─── sole direct caller of THIS directory
```

`market_service/commands/harness.py` does **not** import anything from
`market_service/nooa_harness/`. Every analyst / briefing / memory / agent
operation is reached from the outer harness via `--nooa`, which routes
into the inner NOOA CLI, which is the sole direct caller of this
subordinate runtime module.

## Package shape

```text
market_service/nooa_harness/
├── __init__.py
├── agents.py         # controller and specialist NOOA Agent classes
├── suite.py          # one controller plus specialist composition
├── runner.py         # run_analyze_once orchestrator (sole caller: nooa_cli_ext)
├── pipeline.py       # harness-owned read→calc→analyze→collate→persist
├── memory.py         # MemoryNode over the agent-namespace stores
├── contracts.py      # explicit contract validation for adapters
└── backends.py       # OpenAI-compatible, Ollama, vLLM configuration
```

Start the analyst suite through the inner NOOA CLI (reached from the
outer harness via `--nooa`):

```bash
# Outer harness → inner NOOA CLI → this module
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.harness --nooa market analyst SOLUSDT --cycles 1 --with-memory

# Or invoke the inner CLI directly:
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.nooa_cli market analyst SOLUSDT --cycles 1 --with-memory

# Docker uses the same surface (tools profile, on-demand):
docker compose --profile tools run --rm harness --nooa market analyst SOLUSDT --cycles 1 --with-memory
docker compose --profile tools run --rm nooa    market analyst SOLUSDT --cycles 1 --with-memory
```

Set `NOOA_MODEL_PROVIDER`, `NOOA_MODEL_NAME`, and any provider credentials
before starting it. `--cycles N` can be used for a bounded development run.

## Direct Redis principle

The agent suite receives the complete state through the existing inner NOOA
CLI mount. This avoids another Redis translation layer and does not force
the model through a rigid chain of reasoning. The canonical runtime remains
the source of truth for data freshness, calculations, and persistence.

The complete run envelope remains the unit of analysis and is retained with:

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
