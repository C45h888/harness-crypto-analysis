# Runtime Data Authority — Poller vs Run-cycle vs Canonical Ledger

Status: active contract for the data-fetch and computation split.

## Purpose

Define which endpoint lives where in the runtime. The poller is a rapid
firehose that pushes point-in-time market evidence into Redis. Historical
derivative endpoints and cross-asset data are pulled on demand by the
calculation pipeline (the "run-cycle"). All computation results land in the
canonical Redis ledger, which the harness and NOOA agents read.

This document is the implementation contract for:

- the poller (`market_service.poller`);
- the on-demand derivative fetch
  (`market_service.nooa_harness.pipeline.fetch_derivative_evidence`);
- the canonical Redis ledger
  (`market_service.runtime.redis_store.publish_run`).

## Topology

```text
       +--------------------------+
       | Binance public REST      |
       +----+-----------+---------+
            |           |
   [every 5s]            [on demand]
   8 endpoints            5 own-symbol + 16 cross-asset
            |           |
            v           v
   +-------------+   +-------------------------+
   |   poller    |   | fetch_derivative_       |
   |             |   | evidence                |
   +------+------+   +-----------+-------------+
          |                      |
          v                      v
  marketflow:latest:<SYM>:raw
  marketflow:stream:raw:<SYM>
                            marketflow:latest:<SYM>:derivatives  (TTL 300s)
                            marketflow:stream:domain:derivatives:<SYM>
          |                      |
          +----------+-----------+
                     |
                     v
            +------------------+
            |  harness.py      |  outer CLI — calculation surface
            |  --analyze       |  --refresh-derivatives
            |  --nooa ...      |  --envelope-summary --window ...
            +--------+---------+
                     |
                     v
            +-----------------+
            |  run_cycle      |
            +--------+--------+
                     |
                     v
     +-------+-------+--------+-------+--------+
     |       |       |        |       |        |
     v       v       v        v       v        v
  flow    orderbook  volume  signals  technical  delta
  (calc)  (calc)    profile (calc)   (calc)    (calc)
                                                  +
                                              auction, oi, wall_migration,
                                              path_absorption, demand,
                                              regime, stage (analysis)
                                                     |
                                                     v
                          marketflow:latest:<SYM>:collated
                          marketflow:stream:collated:<SYM>
                          marketflow:run:<run_id>             (TTL 24h)
                          marketflow:latest:<SYM>:{data-access|calculations|analysis}
                          marketflow:stream:domain:{domain}:<SYM>
```

`harness.py --nooa` routes to the NOOA inner CLI for analyst / briefing /
memory operations. NOOA reads the canonical ledger that `harness.py
--analyze` populates; it does not run calculations itself.

The poller never carries historical or cross-asset data. The run-cycle
never re-fetches point-in-time book/ticker/funding/trades — those come from
the poller's Redis raw stream.

## Optional microstructure capture (Pass 1-2)

The paper-derived OFI path is isolated from the five-second REST poller. The
optional `microstructure-capture` Compose profile consumes Binance Spot
`<symbol>@depth@100ms`, bootstraps a local book from a REST snapshot, validates
the Binance depth-update sequence, and writes only the dedicated Redis ledger:

```text
marketflow:stream:microstructure:raw:spot:<SYMBOL>
marketflow:stream:microstructure:events:spot:<SYMBOL>
marketflow:stream:microstructure:ofi:spot:<SYMBOL>
marketflow:latest:microstructure:spot:<SYMBOL>:book
marketflow:latest:microstructure:spot:<SYMBOL>:status
```

Raw deltas are capture evidence; best-quote transition events contain the
deterministic paper contribution `e_n`; completed 10-second (configurable)
intervals contain `OFI_k` and event-average `AD_i`. Statistical fitting,
`MicrostructureEvidence`, canonical-envelope integration, and NOOA
interpretation are not part of this capture pass. A sequence gap resets the
local book and is visible in the status object; it must never be bridged with
inferred events.

Run it separately from the existing poller:

```bash
docker compose --profile microstructure up microstructure-capture
docker compose --profile tools run --rm harness BTCUSDT --microstructure-status
```

## Endpoint split

### Poller (8 endpoints, every `POLL_SECONDS`, default 5s)

| Endpoint | Source | Redis key |
|---|---|---|
| `spot_book` (L2 depth) | Binance `/api/v3/depth` | `evidence.spot.order_book` |
| `fut_book` (L2 depth) | Binance `/fapi/v1/depth` | `evidence.futures.order_book` |
| `spot_24h` (ticker) | Binance `/api/v3/ticker/24hr` | `evidence.spot.ticker_24h` |
| `fut_24h` (ticker) | Binance `/fapi/v1/ticker/24hr` | `evidence.futures.ticker_24h` |
| `fut_funding` (mark/index/funding) | Binance `/fapi/v1/premiumIndex` | `evidence.futures.funding` |
| `fut_open_interest` (current) | Binance `/fapi/v1/openInterest` | `evidence.futures.open_interest` |
| `spot_trades` (aggTrades, `FLOW_WINDOW_SECONDS` back) | Binance `/api/v3/aggTrades` | `evidence.spot.trades_raw` + `trades_normalized` |
| `fut_trades` (aggTrades, `FLOW_WINDOW_SECONDS` back) | Binance `/fapi/v1/aggTrades` | `evidence.futures.trades_raw` + `trades_normalized` |

Written atomically (SET + XADD in one Lua script) to
`marketflow:latest:<SYM>:raw` + `marketflow:stream:raw:<SYM>`. Stream is
bounded by `REDIS_STREAM_MAXLEN`.

### Run-cycle (5 own-symbol + 16 cross-asset, on demand)

Own-symbol endpoints — populated only when `--refresh-derivatives` or
`--analyze` is invoked and the derivative cache is missing or stale (>300s
old by default):

| Endpoint | Source | Merged into evidence at |
|---|---|---|
| `fut_open_interest_history` (`5m`, limit 48) | Binance `/futures/data/openInterestHist` | `evidence.futures.oi_history` |
| `fut_taker_buy_sell` (`5m`, limit 48) | Binance `/futures/data/takerlongshortRatio` | `evidence.futures.taker_buy_sell` |
| `fut_top_long_short_accounts` (`5m`, limit 12) | Binance `/futures/data/topLongShortAccountRatio` | `evidence.futures.top_ls` |
| `fut_long_short_ratio` (`5m`, limit 12) | Binance `/futures/data/globalLongShortAccountRatio` | `evidence.futures.global_ls` |
| `fut_klines` (`5m`, limit 48) | Binance `/fapi/v1/klines` | `evidence.futures.klines` |

Cross-asset endpoints — opt-in via `--with-cross-asset` (default OFF to
save rate-limit). 16 REST calls per cycle:

| Endpoint | Symbols | Merged into evidence at |
|---|---|---|
| `spot_24h` (ticker) | BTC, ETH, SOL, BNB, XRP, DOGE, AVAX, LINK | `evidence.cross_asset.tickers_24h` |
| `fut_funding` (mark/index/funding) | same 8 | `evidence.cross_asset.funding` |

The cross-asset universe matches `analysis/macro.py:DEFAULT_SYMBOLS` so the
two paths agree on the reference set.

### Caching

Derivative evidence lands at `marketflow:latest:<SYM>:derivatives` with a
configurable TTL (default 300s). The cache is keyed on `observed_at_ms` —
back-to-back `--analyze` cycles within the TTL skip the fetch entirely. The
TTL is honored by `Redis SET … EX <ttl>` so the key auto-expires; a stale
key falls through to a fresh fetch on the next call.

The audit trail is at
`marketflow:stream:domain:derivatives:<SYM>` (bounded by
`REDIS_STREAM_MAXLEN`). Every fetch is one stream entry with full payload.

## Command surface

The **outer** CLI (`market_service.commands.harness`) owns the calculation
surface. The **inner** NOOA CLI (`nooa market ...`) is calculation-agnostic
and reuses whatever derivatives are already in Redis.

```bash
# Calculation pipeline — populate the canonical ledger (harness.py).
python -m market_service.commands.harness SOLUSDT --analyze --window 15m --json
python -m market_service.commands.harness SOLUSDT --analyze --envelope-summary --json
python -m market_service.commands.harness SOLUSDT --refresh-derivatives --json
python -m market_service.commands.harness SOLUSDT --refresh-derivatives --with-cross-asset --json
python -m market_service.commands.harness SOLUSDT --analyze --no-derivatives  # legacy raw-only
python -m market_service.commands.harness SOLUSDT --analyze --force-refresh-derivatives

# Envelope reads (harness.py) — no agents, just the canonical ledger.
python -m market_service.commands.harness SOLUSDT --latest --json
python -m market_service.commands.harness --run-id <UUID> --json

# Standalone clean contract (harness.py default, no flags) — debug only.
python -m market_service.commands.harness SOLUSDT --json

# NOOA analyst / briefing / memory (nooa_cli via harness --nooa).
python -m market_service.commands.harness --nooa market analyst SOLUSDT --cycles 1 --with-memory
python -m market_service.commands.harness --nooa market envelope SOLUSDT --latest
python -m market_service.commands.harness --nooa market briefing --session-id <UUID> --run-id <UUID>
python -m market_service.commands.harness --nooa market memory recall --session-id <UUID>
```

The `--nooa` route is a passthrough to the mounted NOOA CLI; it does not run
calculations itself. NOOA reads the canonical ledger that `harness.py
--analyze` populates. Calculation flags (`--deriv-ttl`,
`--with-cross-asset`, `--no-derivatives`, `--force-refresh-derivatives`)
live on `harness.py`, not on `nooa market analyst`.

## Output surface (canonical ledger)

Every run-cycle writes one immutable `MarketRunEnvelope` to:

- `marketflow:latest:<SYM>:collated` — full envelope JSON
- `marketflow:stream:collated:<SYM>` — bounded stream (default 5000)
- `marketflow:run:<run_id>` — TTL 24h
- `marketflow:latest:<SYM>:data-access` — evidence snapshot
- `marketflow:latest:<SYM>:calculations` — calc snapshot
- `marketflow:latest:<SYM>:analysis` — analysis snapshot
- `marketflow:stream:domain:data-access:<SYM>` — per-domain stream
- `marketflow:stream:domain:calculations:<SYM>`
- `marketflow:stream:domain:analysis:<SYM>`

`envelope_summary` (compact projection read by the briefing) is recomputed
from `canonical_state` when the briefing is built — it picks up the new
fields (taker_buy_ratio, top_long_pct, global_long_pct, oi_change_pct,
cross_asset_funding, delta -2..+2, tier_balance, mega_at_keystone, etc.)
without further changes to `runtime/contracts.py`.

## Failure modes

- **Binance down for derivative fetch** — `fetch_derivative_evidence` uses
  `return_exceptions=True`, so one failed endpoint does not drop the rest.
  Adapters that need derivative inputs degrade gracefully to None (same as
  the pre-change behavior).
- **Cache TTL expires mid-cycle** — the next cycle fetches fresh. The
  pipeline reads `derivatives_key` (TTL'd); a missing key is treated as "not
  fresh" and triggers a fetch.
- **Poller Redis out** — the poller keeps retrying; the run-cycle logs and
  proceeds with whatever is in the cache.
- **Back-to-back cycles within TTL** — second cycle reads the cached
  payload and skips the fetch. The stream entry is still updated by
  `--refresh-derivatives` runs that explicitly opt in.

## Acceptance criteria

The split is correct when:

1. `poller.py` has zero historical or cross-asset endpoints.
2. `fetch_derivative_evidence` is the only caller of the 5 historical
   endpoints; the poller never touches them.
3. `run_cycle` reads `marketflow:latest:<SYM>:raw` for point-in-time data
   and `marketflow:latest:<SYM>:derivatives` for the on-demand batch.
4. Both legs land in the same `MarketRunEnvelope.canonical_state` and are
   persisted by `persist_envelope`.
5. The cache TTL works (Redis returns `nil` after `EX`).
6. Cross-asset is opt-in, not the default — saves 16 REST calls per cycle.
7. `--no-derivatives` reproduces the legacy raw-only behavior exactly.
8. The outer CLI (`market_service.commands.harness`) owns the calculation
   surface (`--analyze`, `--refresh-derivatives`, `--deriv-ttl`,
   `--with-cross-asset`, `--no-derivatives`, `--force-refresh-derivatives`,
   `--envelope-summary`, `--no-persist`, `--window`).
9. The NOOA inner CLI (`nooa market ...`) is calculation-agnostic — its
   `analyst` command carries no derivative-fetch flags. It reuses whatever
   derivatives are already in Redis.
