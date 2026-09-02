# Wall / Keystone Calculation Surface — Depth Report

Status: PROPOSAL (no code changes yet)
Scope: `market_service/calculations/orderbook.py`,
       `market_service/calculations/technical.py` (cross-pollinated),
       `market_service/analysis/wall_migration.py`,
       `market_service/analysis/oi.py` (wall_break_assessment + find_walls),
       `market_service/analysis/path_absorption.py` (simulated_ascent/descent),
       `market_service/nooa_harness/pipeline.py` (`_adapt_wall_migration`,
       `_adapt_path_absorption`, `_adapt_oi`),
       `market_service/microstructure/*` (the WS feed that is currently
       unconsumed by walls).

## TL;DR

Wall calculations today are **snapshot-only**. They diff the futures
order-book between two 5-second REST polls and tell you *what the book
looks like*. The microstructure container — which is connected to a
Binance Spot WebSocket depth-diff feed and is already publishing per-event
book transitions, completed OFI intervals, and best-quote state — is
**not wired into the wall pipeline at all**. Walls that get built or
eroded between two REST snapshots are invisible to the calculation
surface.

This plan adds **four depth dimensions** to the wall surface:

1. **OFI at the best quote** — real-time buy/sell pressure at the top
   of the book, currently captured but unused by walls.
2. **Per-event depth-delta attribution** — every price level the
   microstructure feed sees get/touched is logged; we can use that to
   track wall *queue* in real time, not just snapshot diffs.
3. **Average Depth (AD) per OFI interval** — the depth scaling factor
   that turns OFI into price impact, which we can use directly in
   `wall_break_assessment` (currently uses a hardcoded `target_minutes`).
4. **Spot-side microstructure** — we currently poll *both* spot and
   futures order books in the 5s REST, but the WS feed is **spot only**.
   We should mirror the WS capture onto futures, OR add a parallel
   futures WS — the wall analysis must run off the futures book (legacy
   `wall_analysis` / `wall_migration` both use futures).

The plan is divided into a **clean-up pass** (parameterize hardcoded
numbers, remove legacy SOL session assumptions), a **depth-wiring pass**
(read microstructure streams into the wall adapter), and a **new
metric pass** (add a few missing signals that the existing math + new
data make cheap).

---

## 1. Current state — what walls do today

### 1.1 Calc layer (`market_service/calculations/orderbook.py`)

12 pure functions, snapshot-only:

| Function | What it computes | Inputs used | What it ignores |
|---|---|---|---|
| `rolling_density` | Per-level window qty | One book snapshot | Wall history, prior snapshots |
| `top_density_windows` | Top-N densest windows | One book snapshot | Same |
| `find_keystone` | Densest bid cluster (legacy `[-0.30, -0.05]` band) | Bids + last price | Trade intensity at the level |
| `zone_depth` | Bid/ask qty + notional in a band | One book snapshot | Trade flow in the zone |
| `significant_levels` | Levels ≥ min_qty | One book snapshot | Same |
| `absorption_ladder` | Cumulative bid qty below price | Bids | Time-decay, queue dynamics |
| `zone_ratio_grid` | Per-zone bid/ask ratio in steps | One book snapshot | Same |
| `zone_buy_sell` | Per-zone taker buy/sell qty | Trades + zone | Wall persistence |
| `keystone_trade_intensity` | Tight/wide aggressive buy counts | Trades + zone | Per-level queue changes |
| `keystone_bid_stack` | Below/at/above keystone decomposition | Bids | Persistence, build-up rate |
| `ask_wall_ladder` | Per-bucket ask qty above price | Asks | Same |
| `hourly_keystone_migration` | Intra-window keystone drift | Trades | Cross-cycle migration |
| `keystone_cycle_migration` | Cross-cycle keystone drift | Snapshots | Real-time drift |

### 1.2 Analysis layer (`market_service/analysis/wall_migration.py`)

14 pure functions, also snapshot-only:

| Function | What it computes | Inputs used | What it ignores |
|---|---|---|---|
| `depth_qty` / `depth_qty_at` | Total qty in band | One book | Persistence |
| `wall_delta` | Snapshot-diff wall migration | **Prior walls + current asks** | **Real-time build/erode** |
| `fuel_ratio` | Bid pool vs ask pool | One book | OI, funding, OFI |
| `densest_clusters` | Bid density at fixed floors | Bids | Time-evolution |
| `level_absorption` | % book absorbs at each bid | Bids | Replenishment rate |
| `wall_trap_assessment` | Buyer probability of absorbing | Fuel + walls | Real-time queue change |
| `keystone_wall_balance` | Bid/ask qty between keystone + sell wall | One book | Same |
| `keystone_holds_scorecard` | 0–10 score with 5 factors | Snapshot + 1-bar derivatives | **Multi-bar trends** |
| `compute_bid_tiers` | Mega/large/medium/small buckets | One book | Persistence per tier |
| `compute_round_anchors` | Bid qty at **hardcoded** anchors | One book | **Dynamic anchors** |
| `bid_tier_balance` | Mega bid vs mega ask | One book | Same |
| `mega_at_keystone` | Mega bids within ±tol of keystone | Bids | Persistence |

### 1.3 The four hardcoded SOL legacy constants still in code

These are not in `wall_migration.py` but are scattered in pipeline
adapters and other places:

| Constant | Location | Problem |
|---|---|---|
| `_ROUND_ANCHORS` (SOL-specific prices) | `analysis/wall_migration.py:235` | Hardcoded `74.00`, `75.00`, `73.50`, etc. — these were legacy SOL session anchors, not general-purpose. |
| `bid_floor = price * 0.97`, `ask_target = price * 1.03` | `nooa_harness/pipeline.py:937` | 3% magic number — a keystone 3% above mid is the ask target, regardless of instrument volatility. |
| `entry = price * 1.01`, `bid_floor = price * 0.97` | `nooa_harness/pipeline.py:1167` | 1% / 3% magic for path absorption. |
| `_BID_TIER_THRESHOLDS` (mega=5000, large=1000, medium=200) | `analysis/wall_migration.py:178` | Hardcoded qty tiers — for BTC at $65k, 200 BTC is institutional; for SOL at $150, 200 SOL is ~$30k which is tiny. |

### 1.4 Pipeline wiring (`_adapt_wall_migration`)

What flows into the adapter today:

```text
REST poller (5s) → evidence dict → _adapt_wall_migration
  ├── spot.order_book (snapshot)
  ├── futures.order_book (snapshot)
  ├── futures.taker_buy_sell (12 bars, 5-min)
  ├── futures.top_ls (12 bars, 5-min)
  ├── futures.global_ls (12 bars, 5-min)
  ├── futures.oi_history (12 bars, 5-min)
  ├── futures.trades_normalized (raw trades)
  ├── prior_walls from prior cycle (Postgres)
  └── NOTHING from microstructure stream
```

What flows in **today that the wall adapter doesn't use**:

- **`futures.taker_buy_sell`**: only the *last* bar's buy share is read
  (`_taker_buy_share`). The 3-bar trend (bullish/bearish build-up) is
  not computed.
- **`futures.funding`**: not consumed at all by walls. Positive funding
  = buyers paying sellers = a long-biased market where bid fuel may be
  shorter-lived.
- **`futures.oi_history`**: only a 1-bar pct change is read
  (`_oi_pct_change`). The 3-bar trend (is OI genuinely rising or
  noise?) is not computed.
- **`futures.top_ls`**: only the *last* bar's top_long_pct is read
  (`_ls_last_pct`). Drift over 3 bars is not computed.
- **`futures.global_ls`**: read for `oi_weighted_contracts` only.

What flows in **but doesn't exist at all** in walls:

- **Microstructure event stream** (`marketflow:stream:microstructure:events:spot:{SYM}`):
  per-best-quote transition, with bid/ask price + qty deltas.
- **Microstructure OFI stream** (`marketflow:stream:microstructure:ofi:spot:{SYM}`):
  closed deterministic OFI/AD intervals (every 10s by default).
- **Microstructure latest book** (`marketflow:latest:microstructure:spot:{SYM}:book`):
  best quote updated every WS event.
- **Microstructure status** (`:status`): running / gap / reconnecting transitions.
- **Wall history table** (`wall_snapshot`): prior cycle's walls already
  loaded — used by `wall_delta`, but only as a *single* prior. No
  multi-cycle history.

---

## 2. The microstructure container — what it has

`market_service/microstructure/capture.py` runs a separate process
(`market_service.microstructure.capture`) that:

1. Opens a Binance Spot WebSocket to `{symbol}@depth@100ms` (depth-diff
   stream, 100ms cadence).
2. Bootstraps a local order book from REST.
3. For every WS delta:
   - Applies it to the local book.
   - Publishes the raw delta to `marketflow:stream:microstructure:raw:spot:{SYM}`.
   - If the best quote moved, emits an `OrderBookEvent` to
     `marketflow:stream:microstructure:events:spot:{SYM}` — this carries
     `previous` and `current` BestQuoteState (price + qty at both
     sides) and the paper-derived `contribution` (e_n).
   - Updates the latest book key (`:book`) with the new best quote.
4. Aggregates events into fixed 10-second OFI intervals:
   - `ofi` = Σ contributions (decimal, exact)
   - `average_depth` = mean(qB+qA)/2 over the events that landed
   - `mid_start`, `mid_end` for price-impact join
   - `event_count`, `first_update_id`, `last_update_id`
   - `quality` = `exact_feed` (or `insufficient` on empty intervals)
   - Published to `marketflow:stream:microstructure:ofi:spot:{SYM}`.

### 2.1 What the wall surface should consume

| Stream / key | Already published? | Currently consumed by walls? | Should it be? |
|---|---|---|---|
| `stream:microstructure:raw` (every WS delta, all levels) | YES | NO | YES (per-level wall queue tracking) |
| `stream:microstructure:events` (best-quote transitions) | YES | NO | YES (top-of-book wall pressure) |
| `stream:microstructure:ofi` (closed OFI/AD intervals) | YES | NO | YES (wall-break rate context) |
| `latest:microstructure:book` (live best quote) | YES | NO | YES (real-time top-of-book wall qty) |
| `latest:microstructure:status` (running / gap / etc.) | YES | consumed by wake_worker | **also** useful as a data-quality flag in wall envelope |

### 2.2 The gap

The wall analysis runs on the **futures** book; the WS capture currently
runs on **spot only**. Two ways to fix:

1. **Mirror the WS capture to futures** — `BinanceSpotDepthCapture` is
   venue-specific in name but `venue` is a config field. Adding a
   futures capture means a second process. Cheapest, most isolated.
2. **Keep walls on REST snapshot** — accept that wall queue at the top
   of book is best-effort, but still consume the spot OFI for context.
   Cheaper but loses per-level queue tracking on the futures book.

**Recommendation**: (2) for now — the wall math is fundamentally a
*futures* view (legacy `wall_analysis.py` is USD-M-perp-only). Spot
OFI is a *corroborating* signal, not the primary input. We can add a
futures WS capture later if needed; for the wall surface depth pass,
spot-side OFI/AD is the highest-value signal because it tells us
whether the *spot-to-futures* basis is widening or compressing while
the wall is being defended.

---

## 3. The four depth dimensions

### 3.1 Dimension A — real-time top-of-book pressure at the wall

**Today**: `keystone_holds_scorecard` uses `latest_tbr` from the most
recent taker-buy-sell bar (one number). It cannot distinguish "buyers
have been pressing for 3 bars" from "buyers printed once and went
quiet".

**With OFI at the best quote** (from the WS feed):
- `ofi_recent` = sum of `OFIInterval.ofi` over the last N intervals
  (e.g. last 6 = last 60 seconds of WS data).
- `ofi_intensity` = `|ofi_recent| / sum(average_depth)` — pressure
  normalized by book depth.
- A strongly positive OFI at the best bid + large `average_depth`
  means the bid is being defended actively, not passively.
- Combined with the futures `taker_buy_sell` last 3-bar trend, we can
  score "wall is being defended" vs "wall is being passively listed".

**Pure function to add** in `calculations/orderbook.py`:

```python
def ofi_wall_pressure(
    intervals: list[dict],  # recent OFIInterval dicts
    window_n: int = 6,
) -> dict:
    """Aggregate top-of-book pressure over the last N intervals.
    Returns {ofi_sum, depth_sum, intensity, bias, n_intervals, quality}.
    """
```

### 3.2 Dimension B — per-level wall queue tracking

**Today**: `keystone_bid_stack` returns a snapshot decomposition
(below / at / above). Between two REST snapshots, the queue at the
keystone can refill or drain invisibly.

**With raw depth deltas** (every WS event at every level):
- For each level in the keystone tight band, sum the absolute qty
  changes from the depth-delta stream over the last N intervals.
- A level whose queue is being *added to* consistently = institutional
  refilling; one whose queue is *draining* consistently = passive
  listing.

**Pure function to add** in `calculations/orderbook.py`:

```python
def level_queue_dynamics(
    deltas: list[dict],  # recent DepthDelta dicts (raw)
    level: float,
    tol: float = 0.01,
    window_s: float = 60.0,
    now_ms: int | None = None,
) -> dict:
    """Queue add/remove volume at one price level over a sliding window.
    Returns {level, adds, removes, net, events, refill_persistence,
             verdict: 'REINFORCING'|'STABLE'|'DRAINING'|'INSUFFICIENT_DATA'}.
    """
```

This is **the** signal that distinguishes a real wall from a fake
listing. A fake wall has bids that get pulled immediately when tested;
a real wall refills.

### 3.3 Dimension C — depth-aware wall break assessment

**Today**: `wall_break_assessment` takes
`total_wall_sol / target_minutes` (hardcoded 15 min) and compares
against the recent buy rate. This is **not depth-aware** — a $30M ask
wall needs a very different buy rate to clear than a $300k wall.

**With Average Depth (AD) per OFI interval**:
- `average_depth_qB` = mean best-bid qty over the last N intervals.
- `average_depth_qA` = mean best-ask qty over the last N intervals.
- `implied_break_minutes` =
  `(total_wall_sol / average_depth_qB) * (1 / event_rate_per_min)`
  — how many minutes of best-bid refill it would take to absorb
  the ask stack at the current refill rate.

**Pure function to add** in `analysis/oi.py` (or `wall_migration.py`):

```python
def depth_aware_wall_break(
    total_wall_qty: float,
    average_depth_qB: float | None,
    average_depth_qA: float | None,
    ofi_recent: float,
    buy_per_min: float,
    peak_buy_per_min: float,
) -> dict:
    """Wall break assessment that uses microstructure AD as the denominator.

    Replaces the hardcoded target_minutes with an AD-derived
    clearance-rate estimate.
    """
```

### 3.4 Dimension D — multi-bar derivative trends into the scorecard

**Today**: `keystone_holds_scorecard` reads:
- `bid_ask_qty_ratio` — snapshot
- `latest_tbr` — 1 bar
- `oi_chg_5m` — 1 bar
- `top_long_pct` — 1 bar
- `net_buy_ratio` — 1 trade window

**Proposed**: replace each single-bar input with a `trend` (3-bar
slope + last-bar value) so the scorecard can distinguish "rising for
3 bars" from "spiked once":

| Input | New metric | Computation |
|---|---|---|
| `tbr_trend` | `slope(last 3)` + `last` | OLS over 3 bars |
| `oi_chg_trend` | `slope(last 3)` + `last` | OLS over 3 bars |
| `funding_trend` | `slope(last 3)` + `last` | OLS over 3 bars |
| `top_long_drift` | `last - first of 3` | simple drift |
| `global_long_drift` | `last - first of 3` | simple drift |
| `net_buy_trend` | `last - first of 3` from bucketed CVD | drift |
| `ofi_pressure` | NEW (from microstructure) | sum + intensity |

The scorecard becomes:

```text
score = f(tbr_trend, oi_trend, funding_trend, top_long_drift,
          global_long_drift, net_buy_trend, ofi_pressure,
          bid_ask_qty_ratio, mega_at_keystone, mega_tier_balance)
```

Each factor carries both a magnitude and a direction. The scorecard
weights should also become **configurable** so we can A/B test
weightings against post-hoc outcomes (the keystone_history ledger is
the natural evaluation target).

### 3.5 Dimension E — funding rate into walls

Funding is currently consumed by `auction`, `regime`, `stage` — not by
walls. But funding tells you:
- **Positive funding** = longs pay shorts = market is long-biased and
  staying long costs carry = bid fuel is *expensive* to maintain.
  A buyer keystone defended under heavy positive funding is a *fragile*
  wall.
- **Negative funding** = shorts pay longs = market is short-biased =
  bid fuel is *cheap* to maintain = a buyer keystone is more likely
  to be institutional and persistent.

**Pure function to add** in `analysis/wall_migration.py`:

```python
def funding_aware_wall_score(
    funding_trend: float,
    funding_zscore: float,
    oi_trend: float,
) -> dict:
    """Adjust the wall-holds interpretation by funding carry.

    Returns {funding_zscore, carry_cost_adj, interpretation}.
    Positive carry cost adj = wall is expensive to maintain = lower
    confidence in persistence; negative = carry is paying the wall
    = higher confidence.
    """
```

---

## 4. Clean-up pass — the four hardcoded constants

### 4.1 Round-number anchors

Today: 11 hardcoded SOL prices in `_ROUND_ANCHORS`. For BTC, ETH,
SOL, these levels are wrong.

**Proposed**: a new pure function that **derives** round-number
anchors relative to the current price:

```python
def derive_round_anchors(
    price: float,
    tick_size: float = 0.01,
    precision_levels: tuple[int, ...] = (0, 5),  # .00 and .50
    depth: int = 8,  # 4 below + 4 above
    tol: float = 0.02,
) -> list[dict]:
    """Dynamic round-number anchors derived from current price.
    Returns [{name, level, lo, hi}].
    """
```

For SOL at $150 with `precision_levels=(0,5)` and `depth=4`:
- $149.00, $149.50, $150.00, $150.50, $151.00
For BTC at $65000:
- $64950, $65000, $65050

The existing `compute_round_anchors(bids)` becomes a thin wrapper that
takes the derived list and bids — exactly the legacy semantics, but
without the hardcoded numbers.

### 4.2 Bid tier thresholds

Today: hardcoded qty thresholds (mega=5000, large=1000, medium=200).
For SOL at $150, "mega=5000 SOL" = $750k. For BTC at $65000,
"mega=5000 BTC" = $325M which is absurd.

**Proposed**: parameterize by **USD notional**, not raw qty:

```python
@dataclass(frozen=True)
class TierConfig:
    mega_usd: float = 250_000.0
    large_usd: float = 50_000.0
    medium_usd: float = 10_000.0
    # Anything below medium_usd is "small".

def compute_bid_tiers_usd(
    bids, tier_config: TierConfig,
) -> dict[str, Any]:
    """Same shape as compute_bid_tiers but qty is converted to notional.
    Levels are bucketed by USD notional, not by raw qty.
    """
```

The existing `compute_bid_tiers(qty)` stays for backwards
compatibility, but `bid_tier_balance`, `mega_at_keystone`, and the
scorecard use the USD version. This is a one-line switch in the
adapters.

### 4.3 The 3% / 1% magic numbers

Today: `bid_floor = price * 0.97`, `ask_target = price * 1.03`,
`entry = price * 1.01`. These are SOL-session defaults — a 3% band
is reasonable for a $150 SOL but absurd for a $0.01 micro-cap.

**Proposed**: replace with **ATR-aware bands**:

```python
def default_wall_band(
    price: float,
    atr_pct: float | None = None,
    floor_bps: int = 30,  # 0.30%
    ceiling_bps: int = 100,  # 1.00%
) -> tuple[float, float, float]:
    """Compute (bid_floor, entry, ask_target) from price + ATR.

    If atr_pct is provided, the band scales with volatility:
      - Low ATR (<0.5%): use floor_bps / ceiling_bps (default 30/100 bps).
      - High ATR (>3%): scale to 1% / 3%.
    """
```

The 5m ATR comes from `futures.klines` (already in evidence). The
adapter computes it once per cycle and passes it in.

### 4.4 The 5-min hardcoded window for seller aggression

`seller_aggression_classify` uses `window_min=5` by default. The
5-minute window is the SOL-session default. For BTC or ETH on a quiet
day, this misses the trend; on a busy day, it averages too much.

**Proposed**: parameterize; the adapter chooses 5m/15m based on
`atr_pct` (volatile assets → shorter window to capture bursts).

---

## 5. New pure-function surface (proposed additions)

### 5.1 In `calculations/orderbook.py`

| Function | Purpose | Pure? |
|---|---|---|
| `derive_round_anchors(price, tick_size, precision_levels, depth, tol)` | Dynamic round anchors | YES |
| `ofi_wall_pressure(intervals, window_n)` | Top-of-book OFI over recent intervals | YES |
| `level_queue_dynamics(deltas, level, tol, window_s, now_ms)` | Wall refill/drain at one level | YES |
| `cumulative_depth_curve(levels, side, max_dist)` | Cumulative depth vs price (for OBI profile) | YES |

### 5.2 In `analysis/wall_migration.py`

| Function | Purpose | Pure? |
|---|---|---|
| `TierConfig` (frozen dataclass) | USD-based tier thresholds | YES |
| `compute_bid_tiers_usd(bids, tier_config)` | USD-normalized tiers | YES |
| `funding_aware_wall_score(funding_trend, funding_zscore, oi_trend)` | Funding carry into wall holds | YES |
| `wall_persistence_score(level, snapshots)` | How long has this level been a wall | YES |
| `wall_strength_score(level, asks, ofi, depth, trades)` | Composite wall quality | YES |
| `liquidity_vacuum_zones(book, lookback, threshold)` | Empty zones price could fall through | YES |

### 5.3 In `analysis/oi.py` (or `wall_migration.py`)

| Function | Purpose | Pure? |
|---|---|---|
| `depth_aware_wall_break(total_wall_qty, AD_qB, AD_qA, ofi, buy_per_min, peak_buy_per_min)` | AD-normalized wall break | YES |

### 5.4 Adapter-side additions in `pipeline.py::_adapt_wall_migration`

Read the microstructure latest keys + recent stream slices (bounded,
e.g. last 60 OFI intervals = 10 minutes of WS data at 10s intervals):

```python
async def _read_micro_context(
    redis: RedisRuntimeStore, venue: str, symbol: str,
    interval_window_n: int = 6,
    delta_window_n: int = 200,
) -> dict[str, Any]:
    """Read recent OFI intervals + recent depth deltas + live book."""
    return {
        "ofi_intervals": await redis.read_microstructure_intervals(
            venue, symbol, count=interval_window_n,
        ),
        "best_quote": await redis.read_microstructure_book(venue, symbol),
        "status": await redis.read_microstructure_status(venue, symbol),
        # deltas not yet exposed — would need a new reader
    }
```

Note: `read_microstructure_deltas` is **not** currently exposed in
`RedisRuntimeStore` (only `_intervals` and `_events`). This needs to
be added.

---

## 6. Architecture diagram (after this plan)

```text
                                              ┌──────────────────────────┐
   Binance Spot WS ────► microstructure       │ microstructure stream    │
   (depth diff)         capture.py            │ - raw deltas              │
                                              │ - best-quote events       │
                                              │ - OFI intervals (10s)     │
                                              │ - latest book + status    │
                                              └──────────────┬───────────┘
                                                             │ read on cycle
                                                             ▼
   Binance REST ──► poller ──► Redis raw stream ──► run_cycle ──► run_calculations
   (every 5s)                                                          │
                                                                      ▼
                                              ┌─────────────────────────────┐
                                              │ run_analysis                │
                                              │   wall_migration adapter    │
                                              │   ├── calc: orderbook       │
                                              │   ├── calc: technical       │
                                              │   ├── analysis: wall_mig    │
                                              │   │   ├── wall_delta        │
                                              │   │   ├── fuel_ratio        │
                                              │   │   ├── keystone_score    │◄─── NEW: ofi_wall_pressure
                                              │   │   ├── level_queue_dyn   │◄─── NEW: depth deltas
                                              │   │   ├── depth_aware_break │◄─── NEW: AD-normalized
                                              │   │   ├── funding_aware     │◄─── NEW: funding trend
                                              │   │   ├── wall_persistence  │◄─── NEW: cross-cycle
                                              │   │   ├── wall_strength     │◄─── NEW: composite
                                              │   │   ├── liquidity_vacuum  │◄─── NEW: thin zones
                                              │   │   └── mega_at_keystone  │
                                              │   ├── analysis: path_abs    │
                                              │   │   ├── fuel_ratio        │
                                              │   │   ├── ascent / descent  │
                                              │   │   └── ...               │
                                              │   └── analysis: oi          │
                                              │       ├── find_walls        │
                                              │       └── wall_break        │◄─── MODIFIED: depth-aware
                                              └─────────────┬───────────────┘
                                                             │ persist
                                                             ▼
                                              Postgres canonical envelope
                                                  + Redis latest + history
                                                  (keystone_history, wall_snapshot)
```

The four `◄─── NEW` blocks in the wall_migration adapter are the
ones this plan introduces. Everything else is unchanged.

---

## 7. Phasing — what to do first

### Phase 1 — Parameterize (1 PR, ~50 LOC, zero risk)

- Move hardcoded `_ROUND_ANCHORS` to a dynamic `derive_round_anchors`.
- Move hardcoded `_BID_TIER_THRESHOLDS` to a `TierConfig` dataclass +
  `compute_bid_tiers_usd`.
- Replace `price * 0.97` / `price * 1.03` / `price * 1.01` magic with
  ATR-aware `default_wall_band`.
- Keep legacy functions as thin wrappers for backwards compatibility.

**Tests**: existing tests pass; add new unit tests for each
parameterized function.

### Phase 2 — Multi-bar trends in scorecard (1 PR, ~100 LOC)

- Add `compute_trend_slope(series, n=3)` helper.
- Replace `_taker_buy_share`, `_oi_pct_change`, `_ls_last_pct`,
  `_net_buy_share` with multi-bar trend versions.
- Add `funding_trend` and `funding_zscore` from `futures.funding` (need
  to extend the on-demand derivative fetch to populate funding
  history).
- Re-score the scorecard: weights become configurable.

**Tests**: new unit tests for trend helpers; update
`test_wall_migration_extras.py` for the parameterized scorecard.

### Phase 3 — Microstructure wiring (1 PR, ~200 LOC)

- Add `RedisRuntimeStore.read_microstructure_deltas` (parallels
  `_intervals` and `_events`).
- Add `_read_micro_context` in `pipeline.py` that fetches recent OFI
  intervals + recent depth deltas + live best quote.
- Add `ofi_wall_pressure` and `level_queue_dynamics` pure functions.
- Wire into `_adapt_wall_migration` as new fields:
  `micro_ofi_pressure`, `micro_level_queue` (per-level refill/drain).
- These become new scorecard factors.

**Tests**: synthetic OFI interval fixtures; depth-delta fixtures
derived from `DepthDelta.from_binance` examples.

### Phase 4 — Depth-aware break + persistence (1 PR, ~150 LOC)

- Add `depth_aware_wall_break` and use it instead of
  `wall_break_assessment` (keep legacy as fallback when no AD).
- Add `wall_persistence_score` reading from `wall_snapshot` history.
- Add `wall_strength_score` composite.
- Add `liquidity_vacuum_zones` reading the futures book.

**Tests**: synthetic multi-cycle history; vacuum detection unit tests.

### Phase 5 — Documentation + tuning (no code)

- Update `docs/nooa-kb/behavior/tool-manifest.md` to list the new
  wall fields.
- Update briefing projections to include the new factors.
- A/B test scorecard weights against `keystone_history` outcomes.

---

## 8. What this does NOT change

- The poller, microstructure capture, and OFI/AD fitter are all
  unchanged. We only **read** from existing microstructure streams.
- The schema (`wall_snapshot`, `keystone_history`) is unchanged. The
  new fields live inside the envelope's `analysis.wall_migration`
  sub-dict; they don't need new tables.
- The Pass-3 paper-derived models (`PriceImpactFit`,
  `DepthScalingFit`, `MicrostructureEvidence`) are unchanged — those
  are the wake-worker's domain, not the wall surface's.
- The wake worker is unchanged. It already consumes microstructure
  transitions for trigger evaluation; it does not consume walls.
- The NOOA agents' prompt contracts are unchanged. New fields appear
  in the bounded envelope view, not in the agent prompts.

---

## 9. Acceptance criteria per phase

### Phase 1

- `_ROUND_ANCHORS` no longer exists as a hardcoded tuple.
- `compute_bid_tiers_usd` returns tiers bucketed by USD notional.
- `_adapt_wall_migration` no longer has `price * 0.97` /
  `price * 1.03` literals.
- All existing tests pass.
- A new unit test exercises `derive_round_anchors` for SOL and BTC
  and asserts the anchors differ.

### Phase 2

- The scorecard reads 3-bar trends for tbr, oi, funding,
  top_long, net_buy.
- Funding trend is computed from `futures.funding` history (new
  field on the on-demand derivative fetch).
- The scorecard weights are exposed as a config block on the
  Settings object.
- Existing scorecard tests are updated; new tests cover trend
  helpers.

### Phase 3

- `_adapt_wall_migration` reads from the microstructure stream.
- New fields `micro_ofi_pressure`, `micro_level_queue` are
  populated when microstructure is running, null otherwise.
- The scorecard includes the new fields.
- A new unit test exercises `ofi_wall_pressure` with synthetic
  OFIInterval fixtures.
- A new unit test exercises `level_queue_dynamics` with synthetic
  DepthDelta fixtures.

### Phase 4

- `_wall_break` reads AD from microstructure and uses
  `depth_aware_wall_break`.
- `wall_persistence_score` is populated from `wall_snapshot`
  history.
- `liquidity_vacuum_zones` appears in the envelope.
- Existing wall_break tests pass; new tests cover the
  depth-aware path.

---

## 10. Open questions for the user

1. **Futures WS capture** — should we mirror the WS capture to
   Binance USD-M futures depth? The wall surface runs on futures;
   the WS capture is spot only. If yes, that's a separate
   container.

2. **Scorecard weighting** — do we want a config object
   (`Settings.wall_scorecard_weights`) or a default block in
   `analysis/wall_migration.py`? The former is cleaner; the
   latter is more discoverable.

3. **TBR 3-bar trend** — `taker_buy_sell` is a 5-min bar series,
   so 3 bars = 15 minutes. That's the right horizon for walls
   (vs 1-bar which is 5 minutes). Confirmed?

4. **Funding history** — currently `futures.funding` is a single
   value (latest rate). To compute a 3-bar trend we'd need
   `funding_history` similar to `oi_history`. Is the on-demand
   derivative step the right place to add this?

5. **`wall_snapshot` history** — the persistence score needs at
   least N prior cycles. What's the default N? 6 (30 minutes at
   5s/cycle)? 12 (1 hour)?

---

End of plan. No code changes made.
