#!/usr/bin/env python3
"""Live orderflow + walls snapshot for monster wall / $75 supply zone check."""
import urllib.request, json
from datetime import datetime, timezone

BASE = 'https://fapi.binance.com'

def g(p, ps=None):
    u = BASE + p + (('?' + '&'.join(f'{k}={v}' for k, v in ps.items())) if ps else '')
    with urllib.request.urlopen(u, timeout=10) as r:
        return json.loads(r.read())

now = datetime.now(timezone.utc).strftime('%H:%M:%S')
print(f'[LIVE ORDERFLOW + WALLS | {now} UTC]')
print('=' * 72)

px = float(g('/fapi/v1/ticker/price', {'symbol': 'SOLUSDT'})['price'])
print(f'PRICE: ${px:.4f}')
print()

book = g('/fapi/v1/depth', {'symbol': 'SOLUSDT', 'limit': 1000})
bids = sorted([[float(p), float(q)] for p, q in book['bids']], key=lambda x: -x[1])
asks = sorted([[float(p), float(q)] for p, q in book['asks']], key=lambda x: -x[1])

# ASKS
print('=' * 30 + ' ASKS (SUPPLY ABOVE) ' + '=' * 30)
for label, lo, hi in [
    ('76.26  primary absorption', 76.255, 76.265),
    ('76.48  lower seller wall', 76.475, 76.485),
    ('76.50  supply', 76.495, 76.505),
    ('76.59  supply', 76.585, 76.595),
    ('76.66  structural defense', 76.655, 76.665),
    ('76.85  overhead', 76.845, 76.855),
    ('77.00  round', 76.995, 77.005),
    ('77.50  supply', 77.495, 77.505),
    ('78.00  whale', 77.995, 78.005),
]:
    sz = sum(q for p, q in asks if lo <= p <= hi)
    print(f'  $ {label:<28} {sz:>9,.0f} SOL  ${sz*((lo+hi)/2):>11,.0f}')
print()

# BIDS
print('=' * 30 + ' BIDS (DEMAND BELOW) ' + '=' * 30)
for label, lo, hi in [
    ('75.71  immediate bid', 75.705, 75.715),
    ('75.62  TP2 zone', 75.615, 75.625),
    ('75.55  buyer zone', 75.545, 75.555),
    ('75.50  INSTITUTIONAL FLOOR', 75.495, 75.505),
    ('75.42  cluster', 75.415, 75.425),
    ('75.30  cluster', 75.295, 75.305),
    ('75.20  cluster', 75.195, 75.205),
    ('75.13  cluster', 75.125, 75.135),
    ('75.08  cluster', 75.075, 75.085),
    ('75.05  cluster', 75.045, 75.055),
    ('75.00  MONSTER WHALE FLOOR', 74.995, 75.005),
    ('74.80  institutional', 74.795, 74.805),
    ('74.50  deep institutional', 74.495, 74.505),
    ('74.00  major step', 73.995, 74.005),
    ('73.50  WHALE FLOOR', 73.495, 73.505),
]:
    sz = sum(q for p, q in bids if lo <= p <= hi)
    marker = '  << MONSTER' if 'MONSTER' in label else ('  << WHALE' if 'WHALE' in label else '')
    print(f'  $ {label:<30} {sz:>9,.0f} SOL  ${sz*((lo+hi)/2):>11,.0f}{marker}')
print()

# $75 SUPPLY ZONE
print('=' * 28 + ' $75 SUPPLY ZONE (74.90-75.10) ' + '=' * 28)
sz_b = sum(q for p, q in bids if 74.90 <= p <= 75.10)
sz_a = sum(q for p, q in asks if 74.90 <= p <= 75.10)
print(f'  BIDS in $75 zone:    {sz_b:>10,.0f} SOL  ${sz_b*75:>14,.0f}')
print(f'  ASKS in $75 zone:    {sz_a:>10,.0f} SOL  ${sz_a*75:>14,.0f}')
diff = sz_b - sz_a
if diff > 0:
    print(f'  NET: BIDS dominate by {diff:+,.0f} SOL ({diff/(sz_b+sz_a)*100:.1f}%)')
else:
    print(f'  NET: ASKS dominate by {-diff:+,.0f} SOL ({-diff/(sz_b+sz_a)*100:.1f}%)')
print()

# CUMULATIVE FUEL
print('=' * 32 + ' CUMULATIVE FUEL ' + '=' * 32)
for label, pxs in [
    ('BIDS >= 76.00', [(p, q) for p, q in bids if p >= 76.00]),
    ('BIDS >= 75.85', [(p, q) for p, q in bids if p >= 75.85]),
    ('BIDS >= 75.71', [(p, q) for p, q in bids if p >= 75.71]),
    ('BIDS >= 75.50 (institutional)', [(p, q) for p, q in bids if p >= 75.50]),
    ('BIDS >= 75.00 (monster whale)', [(p, q) for p, q in bids if p >= 75.00]),
    ('BIDS >= 74.50 (deep)', [(p, q) for p, q in bids if p >= 74.50]),
    ('ASKS <= 75.80', [(p, q) for p, q in asks if p <= 75.80]),
    ('ASKS <= 75.90', [(p, q) for p, q in asks if p <= 75.90]),
    ('ASKS <= 76.00', [(p, q) for p, q in asks if p <= 76.00]),
    ('ASKS <= 76.26', [(p, q) for p, q in asks if p <= 76.26]),
    ('ASKS <= 76.48', [(p, q) for p, q in asks if p <= 76.48]),
    ('ASKS <= 76.66', [(p, q) for p, q in asks if p <= 76.66]),
]:
    cum = sum(q for _, q in pxs)
    cum_n = sum(p * q for p, q in pxs)
    print(f'  {label:<28} {cum:>9,.0f} SOL  ${cum_n:>14,.0f}')
print()

# ORDERFLOW
print('=' * 36 + ' ORDERFLOW ' + '=' * 36)
trades = g('/fapi/v1/trades', {'symbol': 'SOLUSDT', 'limit': 1000})
bq = sum(float(t['qty']) for t in trades if not t['isBuyerMaker'])
sq = sum(float(t['qty']) for t in trades if t['isBuyerMaker'])
print(f'  5M FLOW: buy {bq:>7,.0f} | sell {sq:>7,.0f} | NET {bq-sq:+>7,.0f} SOL')
big = [t for t in trades if float(t['qty']) >= 100]
bb = sum(float(t['qty']) for t in big if not t['isBuyerMaker'])
bs = sum(float(t['qty']) for t in big if t['isBuyerMaker'])
print(f'  LARGE (>=100): buy {bb:>6,.0f} | sell {bs:>6,.0f}')
blocks = [t for t in trades if float(t['qty']) >= 200]
bb_n = len([t for t in blocks if not t['isBuyerMaker']])
bs_n = len([t for t in blocks if t['isBuyerMaker']])
if blocks:
    print(f'  BLOCKS (>=200): {bb_n} buy / {bs_n} sell')
    for t in blocks[:6]:
        side = 'BUY ' if not t['isBuyerMaker'] else 'SELL'
        q = float(t['qty'])
        p = float(t['price'])
        print(f'    {side} {q:>7,.1f} @ ${p:.4f}  ${p*q:>10,.0f}')
print()

# TBR
tbs = g('/futures/data/takerlongshortRatio', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 6})
ratios = [float(b['buySellRatio']) for b in tbs]
tbrs = [r / (1 + r) * 100 for r in ratios]
print(f'  TBR last: {ratios[-1]:.2f} ({tbrs[-1]:.1f}%)')
print(f'  TBR 3-avg: {sum(ratios[-3:]) / 3:.2f} ({sum(tbrs[-3:]) / 3:.1f}%)')
print(f'  TBR series: {" -> ".join(f"{t:.0f}%" for t in tbrs)}')
print()

# OI
oi_h = g('/futures/data/openInterestHist', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 6})
ois = [float(b['sumOpenInterest']) for b in oi_h]
chg1 = (ois[-1] - ois[-2]) / ois[-2] * 100
chg3 = (ois[-1] - ois[-3]) / ois[-3] * 100
print(f'  OI: {ois[-1]:>10,.0f}  1-bar {chg1:+.3f}%  3-bar {chg3:+.3f}%')
print()

# Funding
fr = g('/fapi/v1/premiumIndex', {'symbol': 'SOLUSDT'})
f_bps = float(fr['lastFundingRate']) * 10000
print(f'  FUNDING: {f_bps:+.2f} bps')
print()

# Top trader / Global
top = g('/futures/data/topLongShortAccountRatio', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 2})
glb = g('/futures/data/globalLongShortAccountRatio', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 2})
print(f'  TOP TRADER LONG:   {float(top[-1]["longAccount"]) * 100:.2f}%')
print(f'  GLOBAL LONG:       {float(glb[-1]["longAccount"]) * 100:.2f}%')
print()

# Cross-asset funding
print('  CROSS-ASSET FUNDING:')
for s in ['BTCUSDT', 'ETHUSDT', 'AVAXUSDT', 'SOLUSDT']:
    f = g('/fapi/v1/premiumIndex', {'symbol': s})
    print(f'    {s}: {float(f["lastFundingRate"]) * 10000:+.2f} bps')