#!/usr/bin/env python3
"""Max support / resistance band + DELTA variable.
Splits the orderbook in 0.10 increments across ±0.75 of px (1.5 range),
identifies densest bid/ask bands, pairs with TBR flow to compute signed delta."""
import urllib.request, json
from datetime import datetime, timezone

BASE = 'https://fapi.binance.com'

def g(p, ps=None):
    u = BASE + p + (('?' + '&'.join(f'{k}={v}' for k, v in ps.items())) if ps else '')
    with urllib.request.urlopen(u, timeout=10) as r:
        return json.loads(r.read())

now = datetime.now(timezone.utc).strftime('%H:%M:%S')
print(f'[MAX SUPPORT/RESISTANCE + DELTA | {now} UTC]')
print('=' * 72)

px = float(g('/fapi/v1/ticker/price', {'symbol': 'SOLUSDT'})['price'])
print(f'PRICE: ${px:.4f}    RANGE: ${px - 0.75:.2f} - ${px + 0.75:.2f}')
print()

# Orderbook
book = g('/fapi/v1/depth', {'symbol': 'SOLUSDT', 'limit': 1000})
bids = sorted([[float(p), float(q)] for p, q in book['bids']], key=lambda x: -x[1])
asks = sorted([[float(p), float(q)] for p, q in book['asks']], key=lambda x: -x[1])

# Build 0.10 bands
lo_band = int((px - 0.75) * 10) / 10
hi_band = int((px + 0.75) * 10) / 10 + 0.10

print('BAND-BY-BAND WALL DENSITY (0.10 increments):')
print(f"{'Band':<10} {'Bids (SOL)':>12} {'Asks (SOL)':>12} {'Delta':>10} {'Bias':>10}")
print('-' * 60)

band_data = []
band = lo_band
while band < hi_band:
    bid_qty = sum(q for p, q in bids if band <= p < band + 0.10)
    ask_qty = sum(q for p, q in asks if band <= p < band + 0.10)
    diff = bid_qty - ask_qty
    bias = 'BID HEAVY' if diff > 500 else ('ASK HEAVY' if diff < -500 else 'BALANCED')
    marker = ' ← CURRENT' if abs(band - px) < 0.05 else ''
    print(f'  ${band:.2f}    {bid_qty:>10,.0f}  {ask_qty:>10,.0f}  {diff:+>9,.0f}  {bias}{marker}')
    band_data.append((band, bid_qty, ask_qty, diff))
    band += 0.10

print()

# Max support / resistance
max_support = max(band_data, key=lambda x: x[1])
max_resist = max(band_data, key=lambda x: x[2])
print('MAX SUPPORT BAND: $%.2f-%s   bids %s SOL ($%s)' % (
    max_support[0], f'{max_support[0] + 0.10:.2f}',
    f'{max_support[1]:,.0f}',
    f'{max_support[1] * max_support[0]:,.0f}'
))
print('MAX RESISTANCE BAND: $%.2f-%s   asks %s SOL ($%s)' % (
    max_resist[0], f'{max_resist[0] + 0.10:.2f}',
    f'{max_resist[2]:,.0f}',
    f'{max_resist[2] * max_resist[0]:,.0f}'
))
print()

# Cumulative fuel in bands
print('CUMULATIVE FUEL (in 1.5 range):')
total_bids = sum(b for _, b, _, _ in band_data)
total_asks = sum(a for _, _, a, _ in band_data)
print(f'  BIDS total: {total_bids:,.0f} SOL  ${total_bids * px:,.0f}')
print(f'  ASKS total: {total_asks:,.0f} SOL  ${total_asks * px:,.0f}')
print()

# Orderflow
trades = g('/fapi/v1/trades', {'symbol': 'SOLUSDT', 'limit': 1000})
bq = sum(float(t['qty']) for t in trades if not t['isBuyerMaker'])
sq = sum(float(t['qty']) for t in trades if t['isBuyerMaker'])
tbs = g('/futures/data/takerlongshortRatio', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 4})
ratios = [float(b['buySellRatio']) for b in tbs]
tbr_last = ratios[-1] / (1 + ratios[-1]) * 100
tbr_3avg = (sum(ratios) / len(ratios)) / (1 + sum(ratios) / len(ratios)) * 100

print('ORDERFLOW:')
print(f'  5M flow: buy {bq:,.0f} | sell {sq:,.0f} | NET {bq - sq:+,.0f} SOL')
print(f'  TBR last: {tbr_last:.1f}%   3-avg: {tbr_3avg:.1f}%')
print()

# === DELTA VARIABLE ===
# delta = wall_imbalance * flow_alignment
# wall_imbalance = (bids - asks) / (bids + asks)  -> -1 to +1
# flow_alignment = (tbr - 50) / 50  -> -1 to +1
# Combined: delta = wall_imbalance + flow_alignment (each -1 to +1, total -2 to +2)
print('=' * 36 + ' DELTA VARIABLE ' + '=' * 36)

wall_imbalance = (total_bids - total_asks) / (total_bids + total_asks) if (total_bids + total_asks) else 0
flow_alignment = (tbr_last - 50) / 50  # -1 = full sell, +1 = full buy
delta = wall_imbalance + flow_alignment

print(f'  Wall imbalance:    {wall_imbalance:+.3f}  (positive = bid heavy, negative = ask heavy)')
print(f'  Flow alignment:    {flow_alignment:+.3f}  (positive = buyers winning, negative = sellers)')
print(f'  ─────────────────────────────')
print(f'  DELTA:             {delta:+.3f}')
print()
if delta > 0.5:
    print('  → DELTA STRONGLY POSITIVE = buyers in control of range')
elif delta > 0:
    print('  → DELTA POSITIVE = buyers slightly favored')
elif delta > -0.5:
    print('  → DELTA NEGATIVE = sellers slightly favored')
else:
    print('  → DELTA STRONGLY NEGATIVE = sellers in control of range')

print()
print('=' * 30 + ' BAND DELTAS (heatmap) ' + '=' * 30)
print(f"{'Band':<10} {'Wall Δ':>10} {'Status':>20}")
print('-' * 45)
for band, bid_q, ask_q, diff in band_data:
    if ask_q == 0 and bid_q == 0:
        status = 'EMPTY'
    elif diff > 3000:
        status = 'STRONG SUPPORT'
    elif diff > 1000:
        status = 'support'
    elif diff < -3000:
        status = 'STRONG RESIST'
    elif diff < -1000:
        status = 'resist'
    else:
        status = 'neutral'
    print(f'  ${band:.2f}    {diff:+>9,.0f}  {status:>20}')