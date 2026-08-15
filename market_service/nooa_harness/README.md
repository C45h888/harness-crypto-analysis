# NOOA Harness Boundary

This directory is the NOOA analyst boundary. It is not the market runtime and
it is not an execution engine.

The canonical runtime remains authoritative for:

- external data collection;
- freshness and coverage checks;
- deterministic calculations;
- market status and error classification;
- PostgreSQL persistence;
- Redis canonical state;
- run identity and replayability.

The NOOA harness is responsible for reasoning over that state. It receives a
complete canonical envelope from `market_service/commands/harness.py` and may
form hypotheses, explain uncertainty, and prepare a human-review briefing.

## Package shape

```text
market_service/nooa_harness/
├── __init__.py
├── agents.py         # controller and specialist NOOA Agent classes
├── suite.py          # one controller plus specialist composition
├── runner.py         # long-running loop mounted by harness.py
└── backends.py       # OpenAI-compatible, Ollama, vLLM configuration
```

Start the long-running suite through the existing harness mount point:

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m market_service.commands.harness SOLUSDT \
  --analyst-loop --interval 60

# Docker uses the same harness mount point and keeps the process running:
docker compose --profile tools run --rm harness SOLUSDT \
  --analyst-loop --interval 60
```

Set `NOOA_MODEL_PROVIDER`, `NOOA_MODEL_NAME`, and any provider credentials
before starting it. `--cycles N` can be used for a bounded development run;
the default `--cycles 0` runs until interrupted.

## Direct Redis principle

The agent suite receives the complete state through the existing harness
mount. This avoids another Redis translation layer and does not force the
model through a rigid chain of reasoning. The canonical runtime remains the
source of truth for data freshness, calculations, and persistence.

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
