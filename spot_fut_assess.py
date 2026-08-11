import urllib.request, json
from datetime import datetime

BASE = 'https://fapi.binance.com'
SPOT = 'https://api.binance.com'
sym = 'SOLUSDT'

def get(url, params=None):
    full = url + path
    if params:
        full += '?' + '&'.join('{}={}'.format(k,v) for k,v in params.items())
    with urllib.request.urlopen(full, timeout=10) as r:
        return json.loads(r.read())

def get_b(url, params=None):
    full = url
    if params:
        full += '?' + '&'.join('{}={}'.format(k,v) for k,v in params.items())
    with urllib.request.urlopen(full, timeout=10) as r:
        return json.loads(r.read())

# Spot
spot_24h = get_b(SPOT + '/api/v3/ticker/24hr', {'symbol': sym})
spot_book = get_b(SPOT + '/api/v3/depth', {'symbol': sym, 'limit': 50})
spot_trades = get_b(SPOT + '/api/v3/trades', {'symbol': sym, 'limit': 1000})

# Futures
fut_24h = get_b(BASE + '/fapi/v1/ticker/24hr', {'symbol': sym})
fut_book = get_b(BASE + '/fapi/v1/depth', {'symbol': sym, 'limit': 50})
fut_trades = get_b(BASE + '/fapi/v1/trades', {'symbol': sym, 'limit': 1000})
funding = get_b(BASE + '/fapi/v1/premiumIndex', {'symbol': sym})
oi = get_b(BASE + '/fapi/v1/openInterest', {'symbol': sym})
oi_hist = get_b(BASE + '/futures/data/openInterestHist', {'symbol': sym, 'period': '5m', 'limit': 20})
tbs = get_b(BASE + '/futures/data/takerlongshortRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
top = get_b(BASE + '/futures/data/topLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
glb = get_b(BASE + '/futures/data/globalLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 10})

# Process spot trades
spot_bids = [[float(p), float(q)] for p,q in spot_book['bids']]
spot_asks = [[float(p), float(q)] for p,q in spot_book['asks']]
spot_bid_top20 = sum(b[1] for b in spot_bids[:20])
spot_ask_top20 = sum(a[1] for a in spot_asks[:20])
spot_obi = (spot_bid_top20 - spot_ask_top20) / max(spot_bid_top20 + spot_ask_top20, 1e-9)

# Process futures
fut_bids = [[float(p), float(q)] for p,q in fut_book['bids']]
fut_asks = [[float(p), float(q)] for p,q in fut_book['asks']]
fut_bid_top20 = sum(b[1] for b in fut_bids[:20])
fut_ask_top20 = sum(a[1] for a in fut_asks[:20])
fut_obi = (fut_bid_top20 - fut_ask_top20) / max(fut_bid_top20 + fut_ask_top20, 1e-9)

# Compute spot vs fut CVD and bucket flow
def summarize_flow(trades, now_ts):
    cutoff = now_ts - 5*60*1000
    recent = [t for t in trades if int(t['time']) >= cutoff]
    buy_qty = sum(float(t['qty']) for t in recent if not t['isBuyerMaker'])
    sell_qty = sum(float(t['qty']) for t in recent if t['isBuyerMaker'])
    buy_vol = sum(float(t['qty'])*float(t['price']) for t in recent if not t['isBuyerMaker'])
    sell_vol = sum(float(t['qty'])*float(t['price']) for t in recent if t['isBuyerMaker'])
    return {
        'n': len(recent),
        'buy_qty': buy_qty,
        'sell_qty': sell_qty,
        'buy_vol': buy_vol,
        'sell_vol': sell_vol,
        'cvd': buy_qty - sell_qty,
        'buy_sell': buy_qty / max(sell_qty, 0.001),
        'now_ts': now_ts,
    }

now_ts_fut = fut_trades[0]['time'] if fut_trades else 0
now_ts_spot = spot_trades[0]['time'] if spot_trades else 0

spot_5m = summarize_flow(spot_trades, now_ts_spot)
fut_5m = summarize_flow(fut_trades, now_ts_fut)

# Bucket flow
from collections import defaultdict

def bucket_flow(trades, now_ts, bucket_size=0.05):
    cutoff = now_ts - 5*60*1000
    recent = [t for t in trades if int(t['time']) >= cutoff]
    buck = defaultdict(lambda: {'buy':0,'sell':0})
    for t in recent:
        price = float(t['price'])
        qty = float(t['qty'])
        bucket = round(price / bucket_size) * bucket_size
        if t['isBuyerMaker']:
            buck[bucket]['sell'] += qty
        else:
            buck[bucket]['buy'] += qty
    return buck

spot_buck = bucket_flow(spot_trades, now_ts_spot)
fut_buck = bucket_flow(fut_trades, now_ts_fut)

# Print
print('=== SPOT vs FUTURES — DUAL VENUE ASSESSMENT ===')
print('Time: {} UTC'.format(datetime.fromtimestamp(now_ts_fut/1000).strftime('%H:%M:%S')))
print()

print('=== 24H TICKER ===')
print('SPOT   : open={:.4f} high={:.4f} low={:.4f}  change={:+.3f}%  vol={:,.0f}  quoteVol=${:,.0f}'.format(
    float(spot_24h['openPrice']), float(spot_24h['highPrice']), float(spot_24h['lowPrice']),
    float(spot_24h['priceChangePercent']), float(spot_24h['volume']), float(spot_24h['quoteVolume'])))
print('FUTURES: open={:.4f} high={:.4f} low={:.4f}  change={:+.3f}%  vol={:,.0f}  quoteVol=${:,.0f}'.format(
    float(fut_24h['openPrice']), float(fut_24h['highPrice']), float(fut_24h['lowPrice']),
    float(fut_24h['priceChangePercent']), float(fut_24h['volume']), float(fut_24h['quoteVolume'])))
print()

print('=== LAST PRICE ===')
print('  Spot:    ${:.4f}'.format(float(spot_24h['lastPrice'])))
print('  Futures: ${:.4f}'.format(float(fut_24h['lastPrice'])))
print('  Basis:   ${:.4f} ({:+.0f} bps)'.format(
    float(fut_24h['lastPrice']) - float(spot_24h['lastPrice']),
    (float(fut_24h['lastPrice']) - float(spot_24h['lastPrice'])) / float(spot_24h['lastPrice']) * 10000))
print('  Mark:    ${:.4f}'.format(float(funding['markPrice'])))
print('  Index:   ${:.4f}'.format(float(funding['indexPrice'])))
print()

print('=== ORDERBOOK (top-20) ===')
print('  SPOT    bid={:,.1f}  ask={:,.1f}  OBI={:+.4f}'.format(spot_bid_top20, spot_ask_top20, spot_obi))
print('  FUTURES bid={:,.1f}  ask={:,.1f}  OBI={:+.4f}'.format(fut_bid_top20, fut_ask_top20, fut_obi))
print()

print('=== 5-MINUTE FLOW ===')
print('  SPOT:    n={}  buy={:.1f}  sell={:.1f}  CVD={:+.1f}  L/S={:.2f}'.format(
    spot_5m['n'], spot_5m['buy_qty'], spot_5m['sell_qty'], spot_5m['cvd'], spot_5m['buy_sell']))
print('  FUTURES: n={}  buy={:.1f}  sell={:.1f}  CVD={:+.1f}  L/S={:.2f}'.format(
    fut_5m['n'], fut_5m['buy_qty'], fut_5m['sell_qty'], fut_5m['cvd'], fut_5m['buy_sell']))
print()

print('=== 5-MIN CVD DIVERGENCE ===')
div = spot_5m['cvd'] - fut_5m['cvd']
print('  Spot CVD:    {:+.1f}'.format(spot_5m['cvd']))
print('  Fut CVD:     {:+.1f}'.format(fut_5m['cvd']))
print('  Divergence:  {:+.1f}'.format(div))
if spot_5m['cvd'] < 0 and fut_5m['cvd'] > 0:
    print('  >>> SPOT SELLING / FUTURES BUYING — divergence (late-stage/short-cover)')
elif spot_5m['cvd'] > 0 and fut_5m['cvd'] < 0:
    print('  >>> SPOT BUYING / FUTURES SELLING — divergence (futures rejection)')
elif spot_5m['cvd'] < 0 and fut_5m['cvd'] < 0:
    print('  >>> BOTH SELLING — wide sell pressure')
elif spot_5m['cvd'] > 0 and fut_5m['cvd'] > 0:
    print('  >>> BOTH BUYING — wide buy pressure')
print()

print('=== PRICE BUCKET FLOW (5min, 0.05 buckets) ===')
print('Region         SPOT           FUTURES')
print('              (buy/sell/net)  (buy/sell/net)')
all_buckets = sorted(set(list(spot_buck.keys()) + list(fut_buck.keys())))
for b in all_buckets:
    if not (72.80 <= b <= 73.50): continue
    s_buy = spot_buck[b]['buy']
    s_sell = spot_buck[b]['sell']
    s_net = s_buy - s_sell
    f_buy = fut_buck[b]['buy']
    f_sell = fut_buck[b]['sell']
    f_net = f_buy - f_sell
    print('  {:6.3f}  {:>6.1f}/{:>6.1f}/{:+6.1f}    {:>6.1f}/{:>6.1f}/{:+6.1f}'.format(
        b, s_buy, s_sell, s_net, f_buy, f_sell, f_net))
print()

print('=== FUNDING ===')
print('  Rate:  {:.6f}  ({:+.2f} bps)'.format(float(funding['lastFundingRate']), float(funding['lastFundingRate'])*10000))
print('  Next:  {} ({})'.format(
    datetime.fromtimestamp(int(funding['nextFundingTime'])/1000).strftime('%H:%M:%S'),
    funding['nextFundingTime']))
print()

print('=== OPEN INTEREST ===')
oi_series = []
for r in oi_hist:
    oi_series.append((int(r['timestamp']), float(r['sumOpenInterest']), float(r['sumOpenInterestValue'])))
oi_series.sort()
print('  Time  OI(contracts)  OI_value($)')
for ts, oi_val, oi_dollar in oi_series[-8:]:
    print('  {}  {:>14,.0f}  ${:>14,.0f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'), oi_val, oi_dollar))
cur_oi = float(oi.get('openInterest', 0))
print('  Current OI: {:,.0f} contracts'.format(cur_oi))
oi_first = oi_series[0][1] if oi_series else 0
oi_last = oi_series[-1][1] if oi_series else 0
if oi_first > 0:
    print('  Window change: {:+.3f}%'.format((oi_last - oi_first) / oi_first * 100))
print()

print('=== TAKER BUY/SELL (5m) ===')
for r in tbs:
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    ts = int(r['timestamp'])
    print('  {}  ratio={:.3f}  buy={:,.0f}  sell={:,.0f}  TBR={:.1%}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'),
        bv/max(sv,0.001), bv, sv, bv/(bv+sv)))
print()

print('=== TOP TRADER POSITIONING ===')
for r in top:
    ts = int(r['timestamp'])
    print('  TOP  {}  long={:.2f}%  short={:.2f}%  ratio={:.4f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'),
        float(r['longAccount'])*100, float(r['shortAccount'])*100, float(r['longShortRatio'])))
print()

print('=== GLOBAL TRADER POSITIONING ===')
for r in glb:
    ts = int(r['timestamp'])
    print('  GLB  {}  long={:.2f}%  short={:.2f}%  ratio={:.4f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'),
        float(r['longAccount'])*100, float(r['shortAccount'])*100, float(r['longShortRatio'])))
