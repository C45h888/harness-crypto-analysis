#!/usr/bin/env python3
"""Seller-wall state + 1h buy flow check. Determines if current move is
institutional absence (weekend) or genuine demand."""
import urllib.request, json
from datetime import datetime, timezone
from collections import defaultdict

BASE = 'https://fapi.binance.com'
sym = 'SOLUSDT'

def get(path, params=None):
    url = BASE + path
    if params:
        url += '?' + '&'.join('{}={}'.format(k,v) for k,v in params.items())
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())

ticker = get('/fapi/v1/ticker/price', {'symbol': sym})
px = float(ticker['price'])
print('LIVE PRICE: {:.4f}  @ {}'.format(px, datetime.now(timezone.utc).strftime('%H:%M:%S UTC')))
day = datetime.now(timezone.utc).strftime('%A')
print('DAY: {}'.format(day))
print()

book = get('/fapi/v1/depth', {'symbol': sym, 'limit': 500})
bids = [[float(p), float(q)] for p,q in book['bids']]
asks = [[float(p), float(q)] for p,q in book['asks']]

# ===== ASK WALL STATE =====
print('=' * 70)
print('SELLER WALL STATE (live)')
print('=' * 70)
ask_dict = {p: q for p,q in asks}
levels = [74.30, 74.40, 74.44, 74.48, 74.50, 74.55, 74.60, 74.65, 74.71, 74.75, 74.80, 75.00]
print('{:<8} {:>10} {:>14}'.format('Price', 'Qty(SOL)', 'Notional($)'))
print('-' * 36)
for p in levels:
    q = ask_dict.get(p, 0)
    print('{:<8.2f} {:>10,.0f} {:>14,.0f}'.format(p, q, p*q))
print()

# 0.05 bucket around 74.48
print('0.05 BUCKETS AROUND ENTRY 74.48:')
for lo in [74.40, 74.45, 74.48, 74.50, 74.55, 74.60]:
    hi = lo + 0.05
    bq = sum(q for p,q in asks if lo <= p < hi)
    print('  {:.2f}-{:.2f}  {:>8,.0f} SOL'.format(lo, hi, bq))
print()

# ===== 1-HOUR BUY FLOW =====
print('=' * 70)
print('1-HOUR BUY FLOW (all trades)')
print('=' * 70)
trades = get('/fapi/v1/trades', {'symbol': sym, 'limit': 1000})
now_ts = trades[0]['time']
cutoff_ms = now_ts - (60 * 60 * 1000)  # 1h ago

min_b = defaultdict(lambda: {'b':0, 's':0, 'bv':0, 'sv':0, 'n':0})
for t in trades:
    if int(t['time']) < cutoff_ms:
        continue
    ts = int(t['time'])
    bucket = ts // 60000 * 60000
    qty = float(t['qty'])
    price = float(t['price'])
    if t['isBuyerMaker']:
        min_b[bucket]['s'] += qty
        min_b[bucket]['sv'] += price * qty
    else:
        min_b[bucket]['b'] += qty
        min_b[bucket]['bv'] += price * qty
    min_b[bucket]['n'] += 1

total_b = sum(d['b'] for d in min_b.values())
total_s = sum(d['s'] for d in min_b.values())
total_bv = sum(d['bv'] for d in min_b.values())
total_sv = sum(d['sv'] for d in min_b.values())
total_n = sum(d['n'] for d in min_b.values())

print('Total trades in 1h: {}'.format(total_n))
print('Total buy qty:    {:>10,.0f} SOL'.format(total_b))
print('Total sell qty:   {:>10,.0f} SOL'.format(total_s))
print('Net:              {:>+10,.0f} SOL'.format(total_b - total_s))
print('Total buy $:      {:>10,.0f}'.format(total_bv))
print('Total sell $:     {:>10,.0f}'.format(total_sv))
print('Net $:            {:>+10,.0f}'.format(total_bv - total_sv))
tbr_q = total_b / max(total_b + total_s, 1)
tbr_v = total_bv / max(total_bv + total_sv, 1)
print('TBR (qty):        {:.1%}'.format(tbr_q))
print('TBR (vol):        {:.1%}'.format(tbr_v))
print()

# Per-minute
print('1-MIN BREAKDOWN:')
print('{:<10} {:>6} {:>9} {:>9} {:>9} {:>7}'.format(
    'Time', 'TrdN', 'BuyQty', 'SellQty', 'NetQty', 'TBR%'))
print('-' * 60)
for b in sorted(min_b.keys(), reverse=True):
    d = min_b[b]
    net = d['b'] - d['s']
    tbr = d['b'] / max(d['b'] + d['s'], 1)
    flag = ' <<<' if tbr > 0.55 else (' >>>' if tbr < 0.45 else '')
    print('{:<10} {:>6} {:>9,.0f} {:>9,.0f} {:>+9,.0f} {:>7.1%}{}'.format(
        datetime.fromtimestamp(b/1000, tz=timezone.utc).strftime('%H:%M'),
        d['n'], d['b'], d['s'], net, tbr, flag))
print()

# ===== LARGE PRINTS (>= 100 SOL) =====
print('=' * 70)
print('LARGE PRINTS IN 1H (qty >= 100 SOL)')
print('=' * 70)
big_buys = []
big_sells = []
for t in trades:
    if int(t['time']) < cutoff_ms:
        continue
    qty = float(t['qty'])
    if qty >= 100:
        price = float(t['price'])
        if t['isBuyerMaker']:
            big_sells.append({'qty': qty, 'price': price})
        else:
            big_buys.append({'qty': qty, 'price': price})
b_buy_q = sum(x['qty'] for x in big_buys)
b_sell_q = sum(x['qty'] for x in big_sells)
b_buy_n = sum(x['price']*x['qty'] for x in big_buys)
b_sell_n = sum(x['price']*x['qty'] for x in big_sells)
print('Large BUYs:  {} prints  {:>8,.0f} SOL  ${:>10,.0f}'.format(
    len(big_buys), b_buy_q, b_buy_n))
print('Large SELLs: {} prints  {:>8,.0f} SOL  ${:>10,.0f}'.format(
    len(big_sells), b_sell_q, b_sell_n))
print('Net large:   {:+8,.0f} SOL  ${:+10,.0f}'.format(b_buy_q - b_sell_q, b_buy_n - b_sell_n))
print()

# Are there ANY large prints at/above 74.48 entry?
entry_zone_large = []
for t in trades:
    if int(t['time']) < cutoff_ms:
        continue
    qty = float(t['qty'])
    price = float(t['price'])
    if qty >= 50 and price >= 74.40:
        side = 'BUY' if not t['isBuyerMaker'] else 'SELL'
        entry_zone_large.append({'side': side, 'price': price, 'qty': qty,
                                  'ts': int(t['time'])})
print('Large prints (>=50 SOL) AT or ABOVE 74.40 in last 1h:')
print('  Count: {}'.format(len(entry_zone_large)))
if entry_zone_large:
    print('  Recent ones:')
    entry_zone_large.sort(key=lambda x: x['ts'], reverse=True)
    for x in entry_zone_large[:10]:
        print('    {}  qty={:.1f}  px={:.3f}'.format(x['side'], x['qty'], x['price']))
else:
    print('  ZERO — no large prints at entry zone')
print()

# ===== TBR 5m bars =====
tbs = get('/futures/data/takerlongshortRatio', {'symbol': sym, 'period': '5m', 'limit': 24})
print('=' * 70)
print('TBR 5-MIN BARS (last 2h)')
print('=' * 70)
print('{:<8} {:>8} {:>10} {:>10} {:>7}'.format('Time', 'Ratio', 'BuyVol', 'SellVol', 'TBR%'))
print('-' * 50)
for r in tbs[-24:]:
    ts = int(r['timestamp'])
    bv = float(r['buyVol'])
    sv = float(r['sellVol'])
    ratio = float(r['buySellRatio'])
    tbr = bv / max(bv+sv, 1)
    flag = ' <<<' if tbr > 0.55 else (' >>>' if tbr < 0.45 else '')
    print('{:<8} {:>8.4f} {:>10,.0f} {:>10,.0f} {:>7.1%}{}'.format(
        datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%H:%M'),
        ratio, bv, sv, tbr, flag))
print()

# ===== OI trend =====
oi_hist = get('/futures/data/openInterestHist', {'symbol': sym, 'period': '5m', 'limit': 12})
print('=' * 70)
print('OI 5-MIN BARS (last 1h)')
print('=' * 70)
oi_series = []
for r in oi_hist:
    try:
        ts = int(r['timestamp'])
        oi = float(r['sumOpenInterest'])
        oi_series.append((ts, oi))
    except: pass
oi_series.sort()
print('{:<8} {:>14} {:>10}'.format('Time', 'OI', 'Chg%'))
print('-' * 36)
for i, (ts, oi) in enumerate(oi_series):
    chg = 0 if i == 0 else (oi - oi_series[i-1][1]) / oi_series[i-1][1] * 100
    flag = ' <<<' if chg > 0.3 else (' >>>' if chg < -0.3 else '')
    print('{:<8} {:>14,.0f} {:>+10.3f}%{}'.format(
        datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%H:%M'),
        oi, chg, flag))
print()
oi_chg = (oi_series[-1][1] - oi_series[0][1]) / oi_series[0][1] * 100
print('OI 1h change: {:+.3f}%'.format(oi_chg))
if oi_chg > 0.3:
    print('  >>> OI BUILDING (new positions opening)')
elif oi_chg < -0.3:
    print('  >>> OI UNWINDING (positions closing)')
else:
    print('  >>> OI FLAT')
print()

# ===== FUNDING =====
fund = get('/fapi/v1/premiumIndex', {'symbol': sym})
fr = float(fund.get('lastFundingRate', 0))
print('=' * 70)
print('FUNDING + MARK')
print('=' * 70)
print('Funding: {:.6f} ({:+.2f} bps)'.format(fr, fr * 10000))
print('Mark:    {:.4f}'.format(float(fund.get('markPrice', 0))))
print('Index:   {:.4f}'.format(float(fund.get('indexPrice', 0))))
print()

# ===== 24H VOLUME =====
t24 = get('/fapi/v1/ticker/24hr', {'symbol': sym})
print('=' * 70)
print('24H STATS')
print('=' * 70)
print('Price change: {:+.2f}%'.format(float(t24.get('priceChangePercent', 0))))
print('Quote volume: ${:,.0f}'.format(float(t24.get('quoteVolume', 0))))
print()

# ===== VERDICT =====
print('=' * 70)
print('VERDICT')
print('=' * 70)
print()
# Are sellers present at 74.48?
wall_74_48 = ask_dict.get(74.48, 0) + sum(q for p,q in asks if 74.45 <= p <= 74.50)
print('Ask liquidity 74.45-74.50: {:,.0f} SOL'.format(wall_74_48))
print('Ask liquidity 74.40-74.48: {:,.0f} SOL'.format(
    sum(q for p,q in asks if 74.40 <= p <= 74.48)))
print('Ask liquidity above 74.48: {:,.0f} SOL'.format(
    sum(q for p,q in asks if p > 74.48 and p <= 75.00)))
print()
print('1h TBR (qty): {:.1%}'.format(tbr_q))
print('1h net flow:  {:+,.0f} SOL (${:+,.0f})'.format(total_b - total_s, total_bv - total_sv))
print('Large prints at 74.40+: {} ({} buys / {} sells)'.format(
    len(entry_zone_large),
    sum(1 for x in entry_zone_large if x['side'] == 'BUY'),
    sum(1 for x in entry_zone_large if x['side'] == 'SELL')))
print('OI 1h chg:    {:+.3f}%'.format(oi_chg))
print()