import urllib.request, json
from datetime import datetime
from collections import defaultdict

BASE = 'https://fapi.binance.com'
sym = 'SOLUSDT'

def get(path, params=None):
    url = BASE + path
    if params:
        url += '?' + '&'.join('{}={}'.format(k,v) for k,v in params.items())
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())

D = lambda s: ''

# Full orderbook
book = get('/fapi/v1/depth', {'symbol': sym, 'limit': 100})
bids = [[float(p), float(q)] for p,q in book['bids']]
asks = [[float(p), float(q)] for p,q in book['asks']]

# Aggregate trades by 0.10 price buckets
trades = get('/fapi/v1/trades', {'symbol': sym, 'limit': 1000})
now_ts = trades[0]['time'] if trades else 0
cutoff_5m = now_ts - 5*60*1000

print('=== CURRENT PRICE: {} ==='.format(trades[0]['price']))
print('Now ts: {}'.format(datetime.fromtimestamp(now_ts/1000).strftime('%H:%M:%S')))
print()

# Analyze only last 5 minutes
trades_5m = [t for t in trades if int(t['time']) >= cutoff_5m]
print('Trades in last 5 min: {} (out of {} total)'.format(len(trades_5m), len(trades)))

# 5-min aggregated flow
buy_5m_qty = sum(float(t['qty']) for t in trades_5m if not t['isBuyerMaker'])
sell_5m_qty = sum(float(t['qty']) for t in trades_5m if t['isBuyerMaker'])
buy_5m_vol = sum(float(t['qty'])*float(t['price']) for t in trades_5m if not t['isBuyerMaker'])
sell_5m_vol = sum(float(t['qty'])*float(t['price']) for t in trades_5m if t['isBuyerMaker'])
print()
print('=== 5-MINUTE AGGREGATED FLOW ===')
print('  Buy qty:  {:>10,.1f} SOL  ${:>14,.0f}'.format(buy_5m_qty, buy_5m_vol))
print('  Sell qty: {:>10,.1f} SOL  ${:>14,.0f}'.format(sell_5m_qty, sell_5m_vol))
print('  Net:      {:>+10,.1f} SOL  ${:>+14,.0f}'.format(buy_5m_qty - sell_5m_qty, buy_5m_vol - sell_5m_vol))
print('  Buy/Sell:  {:>10.2f}'.format(buy_5m_qty / max(sell_5m_qty, 0.001)))
print('  CVD:      {:>+10,.1f}'.format(buy_5m_qty - sell_5m_qty))

# Price bucket breakdown for 5min
print()
print('=== PRICE BUCKET FLOW (5min, 0.05 bucketing) ===')
buckets = defaultdict(lambda: {'buy':0,'sell':0,'buy_vol':0,'sell_vol':0,'buy_n':0,'sell_n':0})
for t in trades_5m:
    price = float(t['price'])
    qty = float(t['qty'])
    notional = price * qty
    bucket = round(price * 20) / 20
    if t['isBuyerMaker']:
        buckets[bucket]['sell'] += qty
        buckets[bucket]['sell_vol'] += notional
        buckets[bucket]['sell_n'] += 1
    else:
        buckets[bucket]['buy'] += qty
        buckets[bucket]['buy_vol'] += notional
        buckets[bucket]['buy_n'] += 1

print('{:<8}  {:>6}  {:>10}  {:>10}  {:>10}  {:>12}  {:>8}'.format(
    'Bucket', 'TrdN', 'BuyQty', 'SellQty', 'NetQty', 'NetVol', 'L/S'))
print('-' * 70)
for b in sorted(buckets.keys()):
    d = buckets[b]
    if d['buy'] + d['sell'] < 1: continue
    net = d['buy'] - d['sell']
    ls = d['sell'] / max(d['buy'], 0.001)
    flag = ' <---' if abs(net) > 50 else ''
    print('{:<8.3f}  {:>6}  {:>10,.1f}  {:>10,.1f}  {:>+10,.1f}  {:>+12,.0f}  {:>8.2f}{}'.format(
        b, d['buy_n'] + d['sell_n'], d['buy'], d['sell'], net, d['buy_vol'] - d['sell_vol'], ls, flag))

# Identify the new keystone - where bid density is concentrated
print()
print('=== NEW BID KEYSTONE STRUCTURE (significant levels >= 1500 SOL) ===')
significant_bids = [(p,q) for p,q in bids if q >= 1500]
significant_bids.sort()
print('Significant bid levels (>= 1500 SOL):')
for p, q in significant_bids[:30]:
    print('  {:6.4f}  {:>10,.1f} SOL  ${:>12,.0f}'.format(p, q, p*q))

# Find concentrated bid zones (rolling 0.20 window)
print()
print('=== ROLLING BID DEPTH (0.20 windows) ===')
windows = []
for i in range(0, len(bids), 1):
    base = bids[i][0]
    window_qty = sum(q for p,q in bids if base - 0.20 <= p <= base)
    windows.append((base, window_qty))
windows.sort(key=lambda x: x[1], reverse=True)
print('Top 10 bid density windows (0.20 wide):')
for p, q in windows[:10]:
    print('  center={:.4f}  qty={:,.0f} SOL'.format(p, q))

# Find concentrated ask zones
print()
print('=== ROLLING ASK DEPTH (0.20 windows) ===')
windows_a = []
for i in range(0, len(asks), 1):
    base = asks[i][0]
    window_qty = sum(q for p,q in asks if base <= p <= base + 0.20)
    windows_a.append((base, window_qty))
windows_a.sort(key=lambda x: x[1], reverse=True)
print('Top 10 ask density windows (0.20 wide):')
for p, q in windows_a[:10]:
    print('  center={:.4f}  qty={:,.0f} SOL'.format(p, q))

# Recent large prints
print()
print('=== LARGE PRINTS IN LAST 5 MIN (qty >= 50 SOL) ===')
large = []
for t in trades_5m:
    qty = float(t['qty'])
    if qty < 50: continue
    price = float(t['price'])
    ts = int(t['time'])
    age_s = (now_ts - ts) / 1000
    side = 'BUY' if not t['isBuyerMaker'] else 'SELL'
    large.append({'ts': ts, 'side': side, 'price': price, 'qty': qty, 'age_s': age_s})
large.sort(key=lambda x: x['ts'], reverse=True)
print('Large prints: {}'.format(len(large)))
for lp in large[:30]:
    print('  {}  {:4s}  px={:.4f}  qty={:.2f}  ${:,.0f}  {:.0f}s ago'.format(
        datetime.fromtimestamp(lp['ts']/1000).strftime('%H:%M:%S'),
        lp['side'], lp['price'], lp['qty'], lp['price']*lp['qty'], lp['age_s']))

# Taker flow per minute
print()
print('=== PER-MINUTE TAKER FLOW (last 5 min) ===')
print('{:<10}  {:>5}  {:>10}  {:>10}  {:>10}  {:>8}'.format(
    'Minute', 'TrdN', 'BuyQty', 'SellQty', 'NetQty', 'TBR%'))
print('-' * 60)
minute_buckets = defaultdict(lambda: {'buy':0,'sell':0,'n':0})
for t in trades_5m:
    ts = int(t['time'])
    minute = ts // 60000 * 60000
    qty = float(t['qty'])
    if t['isBuyerMaker']:
        minute_buckets[minute]['sell'] += qty
    else:
        minute_buckets[minute]['buy'] += qty
    minute_buckets[minute]['n'] += 1
for m in sorted(minute_buckets.keys(), reverse=True):
    d = minute_buckets[m]
    net = d['buy'] - d['sell']
    tbr = d['buy'] / max(d['buy'] + d['sell'], 0.001)
    print('{:<10}  {:>5}  {:>10,.1f}  {:>10,.1f}  {:>+10,.1f}  {:>7.1%}'.format(
        datetime.fromtimestamp(m/1000).strftime('%H:%M'), d['n'], d['buy'], d['sell'], net, tbr))

# OI change
print()
print('=== OI TREND ===')
oi_hist = get('/futures/data/openInterestHist', {'symbol': sym, 'period': '5m', 'limit': 10})
oi_now = get('/fapi/v1/openInterest', {'symbol': sym})
oi_series = []
for r in oi_hist:
    oi_series.append((int(r['timestamp']), float(r['sumOpenInterest'])))
oi_series.sort()
for ts, oi in oi_series[-6:]:
    print('  {}  OI={:,.0f}'.format(datetime.fromtimestamp(ts/1000).strftime('%H:%M'), oi))
cur_oi = float(oi_now.get('openInterest', 0))
print('  Current OI: {:,.0f}'.format(cur_oi))

# Taker ratios (5m official)
print()
print('=== TAKER BUY/SELL (5m official) ===')
tbs = get('/futures/data/takerlongshortRatio', {'symbol': sym, 'period': '5m', 'limit': 6})
for r in tbs:
    ts = int(r['timestamp'])
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    ratio = bv / max(sv, 0.001)
    print('  {}  ratio={:.3f}  buy={:,.0f}  sell={:,.0f}  TBR={:.1%}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'), ratio, bv, sv, bv/(bv+sv)))

# Top trader
print()
print('=== TOP/GLOBAL TRADER POSITIONING ===')
top = get('/futures/data/topLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 4})
glb = get('/futures/data/globalLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 4})
for r in top:
    ts = int(r['timestamp'])
    print('  TOP  {}  long={:.2f}%  short={:.2f}%  ratio={:.4f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'),
        float(r['longAccount'])*100, float(r['shortAccount'])*100, float(r['longShortRatio'])))
for r in glb:
    ts = int(r['timestamp'])
    print('  GLB  {}  long={:.2f}%  short={:.2f}%  ratio={:.4f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'),
        float(r['longAccount'])*100, float(r['shortAccount'])*100, float(r['longShortRatio'])))

# Funding
print()
print('=== FUNDING ===')
funding = get('/fapi/v1/premiumIndex', {'symbol': sym})
print('Funding rate: {:.6f} ({:+.2f} bps)'.format(float(funding['lastFundingRate']), float(funding['lastFundingRate'])*10000))
print('Mark price:   {:.4f}'.format(float(funding['markPrice'])))
print('Index price:  {:.4f}'.format(float(funding['indexPrice'])))
