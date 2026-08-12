# Docker Runtime Verification Doctrine

Version: 1

This document is the execution doctrine for an independent testing agent. Its
purpose is to prove that the canonical runtime produces one traceable,
evidence-backed market run inside Docker before any NOOA or Hermes analyst
integration is enabled.

The tester must treat the repository contracts as authoritative. It must not
rewrite market logic, add a second interpretation layer, place trades, access
exchange credentials outside `data-access`, or grant an agent Docker/Redis
administrative access.

## Runtime authority

The production path is:

```text
orchestrator
  -> data-access
  -> calculations
  -> analysis
  -> domain-aware collator
  -> PostgreSQL first
  -> Redis latest + collated stream
```

The `run_id` created by the orchestrator is the identity of the entire cycle.
The same ID must appear in every domain envelope, the final
`MarketRunEnvelope`, PostgreSQL, Redis latest state, and the Redis collated
stream.

The legacy unified collator remains a diagnostic path. It is not evidence that
the containerized pipeline completed.

## Safety rules

- Test `SOLUSDT` first and use public market data only.
- Do not run trading execution, order sizing, exchange private endpoints, or
  risk automation.
- Do not mount the host repository into runtime containers.
- Do not mount the Docker socket.
- Do not delete volumes or run `docker compose down -v` during normal testing.
- Do not print passwords, API keys, or complete environment files.
- A degraded or invalid result is a valid observation; never convert it to
  healthy by changing the assertion.
- `null` means unavailable. It must never be replaced with zero.

## Verification sequence

Run from the repository root.

### 1. Static gates

```text
docker compose config --quiet
python -m unittest discover -s tests -q
python -m market_service.commands.run_all --json
```

The unit gate must pass. Warnings are acceptable only when they become
structured errors in the resulting domain envelope.

### 2. Build and start a single-symbol runtime

Use shell environment overrides for the test run; do not edit `.env` for a
temporary test:

```text
SYMBOLS=SOLUSDT POLL_SECONDS=3600 docker compose up -d \
  data-access calculations analysis orchestrator
```

The first cycle starts immediately. The long poll interval prevents a second
cycle from overlapping the inspection.

Confirm that Redis, PostgreSQL, data-access, calculations, analysis, and
orchestrator are healthy or running. Any restart loop is a failure.

### 3. Inspect one cycle

```text
docker compose logs --tail=200 orchestrator data-access calculations analysis
```

The logs must show the same `run_id` for `SOLUSDT` in this order:

```text
data-access -> calculations -> analysis -> collator
```

Adapter warnings are acceptable only if the final status is `degraded` or
`invalid` and the error is retained in the envelope.

### 4. Verify PostgreSQL durability

Use the container-local client and inspect only non-secret fields:

```text
docker compose exec -T postgres psql -U marketflow -d marketflow -Atc \
  "SELECT run_id, symbol, status, schema_version FROM market_run
   WHERE symbol='SOLUSDT' ORDER BY completed_at DESC LIMIT 1"
```

The row must contain one UUID run ID, `SOLUSDT`, a permitted status, and
schema version `1`.

### 5. Verify Redis projections

```text
docker compose exec -T redis redis-cli GET \
  marketflow:latest:SOLUSDT:collated

docker compose exec -T redis redis-cli XRANGE \
  marketflow:stream:collated:SOLUSDT - + COUNT 1
```

Parse the JSON without printing credentials. The Redis latest object and the
stream payload must contain the same `run_id` and `schema_version` as the
PostgreSQL row.

Also verify the per-run domain projections exist:

```text
marketflow:runtime-run:<RUN_ID>:domain:data-access
marketflow:runtime-run:<RUN_ID>:domain:calculations
marketflow:runtime-run:<RUN_ID>:domain:analysis
```

Each must contain the same run ID and its expected source.

### 6. Verify failure semantics

The tester must exercise at least one controlled missing-input or unavailable
source case in a unit/integration fixture. It must confirm:

- the domain publishes `degraded` or `invalid`;
- the error is structured and retained;
- the collator does not publish `healthy` for a missing required domain;
- PostgreSQL is attempted before Redis for a final envelope;
- a PostgreSQL failure produces no new Redis collated publication.

Do not induce failure by deleting volumes or changing production credentials.

## Acceptance record

The test agent should report:

```text
test_timestamp_utc:
symbol: SOLUSDT
static_tests: PASS|FAIL
compose_config: PASS|FAIL
service_health: PASS|FAIL
run_id:
domain_statuses:
final_status: healthy|degraded|invalid
postgres_run_id:
redis_latest_run_id:
redis_stream_run_id:
schema_versions_match: PASS|FAIL
domain_run_ids_match: PASS|FAIL
source_errors_preserved: PASS|FAIL
reconciliation: PASS|FAIL
```

The runtime is ready for an analyst-model seam only when the run IDs and
schema versions match across all three stores and the failure semantics pass.

## NOOA/Hermes readiness boundary

Once this doctrine passes, the first model integration is read-only:

```text
analyst model
  -> typed briefing query
  -> latest collated MarketRunEnvelope
  -> recent collated history when requested
  -> explicit coverage, errors, and limitations
```

The model must not read raw exchange clients, execute arbitrary Redis
commands, invoke Docker, write PostgreSQL, place orders, size positions, or
override deterministic calculations. Its output must cite the run ID and
separate observed evidence, deterministic interpretation, uncertainty, and
questions for the human trader.
