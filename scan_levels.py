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

# --- Orderbook ---
book = get('/fapi/v1/depth', {'symbol': sym, 'limit': 100})
bids = [[float(p), float(q)] for p,q in book['bids']]
asks = [[float(p), float(q)] for p,q in book['asks']]

print('=== ORDERBOOK: KEYSTONE ZONE 71.90 - 72.15 ===')
kz_bids = [(p,q) for p,q in bids if 71.90 <= p <= 72.15]
kz_asks = [(p,q) for p,q in asks if 71.90 <= p <= 72.15]
kz_bid_qty = sum(q for _,q in kz_bids)
kz_ask_qty = sum(q for _,q in kz_asks)
print('  Bids in zone: {} levels, total qty={:,.0f}, notional=${:,.0f}'.format(
    len(kz_bids), kz_bid_qty, sum(p*q for p,q in kz_bids)))
print('  Asks in zone: {} levels, total qty={:,.0f}, notional=${:,.0f}'.format(
    len(kz_asks), kz_ask_qty, sum(p*q for p,q in kz_asks)))
print('  NET BID DEPTH IN ZONE: {:+,.0f} SOL'.format(kz_bid_qty - kz_ask_qty))

print()
print('=== ORDERBOOK: SELLER ZONE 72.70 - 72.90 ===')
sz_bids = [(p,q) for p,q in bids if 72.70 <= p <= 72.90]
sz_asks = [(p,q) for p,q in asks if 72.70 <= p <= 72.90]
sz_bid_qty = sum(q for _,q in sz_bids)
sz_ask_qty = sum(q for _,q in sz_asks)
print('  Bids in zone: {} levels, total qty={:,.0f}, notional=${:,.0f}'.format(
    len(sz_bids), sz_bid_qty, sum(p*q for p,q in sz_bids)))
print('  Asks in zone: {} levels, total qty={:,.0f}, notional=${:,.0f}'.format(
    len(sz_asks), sz_ask_qty, sum(p*q for p,q in sz_asks)))
print('  NET ASK DEPTH IN ZONE: {:+,.0f} SOL'.format(sz_ask_qty - sz_bid_qty))

print()
print('=== FULL BOOK TOP-20 ===')
bb20 = sum(b[1] for b in bids[:20])
ba20 = sum(a[1] for a in asks[:20])
obi20 = (bb20 - ba20) / max(bb20 + ba20, 1e-9)
print('  Bid top20: {:,.0f} SOL'.format(bb20))
print('  Ask top20: {:,.0f} SOL'.format(ba20))
print('  OBI top20: {:+.4f}'.format(obi20))

print()
print('=== TOP TRADER LONG/SHORT (5m) ===')
top = get('/futures/data/topLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
for r in top:
    ts = int(r['timestamp'])
    long_pct = float(r['longAccount'])
    short_pct = float(r['shortAccount'])
    ratio = float(r['longShortRatio'])
    print('  {}  long={:.2%}  short={:.2%}  ratio={:.4f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'), long_pct, short_pct, ratio))

print()
print('=== GLOBAL LONG/SHORT (5m) ===')
glb = get('/futures/data/globalLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
for r in glb:
    ts = int(r['timestamp'])
    long_pct = float(r['longAccount'])
    short_pct = float(r['shortAccount'])
    ratio = float(r['longShortRatio'])
    print('  {}  long={:.2%}  short={:.2%}  ratio={:.4f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'), long_pct, short_pct, ratio))

print()
print('=== TAKER BUY/SELL RATIO (5m) ===')
tbs = get('/futures/data/takerlongshortRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
for r in tbs:
    ts = int(r['timestamp'])
    ratio = float(r['buySellRatio'])
    buy_vol = float(r['buyVol'])
    sell_vol = float(r['sellVol'])
    print('  {}  ratio={:.4f}  buy={:,.0f}  sell={:,.0f}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'), ratio, buy_vol, sell_vol))

print()
print('=== RECENT LARGE PRINTS (last 100 trades, qty >= 20 SOL) ===')
trades = get('/fapi/v1/trades', {'symbol': sym, 'limit': 100})
now_ts = trades[0]['time'] if trades else 0
print('Now: {} ({})'.format(now_ts, datetime.fromtimestamp(now_ts/1000).strftime('%H:%M:%S')))
print()
large = []
for t in trades:
    qty = float(t['qty'])
    if qty < 20: continue
    price = float(t['price'])
    ts = t['time']
    age_s = (now_ts - ts) / 1000
    side = 'BUY' if not t['isBuyerMaker'] else 'SELL'
    large.append({'ts': ts, 'side': side, 'price': price, 'qty': qty, 'age_s': age_s})

large.sort(key=lambda x: x['ts'], reverse=True)
for lp in large[:25]:
    print('  {}  {}  px={:.4f}  qty={:.2f}  ${:,.0f}  {}s ago'.format(
        datetime.fromtimestamp(lp['ts']/1000).strftime('%H:%M:%S'),
        lp['side'], lp['price'], lp['qty'], lp['price']*lp['qty'], int(lp['age_s'])))

print()
print('=== PRICE BUCKET NET FLOW (last 100 trades, 0.025 bucket) ===')
buy_buckets = defaultdict(lambda: {'qty':0,'vol':0,'n':0})
sell_buckets = defaultdict(lambda: {'qty':0,'vol':0,'n':0})
for t in trades:
    price = float(t['price'])
    qty = float(t['qty'])
    notional = price * qty
    bucket = round(price * 40) / 40
    if t['isBuyerMaker']:
        sell_buckets[bucket]['qty'] += qty
        sell_buckets[bucket]['vol'] += notional
        sell_buckets[bucket]['n'] += 1
    else:
        buy_buckets[bucket]['qty'] += qty
        buy_buckets[bucket]['vol'] += notional
        buy_buckets[bucket]['n'] += 1

all_buckets = sorted(set(list(buy_buckets.keys()) + list(sell_buckets.keys())))
print('{:<8}  {:>10}  {:>10}  {:>12}  {:>12}  {:>10}'.format(
    'Bucket', 'BuyQty', 'SellQty', 'BuyVol', 'SellVol', 'NetQty'))
for b in all_buckets:
    if not (71.80 <= b <= 73.20): continue
    bq = buy_buckets[b]['qty']
    sq = sell_buckets[b]['qty']
    bv = buy_buckets[b]['vol']
    sv = sell_buckets[b]['vol']
    net = bq - sq
    flag = ' <---' if abs(net) > 30 else ''
    print('{:<8.3f}  {:>10.2f}  {:>10.2f}  {:>12,.0f}  {:>12,.0f}  {:>+10.2f}{}'.format(
        b, bq, sq, bv, sv, net, flag))

print()
print('=== 1m CANDLES NEAR KEY LEVELS ===')
k1 = get('/fapi/v1/klines', {'symbol': sym, 'interval': '1m', 'limit': 10})
for k in k1[-6:]:
    ts = int(k[0])
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    vol = float(k[5])
    tb = float(k[9])
    tb_ratio = tb / vol if vol > 0 else 0
    print('  {}  O={:.2f} H={:.2f} L={:.2f} C={:.2f}  vol={:,.0f}  taker_buy_ratio={:.2%}'.format(
        datetime.fromtimestamp(ts/1000).strftime('%H:%M'), o, h, l, c, vol, tb_ratio))
