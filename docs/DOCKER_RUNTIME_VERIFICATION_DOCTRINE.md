# Docker Runtime Verification Doctrine

Version: 2

This is the execution doctrine for an independent testing agent. Its purpose
is to prove that the canonical runtime can be controlled through the basic
harness, produce a traceable evidence-backed market run inside Docker, and
persist that run consistently in PostgreSQL and Redis before any NOOA or
Hermes analyst integration is enabled.

The tester must treat the repository contracts and the canonical runtime as
authoritative. It must not rewrite market logic, add a second interpretation
layer, place trades, access exchange credentials outside `data-access`, or
grant an agent Docker or Redis administrative access.

## Current runtime authority

The canonical live path is:

```text
harness request
      |
      v
orchestrator
      |
      +--> data-access       pulls scoped raw evidence
      +--> calculations      deterministic metrics
      +--> analysis          deterministic interpretation
      |
      v
domain-aware collator
      |
      +--> PostgreSQL first  durable market_run row
      |
      +--> Redis             latest projection + collated stream
```

The autonomous runner is the timer-driven `orchestrator`. The harness is a
read-only client and controlled trigger: it submits a typed request to the
orchestrator and waits for the exact run identified by that request.

The request `run_id` is the identity of the complete cycle. It must be
preserved through all domain envelopes, the final `MarketRunEnvelope`, the
PostgreSQL row, the run-addressed Redis record, and the collated stream.

The legacy unified harness/collator path remains available for diagnostics. It
is not evidence that the Docker domain pipeline completed.

## Harness container and mount point

The Docker harness is the `harness` service under the `tools` profile. The
image uses `/app` as its working directory and contains the package at:

```text
/app/market_service
```

The harness has no host repository bind mount and no Docker socket. It only
uses the Redis and PostgreSQL service endpoints needed to submit a request and
read the resulting envelope.

Start a full canonical run for `SOLUSDT`:

```text
docker compose --profile tools run --rm harness \
  SOLUSDT --trigger --scope all --timeout 120 --json
```

Request only a specific data scope:

```text
docker compose --profile tools run --rm harness \
  SOLUSDT --trigger --scope order_book --timeout 120 --json
```

Supported scopes are:

```text
all | order_book | trades | funding | open_interest | tickers
```

The `--domain` option can trigger an individual node, for example:

```text
docker compose --profile tools run --rm harness \
  SOLUSDT --domain data-access --scope order_book --timeout 90 --json
```

An individual-domain result is a node completion event, not a complete
collated market run. Calculations and analysis require matching upstream
run-addressed state, so the full `--trigger` path is the acceptance path.

After a full trigger, read the immutable exact run rather than relying on the
moving latest projection:

```text
docker compose --profile tools run --no-deps --rm harness \
  --run-id <RUN_ID> --json
```

`--latest` remains useful for current-state inspection, but it may advance as
the autonomous timer produces another cycle. It must not be used to prove
that a particular harness request persisted correctly.

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
- A partial scope must not be interpreted as a complete market state.
- The model or testing agent must not read individual exchange clients or
  execute arbitrary Redis commands outside the documented adapter surfaces.

## Verification sequence

Run from the repository root.

### 1. Static gates

```text
docker compose config --quiet
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m market_service.commands.run_all --json
git diff --check
```

The unit gate must pass. Warnings are acceptable only when they become
structured errors in the resulting domain or run envelope.

### 2. Build and start the canonical services

Use a single symbol for an isolated live test. Do not change `.env` for a
temporary test; use an environment override if needed:

```text
SYMBOLS=SOLUSDT POLL_SECONDS=3600 docker compose up -d \
  redis postgres data-access calculations analysis orchestrator
```

The first timer cycle starts immediately. The long poll interval reduces
interference from a second timer cycle while the tester inspects the result.

Confirm service state:

```text
docker compose ps --all
```

Redis and PostgreSQL must be healthy. `data-access`, `calculations`,
`analysis`, and `orchestrator` must be healthy or running without restart
loops. The one-shot `collator` may be exited when it was not explicitly run;
that is not a failure of the harness-controlled path.

### 3. Trigger and capture one exact harness run

Run the full scope and capture the JSON output. Record the `request_id` and
the nested `runtime.run_id`:

```text
docker compose --profile tools run --rm harness \
  SOLUSDT --trigger --scope all --timeout 120 --json
```

The following must hold:

- `completed` is `true`;
- `runtime.phase` is `PUBLISHED`;
- `runtime.run_id == request_id`;
- `envelope.run_id == request_id`;
- `runtime.publication == "published"`;
- `envelope.schema_version == 1`;
- `envelope.status` is `healthy` or an explicitly justified `degraded`;
- `envelope.errors` is structured and retained when degradation occurs.

The live run may be degraded when an optional source is unavailable. For
example, missing 4-hour kline history must appear as an explicit analysis
error; it must not be represented as fabricated trend data.

### 4. Verify domain run identity

The envelope must expose the same run ID for all three domains:

```text
envelope.source_metadata.domain_run_ids.data-access
envelope.source_metadata.domain_run_ids.calculations
envelope.source_metadata.domain_run_ids.analysis
```

All three values must equal the captured request ID. The corresponding
run-addressed Redis keys must exist:

```text
marketflow:runtime-run:<RUN_ID>:domain:data-access
marketflow:runtime-run:<RUN_ID>:domain:calculations
marketflow:runtime-run:<RUN_ID>:domain:analysis
```

Each domain payload must retain its expected `source`, `symbol`, `run_id`,
`schema_version`, status, and structured errors.

### 5. Verify PostgreSQL durability

Use the container-local client and inspect only non-secret fields:

```text
docker compose exec -T postgres psql -U marketflow -d marketflow -Atc \
  "SELECT run_id, symbol, status, schema_version FROM market_run
   WHERE run_id='<RUN_ID>'"
```

The row must contain the exact request ID, `SOLUSDT`, a permitted status, and
schema version `1`. PostgreSQL is the durable record and must be written
before the final Redis publication.

### 6. Verify Redis exact-run storage and stream publication

First verify the run-addressed immutable Redis record. This is the exact-run
check and is not affected by later timer cycles:

```text
docker compose exec -T redis redis-cli --raw GET \
  marketflow:run:<RUN_ID>
```

Then verify the moving latest projection and collated stream:

```text
docker compose exec -T redis redis-cli --raw GET \
  marketflow:latest:SOLUSDT:collated

docker compose exec -T redis redis-cli --raw XREVRANGE \
  marketflow:stream:collated:SOLUSDT + - COUNT 100
```

Parse the JSON without printing credentials. The exact-run Redis record must
match PostgreSQL on `run_id`, `symbol`, `schema_version`, and status. The
collated stream must contain an entry whose `run_id` equals `<RUN_ID>` and
whose `schema_version` is `1`.

The latest projection may contain a newer timer-generated run. That is
expected and is not a reconciliation failure if the exact run record and
stream entry are correct.

### 7. Verify scoped harness behavior

Run an order-book-only request:

```text
docker compose --profile tools run --rm harness \
  SOLUSDT --trigger --scope order_book --timeout 120 --json
```

Confirm:

- the request still reaches `PUBLISHED`;
- all three domain run IDs match the request ID;
- `requested_scope` is `order_book` in the evidence;
- order-book calculations are present;
- trade/CVD/volume metrics are explicitly unavailable, not zero-filled;
- the final status is degraded unless all required complete-scope inputs exist.

This proves the harness can request data deliberately without allowing absent
inputs to become false evidence.

### 8. Verify failure semantics

The tester must exercise at least one controlled missing-input or unavailable
source case in a unit/integration fixture. It must confirm:

- the domain publishes `degraded` or `invalid`;
- the error is structured and retained;
- the collator does not publish `healthy` for a missing required domain;
- PostgreSQL is attempted before Redis for a final envelope;
- a PostgreSQL failure produces no new Redis collated publication;
- a Redis publication failure leaves the PostgreSQL row available for
  reconciliation or retry;
- a transient Redis connection/DNS interruption does not permanently kill a
  long-lived domain consumer.

Do not induce failure by deleting volumes or changing production credentials.
Use mocks, controlled test doubles, or a disposable test fixture.

## Acceptance record

The test agent should report:

```text
test_timestamp_utc:
symbol: SOLUSDT
static_tests: PASS|FAIL
compose_config: PASS|FAIL
service_health: PASS|FAIL
harness_trigger: PASS|FAIL
request_id:
runtime_run_id:
envelope_run_id:
domain_statuses:
final_status: healthy|degraded|invalid
postgres_run_id:
redis_exact_run_id:
redis_latest_run_id:
redis_stream_run_id:
schema_versions_match: PASS|FAIL
domain_run_ids_match: PASS|FAIL
scope_contract: PASS|FAIL
null_semantics: PASS|FAIL
source_errors_preserved: PASS|FAIL
failure_semantics: PASS|FAIL
reconciliation: PASS|FAIL
blocking_issue:
```

The harness-controlled runtime passes this doctrine when the request reaches
`PUBLISHED`, the exact request ID is present across the domain outputs,
PostgreSQL, the run-addressed Redis record, and the collated stream, and the
failure/scoping semantics pass. A `degraded` final status does not fail the
doctrine when its cause is explicit, structured, and expected.

## NOOA/Hermes readiness boundary

Passing this doctrine permits only a read-only analyst seam:

```text
analyst model
      |
      +--> typed harness/briefing request
      +--> exact run-addressed MarketRunEnvelope
      +--> recent collated history when explicitly requested
      v
observed evidence + deterministic outputs + coverage + errors
```

The model must not read raw exchange clients, execute arbitrary Redis
commands, invoke Docker, write PostgreSQL, place orders, size positions, or
override deterministic calculations. Every analyst response must cite the
exact `run_id`, separate observed evidence from deterministic interpretation,
state coverage and limitations, and leave the trading decision with the human
trader.
