"""
Long-term order-flow analysis for SOL/USDT.

Builds volume profile, tracks keystone migration, detects chase patterns,
identifies institutional orders, and infers market stage over 1h + 4h windows.

Data sources:
  - /fapi/v1/aggTrades  : historical trade aggregation (paginated by fromId)
  - /fapi/v1/depth      : current orderbook
  - /fapi/v1/klines     : 1h + 4h klines for structure
  - /futures/data/openInterestHist : OI history
  - /futures/data/takerlongshortRatio : taker buy ratio
  - /futures/data/topLongShortAccountRatio : top trader positioning
  - /futures/data/globalLongShortAccountRatio : global positioning
"""
import urllib.request, json
from datetime import datetime, timezone
from collections import defaultdict
import time

BASE = 'https://fapi.binance.com'
SYM = 'SOLUSDT'
BUCKET = 0.05            # price bucket size for volume profile
INST_MIN_QTY = 100.0     # single institutional print threshold (SOL)
CLUSTER_MIN_QTY = 50.0   # per-trade threshold inside a cluster
CLUSTER_MIN_TRADES = 5   # min trades inside a 2s window to count as cluster
CLUSTER_WINDOW_MS = 2000 # cluster time window
MIG_WINDOW = 0.20        # rolling window for keystone detection
MIG_RANGE_LO = -0.50     # keystone search range below price (relative)
MIG_RANGE_HI = -0.05


def get(path, params=None):
    url = BASE + path
    if params:
        url += '?' + '&'.join('{}={}'.format(k, v) for k, v in params.items())
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


# --- Live reference price + clock ---
ticker = get('/fapi/v1/ticker/price', {'symbol': SYM})
px = float(ticker['price'])
server_time = get('/fapi/v1/time')['serverTime']
now_ms = server_time
print('=== LIVE PRICE: {:.4f} (ts: {} UTC) ==='.format(
    px, datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
print('Symbol: {}  Window: 1h + 4h'.format(SYM))
print()


# --- Paginated aggTrades pull ---
def pull_trades(start_ms, end_ms, label):
    """Pull all aggTrades in [start_ms, end_ms] via fromId pagination."""
    print('  Pulling {} aggTrades ({} ... {})...'.format(
        label,
        datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime('%H:%M:%S'),
        datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime('%H:%M:%S')))
    trades = []
    # start with a single call to get the fromId baseline
    first = get('/fapi/v1/aggTrades', {'symbol': SYM, 'limit': 1000})
    # Walk forward in time: use fromId ascending
    # We need fromId where ts >= start_ms; walk backwards then forwards if needed
    # Simpler: keep calling with startTime/endTime/limit, paginate via fromId
    last_id = None
    # First batch with time bounds
    params = {'symbol': SYM, 'limit': 1000, 'startTime': start_ms, 'endTime': end_ms}
    batch = get('/fapi/v1/aggTrades', params)
    trades.extend(batch)
    last_id = batch[-1]['a'] if batch else None
    safety = 0
    while batch and safety < 200:
        safety += 1
        # next page: fromId = last_id + 1, keep endTime
        params = {'symbol': SYM, 'limit': 1000, 'fromId': last_id + 1, 'endTime': end_ms}
        batch = get('/fapi/v1/aggTrades', params)
        if not batch:
            break
        # If the first trade in this batch is past end_ms, stop
        if batch[0]['T'] > end_ms:
            break
        trades.extend(batch)
        last_id = batch[-1]['a']
    print('    -> {} trades pulled ({} API calls)'.format(len(trades), safety + 1))
    return trades


# --- Define windows ---
FOUR_H_MS = 4 * 3600 * 1000
ONE_H_MS = 1 * 3600 * 1000

end_ms = now_ms
start_4h = end_ms - FOUR_H_MS
start_1h = end_ms - ONE_H_MS

print('=== PULLING TRADE TAPES ===')
trades_4h = pull_trades(start_4h, end_ms, '4h')
trades_1h = pull_trades(start_1h, end_ms, '1h')
print()

# Normalize: each trade dict has a, p, q, T, m (isBuyerMaker)
def norm(t):
    return {
        'id': int(t['a']),
        'px': float(t['p']),
        'qty': float(t['q']),
        'ts': int(t['T']),
        'is_buyer_maker': bool(t['m']),  # True = aggressive sell
        'notional': float(t['p']) * float(t['q']),
    }

trades_4h = [norm(t) for t in trades_4h]
trades_1h = [norm(t) for t in trades_1h]

print('Normalized trades: {} (4h)  {} (1h)'.format(len(trades_4h), len(trades_1h)))
print()


# --- Supporting data: orderbook, klines, OI, taker ratios, positioning ---
book = get('/fapi/v1/depth', {'symbol': SYM, 'limit': 100})
bids = [(float(p), float(q)) for p, q in book['bids']]
asks = [(float(p), float(q)) for p, q in book['asks']]

klines_1h = get('/fapi/v1/klines', {'symbol': SYM, 'interval': '1h', 'limit': 6})
klines_4h = get('/fapi/v1/klines', {'symbol': SYM, 'interval': '4h', 'limit': 8})
oi_hist = get('/futures/data/openInterestHist', {'symbol': SYM, 'period': '5m', 'limit': 96})
tbs = get('/futures/data/takerlongshortRatio', {'symbol': SYM, 'period': '5m', 'limit': 96})
top = get('/futures/data/topLongShortAccountRatio', {'symbol': SYM, 'period': '5m', 'limit': 24})
glb = get('/futures/data/globalLongShortAccountRatio', {'symbol': SYM, 'period': '5m', 'limit': 24})
funding = get('/fapi/v1/premiumIndex', {'symbol': SYM})

# ============================================================
# 1. VOLUME PROFILE (4h and 1h, side-split)
# ============================================================
print('=' * 70)
print('1. VOLUME PROFILE (4h and 1h, side-split, 0.05 SOL buckets)')
print('=' * 70)


def build_vp(trades):
    """Return: per-bucket buy_qty, sell_qty, total_qty, trade_count, low, high."""
    bucket_data = defaultdict(lambda: {'buy': 0.0, 'sell': 0.0, 'n': 0, 'buy_n': 0, 'sell_n': 0})
    for t in trades:
        b = round(t['px'] / BUCKET) * BUCKET
        bucket_data[b]['n'] += 1
        if t['is_buyer_maker']:
            # aggressive sell
            bucket_data[b]['sell'] += t['qty']
            bucket_data[b]['sell_n'] += 1
        else:
            # aggressive buy
            bucket_data[b]['buy'] += t['qty']
            bucket_data[b]['buy_n'] += 1
    return bucket_data


vp_4h = build_vp(trades_4h)
vp_1h = build_vp(trades_1h)


def vp_summary(vp, label):
    if not vp:
        print('  {}: no trades'.format(label))
        return
    total_vol = sum(d['buy'] + d['sell'] for d in vp.values())
    sorted_buckets = sorted(vp.keys())
    # POC = bucket with max total volume
    poc = max(sorted_buckets, key=lambda b: vp[b]['buy'] + vp[b]['sell'])
    # Value area: 70% of total volume centered around POC
    sorted_by_vol = sorted(sorted_buckets, key=lambda b: vp[b]['buy'] + vp[b]['sell'], reverse=True)
    cum = 0
    va_buckets = []
    for b in sorted_by_vol:
        cum += vp[b]['buy'] + vp[b]['sell']
        va_buckets.append(b)
        if cum >= total_vol * 0.70:
            break
    vah = max(va_buckets)
    val = min(va_buckets)
    # High Volume Nodes (top 10% by volume)
    hvn_threshold = sorted([vp[b]['buy'] + vp[b]['sell'] for b in sorted_buckets], reverse=True)[max(1, len(sorted_buckets) // 10)]
    hvn = [b for b in sorted_buckets if vp[b]['buy'] + vp[b]['sell'] >= hvn_threshold]
    lvn = [b for b in sorted_buckets if vp[b]['buy'] + vp[b]['sell'] < (total_vol / len(sorted_buckets)) * 0.3]

    print()
    print('  {} PROFILE (total vol = {:,.0f} SOL)'.format(label, total_vol))
    print('    POC (point of control):       {:.4f}  (vol = {:,.0f})'.format(
        poc, vp[poc]['buy'] + vp[poc]['sell']))
    print('    VAH / VAL (70% value area):   {:.4f} / {:.4f}'.format(vah, val))
    print('    Price range traded:           {:.4f} - {:.4f}'.format(min(sorted_buckets), max(sorted_buckets)))
    print('    HVN (top volume nodes):       {}'.format(', '.join('{:.4f}'.format(b) for b in hvn[:8])))
    print('    LVN (low volume nodes / gaps):{}'.format(', '.join('{:.4f}'.format(b) for b in lvn[:8])))
    return {
        'total': total_vol,
        'poc': poc,
        'vah': vah,
        'val': val,
        'low': min(sorted_buckets),
        'high': max(sorted_buckets),
        'hvn': hvn,
    }


vp_4h_summary = vp_summary(vp_4h, '4H')
vp_1h_summary = vp_summary(vp_1h, '1H')


def vp_side_split(vp, label):
    """Show top buckets by BUY volume vs SELL volume."""
    sorted_buckets = sorted(vp.keys())
    by_buy = sorted(sorted_buckets, key=lambda b: vp[b]['buy'], reverse=True)[:8]
    by_sell = sorted(sorted_buckets, key=lambda b: vp[b]['sell'], reverse=True)[:8]
    print()
    print('  {} TOP BUY BUCKETS (where aggressive bids concentrated):'.format(label))
    print('    {:>10}  {:>10}  {:>10}  {:>8}'.format('Bucket', 'BuyQty', 'Notional$', 'Trades'))
    for b in by_buy:
        d = vp[b]
        print('    {:>10.4f}  {:>10,.0f}  ${:>9,.0f}  {:>8}'.format(
            b, d['buy'], d['buy'] * b, d['buy_n']))
    print('  {} TOP SELL BUCKETS (where aggressive offers concentrated):'.format(label))
    print('    {:>10}  {:>10}  {:>10}  {:>8}'.format('Bucket', 'SellQty', 'Notional$', 'Trades'))
    for b in by_sell:
        d = vp[b]
        print('    {:>10.4f}  {:>10,.0f}  ${:>9,.0f}  {:>8}'.format(
            b, d['sell'], d['sell'] * b, d['sell_n']))


vp_side_split(vp_4h, '4H')
vp_side_split(vp_1h, '1H')


# ============================================================
# 2. KEYSTONE MIGRATION (per hour over the 4h window)
# ============================================================
print()
print('=' * 70)
print('2. KEYSTONE MIGRATION (per hour, 4h window)')
print('=' * 70)


def hourly_keystones(trades, window=MIG_WINDOW):
    """For each hour bucket, find the price level where taker buys concentrated most.
    This is the 'buyer keystone' for that hour — where buyers stepped in to take liquidity.
    Returns list of (hour_start_ts, keystone_price, buy_vol, sell_vol, low, high).
    """
    hourly = defaultdict(lambda: {'buy_vol': 0.0, 'sell_vol': 0.0, 'low': 1e9, 'high': 0.0,
                                   'open': None, 'close': None, 'first_ts': None, 'last_ts': None,
                                   'buys_by_px': defaultdict(float), 'sells_by_px': defaultdict(float)})
    for t in trades:
        hr = (t['ts'] // 3600000) * 3600000
        d = hourly[hr]
        b = round(t['px'] / BUCKET) * BUCKET
        if t['is_buyer_maker']:
            d['sell_vol'] += t['qty']
            d['sells_by_px'][b] += t['qty']
        else:
            d['buy_vol'] += t['qty']
            d['buys_by_px'][b] += t['qty']
        d['low'] = min(d['low'], t['px'])
        d['high'] = max(d['high'], t['px'])
        if d['first_ts'] is None:
            d['first_ts'] = t['ts']
            d['open'] = t['px']
        d['last_ts'] = t['ts']
        d['close'] = t['px']
    out = []
    for hr in sorted(hourly.keys()):
        d = hourly[hr]
        if not d['buys_by_px']:
            # No aggressive buys in this hour — use price midpoint as fallback
            keystone = (d['low'] + d['high']) / 2.0 if d['high'] > d['low'] else px
            buy_vol = 0.0
        else:
            # Keystone = price level with highest aggressive BUY volume
            keystone = max(d['buys_by_px'].keys(), key=lambda p: d['buys_by_px'][p])
            buy_vol = d['buys_by_px'][keystone]
        out.append({
            'hour_ts': hr,
            'keystone': keystone,
            'buy_vol': buy_vol,
            'sell_vol': d['sell_vol'],
            'low': d['low'],
            'high': d['high'],
            'open': d['open'],
            'close': d['close'],
            'n_buys': sum(d['buys_by_px'].values()),
            'n_sells': sum(d['sells_by_px'].values()),
        })
    return out


migs = hourly_keystones(trades_4h)
print()
print('  Hour | Open   Close  Low    High   | Keystone | BuyVol@K | SellVol | Direction')
print('  ' + '-' * 90)
prev_k = None
mig_deltas = []
for m in migs:
    direction = '—'
    delta = 0.0
    if prev_k is not None:
        delta = m['keystone'] - prev_k
        if delta > 0.05: direction = 'UP'
        elif delta < -0.05: direction = 'DOWN'
        else: direction = 'FLAT'
        mig_deltas.append((m['hour_ts'], delta, direction))
    print('  {} | {:6.3f} {:6.3f} {:6.3f} {:6.3f} | {:9.4f} | {:>9,.0f} | {:>8,.0f} | {:>6} ({:+.4f})'.format(
        datetime.fromtimestamp(m['hour_ts'] / 1000, tz=timezone.utc).strftime('%H:%M'),
        m['open'], m['close'], m['low'], m['high'],
        m['keystone'], m['buy_vol'], m['sell_vol'], direction, delta))
    prev_k = m['keystone']

if mig_deltas:
    up_n = sum(1 for _, _, d in mig_deltas if d == 'UP')
    down_n = sum(1 for _, _, d in mig_deltas if d == 'DOWN')
    flat_n = sum(1 for _, _, d in mig_deltas if d == 'FLAT')
    avg_delta = sum(d for _, d, _ in mig_deltas) / len(mig_deltas)
    print()
    print('  MIGRATION SUMMARY:')
    print('    UP steps: {}  |  DOWN steps: {}  |  FLAT steps: {}'.format(up_n, down_n, flat_n))
    print('    Avg hourly delta: {:+.4f}'.format(avg_delta))
    if down_n > up_n:
        mig_verdict = 'BEARISH — keystone migrating DOWN (buyers losing conviction)'
    elif up_n > down_n:
        mig_verdict = 'BULLISH — keystone migrating UP (buyers advancing)'
    else:
        mig_verdict = 'NEUTRAL — keystone oscillating'
    print('    >>> {}'.format(mig_verdict))


# ============================================================
# 3. CHASE DETECTION (which side is chasing?)
# ============================================================
print()
print('=' * 70)
print('3. CHASE DETECTION')
print('=' * 70)
print()
print('  Method: at each keystone migration step, compare aggressive buy vol near the')
print('          new keystone vs aggressive sell vol above it. Heavy buys chasing a')
print('          falling keystone = LONG CHASERS = sellers winning. Heavy sells above')
print('          a rising keystone = SHORT CHASERS = buyers winning.')
print()


def chase_per_step(trades, migs):
    """For each hour, look at trades within 0.10 of the hourly keystone.
    Compute buy/sell ratio INSIDE the zone. The dominant side at the keystone level
    is the side CHASING (defending or hitting).
    """
    results = []
    for m in migs:
        hr_lo = m['hour_ts']
        hr_hi = hr_lo + 3600000
        zone_lo = m['keystone'] - 0.10
        zone_hi = m['keystone'] + 0.10
        in_zone = [t for t in trades if hr_lo <= t['ts'] < hr_hi and zone_lo <= t['px'] <= zone_hi]
        buy_qty = sum(t['qty'] for t in in_zone if not t['is_buyer_maker'])
        sell_qty = sum(t['qty'] for t in in_zone if t['is_buyer_maker'])
        results.append({
            'hour_ts': hr_lo,
            'keystone': m['keystone'],
            'buy_qty': buy_qty,
            'sell_qty': sell_qty,
            'net': buy_qty - sell_qty,
        })
    return results


chases = chase_per_step(trades_4h, migs)
print('  Per-hour chase activity (within ±0.10 of hourly keystone):')
print('  Hour   | Keystone | BuyQty  | SellQty | Net     | Who is chasing?')
print('  ' + '-' * 75)
for c in chases:
    if c['buy_qty'] + c['sell_qty'] < 1:
        chaser = 'none (no flow)'
    elif c['net'] > 0:
        chaser = 'BUYERS (absorbing)'
    else:
        chaser = 'SELLERS (hitting)'
    print('  {} | {:9.4f} | {:>7,.0f} | {:>7,.0f} | {:>+7,.0f} | {}'.format(
        datetime.fromtimestamp(c['hour_ts'] / 1000, tz=timezone.utc).strftime('%H:%M'),
        c['keystone'], c['buy_qty'], c['sell_qty'], c['net'], chaser))

# Overall verdict
total_buy = sum(c['buy_qty'] for c in chases)
total_sell = sum(c['sell_qty'] for c in chases)
print()
print('  Total buy qty at keystones:  {:,.0f}'.format(total_buy))
print('  Total sell qty at keystones: {:,.0f}'.format(total_sell))
print('  Buy/Sell ratio:              {:.2f}'.format(total_buy / max(total_sell, 1)))
if total_sell > total_buy * 1.5:
    chase_verdict = 'SELLERS HITTING KEYSTONE — institutional supply confirmed'
elif total_buy > total_sell * 1.5:
    chase_verdict = 'BUYERS ABSORBING — keystone defended, accumulation underway'
else:
    chase_verdict = 'BALANCED — no dominant chaser'
print('  >>> {}'.format(chase_verdict))


# ============================================================
# 4. INSTITUTIONAL ORDER DETECTION
# ============================================================
print()
print('=' * 70)
print('4. INSTITUTIONAL ORDER DETECTION (4h window)')
print('=' * 70)
print()
print('  Single prints >= {} SOL and clusters of {} trades >= {} SOL within {}s window.'.format(
    INST_MIN_QTY, CLUSTER_MIN_TRADES, CLUSTER_MIN_QTY, CLUSTER_WINDOW_MS / 1000))


def find_clusters(trades):
    """Find clusters of large trades within CLUSTER_WINDOW_MS.
    Returns list of cluster dicts sorted by total notional.
    """
    sorted_t = sorted(trades, key=lambda x: x['ts'])
    clusters = []
    i = 0
    while i < len(sorted_t):
        if sorted_t[i]['qty'] < CLUSTER_MIN_QTY:
            i += 1
            continue
        cluster_start = sorted_t[i]['ts']
        cluster = [sorted_t[i]]
        j = i + 1
        while (j < len(sorted_t)
               and sorted_t[j]['ts'] - cluster_start <= CLUSTER_WINDOW_MS
               and sorted_t[j]['qty'] >= CLUSTER_MIN_QTY):
            cluster.append(sorted_t[j])
            j += 1
        if len(cluster) >= CLUSTER_MIN_TRADES:
            total_qty = sum(t['qty'] for t in cluster)
            long_qty = sum(t['qty'] for t in cluster if not t['is_buyer_maker'])
            short_qty = sum(t['qty'] for t in cluster if t['is_buyer_maker'])
            avg_px = sum(t['px'] * t['qty'] for t in cluster) / total_qty
            total_notional = total_qty * avg_px
            if long_qty > short_qty:
                cluster_type = 'ABSORPTION'
            elif short_qty > long_qty:
                cluster_type = 'DISTRIBUTION'
            else:
                cluster_type = 'TWO-SIDED'
            clusters.append({
                'ts': cluster_start,
                'n': len(cluster),
                'total_qty': total_qty,
                'long_qty': long_qty,
                'short_qty': short_qty,
                'avg_px': avg_px,
                'total_notional': total_notional,
                'type': cluster_type,
            })
        i = max(j, i + 1)
    return clusters


clusters = find_clusters(trades_4h)
clusters.sort(key=lambda c: c['total_notional'], reverse=True)

print()
print('  TOP 20 INSTITUTIONAL CLUSTERS (by notional):')
print('  {:<8}  {:>5}  {:>10}  {:>10}  {:>10}  {:>10}  {:>13}  {:<13}'.format(
    'Time', 'TrdN', 'TotalSOL', 'LongSOL', 'ShortSOL', 'AvgPx', 'Notional($)', 'Type'))
print('  ' + '-' * 95)
for c in clusters[:20]:
    print('  {:<8}  {:>5}  {:>10,.0f}  {:>10,.0f}  {:>10,.0f}  {:>10.4f}  ${:>12,.0f}  {:<13}'.format(
        datetime.fromtimestamp(c['ts'] / 1000, tz=timezone.utc).strftime('%H:%M:%S'),
        c['n'], c['total_qty'], c['long_qty'], c['short_qty'], c['avg_px'],
        c['total_notional'], c['type']))

# Cluster type breakdown
type_counts = defaultdict(int)
type_notional = defaultdict(float)
for c in clusters:
    type_counts[c['type']] += 1
    type_notional[c['type']] += c['total_notional']
print()
print('  CLUSTER TYPE BREAKDOWN:')
for t in ['ABSORPTION', 'DISTRIBUTION', 'TWO-SIDED']:
    print('    {:<13}: {:>3} clusters  ${:>14,.0f} total'.format(
        t, type_counts.get(t, 0), type_notional.get(t, 0)))

# Largest single prints
big_prints = sorted([t for t in trades_4h if t['qty'] >= INST_MIN_QTY],
                    key=lambda x: x['notional'], reverse=True)
print()
print('  TOP 15 SINGLE PRINTS (>= {} SOL):'.format(INST_MIN_QTY))
print('  {:<8}  {:>5}  {:>9}  {:>9}  {:>11}  {:>5}'.format(
    'Time', 'Side', 'QtySOL', 'Px', 'Notional($)', 'Age(m)'))
print('  ' + '-' * 65)
for t in big_prints[:15]:
    side = 'BUY ' if not t['is_buyer_maker'] else 'SELL'
    age_m = (now_ms - t['ts']) / 60000
    print('  {:<8}  {:>5}  {:>9,.0f}  {:>9.4f}  ${:>10,.0f}  {:>5.0f}m'.format(
        datetime.fromtimestamp(t['ts'] / 1000, tz=timezone.utc).strftime('%H:%M:%S'),
        side, t['qty'], t['px'], t['notional'], age_m))


# ============================================================
# 5. STAGE INFERENCE
# ============================================================
print()
print('=' * 70)
print('5. STAGE INFERENCE')
print('=' * 70)
print()

# Volume direction split: which % of volume was on up-bars vs down-bars
def vol_direction_split(klines):
    up_vol = 0.0
    down_vol = 0.0
    for k in klines:
        o = float(k[1]); c = float(k[4]); v = float(k[5])
        if c > o:
            up_vol += v
        elif c < o:
            down_vol += v
    total = up_vol + down_vol
    return up_vol, down_vol, (up_vol / total * 100 if total > 0 else 0)

up_4h, down_4h, up_pct_4h = vol_direction_split(klines_4h)

# OI trend over last 4h
oi_series = []
for r in oi_hist:
    try:
        ts = int(r['timestamp'])
        oi = float(r['sumOpenInterest'])
        oi_series.append({'ts': ts, 'oi': oi})
    except Exception:
        pass
oi_series.sort(key=lambda x: x['ts'])
# Take last 48 bars (4h)
oi_4h = oi_series[-48:] if len(oi_series) >= 48 else oi_series
if len(oi_4h) >= 2:
    oi_open = oi_4h[0]['oi']
    oi_close = oi_4h[-1]['oi']
    oi_chg_4h = (oi_close - oi_open) / oi_open * 100
else:
    oi_open = oi_close = oi_chg_4h = 0

# Price change over 4h
if klines_4h:
    px_4h_open = float(klines_4h[0][1])
    px_4h_close = float(klines_4h[-1][4])
    px_chg_4h = (px_4h_close - px_4h_open) / px_4h_open * 100
else:
    px_4h_open = px_4h_close = px_chg_4h = 0

# Keystone migration: count up vs down
if mig_deltas:
    up_steps = sum(1 for _, _, d in mig_deltas if d == 'UP')
    down_steps = sum(1 for _, _, d in mig_deltas if d == 'DOWN')
else:
    up_steps = down_steps = 0

# Funding
funding_bps = float(funding['lastFundingRate']) * 10000

# Top trader long %
top_long_pct = float(top[-1]['longAccount']) * 100
glb_long_pct = float(glb[-1]['longAccount']) * 100

print('  Quantitative inputs (4h window):')
print('    Price change:           {:+.2f}%'.format(px_chg_4h))
print('    OI change:              {:+.2f}%'.format(oi_chg_4h))
print('    Up-bar vol:             {:,.0f}  ({:.1f}%)'.format(up_4h, up_pct_4h))
print('    Down-bar vol:           {:,.0f}  ({:.1f}%)'.format(down_4h, 100 - up_pct_4h))
print('    Keystone direction:     {} UP, {} DOWN'.format(up_steps, down_steps))
print('    Funding:                {:+.2f} bps'.format(funding_bps))
print('    Top trader long%:       {:.2f}%'.format(top_long_pct))
print('    Global long%:           {:.2f}%'.format(glb_long_pct))
print('    Cluster type dominant:  {} (${:,.0f})'.format(
    max(type_notional.keys(), key=lambda k: type_notional[k]) if type_notional else 'N/A',
    max(type_notional.values()) if type_notional else 0))
print()

# Stage scoring
score = 0
reasons = []

# Accumulation signals
# Tight range, low vol, no trend
if abs(px_chg_4h) < 1.0:
    score += 1
    reasons.append('Tight 4h range (<1%) — accumulation signature')
if up_pct_4h > 50 and up_pct_4h < 70:
    score += 1
    reasons.append('Balanced volume direction (50-70% on up-bars) — neutral')

# Markup signals
# Rising prices, rising vol, keystone up
if px_chg_4h > 1.0 and oi_chg_4h > 0.5:
    score += 2
    reasons.append('Price + OI rising — markup (longs entering)')
if up_steps > down_steps:
    score += 1
    reasons.append('Keystone migrating UP — buyers advancing')

# Distribution signals
# High vol at top, OI flat while price falls, keystone down
if px_chg_4h < -0.5 and abs(oi_chg_4h) < 0.5:
    score += 2
    reasons.append('Price DOWN but OI flat — distribution (longs exiting, not fresh shorts)')
if down_steps > up_steps:
    score += 2
    reasons.append('Keystone migrating DOWN — buyers retreating')
if up_pct_4h < 50:
    score += 1
    reasons.append('More volume on DOWN-bars — sellers dominant')
if type_notional.get('DISTRIBUTION', 0) > type_notional.get('ABSORPTION', 0) * 2:
    score += 2
    reasons.append('DISTRIBUTION clusters 2x ABSORPTION — institutional supply')

# Markdown signals
# Falling prices, OI rising, sellers aggressive
if px_chg_4h < -1.0 and oi_chg_4h > 0.5:
    score += 2
    reasons.append('Price DOWN + OI UP — markdown (fresh shorts entering)')

# Classification
print('  Signals detected:')
for r in reasons:
    print('    + {}'.format(r))

# Map score to stage
if score >= 4 and 'Keystone migrating DOWN' in '\n'.join(reasons) and 'Price DOWN but OI flat' in '\n'.join(reasons):
    stage = 'DISTRIBUTION (late stage — high conviction)'
    stage_conf = 'HIGH'
elif score >= 3 and 'Keystone migrating DOWN' in '\n'.join(reasons):
    stage = 'DISTRIBUTION (early/mid stage)'
    stage_conf = 'MEDIUM'
elif score >= 3 and 'Price + OI rising' in '\n'.join(reasons):
    stage = 'MARKUP'
    stage_conf = 'MEDIUM'
elif score >= 3 and 'Price DOWN + OI UP' in '\n'.join(reasons):
    stage = 'MARKDOWN'
    stage_conf = 'MEDIUM'
elif score <= 2 and abs(px_chg_4h) < 1.0:
    stage = 'ACCUMULATION'
    stage_conf = 'LOW-MEDIUM'
else:
    stage = 'TRANSITION / UNCLEAR'
    stage_conf = 'LOW'

print()
print('  STAGE READ: {} (confidence: {})'.format(stage, stage_conf))
print('  Score: {}/10'.format(score))


# ============================================================
# 6. INSTITUTIONAL FOOTPRINT SUMMARY (top by notional, all 4h)
# ============================================================
print()
print('=' * 70)
print('6. LARGE ORDERS SUMMARY (all trades >= {} SOL over 4h)'.format(INST_MIN_QTY))
print('=' * 70)

big_total = big_prints
buy_total = sum(t['notional'] for t in big_total if not t['is_buyer_maker'])
sell_total = sum(t['notional'] for t in big_total if t['is_buyer_maker'])
total_notional_4h = buy_total + sell_total

print()
print('  Total institutional prints (>= {} SOL): {}'.format(INST_MIN_QTY, len(big_total)))
print('  Buy prints:  {}  (${:,.0f})'.format(sum(1 for t in big_total if not t['is_buyer_maker']), buy_total))
print('  Sell prints: {}  (${:,.0f})'.format(sum(1 for t in big_total if t['is_buyer_maker']), sell_total))
print('  Buy/Sell notional ratio: {:.2f}'.format(buy_total / max(sell_total, 1)))
print()

# Side by hour: when did the institutional activity concentrate?
hourly_inst = defaultdict(lambda: {'buy': 0.0, 'sell': 0.0, 'buy_n': 0, 'sell_n': 0})
for t in big_total:
    hr = (t['ts'] // 3600000) * 3600000
    if not t['is_buyer_maker']:
        hourly_inst[hr]['buy'] += t['notional']
        hourly_inst[hr]['buy_n'] += 1
    else:
        hourly_inst[hr]['sell'] += t['notional']
        hourly_inst[hr]['sell_n'] += 1
print('  Institutional footprint by hour:')
print('  Hour   | BuyNotional    SellNotional   Net         | BuyN SellN')
print('  ' + '-' * 75)
for hr in sorted(hourly_inst.keys()):
    d = hourly_inst[hr]
    net = d['buy'] - d['sell']
    print('  {} | ${:>12,.0f} | ${:>12,.0f} | ${:>+10,.0f} | {:>4} {:>4}'.format(
        datetime.fromtimestamp(hr / 1000, tz=timezone.utc).strftime('%H:%M'),
        d['buy'], d['sell'], net, d['buy_n'], d['sell_n']))


print()
print('=' * 70)
print('END OF REPORT')
print('=' * 70)
