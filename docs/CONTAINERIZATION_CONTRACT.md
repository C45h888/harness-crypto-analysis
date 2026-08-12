# Canonical Runtime Containerization Contract

Version: `1`

This document is the implementation contract for the agent responsible for
containerizing the canonical runtime. It defines the target service boundaries
and the interfaces that must remain stable while the current single-image
runtime is split into domain containers.

The containerization agent must implement this contract without adding NOOA,
Hermes integration, trading execution, order sizing, or new market logic.

## Objective

Move the existing canonical `market_service` runtime from one shared
application container into independently runnable domain services while
preserving:

- the existing deterministic calculations;
- the existing structured market contracts;
- the existing Redis and PostgreSQL responsibilities;
- the current live SOLUSDT collation path;
- the current test and import gates.

The split is an operational boundary, not a rewrite of domain behavior.

## Target topology

```text
                    external public APIs
              Binance / CoinGecko / CryptoQuant
                              |
                              v
                    +---------------------+
                    | data-access service |
                    +----------+----------+
                               |
                         Redis domain stream
                               |
                    +----------v----------+
                    | calculations service|
                    +----------+----------+
                               |
                         Redis domain stream
                               |
                    +----------v----------+
                    | analysis service   |
                    +----------+----------+
                               |
                       Redis domain outputs
                               |
                    +----------v----------+
                    | collator service    |
                    +----------+----------+
                               |
                   +-----------+-----------+
                   |                       |
             Redis live state       PostgreSQL ledger
```

PostgreSQL and Redis are infrastructure services. The domain services are
`data-access`, `calculations`, and `analysis`. `collector` remains the
periodic snapshot writer. `collator` remains the canonical run-envelope
publisher and is the only service that writes `market_run`.

## Service responsibilities

### `data-access`

Owns network I/O through `market_service.clients`.

It may:

- call Binance, CoinGecko, and CryptoQuant clients;
- normalize source responses at the client boundary;
- preserve raw source evidence;
- publish source-attributed state to Redis.

It must not:

- calculate derived trading metrics;
- classify regimes or signals;
- write PostgreSQL business tables;
- access Docker or the host filesystem.

### `calculations`

Owns pure deterministic calculations from `market_service.calculations`.

It may:

- consume a versioned data-access payload;
- calculate flow, CVD, OBI, VWAP, order-book, technical, volume-profile, and
  deterministic signal values;
- publish figures, inputs, and calculation metadata to Redis.

It must not perform external network I/O or reinterpret missing values.

### `analysis`

Owns deterministic analysis from `market_service.analysis`.

It may:

- consume source evidence and calculation outputs;
- produce OI, liquidation-pressure, macro, auction, demand, regime,
  wall-migration, path-absorption, and stage outputs;
- preserve the inputs and limitations used by each result;
- publish analysis outputs to Redis.

It must not place trades, size positions, or override deterministic inputs.

### `collector`

Remains the periodic operational path for `market_snapshot` and
`signal_event`. It may continue using the current unified client path during
the migration. The container split must not remove or silently change this
path.

### `collator`

Builds one `MarketRunEnvelope` for a symbol from canonical domain outputs. It
must:

1. validate the envelope;
2. write PostgreSQL first;
3. publish the identical serialized envelope to Redis;
4. retain the same `run_id` across both stores.

Only the collator writes `market_run`. A failed PostgreSQL write must prevent
Redis publication.

## Stable process commands

The following commands are the target service entrypoints:

```text
python -m market_service.nodes.data_access
python -m market_service.nodes.calculations
python -m market_service.nodes.analysis
python -m market_service.collector
python -m market_service.commands.collate SOLUSDT --json
```

The node modules may be implemented as thin adapters around existing canonical
functions. Do not duplicate calculation or analysis logic inside node files.

## Transport contracts

All domain messages are JSON objects and must include:

```json
{
  "schema_version": 1,
  "symbol": "SOLUSDT",
  "source": "data-access|calculations|analysis",
  "observed_at": "ISO-8601 timestamp",
  "produced_at": "ISO-8601 timestamp",
  "status": "healthy|degraded|invalid",
  "coverage_seconds": null,
  "data": {},
  "errors": []
}
```

This is the wire shape represented by `MarketStateEnvelope`. The following
rules are mandatory:

- `null` means unavailable; it never means zero;
- timestamps are UTC ISO-8601 values;
- symbols are uppercase;
- every payload is source-attributed;
- errors are structured and retained;
- published payloads are immutable;
- unsupported schema versions are rejected.

The final collated object is the existing `MarketRunEnvelope` with
`schema_version: 1`. It must retain `run_id`, `coverage`, `canonical_state`,
`domain_outputs`, `errors`, and `source_metadata`.

## Redis contract

Use the configured `REDIS_KEY_PREFIX` and preserve these existing keys:

```text
<prefix>:latest:<SYMBOL>:<SOURCE>
<prefix>:stream:market:<SYMBOL>
<prefix>:stream:commands
<prefix>:stream:results
<prefix>:latest:<SYMBOL>:collated
<prefix>:stream:collated:<SYMBOL>
```

Add domain-specific projections only under this namespace:

```text
<prefix>:latest:<SYMBOL>:data-access
<prefix>:latest:<SYMBOL>:calculations
<prefix>:latest:<SYMBOL>:analysis
<prefix>:stream:domain:data-access:<SYMBOL>
<prefix>:stream:domain:calculations:<SYMBOL>
<prefix>:stream:domain:analysis:<SYMBOL>
```

Domain services may read and write Redis. They must not connect directly to
each other through undocumented ports or filesystem mounts.

The collated stream remains untrimmed by default. Domain streams may use the
configured bounded retention policy, but no raw evidence may be discarded
before the collator has consumed it.

## PostgreSQL contract

PostgreSQL remains the durable system of record.

- `market_snapshot` and `signal_event` remain collector-owned;
- `market_run` remains collator-owned;
- domain services must not write `market_run`;
- database credentials come only from environment configuration;
- schema initialization remains under `db/init`;
- no service may depend on host-local database files.

The database health check must pass before `collector` or `collator` starts.

## Required environment

Every service that uses infrastructure must receive the relevant values from
the environment:

```text
DATABASE_URL
REDIS_URL
REDIS_KEY_PREFIX
REDIS_STREAM_MAXLEN
SYMBOLS
POLL_SECONDS
FLOW_WINDOW_SECONDS
DEPTH_LEVELS
```

Only `data-access` receives external API credentials or external endpoint
configuration. No credentials are passed to calculation or analysis services.

## Runtime and security requirements

- Use the existing Python 3.12 base image and pinned `requirements.txt`.
- Run every application service as the non-root `marketflow` user.
- Ensure the complete installed package is readable by that user.
- Do not mount the host repository into production services.
- Do not mount the Docker socket.
- Do not include exchange private keys or trading credentials.
- Keep Redis and PostgreSQL exposed to localhost only in local Compose use.
- Provide health checks for Redis, PostgreSQL, and every domain service.
- Ensure a failed domain service is observable and does not silently produce a
  healthy envelope.

## Migration rules

The containerization agent must work in this order:

1. create node entrypoints around existing canonical modules;
2. add domain-specific Redis publication and consumption;
3. add Compose services and dependency health checks;
4. keep the current unified collector and collator paths working;
5. run contract, import, and Docker smoke tests;
6. only then remove any duplicated unified-path code, and only if tests prove
   behavior is unchanged.

Do not delete `market_service` modules, change schema versions, or introduce
model code as part of containerization.

## Acceptance criteria

The containerization work is complete only when all of the following pass:

1. `python -m unittest discover -s tests -q` passes.
2. `python -m market_service.commands.run_all --json` reports every canonical
   module imported successfully.
3. `docker compose config` succeeds with a valid private `.env`.
4. Redis and PostgreSQL health checks become healthy.
5. Each domain service starts as a non-root user.
6. A data-access payload reaches the calculations service through Redis.
7. A calculations payload reaches the analysis service through Redis.
8. A SOLUSDT collator run completes with an explicit status.
9. The resulting `run_id` exists in PostgreSQL and Redis.
10. PostgreSQL and Redis contain matching `run_id` and `schema_version` values.
11. A source failure produces `degraded` or `invalid`, never fabricated healthy
    data.
12. No service imports from a deleted legacy path.

## Explicit non-goals

This contract does not authorize:

- NOOA or Hermes implementation;
- autonomous trading;
- order placement or position sizing;
- exchange private API access;
- replay/backtesting;
- dashboards;
- replacing PostgreSQL with Redis;
- changing deterministic thresholds;
- adding a second analytical authority.
