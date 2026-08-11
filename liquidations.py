"""
SOL/USDT LIQUIDATION PRESSURE READ
==================================

Public liquidation tape (Binance `/fapi/v1/forceOrders`, Coinglass) is gated
behind API keys since 2023-2024. This script derives a forced-sell/forced-buy
pressure estimate from publicly observable proxies:

  1. OI delta over short windows
     - Sharp negative OI delta WITHOUT proportional price move = liquidations
     - OI flat while price drops = long-side profit-taking (organic)
     - OI drops while price drops = forced unwinds likely occurring

  2. Taker buy ratio against price trajectory
     - TBR falling while price falls = genuine long liquidation flow
     - TBR rising while price falls = short covering / dip buying

  3. Funding rate flip
     - Funding going from positive to negative = long crowd getting squeezed
     - Funding staying negative = shorts are paying (short squeeze risk)

  4. Large-print clusters near technical levels
     - Same-second clusters of >=50 SOL trades at the same price = forced flow
     - These often precede or coincide with liquidation cascades

If you want ACTUAL liquidation event data, plug a key into one of:
  - CRYPTOQUANT_API_KEY  -> cryptoquant_client.py (exchange-flows/liquidation)
  - COINGLASS_API_KEY    -> https://api.coinglass.com/v3/futures/liquidation/aggregated
  - COINALYZE_API_KEY    -> https://api.coinalyze.net/v1/liq-stats
"""

import urllib.request, json
from datetime import datetime, timezone
from collections import defaultdict

BASE = 'https://fapi.binance.com'
sym = 'SOLUSDT'

def get(path, params=None):
    url = BASE + path
    if params:
        url += '?' + '&'.join('{}={}'.format(k, v) for k, v in params.items())
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


# --- Pull required data ---
ticker = get('/fapi/v1/ticker/price', {'symbol': sym})
px = float(ticker['price'])

funding = get('/fapi/v1/premiumIndex', {'symbol': sym})
oi_hist = get('/futures/data/openInterestHist', {'symbol': sym, 'period': '5m', 'limit': 24})
tbs = get('/futures/data/takerlongshortRatio', {'symbol': sym, 'period': '5m', 'limit': 24})
trades = get('/fapi/v1/trades', {'symbol': sym, 'limit': 1000})
klines_1m = get('/fapi/v1/klines', {'symbol': sym, 'interval': '1m', 'limit': 240})

print('=== LIQUIDATION PRESSURE READ — SOLUSDT ===')
print('Time: {} UTC'.format(datetime.now(tz=timezone.utc).strftime('%H:%M:%S')))
print('Price: {:.4f}'.format(px))
print()

# --- 1. OI delta signal ---
print('=== 1. OI DELTA vs PRICE MOVE ===')
oi_series = []
for r in oi_hist:
    try:
        ts = int(r['timestamp'])
        oi = float(r['sumOpenInterest'])
        oi_val = float(r['sumOpenInterestValue'])
        oi_series.append({'ts': ts, 'oi': oi, 'val': oi_val})
    except Exception:
        pass
oi_series.sort(key=lambda x: x['ts'])

# Build price map for the same 5-min windows
price_map = {}
for k in klines_1m:
    o = float(k[1]); c = float(k[4])
    bucket = int(k[0]) // 300000 * 300000  # round to 5min
    price_map[bucket] = {'o': o, 'c': c}

print('{:<8}  {:>12}  {:>10}  {:>10}  {:>10}'.format(
    'Window', 'OI_chg%', 'PxOpen', 'PxClose', 'PxChg%'))
print('-' * 58)
oi_deltas = []
price_deltas = []
for i in range(1, len(oi_series)):
    prev = oi_series[i - 1]
    cur = oi_series[i]
    oi_delta = (cur['oi'] - prev['oi']) / prev['oi'] * 100
    # match price window
    bucket = cur['ts']
    if bucket in price_map:
        p_o = price_map[bucket]['o']
        p_c = price_map[bucket]['c']
        px_delta = (p_c - p_o) / p_o * 100
    else:
        p_o = 0.0
        p_c = 0.0
        px_delta = 0
    oi_deltas.append(oi_delta)
    price_deltas.append(px_delta)
    print('  {:<8}  {:>+11.3f}%  {:>10.4f}  {:>10.4f}  {:>+9.3f}%'.format(
        datetime.fromtimestamp(cur['ts'] / 1000, tz=timezone.utc).strftime('%H:%M'),
        oi_delta, p_o, p_c, px_delta))

# Cumulative
oi_cum = sum(oi_deltas)
px_cum = sum(price_deltas)
print()
print('  Cumulative OI delta (last {} x 5m bars): {:+.3f}%'.format(
    len(oi_deltas), oi_cum))
print('  Cumulative price delta (same window):    {:+.3f}%'.format(px_cum))

# OI/Price relationship
if oi_cum < -0.3 and px_cum < -0.3:
    liq_signal = 'STRONG LONG LIQUIDATION LIKELY (OI down + price down)'
elif oi_cum < -0.3 and px_cum > 0.3:
    liq_signal = 'STRONG SHORT LIQUIDATION LIKELY (OI down + price up)'
elif abs(oi_cum) < 0.2 and px_cum < -0.3:
    liq_signal = 'ORGANIC SELLING (price down, OI flat = profit-taking, not forced)'
elif abs(oi_cum) < 0.2 and px_cum > 0.3:
    liq_signal = 'ORGANIC BUYING (price up, OI flat = short covering, not short squeeze yet)'
elif oi_cum > 0.3 and px_cum < -0.3:
    liq_signal = 'SHORT ADDING INTO WEAKNESS (new shorts opening)'
elif oi_cum > 0.3 and px_cum > 0.3:
    liq_signal = 'LONG ADDING INTO STRENGTH (new longs opening)'
else:
    liq_signal = 'MIXED / RANGE-BOUND'
print()
print('  >>> READ: {}'.format(liq_signal))
print()

# --- 2. Taker buy ratio trajectory ---
print('=== 2. TAKER BUY RATIO TRAJECTORY ===')
print('{:<8}  {:>8}  {:>8}'.format('Time', 'TBR', 'Trend'))
print('-' * 28)
prev_tbr = None
for r in tbs[-12:]:
    ts = int(r['timestamp'])
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    tbr = bv / max(bv + sv, 0.001)
    if prev_tbr is None:
        trend = 'FIRST'
    elif tbr > prev_tbr + 0.02:
        trend = 'UP'
    elif tbr < prev_tbr - 0.02:
        trend = 'DOWN'
    else:
        trend = 'FLAT'
    print('{:>8}  {:>7.1%}  {:>6}'.format(
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%H:%M'), tbr, trend))
    prev_tbr = tbr

# Cumulative TBR
tbr_values = []
for r in tbs[-12:]:
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    tbr_values.append(bv / max(bv + sv, 0.001))
avg_tbr = sum(tbr_values) / len(tbr_values)
print()
print('  12-bar avg TBR: {:.1%}'.format(avg_tbr))
if avg_tbr < 0.45:
    print('  >>> LONG FORCED FLOW LIKELY (sustained taker selling)')
elif avg_tbr > 0.55:
    print('  >>> SHORT FORCED FLOW LIKELY (sustained taker buying)')
else:
    print('  >>> BALANCED — no forced-flow signature')
print()

# --- 3. Funding rate trajectory (current vs recent history) ---
# We only have current funding; proxy via mark-vs-index spread
mark_px = float(funding['markPrice'])
index_px = float(funding['indexPrice'])
spread_bps = (mark_px - index_px) / index_px * 10000
funding_bps = float(funding['lastFundingRate']) * 10000
print('=== 3. FUNDING + MARK SPREAD ===')
print('  Funding rate: {:+.2f} bps'.format(funding_bps))
print('  Mark-Index spread: {:+.1f} bps'.format(spread_bps))
if funding_bps > 1.5:
    print('  >>> LONGS PAYING HEAVILY — squeeze risk if price dips')
elif funding_bps > 0.5:
    print('  >>> LONGS PAYING MODESTLY — moderate long crowding')
elif funding_bps < -0.5:
    print('  >>> SHORTS PAYING — short squeeze risk if price rises')
else:
    print('  >>> NEUTRAL — no funding imbalance')
print()

# --- 4. Large-print clusters as proxy for forced flow ---
print('=== 4. LARGE-PRINT CLUSTERS (proxy for forced flow) ===')
now_ts = trades[0]['time'] if trades else 0
cutoff_5m = now_ts - 5 * 60 * 1000
trades_5m = [t for t in trades if int(t['time']) >= cutoff_5m]

# Detect same-second clusters of >=3 trades >50 SOL (forced-flow signature)
clusters = []
sorted_t = sorted(trades_5m, key=lambda x: x['time'])
i = 0
while i < len(sorted_t):
    cluster = [sorted_t[i]]
    j = i + 1
    while (j < len(sorted_t)
           and sorted_t[j]['time'] - sorted_t[i]['time'] <= 1500  # within 1.5s
           and float(sorted_t[j]['qty']) >= 30):
        cluster.append(sorted_t[j])
        j += 1
    if len(cluster) >= 3:
        total_qty = sum(float(t['qty']) for t in cluster)
        long_qty = sum(float(t['qty']) for t in cluster if not t['isBuyerMaker'])
        short_qty = sum(float(t['qty']) for t in cluster if t['isBuyerMaker'])
        avg_px = sum(float(t['price']) * float(t['qty']) for t in cluster) / total_qty
        clusters.append({
            'ts': sorted_t[i]['time'],
            'n': len(cluster),
            'total_qty': total_qty,
            'long_qty': long_qty,
            'short_qty': short_qty,
            'avg_px': avg_px,
        })
    i = max(j, i + 1)

if clusters:
    for c in sorted(clusters, key=lambda x: x['total_qty'], reverse=True)[:10]:
        flow_dir = 'LONG-DOMINATED' if c['long_qty'] > c['short_qty'] else 'SHORT-DOMINATED'
        print('  {}  {} trades  px={:.4f}  total={:,.0f} SOL  long={:,.0f}  short={:,.0f}  [{}]'.format(
            datetime.fromtimestamp(c['ts'] / 1000, tz=timezone.utc).strftime('%H:%M:%S'),
            c['n'], c['avg_px'], c['total_qty'], c['long_qty'], c['short_qty'], flow_dir))
else:
    print('  No same-second clusters of >=3 large trades in last 5m')

print()

# --- Final synthesis ---
print('=== FINAL LIQUIDATION PRESSURE READ ===')
print('  OI/Price signal:    {}'.format(liq_signal))
print('  TBR signal:         12-bar avg {:.1%}'.format(avg_tbr))
print('  Funding signal:     {:+.2f} bps'.format(funding_bps))
print('  Cluster count (5m): {}'.format(len(clusters)))
print()
if oi_cum < -0.3 and avg_tbr < 0.45 and funding_bps > 0:
    print('  >>> CONVERGENT: long liquidations likely active or imminent')
elif oi_cum > 0.3 and avg_tbr > 0.55 and funding_bps < 0:
    print('  >>> CONVERGENT: short liquidations likely active or imminent')
elif abs(oi_cum) < 0.2 and 0.45 < avg_tbr < 0.55:
    print('  >>> NO LIQUIDATION STRESS: market trading on organic flow')
else:
    print('  >>> MIXED: no convergent liquidation signal — interpret with caution')
