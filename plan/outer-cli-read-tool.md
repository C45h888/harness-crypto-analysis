# Plan — outer-CLI designated read tool (`--read` on harness)

Slice: outer harness CLI read surface (post-envelope deviation).
Status: SPEC — not yet implemented.

## Problem

The outer CLI that coding agents / humans shell out to (`harness.py`) has NO
single designated read tool that reaches both the Redis container AND the
Postgres durable archive. Today:

- `--latest` / `--run-id`  -> Redis-ONLY (`harness.py:372-390`), no modes,
  no `source` tag, no Postgres fallback.
- `--keystone-history`     -> Redis + Postgres fallback, but ledger-specific.
- The unified Redis-first/Postgres-fallback 3-mode read exists ONLY on the
  inner NOOA CLI (`nooa_cli_ext.py:_read_collated_run`, lines 47-102).

A model reading market state from the outer CLI therefore cannot reach the
durable Postgres archive, and can never tell *where* the payload came from.
This is the deviation.

## Design

### 1. `market_service/runtime/read_paths.py` — shared fallback + guard

Add one store-agnostic reader to the existing pure-dict read module (keep
it the single source of read truth):

```python
async def read_collated_with_fallback(
    redis: Any, postgres: Any,
    *,
    symbol: str | None = None,
    run_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Redis-first, Postgres-fallback read of a collated run.

    Returns (payload, source) where source in {"redis", "postgres"} or None
    when nothing is persisted. Null = absent (repo discipline); never
    zero-substitute. Postgres may be a stub/None when DATABASE_URL is unset.
    """
```

- Redis path reuses `read_collated` / `read_collated_by_run` (schema-guarded).
- On Redis miss, if `postgres` is usable, try `postgres.latest_run(symbol)` /
  `postgres.read_run(run_id)` and route the result through the **same
  `_guard_payload` schema-version guard** the Redis path uses. Currently
  `postgres_store._run_payload_from_row` returns the payload UNGUARDED —
  align Postgres reads to the schema guard so a stale durable writer raises
  rather than silently passing through.
- Expose `source` explicitly so every caller (outer CLI, inner CLI) can tag
  provenance. Inner CLI `_read_collated_run` may call this and drop the
  payload-only shape for the `(payload, source)` pair (kept source-compatible).

### 2. `market_service/commands/harness.py` — new `--read` surface

Parser additions (in the existing envelope-read group):

```text
--read                        store_true  designated read tool (outer CLI)
--mode {snapshot,inventory,full}  default "snapshot"  output shape
```

Dispatcher Route 3 — when `--read` set (takes precedence if combined with
`--latest`/`--run-id`):

```python
if args.read:
    result = asyncio.run(_read_market(args, mode=args.mode))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("errors") is None else 1
```

New handler `_read_market(args, mode)`:

1. `settings = Settings.from_redis_env()`.
2. Open `RedisRuntimeStore`.
3. Open `PostgresRuntimeStore(settings.database_url)` ONLY when
   `settings.database_url` is set — otherwise `postgres=None`. The outer CLI
   stays usable on the Redis-only host venv (no DATABASE_URL).
4. `payload, source = await read_collated_with_fallback(...)`.
5. Mode routing (reuse read_paths projections — NO local re-implementation):
   - `"full"`       -> raw `payload`
   - `"inventory"`  -> `read_paths.market_inventory(payload)`
   - `"snapshot"` (default) -> `read_paths.market_snapshot(payload)`
6. Build response:
   ```python
   {
     "symbol": ..., "run_id": ..., "source": source,   # or None
     "mode": mode,
     "read": <projection or raw>,
     "errors": [] if payload else ["no run persisted (redis miss, postgres absent/empty)"]
   }
   ```
   Redis miss + Postgres unreachable -> `source=null`, `errors` populated,
   **exit 0** (an empty read is a legitimate "no data" result; hard-fail is
   reserved for schema mismatch only). Exit 1 only when an exception escaped
   (e.g. schema mismatch).

`--latest` / `--run-id` remain the Redis-only fast path — unchanged contract.

### 3. nooa_cli_ext.py — parity (optional, cheap)

`_read_collated_run` may delegate to `read_collated_with_fallback` so the
inner `market read` and outer `--read` share one fallback+guard+source
implementation. Behavior identical; add `source` to `read_cmd` output.

## Boundaries (mirror outer-cli-contract discipline)

- Read-only. Never writes, never touches `marketflow:agent:*`, never crosses
  the `nooa_harness` boundary.
- Projections preserve `None`; no zero-substitute; no raw re-fetch.
- `schema_version` mismatch -> raise (never coerce a stale writer) — same on
  Postgres now.
- The OO-agent `market.read` tool (inner loop) already has the fallback via
  `_read_collated_run`; remains untouched.

## Not in this slice

- `--latest`/`--run-id` Redis-only contract (unchanged).
- pipeline persistence, redis_store.publish_run (write side, separate).
- Microstructure / inference / poller reads (have their own routes).

## Verify

```bash
# parser smoke (both surfaces)
uv run --python 3.12 python -c "from market_service.commands.harness import build_parser; print(build_parser().parse_args(['SOLUSDT','--read','--mode','full']))"
uv run --python 3.12 python -c "from market_service.commands.harness import build_parser; print(build_parser().parse_args(['SOLUSDT','--read']))"

# outer-CLI designated read (docker runtime)
docker compose --profile tools run --rm harness SOLUSDT --read --json
docker compose --profile tools run --rm harness SOLUSDT --read --mode full --json
docker compose --profile tools run --rm harness --read --run-id <UUID> --mode full --json

# Postgres fallback tag: kill redis collated key, re-read, assert source=="postgres"
docker compose exec redis redis-cli DEL 'marketflow:latest:SOLUSDT:collated'
docker compose --profile tools run --rm harness SOLUSDT --read --json   # source=postgres

# Parity — inner market read must match the outer read
docker compose --profile tools run --rm harness --nooa market read SOLUSDT --mode snapshot
# Redis-only host (no DATABASE_URL) stays usable
uv run --python 3.12 python -m market_service.commands.harness SOLUSDT --read --json
```

Tests: extend `tests/test_harness_commands.py` with a fake
Redis/Postgres pair asserting `read_collated_with_fallback` redis-first,
postgres-fallback, schema-guard, and `source` tagging.