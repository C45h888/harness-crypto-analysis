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

# --- Orderbook + supporting data ---
book = get('/fapi/v1/depth', {'symbol': sym, 'limit': 100})
bids = [[float(p), float(q)] for p, q in book['bids']]
asks = [[float(p), float(q)] for p, q in book['asks']]

funding = get('/fapi/v1/premiumIndex', {'symbol': sym})
oi_now = get('/fapi/v1/openInterest', {'symbol': sym})
oi_hist = get('/futures/data/openInterestHist', {'symbol': sym, 'period': '5m', 'limit': 20})
tbs = get('/futures/data/takerlongshortRatio', {'symbol': sym, 'period': '5m', 'limit': 20})
top = get('/futures/data/topLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 20})
glb = get('/futures/data/globalLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 20})
trades = get('/fapi/v1/trades', {'symbol': sym, 'limit': 200})
now_ts = trades[0]['time'] if trades else 0

print('=== AUTO-DERIVED WALL ANALYSIS (price: {:.4f}) ==='.format(px))
print('Time: {} UTC'.format(datetime.now(timezone.utc).strftime('%H:%M:%S')))
print()

# --- Find keystone: densest rolling 0.20 bid window in [px-0.50, px-0.05] ---
# User-defined keystone = rolling bid density window center, not a fixed sub-zone.
# Slide a 0.20 window across the bid side, sum qty, take the center of the densest one.
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
keystone_center = kz_candidates[0][0] if kz_candidates else (px - 0.20)
KZ_W_LO = keystone_center - 0.20
KZ_W_HI = keystone_center

# --- Find seller wall: densest rolling 0.20 ask window in [px+0.10, px+0.60] ---
SW_LO, SW_HI = px + 0.10, px + 0.60
sw_candidates = []
seen = set()
for level in asks:
    center = level[0]
    if not (SW_LO <= center <= SW_HI):
        continue
    c_round = round(center, 4)
    if c_round in seen:
        continue
    seen.add(c_round)
    window_qty = sum(q for p, q in asks if center <= p <= center + 0.20)
    sw_candidates.append((center, window_qty))
sw_candidates.sort(key=lambda x: x[1], reverse=True)
seller_center = sw_candidates[0][0] if sw_candidates else (px + 0.30)
SW_W_LO = seller_center
SW_W_HI = seller_center + 0.20

print('=== AUTO-DERIVED ZONES ===')
print('  Keystone center:  {:.4f}  (window {:.4f}-{:.4f})'.format(keystone_center, KZ_W_LO, KZ_W_HI))
print('  Seller wall center: {:.4f}  (window {:.4f}-{:.4f})'.format(seller_center, SW_W_LO, SW_W_HI))
print()

# --- Ask wall: 0.05 buckets above current price up to seller_wall+0.30 ---
ask_wall_zones = []
z = px
while z <= SW_W_HI + 0.30 + 1e-9:
    ask_wall_zones.append(('{:.2f}-{:.2f}'.format(z, z + 0.05), z, z + 0.05))
    z += 0.05

print('=== ASK LADDER (current price -> seller wall + 0.30) ===')
print('{:<14}  {:>10}  {:>12}  {:>14}'.format(
    'Zone', 'Levels', 'AskQty(SOL)', 'CumNotional($)'))
print('-' * 56)
cum_notional = 0
total_ask_qty = 0
for name, lo, hi in ask_wall_zones:
    levels = [(p, q) for p, q in asks if lo <= p < hi]
    n_levels = len(levels)
    qty = sum(q for _, q in levels)
    notional = sum(p * q for p, q in levels)
    cum_notional += notional
    total_ask_qty += qty
    print('{:<14}  {:>10}  {:>12,.0f}  ${:>13,.0f}'.format(
        name, n_levels, qty, cum_notional))

# --- Ask wall total: from px to seller wall + 0.30 ---
wall_total_qty = total_ask_qty
wall_total_notional = cum_notional
print()
print('  Total ask qty above price:  {:>10,.0f} SOL'.format(total_ask_qty))
print('  Total ask notional:         ${:>10,.0f}'.format(cum_notional))
print()

# --- Bid ladder: keystone area ---
kz_zones = []
z = KZ_W_LO - 0.10
while z <= KZ_W_HI + 0.05 + 1e-9:
    kz_zones.append(('{:.2f}-{:.2f}'.format(z, z + 0.05), z, z + 0.05))
    z += 0.05

print('=== BID LADDER (keystone area) ===')
print('{:<14}  {:>10}  {:>12}  {:>14}'.format(
    'Zone', 'Levels', 'BidQty(SOL)', 'CumNotional($)'))
print('-' * 56)
cum_bid_notional = 0
total_bid_qty = 0
for name, lo, hi in kz_zones:
    levels = [(p, q) for p, q in bids if lo <= p < hi]
    n_levels = len(levels)
    qty = sum(q for _, q in levels)
    notional = sum(p * q for p, q in levels)
    cum_bid_notional += notional
    total_bid_qty += qty
    print('{:<14}  {:>10}  {:>12,.0f}  ${:>13,.0f}'.format(
        name, n_levels, qty, cum_bid_notional))

print()
print('  Total bid qty in keystone area: {:>10,.0f} SOL'.format(total_bid_qty))
print('  Total bid notional:              ${:>10,.0f}'.format(cum_bid_notional))
print()

# --- Keystone vs wall balance ---
kz_window_qty = sum(q for p, q in bids if KZ_W_LO <= p <= KZ_W_HI)
kz_window_notional = sum(p * q for p, q in bids if KZ_W_LO <= p <= KZ_W_HI)
sw_window_qty = sum(q for p, q in asks if SW_W_LO <= p <= SW_W_HI)
sw_window_notional = sum(p * q for p, q in asks if SW_W_LO <= p <= SW_W_HI)

print('=== KEYSTONE vs SELLER WALL BALANCE ===')
print('  Keystone (window): {:>10,.0f} SOL  ${:>12,.0f}'.format(kz_window_qty, kz_window_notional))
print('  Seller wall:       {:>10,.0f} SOL  ${:>12,.0f}'.format(sw_window_qty, sw_window_notional))
print('  Bid/Ask qty ratio: {:>10.2f}x'.format(kz_window_qty / max(sw_window_qty, 0.001)))
print('  Bid/Ask notional:  {:>10.2f}x'.format(kz_window_notional / max(sw_window_notional, 0.001)))
print()

# --- Recent trade activity (5-min buckets) ---
trade_buckets = defaultdict(lambda: {'buy_qty': 0, 'sell_qty': 0, 'buy_vol': 0, 'sell_vol': 0, 'buy_n': 0, 'sell_n': 0})
for t in trades:
    price = float(t['price'])
    qty = float(t['qty'])
    notional = price * qty
    ts = int(t['time'])
    bucket = ts // 300000 * 300000
    if t['isBuyerMaker']:
        trade_buckets[bucket]['sell_qty'] += qty
        trade_buckets[bucket]['sell_vol'] += notional
        trade_buckets[bucket]['sell_n'] += 1
    else:
        trade_buckets[bucket]['buy_qty'] += qty
        trade_buckets[bucket]['buy_vol'] += notional
        trade_buckets[bucket]['buy_n'] += 1

print('=== RECENT 5-MIN BUCKET FLOW (last 8) ===')
print('{:<10}  {:>6}  {:>10}  {:>10}  {:>10}  {:>8}  {:>12}'.format(
    'Time', 'TrdN', 'BuyQty', 'SellQty', 'NetQty', 'L/S', 'NetVol($)'))
print('-' * 70)
for bucket in sorted(trade_buckets.keys(), reverse=True)[:8]:
    d = trade_buckets[bucket]
    net = d['buy_qty'] - d['sell_qty']
    ls = d['sell_qty'] / max(d['buy_qty'], 0.001)
    print('{}  {:>6}  {:>10,.1f}  {:>10,.1f}  {:>+10,.1f}  {:>8.2f}  {:>+12,.0f}'.format(
        datetime.fromtimestamp(bucket / 1000, tz=__import__('datetime').timezone.utc).strftime('%H:%M'),
        d['buy_n'] + d['sell_n'],
        d['buy_qty'], d['sell_qty'], net, ls,
        d['buy_vol'] - d['sell_vol']))
print()

# --- Where are trades happening ---
all_zones = []
z = px - 0.40
while z <= px + 0.70 + 1e-9:
    all_zones.append(('{:.2f}-{:.2f}'.format(z, z + 0.10), z, z + 0.10))
    z += 0.10

zone_trades = defaultdict(lambda: {'buy': 0, 'sell': 0, 'buy_vol': 0, 'sell_vol': 0})
for t in trades:
    price = float(t['price'])
    qty = float(t['qty'])
    notional = price * qty
    for name, lo, hi in all_zones:
        if lo <= price < hi:
            if t['isBuyerMaker']:
                zone_trades[name]['sell'] += qty
                zone_trades[name]['sell_vol'] += notional
            else:
                zone_trades[name]['buy'] += qty
                zone_trades[name]['buy_vol'] += notional
            break

print('=== WHERE TRADES ARE HAPPENING (last 200, 0.10 buckets) ===')
print('{:<14}  {:>10}  {:>10}  {:>10}  {:>10}  {:>8}'.format(
    'Zone', 'BuyQty', 'SellQty', 'Net', 'BuyVol', 'SellVol'))
print('-' * 68)
for name, _, _ in all_zones:
    d = zone_trades[name]
    net = d['buy'] - d['sell']
    if d['buy'] > 0 or d['sell'] > 0:
        print('{:<14}  {:>10,.1f}  {:>10,.1f}  {:>+10,.1f}  {:>10,.0f}  {:>10,.0f}'.format(
            name, d['buy'], d['sell'], net, d['buy_vol'], d['sell_vol']))
print()

# --- OI trend ---
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
print('=== OI TREND ===')
print('{:<8}  {:>16}  {:>16}  {:>10}'.format('Time', 'OI(contracts)', 'OI_val($)', 'OI_chg%'))
print('-' * 54)
for idx in range(len(oi_series)):
    r = oi_series[idx]
    if idx == 0:
        chg = 0
    else:
        prev = oi_series[idx - 1]['oi']
        chg = (r['oi'] - prev) / prev * 100
    print('{}  {:>16,.0f}  ${:>15,.0f}  {:>+9.3f}%'.format(
        datetime.fromtimestamp(r['ts'] / 1000, tz=__import__('datetime').timezone.utc).strftime('%H:%M'),
        r['oi'], r['val'], chg))

oi_now_val = float(oi_now.get('openInterest', 0))
oi_first = oi_series[0]['oi'] if oi_series else oi_now_val
oi_last = oi_series[-1]['oi'] if oi_series else oi_now_val
oi_window_chg = (oi_last - oi_first) / oi_first * 100 if oi_first > 0 else 0
print()
print('OI window change: {:+.3f}%'.format(oi_window_chg))
print('Current OI: {:,.0f} contracts'.format(oi_now_val))
print()

# --- Taker buy ratio evolution ---
print('=== TAKER BUY RATIO (last 10 x 5m bars) ===')
print('{:<8}  {:>8}  {:>10}  {:>10}  {:>8}  {:>8}'.format(
    'Time', 'Ratio', 'BuyVol', 'SellVol', 'Net5m', 'TBR%'))
print('-' * 58)
for r in tbs[-10:]:
    ts = int(r['timestamp'])
    ratio = float(r['buySellRatio'])
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    net = bv - sv
    print('{}  {:>8.4f}  {:>10,.0f}  {:>10,.0f}  {:>+8,.0f}  {:>7.1%}'.format(
        datetime.fromtimestamp(ts / 1000, tz=__import__('datetime').timezone.utc).strftime('%H:%M'),
        ratio, bv, sv, net, bv / (bv + sv)))
print()

# --- Top trader positioning ---
print('=== TOP TRADER POSITIONING ===')
print('{:<8}  {:>8}  {:>8}  {:>8}  {:>8}'.format(
    'Time', 'Long%', 'Short%', 'Ratio', '1m_chg'))
print('-' * 44)
prev_ratio = None
for r in top[-10:]:
    ts = int(r['timestamp'])
    long_pct = float(r['longAccount']) * 100
    short_pct = float(r['shortAccount']) * 100
    ratio = float(r['longShortRatio'])
    chg = ''
    if prev_ratio is not None:
        delta = ratio - prev_ratio
        chg = '{:+.4f}'.format(delta)
    print('{:<8}  {:>7.2f}%  {:>7.2f}%  {:>8.4f}  {}'.format(
        datetime.fromtimestamp(ts / 1000, tz=__import__('datetime').timezone.utc).strftime('%H:%M'),
        long_pct, short_pct, ratio, chg))
    prev_ratio = ratio

print()
print('=== GLOBAL TRADER POSITIONING ===')
print('{:<8}  {:>8}  {:>8}  {:>8}  {:>8}'.format(
    'Time', 'Long%', 'Short%', 'Ratio', '1m_chg'))
print('-' * 44)
prev_ratio = None
for r in glb[-10:]:
    ts = int(r['timestamp'])
    long_pct = float(r['longAccount']) * 100
    short_pct = float(r['shortAccount']) * 100
    ratio = float(r['longShortRatio'])
    chg = ''
    if prev_ratio is not None:
        delta = ratio - prev_ratio
        chg = '{:+.4f}'.format(delta)
    print('{:<8}  {:>7.2f}%  {:>7.2f}%  {:>8.4f}  {}'.format(
        datetime.fromtimestamp(ts / 1000, tz=__import__('datetime').timezone.utc).strftime('%H:%M'),
        long_pct, short_pct, ratio, chg))
    prev_ratio = ratio

print()
print('=== FUNDING + MARK ===')
mark_px = float(funding.get('lastFundingRate', 0))
funding_bps = mark_px * 10000
print('Funding rate: {:.6f} ({:+.2f} bps)'.format(mark_px, funding_bps))
print('Mark price:   {}'.format(funding.get('markPrice', 'N/A')))
print('Index price:  {}'.format(funding.get('indexPrice', 'N/A')))
print()

# --- Probability scorecard ---
print('=== PROBABILITY SCORECARD: WILL KEYSTONE HOLD? ===')
latest_tbr = float(tbs[-1]['buyVol']) / max(float(tbs[-1]['buyVol']) + float(tbs[-1]['sellVol']), 1)
top_long_pct = float(top[-1]['longAccount'])
glb_long_pct = float(glb[-1]['longAccount'])

oi_chg_5m = 0
if len(oi_series) >= 2:
    oi_chg_5m = (oi_series[-1]['oi'] - oi_series[-2]['oi']) / oi_series[-2]['oi'] * 100

total_buy_qty = sum(float(t['qty']) for t in trades if not t['isBuyerMaker'])
total_sell_qty = sum(float(t['qty']) for t in trades if t['isBuyerMaker'])
net_buy_ratio = total_buy_qty / max(total_buy_qty + total_sell_qty, 1)

print('KEYSTONE STRENGTH METRICS:')
print('  Bid qty at derived keystone:   {:>10,.0f} SOL'.format(kz_window_qty))
print('  Bid notional at keystone:      ${:>12,.0f}'.format(kz_window_notional))
print('  Latest taker buy ratio:        {:>10.1%}'.format(latest_tbr))
print('  Taker buy ratio (last 5 bars):')
for r in tbs[-5:]:
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    tbr = bv / max(bv + sv, 0.001)
    print('    {}  TBR={:.1%}  buy={:,.0f}  sell={:,.0f}'.format(
        datetime.fromtimestamp(int(r['timestamp']) / 1000, tz=__import__('datetime').timezone.utc).strftime('%H:%M'),
        tbr, bv, sv))
print()
print('SELLER WALL METRICS:')
print('  Ask qty at derived wall:        {:>10,.0f} SOL'.format(sw_window_qty))
print('  Ask notional at wall:           ${:>12,.0f}'.format(sw_window_notional))
print('  Bid/Ask ratio (qty):            {:>10.2f}x'.format(kz_window_qty / max(sw_window_qty, 0.001)))
print('  Bid/Ask ratio (notional):       {:>10.2f}x'.format(kz_window_notional / max(sw_window_notional, 0.001)))
print()
print('POSITIONING METRICS:')
print('  Top trader long%:               {:>10.2f}%'.format(top_long_pct * 100))
print('  Global long%:                   {:>10.2f}%'.format(glb_long_pct * 100))
print('  OI 5m change:                   {:>+10.3f}%'.format(oi_chg_5m))
print()
print('BUYER ACTIVITY (last 200 trades):')
print('  Total buy qty:   {:>10,.1f} SOL'.format(total_buy_qty))
print('  Total sell qty:  {:>10,.1f} SOL'.format(total_sell_qty))
print('  Net buy:         {:>+10,.1f} SOL'.format(total_buy_qty - total_sell_qty))
print('  Buy/Sell ratio:  {:>10.2f}'.format(total_buy_qty / max(total_sell_qty, 0.001)))
print()

# TBR trend
tbr_values = []
for r in tbs[-6:]:
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    tbr_values.append(bv / max(bv + sv, 0.001))
print('TBR TREND (last 6 bars):')
for i, r in enumerate(tbs[-6:]):
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    tbr = bv / max(bv + sv, 0.001)
    direction = 'UP' if i > 0 and tbr > tbr_values[i - 1] else ('DOWN' if i > 0 and tbr < tbr_values[i - 1] else 'FIRST')
    print('  {}  TBR={:.1%}  [{}]'.format(
        datetime.fromtimestamp(int(r['timestamp']) / 1000, tz=__import__('datetime').timezone.utc).strftime('%H:%M'),
        tbr, direction))

print()
print('=== SCORECARD (keystone holds?) ===')
score = 0
max_score = 10

bid_ask_ratio = kz_window_qty / max(sw_window_qty, 1)
if bid_ask_ratio > 0.5: score += 2
elif bid_ask_ratio > 0.3: score += 1
print('1. Keystone / Wall ratio: {:.2f}x -> +{}'.format(
    bid_ask_ratio, min(2, int(bid_ask_ratio * 5))))

if latest_tbr > 0.55: score += 2
elif latest_tbr > 0.50: score += 1
print('2. Taker buy ratio: {:.1%} -> +{}'.format(
    latest_tbr, min(2, int(latest_tbr * 4) - 1)))

if oi_chg_5m > 0: score += 2
elif oi_chg_5m > -0.1: score += 1
print('3. OI trend 5m: {:+.3f}% -> +{}'.format(
    oi_chg_5m, 2 if oi_chg_5m > 0 else (1 if oi_chg_5m > -0.1 else 0)))

if top_long_pct < 0.75: score += 2
elif top_long_pct < 0.80: score += 1
print('4. Top trader long%: {:.2f}% -> +{}'.format(
    top_long_pct * 100, 2 if top_long_pct < 0.75 else (1 if top_long_pct < 0.80 else 0)))

if net_buy_ratio > 0.55: score += 2
elif net_buy_ratio > 0.50: score += 1
print('5. Net buy ratio (recent trades): {:.1%} -> +{}'.format(
    net_buy_ratio, min(2, int(net_buy_ratio * 4) - 1)))

print()
print('  SCORE: {}/{}'.format(score, max_score))
prob_keystone_holds = score / max_score * 100
print('  Keystone holds: ~{:.0f}%'.format(prob_keystone_holds))
print('  Keystone breaks: ~{:.0f}%'.format(100 - prob_keystone_holds))
