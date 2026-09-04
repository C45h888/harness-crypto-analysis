# Pass 2 — Keystone Pivot Spec

Status: EXECUTING
Date: 2026-08-23
Depends on: Pass 1 (Issue 3 legacy signal ports — complete, verified live)

## The architectural pivot

The outer CLI (`harness.py`) is decoupled from the bounded
`_envelope_summary` projection. The model / outer-CLI path reads the FULL
`MarketRunEnvelope` directly from Redis (`--latest` / `--run-id` —
Route 3, `RedisRuntimeStore.read_latest_run`). The bounded
`_envelope_summary` in `runtime/contracts.py` stays owned exclusively by
the NOOA inner-CLI briefing path (`AnalystBriefing.from_controller_text`)
and is NOT extended.

This resolves the original Pass 2 problems 1 & 2 without bloating the
briefing projection: the five Pass 1 signal groups are already inside the
full envelope and therefore already visible on the canonical read path.

## Workstreams

### A — outer-CLI read-path decoupling (problems 1 & 2)

- `--latest` / `--run-id`: unchanged — already the decoupled full-envelope read.
- `_projection()` (harness.py, used by `--envelope-summary`): becomes a
  signal-inventory projection — keys per section + scalar headlines,
  never raw arrays. CLI convenience only.
- New route `--keystone-history`: reads the cross-cycle keystone ledger
  (Redis first, Postgres fallback) and derives the migration verdict on
  the read side.

### B — keystone_history durable state (problem 4 — new table, clean separation)

New table `keystone_history`, separate from `wall_snapshot`, mirroring its
discipline: postgres-first, exact-run, schema_version=1,
PK(symbol, cycle_ts), idempotent ON CONFLICT DO NOTHING.

Columns: symbol, cycle_ts, run_id, schema_version, keystone_price,
window_qty, tight_lo, tight_hi, wide_lo, wide_hi, keystone_bid_qty,
ask_ladder_notional, inserted_at.

Redis live projection: bounded stream `{prefix}:history:{SYM}:keystones`.

Store methods (mirror `record_wall_snapshot` pattern):
- `RedisRuntimeStore.record_keystone_snapshot / read_keystone_history /
  read_last_keystone_snapshot / keystone_history_stream / keystone_history_count`
- `PostgresRuntimeStore.record_keystone_snapshot / read_keystone_history /
  read_last_keystone_snapshot / keystone_snapshot_count`

Pipeline seam (`nooa_harness/pipeline.py`):
- `_keystone_snapshot_payload()` extracts this cycle's fut_keystone +
  keystone_bid_stack.tight.total_qty + ask_wall_ladder.total_notional.
- `_record_keystone_snapshot()` writes Postgres-first, then Redis.
- `run_cycle` persist block calls it alongside `_record_wall_snapshot`.

Cross-cycle migration verdict = READ-SIDE derivation:
`keystone_cycle_migration(rows, width)` in `calculations/orderbook.py`,
called by `harness.py --keystone-history`. Consecutive keystone_price
deltas → UP/DOWN/FLAT + verdict + net_buckets.

### C — hourly_keystone_migration wiring (problem 3, 15m slice)

Wire the orphan `hourly_keystone_migration` into the orderbook section via
`_strict()`. At the default 15m window it yields one bucket (verdict FLAT —
valid); meaningful at `--window 1h/4h`.

### D — schema

- `db/init/001_schema.sql`: keystone_history DDL (fresh-init path).
- `alembic/versions/0006_keystone_history.py`: handwritten migration
  (matches 0002_wall_snapshot style; target_metadata is None).

## Doctrine compliance

- Null discipline: nullable metric columns; no zero-substitution anywhere.
- Schema version: keystone_history rows carry schema_version=1; no
  MarketRunEnvelope bump (canonical_state is freeform JSONB).
- NOOA boundary untouched: contracts.py `_envelope_summary`, nooa_cli.py,
  nooa_cli_ext.py, agents.py, suite.py, memory.py are NOT modified.
- Transport-safe JSON only on every persisted payload.

## Files touched

```
docs/PASS2_KEYSTONE_PIVOT_SPEC.md         this spec
db/init/001_schema.sql                    keystone_history DDL
alembic/versions/0006_keystone_history.py migration
market_service/runtime/redis_store.py     keystone stream + methods
market_service/runtime/postgres_store.py  keystone table + methods
market_service/calculations/orderbook.py  keystone_cycle_migration (pure)
market_service/nooa_harness/pipeline.py   wire hourly + write seam
market_service/commands/harness.py        _projection + --keystone-history
tests/test_keystone_history.py            unit tests
```

## Verification

- `uv run python -m pytest tests/ -q` full suite green.
- Docker live: `harness SOLUSDT --analyze --json` → keystone_history row
  written + `hourly_keystone_migration` in envelope.
- Docker live: `harness SOLUSDT --keystone-history --json` → read path
  returns history + verdict.
