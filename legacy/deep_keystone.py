"""Deep analysis of the auto-derived keystone defense.

Pulls the current price, finds the rolling 0.20 bid-density window center as
the keystone (consistent with user keystone definition), then drills into the
bid stack, aggressive flow, and surrounding trade activity.
"""
import urllib.request, json
from datetime import datetime, timezone
from collections import defaultdict

BASE_FUT = 'https://fapi.binance.com'
BASE_SPOT = 'https://api.binance.com'
sym = 'SOLUSDT'

def get_fut(path, params=None):
    url = BASE_FUT + path
    if params:
        url += '?' + '&'.join('{}={}'.format(k, v) for k, v in params.items())
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())

def get_spot(path, params=None):
    url = BASE_SPOT + path
    if params:
        url += '?' + '&'.join('{}={}'.format(k, v) for k, v in params.items())
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


# --- Live reference price ---
ticker = get_fut('/fapi/v1/ticker/price', {'symbol': sym})
px = float(ticker['price'])

# 1. Orderbook full depth
book = get_fut('/fapi/v1/depth', {'symbol': sym, 'limit': 100})
bids = [[float(p), float(q)] for p, q in book['bids']]
asks = [[float(p), float(q)] for p, q in book['asks']]

# --- Derive keystone (densest rolling 0.20 bid window in [px-0.50, px-0.05]) ---
KZ_LO, KZ_HI = px - 0.50, px - 0.05
kz_candidates = []
seen = set()
for level in bids:
    center = level[0]
    if not (KZ_LO <= center <= KZ_HI):
        continue
    c_round = round(center, 4)
    if c_round in seen:
        continue
    seen.add(c_round)
    window_qty = sum(q for p, q in bids if center - 0.20 <= p <= center)
    kz_candidates.append((center, window_qty))
kz_candidates.sort(key=lambda x: x[1], reverse=True)
keystone = kz_candidates[0][0] if kz_candidates else (px - 0.20)

# Zones derived from the keystone
TIGHT_LO, TIGHT_HI = keystone - 0.05, keystone + 0.05      # ±0.05 = exact defense band
WIDE_LO, WIDE_HI = keystone - 0.10, keystone + 0.10        # ±0.10 = wide defense band
FLOOR_LO = keystone - 0.30                                   # below-floor reference
CEILING_HI = keystone + 0.15                                 # above-keystone next defense
ABOVE_LO, ABOVE_HI = keystone, keystone + 0.15              # band above keystone

print('=== LIVE PRICE: {:.4f} (ts: {} UTC) ==='.format(
    px, datetime.now(timezone.utc).strftime('%H:%M:%S')))
print('=== AUTO-DERIVED KEYSTONE: {:.4f} ==='.format(keystone))
print('  Tight zone (±0.05):  {:.4f} - {:.4f}'.format(TIGHT_LO, TIGHT_HI))
print('  Wide zone (±0.10):   {:.4f} - {:.4f}'.format(WIDE_LO, WIDE_HI))
print()

# 2. Trades
trades = get_fut('/fapi/v1/trades', {'symbol': sym, 'limit': 1000})
now_ts = trades[0]['time'] if trades else 0

# 3. Large trades at the keystone zone (dynamic)
print('=== LARGE TRADES AT KEYSTONE (±0.05: {:.4f} - {:.4f}) ==='.format(TIGHT_LO, TIGHT_HI))
trades_kz = [t for t in trades if TIGHT_LO <= float(t['price']) <= TIGHT_HI]
buy_kz = [t for t in trades_kz if not t['isBuyerMaker']]
sell_kz = [t for t in trades_kz if t['isBuyerMaker']]
print('Buy trades:  {}  Total qty: {:>10.2f}  Notional: ${:>12,.0f}'.format(
    len(buy_kz), sum(float(t['qty']) for t in buy_kz),
    sum(float(t['qty']) * float(t['price']) for t in buy_kz)))
print('Sell trades: {}  Total qty: {:>10.2f}  Notional: ${:>12,.0f}'.format(
    len(sell_kz), sum(float(t['qty']) for t in sell_kz),
    sum(float(t['qty']) * float(t['price']) for t in sell_kz)))
print()

# 4. All big prints
print('=== RECENT LARGE PRINTS (qty >= 100 SOL) ===')
big = []
for t in trades:
    qty = float(t['qty'])
    if qty >= 100:
        px_t = float(t['price'])
        side = 'BUY' if not t['isBuyerMaker'] else 'SELL'
        big.append({'ts': int(t['time']), 'side': side, 'px': px_t,
                    'qty': qty, 'age': (now_ts - int(t['time'])) / 1000})
big.sort(key=lambda x: x['ts'], reverse=True)
for lp in big[:25]:
    print('  {}  {:4s}  px={:.4f}  qty={:>7.2f}  ${:>10,.0f}  {:.0f}s ago'.format(
        datetime.fromtimestamp(lp['ts'] / 1000, tz=timezone.utc).strftime('%H:%M:%S'),
        lp['side'], lp['px'], lp['qty'], lp['px'] * lp['qty'], lp['age']))
print()

# 5. Keystone bid book (dynamic tight zone)
print('=== KEYSTONE BID BOOK (tight zone {:.4f} - {:.4f}) ==='.format(TIGHT_LO, TIGHT_HI))
keystone_bids = [(p, q) for p, q in bids if TIGHT_LO <= p <= TIGHT_HI]
keystone_bids.sort()
cum_qty = 0
cum_notional = 0
for p, q in keystone_bids:
    cum_qty += q
    cum_notional += p * q
    print('  {:7.4f}  qty={:>10.2f}  cum_qty={:>10.2f}  cum_notional=${:>12,.0f}'.format(
        p, q, cum_qty, cum_notional))

print()
print('  TOTAL bids in tight zone: {:>10,.2f} SOL  ${:>12,.0f}'.format(
    sum(q for _, q in keystone_bids),
    sum(p * q for p, q in keystone_bids)))
print()

# 6. Bid stack: below / at / above the keystone
print('=== BID DENSITY STACK (anchored at keystone {:.4f}) ==='.format(keystone))
print()
print('Levels below keystone (floor area):')
for p, q in sorted(bids):
    if FLOOR_LO <= p < keystone:
        print('  {:7.4f}  qty={:>10.2f}  notional=${:>12,.0f}'.format(p, q, p * q))

print()
print('Levels at keystone (exact match):')
exact_levels = [(p, q) for p, q in sorted(bids) if round(p, 4) == round(keystone, 4)]
if exact_levels:
    for p, q in exact_levels:
        print('  {:7.4f}  qty={:>10.2f}  notional=${:>12,.0f}'.format(p, q, p * q))
else:
    # find nearest level
    nearest = min(bids, key=lambda x: abs(x[0] - keystone))
    print('  No exact level. Nearest: {:7.4f}  qty={:>10.2f}  notional=${:>12,.0f}'.format(
        nearest[0], nearest[1], nearest[0] * nearest[1]))

print()
print('Levels above keystone (next defense):')
for p, q in sorted(bids):
    if keystone < p <= ABOVE_HI:
        print('  {:7.4f}  qty={:>10.2f}  notional=${:>12,.0f}'.format(p, q, p * q))
print()

# 7. Aggressive bids (large prints) over the last 5 min within the wide zone
print('=== AGGRESSIVE BUYS IN WIDE ZONE ({:.4f} - {:.4f}, last 1000 trades) ==='.format(WIDE_LO, WIDE_HI))
ag_buy = []
for t in trades:
    p = float(t['price'])
    qty = float(t['qty'])
    if WIDE_LO <= p <= WIDE_HI and not t['isBuyerMaker'] and qty >= 5:
        ag_buy.append({
            'ts': int(t['time']),
            'px': p,
            'qty': qty,
            'age': (now_ts - int(t['time'])) / 1000,
            'notional': p * qty,
        })
ag_buy.sort(key=lambda x: x['ts'], reverse=True)
print('Total aggressive buys in wide zone: {}'.format(len(ag_buy)))
total_notional = sum(t['notional'] for t in ag_buy)
print('Total notional: ${:,.0f}'.format(total_notional))
for t in ag_buy[:20]:
    print('  {}  BUY  px={:.4f}  qty={:>7.2f}  ${:>10,.0f}  {:.0f}s ago'.format(
        datetime.fromtimestamp(t['ts'] / 1000, tz=timezone.utc).strftime('%H:%M:%S'),
        t['px'], t['qty'], t['notional'], t['age']))
print()

# 8. Specific keystone level stack — multiple aggregations
print('=== KEYSTONE LEVEL STACK (multiple aggregations) ===')
print()

# Exact level
exact_qty = sum(q for p, q in bids if round(p, 4) == round(keystone, 4))
exact_notional = sum(p * q for p, q in bids if round(p, 4) == round(keystone, 4))
print('Bid at keystone ({:.4f}, exact):'.format(keystone))
if exact_qty > 0:
    print('  Single level: {:>10,.2f} SOL (${:,.0f})'.format(exact_qty, exact_notional))
else:
    print('  No exact level at {:.4f}'.format(keystone))

# Tight zone (5-cent)
zone_5 = [(p, q) for p, q in bids if TIGHT_LO <= p <= TIGHT_HI]
total_5 = sum(q for _, q in zone_5)
print()
print('Bids at keystone ±0.05 ({:.4f} - {:.4f}):'.format(TIGHT_LO, TIGHT_HI))
print('  5-cent zone: {:>10,.2f} SOL (${:,.0f})'.format(total_5, sum(p * q for p, q in zone_5)))

# Wide zone (15-cent)
zone_15 = [(p, q) for p, q in bids if WIDE_LO <= p <= WIDE_HI]
total_15 = sum(q for _, q in zone_15)
print()
print('Bids at keystone ±0.10 ({:.4f} - {:.4f}):'.format(WIDE_LO, WIDE_HI))
print('  10-cent zone: {:>10,.2f} SOL (${:,.0f})'.format(total_15, sum(p * q for p, q in zone_15)))
print()

# 9. Trade intensity at the keystone — last 5 min (trades are within last 1k, ~few min)
print('=== TRADE FLOW AT KEYSTONE TIGHT ZONE ({:.4f} - {:.4f}) ==='.format(TIGHT_LO, TIGHT_HI))
ag_buy_recent = [t for t in trades if TIGHT_LO <= float(t['price']) <= TIGHT_HI and not t['isBuyerMaker']]
ag_sell_recent = [t for t in trades if TIGHT_LO <= float(t['price']) <= TIGHT_HI and t['isBuyerMaker']]

print('Aggressive buy at keystone (buyer taking liquidity):')
print('  Count: {:>4}  Total qty: {:>10,.2f}  Total notional: ${:>12,.0f}'.format(
    len(ag_buy_recent), sum(float(t['qty']) for t in ag_buy_recent),
    sum(float(t['qty']) * float(t['price']) for t in ag_buy_recent)))

print()
print('Aggressive sell at keystone (seller taking liquidity):')
print('  Count: {:>4}  Total qty: {:>10,.2f}  Total notional: ${:>12,.0f}'.format(
    len(ag_sell_recent), sum(float(t['qty']) for t in ag_sell_recent),
    sum(float(t['qty']) * float(t['price']) for t in ag_sell_recent)))
print()

# 10. Funding / OI / Positioning
print('=== POSITIONING ===')
funding = get_fut('/fapi/v1/premiumIndex', {'symbol': sym})
oi = get_fut('/fapi/v1/openInterest', {'symbol': sym})
tbs = get_fut('/futures/data/takerlongshortRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
top = get_fut('/futures/data/topLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
glb = get_fut('/futures/data/globalLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 10})
oi_hist = get_fut('/futures/data/openInterestHist', {'symbol': sym, 'period': '5m', 'limit': 10})

print('Funding rate: {:.6f} ({:+.2f} bps)'.format(
    float(funding['lastFundingRate']), float(funding['lastFundingRate']) * 10000))
print('Current OI: {:,.0f} contracts'.format(float(oi['openInterest'])))
print()
print('TBR (5m):')
for r in tbs[-5:]:
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    ratio = bv / max(sv, 0.001)
    print('  {}  TBR={:.1%}  ratio={:.3f}  buy={:,.0f}  sell={:,.0f}'.format(
        datetime.fromtimestamp(int(r['timestamp']) / 1000, tz=timezone.utc).strftime('%H:%M'),
        bv / (bv + sv), ratio, bv, sv))

print()
print('Top trader long%:')
for r in top[-5:]:
    print('  {}  long={:.2f}%  short={:.2f}%  ratio={:.4f}'.format(
        datetime.fromtimestamp(int(r['timestamp']) / 1000, tz=timezone.utc).strftime('%H:%M'),
        float(r['longAccount']) * 100, float(r['shortAccount']) * 100,
        float(r['longShortRatio'])))

print()
print('Global long%:')
for r in glb[-5:]:
    print('  {}  long={:.2f}%  short={:.2f}%  ratio={:.4f}'.format(
        datetime.fromtimestamp(int(r['timestamp']) / 1000, tz=timezone.utc).strftime('%H:%M'),
        float(r['longAccount']) * 100, float(r['shortAccount']) * 100,
        float(r['longShortRatio'])))

print()
print('OI history:')
for r in oi_hist[-8:]:
    print('  {}  OI={:,.0f}'.format(
        datetime.fromtimestamp(int(r['timestamp']) / 1000, tz=timezone.utc).strftime('%H:%M'),
        float(r['sumOpenInterest'])))
