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


# --- Pull live reference price ---
ticker = get('/fapi/v1/ticker/price', {'symbol': sym})
px = float(ticker['price'])
print('=== LIVE PRICE: {:.4f} (ts: {} UTC) ==='.format(
    px, datetime.now(timezone.utc).strftime('%H:%M:%S')))
print()

# --- Orderbook full depth ---
book = get('/fapi/v1/depth', {'symbol': sym, 'limit': 100})
bids = [[float(p), float(q)] for p, q in book['bids']]
asks = [[float(p), float(q)] for p, q in book['asks']]

# --- Derive zones dynamically from current price ---
# Keystone: -0.30 to -0.05 below price (where buyers defend)
# Seller wall: +0.10 to +0.50 above price (where supply sits)
# Inside market: -0.20 to +0.30 around price
K_LO, K_HI = px - 0.30, px - 0.05
S_LO, S_HI = px + 0.10, px + 0.50
INSIDE_LO, INSIDE_HI = px - 0.20, px + 0.30

print('=== DYNAMIC ZONES (anchored to live price {:.4f}) ==='.format(px))
print('  Keystone zone:  {:.4f} - {:.4f}  (below price)'.format(K_LO, K_HI))
print('  Seller zone:    {:.4f} - {:.4f}  (above price)'.format(S_LO, S_HI))
print('  Inside market:  {:.4f} - {:.4f}'.format(INSIDE_LO, INSIDE_HI))
print()

# --- Keystone bid structure ---
kz_bids = [(p, q) for p, q in bids if K_LO <= p <= K_HI]
kz_bids.sort()
print('=== KEYSTONE BID STRUCTURE ({:.4f} - {:.4f}) ==='.format(K_LO, K_HI))
print('Bid levels in zone: {}'.format(len(kz_bids)))
total_kz_bid_qty = sum(q for _, q in kz_bids)
total_kz_bid_notional = sum(p * q for p, q in kz_bids)
print('Total bid qty:     {:>12,.0f} SOL'.format(total_kz_bid_qty))
print('Total bid notional: ${:>12,.0f}'.format(total_kz_bid_notional))
print()
print('  Level breakdown (sorted by price ascending):')
cum = 0
for p, q in kz_bids:
    cum += q
    print('  {:6.4f}  qty={:>10.2f}  cum={:>12.2f}  pct_of_zone={:.1%}'.format(
        p, q, cum, cum / max(total_kz_bid_qty, 0.001)))

total_bid_top20 = sum(b[1] for b in bids[:20])
print()
print('Keystone bid = {:.1%} of top-20 bid depth'.format(
    total_kz_bid_qty / max(total_bid_top20, 0.001)))
print()

# --- Seller zone ask structure ---
sz_asks = [(p, q) for p, q in asks if S_LO <= p <= S_HI]
sz_asks.sort()
total_sz_ask_qty = sum(q for _, q in sz_asks)
print('=== SELLER ZONE STRUCTURE ({:.4f} - {:.4f}) ==='.format(S_LO, S_HI))
print('Ask levels in zone: {}'.format(len(sz_asks)))
print('Total ask qty:     {:>12,.0f} SOL'.format(total_sz_ask_qty))
print()
for p, q in sz_asks:
    print('  {:6.4f}  qty={:>10.2f}'.format(p, q))
print()

# --- Inside market: 0.05 buckets around price ---
zones = []
step = 0.05
z = round((INSIDE_LO) / step) * step
while z <= INSIDE_HI + 1e-9:
    zones.append(('{:.2f}-{:.2f}'.format(z, z + step), z, z + step))
    z += step

print('=== BID/ASK RATIO BY 0.05 ZONE (inside market) ===')
print('{:<14}  {:>12}  {:>12}  {:>10}  {:>10}'.format('Zone', 'BidQty', 'AskQty', 'Net', 'Ratio'))
print('-' * 62)
for name, lo, hi in zones:
    bq = sum(q for p, q in bids if lo <= p < hi)
    aq = sum(q for p, q in asks if lo <= p < hi)
    net = bq - aq
    ratio = bq / max(aq, 0.001)
    flag = ' << BID' if bq > aq * 3 else (' ASK >>' if aq > bq * 3 else '')
    print('{:<14}  {:>12,.0f}  {:>12,.0f}  {:>+10,.0f}  {:>9.2f}{}'.format(
        name, bq, aq, net, ratio, flag))
print()

# --- Recent trade timing at keystone zone ---
trades = get('/fapi/v1/trades', {'symbol': sym, 'limit': 100})
now_ts = trades[0]['time'] if trades else 0

kz_lo_k, kz_hi_k = K_LO, K_HI
zone_buys = defaultdict(lambda: {'qty': 0, 'vol': 0, 'n': 0})
zone_sells = defaultdict(lambda: {'qty': 0, 'vol': 0, 'n': 0})
for t in trades:
    price = float(t['price'])
    if not (INSIDE_LO <= price <= INSIDE_HI):
        continue
    qty = float(t['qty'])
    notional = price * qty
    for name, lo, hi in zones:
        if lo <= price < hi:
            if t['isBuyerMaker']:
                zone_sells[name]['qty'] += qty
                zone_sells[name]['vol'] += notional
                zone_sells[name]['n'] += 1
            else:
                zone_buys[name]['qty'] += qty
                zone_buys[name]['vol'] += notional
                zone_buys[name]['n'] += 1
            break

print('=== RECENT TRADE FLOW BY 0.05 ZONE (last 100 trades) ===')
print('{:<14}  {:>8}  {:>12}  {:>8}  {:>12}  {:>10}'.format(
    'Zone', 'BuyN', 'BuyQty', 'SellN', 'SellQty', 'Net'))
print('-' * 68)
for name, _, _ in zones:
    bd = zone_buys[name]
    sd = zone_sells[name]
    net = bd['qty'] - sd['qty']
    print('{:<14}  {:>8}  {:>12,.1f}  {:>8}  {:>12,.1f}  {:>+10,.1f}'.format(
        name, bd['n'], bd['qty'], sd['n'], sd['qty'], net))
print()

# --- Aggressive vs passive at keystone ---
print('=== AGGRESSIVE VS PASSIVE WITHIN KEYSTONE ZONE ({:.4f}-{:.4f}) ==='.format(K_LO, K_HI))
kz_trades_buy, kz_trades_sell = [], []
for t in trades:
    price = float(t['price'])
    if K_LO <= price <= K_HI:
        qty = float(t['qty'])
        ts = t['time']
        age_s = (now_ts - ts) / 1000
        side = 'BUY' if not t['isBuyerMaker'] else 'SELL'
        if side == 'BUY':
            kz_trades_buy.append({'px': price, 'qty': qty, 'ts': ts, 'age': age_s})
        else:
            kz_trades_sell.append({'px': price, 'qty': qty, 'ts': ts, 'age': age_s})
print('  Buy prints in zone:  {}  total qty={:,.2f}'.format(
    len(kz_trades_buy), sum(t['qty'] for t in kz_trades_buy)))
print('  Sell prints in zone: {}  total qty={:,.2f}'.format(
    len(kz_trades_sell), sum(t['qty'] for t in kz_trades_sell)))
for t in kz_trades_buy:
    print('    BUY  px={:.4f}  qty={:.2f}  ${:,.0f}  {}s ago'.format(
        t['px'], t['qty'], t['px'] * t['qty'], int(t['age'])))
for t in kz_trades_sell:
    print('    SELL px={:.4f}  qty={:.2f}  ${:,.0f}  {}s ago'.format(
        t['px'], t['qty'], t['px'] * t['qty'], int(t['age'])))
print()

# --- Orderbook absorption: nearest bids below current price ---
near_bids = [(p, q) for p, q in bids[:30] if p < px]
near_bids.sort()
print('=== NEAREST BIDS BELOW CURRENT PRICE (absorption ladder) ===')
cum = 0
for p, q in near_bids[:15]:
    cum += q
    print('  {:6.4f}  qty={:>10.2f}  cum={:>12.2f}'.format(p, q, cum))
print()

# --- Rolling bid density windows (top of book structure) ---
print('=== ROLLING BID DEPTH (0.20 windows, top 10) ===')
windows = []
for i in range(len(bids)):
    base = bids[i][0]
    window_qty = sum(q for p, q in bids if base - 0.20 <= p <= base)
    windows.append((base, window_qty))
windows.sort(key=lambda x: x[1], reverse=True)
for p, q in windows[:10]:
    print('  center={:.4f}  qty={:,.0f} SOL'.format(p, q))
print()

print('=== ROLLING ASK DEPTH (0.20 windows, top 10) ===')
windows_a = []
for i in range(len(asks)):
    base = asks[i][0]
    window_qty = sum(q for p, q in asks if base <= p <= base + 0.20)
    windows_a.append((base, window_qty))
windows_a.sort(key=lambda x: x[1], reverse=True)
for p, q in windows_a[:10]:
    print('  center={:.4f}  qty={:,.0f} SOL'.format(p, q))
