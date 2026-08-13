#!/usr/bin/env python3
"""Seller hedge wall map - identifies where shorts are positioned and hedging bids."""
import urllib.request, json
from datetime import datetime, timezone

BASE = 'https://fapi.binance.com'

def g(p, ps=None):
    u = BASE + p + (('?' + '&'.join(f'{k}={v}' for k, v in ps.items())) if ps else '')
    with urllib.request.urlopen(u, timeout=10) as r:
        return json.loads(r.read())

now = datetime.now(timezone.utc).strftime('%H:%M:%S')
print(f'[SELLER HEDGE WALL MAP | {now} UTC]')
print('=' * 72)

px = float(g('/fapi/v1/ticker/price', {'symbol': 'SOLUSDT'})['price'])
print(f'PRICE: ${px:.4f}')
print()

asks_data = g('/fapi/v1/depth', {'symbol': 'SOLUSDT', 'limit': 1000})
bids_data = g('/fapi/v1/depth', {'symbol': 'SOLUSDT', 'limit': 1000})
ask_list = sorted([[float(p), float(q)] for p, q in asks_data['asks']], key=lambda x: x[0])
bid_list = sorted([[float(p), float(q)] for p, q in bids_data['bids']], key=lambda x: x[0])

print('SELLER DEFENSE WALLS (asks above current px):')
for label, lo, hi in [
    ('75.85-75.95 (immediate)', 75.85, 75.95),
    ('76.00-76.15', 76.00, 76.15),
    ('76.20-76.30 (PRIMARY WALL)', 76.20, 76.30),
    ('76.40-76.55', 76.40, 76.55),
    ('76.60-76.70 (structural zone)', 76.60, 76.70),
    ('76.80-76.95 (NEW WALL)', 76.80, 76.95),
    ('77.00-77.10', 77.00, 77.10),
    ('77.40-77.60', 77.40, 77.60),
    ('77.90-78.10 (whale)', 77.90, 78.10),
]:
    sz = sum(q for p, q in ask_list if lo <= p <= hi)
    sz_n = sum(p * q for p, q in ask_list if lo <= p <= hi)
    if sz > 0:
        avg = sz_n / sz
        marker = '  << HEDGE WALL' if sz > 8000 else ''
        print(f'  $ {label:<35} {sz:>9,.0f} SOL  ${sz_n:>11,.0f}  avg ${avg:.3f}{marker}')
print()

print('SELLER HEDGING BIDS (where shorts park profit-taking bids):')
for label, lo, hi in [
    ('75.40-75.55 (institutional)', 75.40, 75.55),
    ('75.10-75.25', 75.10, 75.25),
    ('75.00-75.10 (MONSTER)', 75.00, 75.10),
    ('74.90-75.00', 74.90, 75.00),
    ('74.80-74.90', 74.80, 74.90),
    ('74.45-74.55 (deep institutional)', 74.45, 74.55),
    ('74.00-74.10', 74.00, 74.10),
    ('73.45-73.55 (WHALE)', 73.45, 73.55),
]:
    sz = sum(q for p, q in bid_list if lo <= p <= hi)
    sz_n = sum(p * q for p, q in bid_list if lo <= p <= hi)
    if sz > 0:
        avg = sz_n / sz
        marker = '  << SHORT TP ZONE' if sz > 15000 else ''
        print(f'  $ {label:<37} {sz:>9,.0f} SOL  ${sz_n:>11,.0f}  avg ${avg:.3f}{marker}')
print()

trades = g('/fapi/v1/trades', {'symbol': 'SOLUSDT', 'limit': 1000})
bq = sum(float(t['qty']) for t in trades if not t['isBuyerMaker'])
sq = sum(float(t['qty']) for t in trades if t['isBuyerMaker'])
big = [t for t in trades if float(t['qty']) >= 200]
bb = sum(float(t['qty']) for t in big if not t['isBuyerMaker'])
bs = sum(float(t['qty']) for t in big if t['isBuyerMaker'])
bb_n = len([t for t in big if not t['isBuyerMaker']])
bs_n = len([t for t in big if t['isBuyerMaker']])
print(f'5M FLOW: buy {bq:>7,.0f} | sell {sq:>7,.0f} | NET {bq-sq:+>7,.0f} SOL')
print(f'BLOCKS: {bb_n} buy / {bs_n} sell  (buy {bb:,.0f} / sell {bs:,.0f})')
print()

tbs = g('/futures/data/takerlongshortRatio', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 4})
ratios = [float(b['buySellRatio']) for b in tbs]
tbrs = [r / (1 + r) * 100 for r in ratios]
print(f'TBR: last {ratios[-1]:.2f} ({tbrs[-1]:.1f}%) | 3-avg {sum(ratios)/3:.2f} ({sum(tbrs)/3:.1f}%)')