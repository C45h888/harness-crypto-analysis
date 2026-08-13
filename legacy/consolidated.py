#!/usr/bin/env python3
"""CONSOLIDATED EXECUTION VIEW: bid walls + ask walls + delta + flow.
The final pre-trade dashboard. One snapshot = everything."""
import urllib.request, json
from datetime import datetime, timezone

BASE = 'https://fapi.binance.com'

def g(p, ps=None):
    u = BASE + p + (('?' + '&'.join(f'{k}={v}' for k, v in ps.items())) if ps else '')
    with urllib.request.urlopen(u, timeout=10) as r:
        return json.loads(r.read())

now = datetime.now(timezone.utc).strftime('%H:%M:%S')
print(f'[CONSOLIDATED EXECUTION VIEW | {now} UTC]')
print('=' * 78)

px = float(g('/fapi/v1/ticker/price', {'symbol': 'SOLUSDT'})['price'])
print(f'PRICE: ${px:.4f}')
print()

book = g('/fapi/v1/depth', {'symbol': 'SOLUSDT', 'limit': 1000})
bids = sorted([[float(p), float(q)] for p, q in book['bids']], key=lambda x: -x[1])
asks = sorted([[float(p), float(q)] for p, q in book['asks']], key=lambda x: -x[1])

# ============================================================
# PART 1: BID WALLS (TP / SUPPORT ZONES BELOW)
# ============================================================
print('=' * 30 + ' BID WALLS (SUPPORT BELOW) ' + '=' * 31)
print(f"{'Price':<8} {'Size':>10} {'Notional':>13} {'Role':<35}")
print('-' * 70)
for label, lo, hi, role in [
    ('75.71', 75.705, 75.715, 'immediate bid'),
    ('75.62', 75.615, 75.625, 'TP2 zone'),
    ('75.55', 75.545, 75.555, 'buyer zone'),
    ('75.50', 75.495, 75.505, 'INSTITUTIONAL FLOOR'),
    ('75.42', 75.415, 75.425, 'cluster'),
    ('75.30', 75.295, 75.305, 'cluster'),
    ('75.20', 75.195, 75.205, 'cluster'),
    ('75.13', 75.125, 75.135, 'cluster'),
    ('75.08', 75.075, 75.085, 'cluster'),
    ('75.05', 75.045, 75.055, 'cluster'),
    ('75.00', 74.995, 75.005, 'MONSTER WHALE FLOOR'),
    ('74.80', 74.795, 74.805, 'institutional'),
    ('74.50', 74.495, 74.505, 'DEEP INSTITUTIONAL'),
    ('74.00', 73.995, 74.005, 'major step'),
    ('73.50', 73.495, 73.505, 'WHALE FLOOR (final)'),
]:
    sz = sum(q for p, q in bids if lo <= p <= hi)
    sz_n = sum(p * q for p, q in bids if lo <= p <= hi)
    marker = '  << MAX' if sz > 20000 else ('  << INST' if sz > 8000 else '')
    print(f'  ${label:<7} {sz:>8,.0f} SOL  ${sz_n:>11,.0f}  {role:<35}{marker}')

# ============================================================
# PART 2: ASK WALLS (RESISTANCE / RISK ZONES ABOVE)
# ============================================================
print()
print('=' * 30 + ' ASK WALLS (RESISTANCE ABOVE) ' + '=' * 30)
print(f"{'Price':<8} {'Size':>10} {'Notional':>13} {'Role':<35}")
print('-' * 70)
for label, lo, hi, role in [
    ('75.85', 75.845, 75.855, 'immediate overhead'),
    ('75.95', 75.945, 75.955, 'squeeze trigger'),
    ('76.00', 75.995, 76.005, 'round'),
    ('76.10', 76.095, 76.105, 'institutional'),
    ('76.20', 76.195, 76.205, 'MAX RESISTANCE'),
    ('76.30', 76.295, 76.305, 'MAX RESISTANCE'),
    ('76.48', 76.475, 76.485, 'lower seller wall'),
    ('76.50', 76.495, 76.505, 'supply'),
    ('76.59', 76.585, 76.595, 'supply'),
    ('76.66', 76.655, 76.665, 'structural (BROKEN)'),
    ('76.85', 76.845, 76.855, 'NEW WALL (repositioned)'),
    ('77.00', 76.995, 77.005, 'round'),
    ('77.50', 77.495, 77.505, 'supply'),
    ('78.00', 77.995, 78.005, 'whale'),
]:
    sz = sum(q for p, q in asks if lo <= p <= hi)
    sz_n = sum(p * q for p, q in asks if lo <= p <= hi)
    marker = '  << MAX' if sz > 20000 else ('  << INST' if sz > 8000 else '')
    print(f'  ${label:<7} {sz:>8,.0f} SOL  ${sz_n:>11,.0f}  {role:<35}{marker}')

# ============================================================
# PART 3: DELTA VARIABLE (signed -2 to +2)
# ============================================================
print()
print('=' * 33 + ' DELTA VARIABLE ' + '=' * 34)
trades = g('/fapi/v1/trades', {'symbol': 'SOLUSDT', 'limit': 1000})
bq = sum(float(t['qty']) for t in trades if not t['isBuyerMaker'])
sq = sum(float(t['qty']) for t in trades if t['isBuyerMaker'])
tbs = g('/futures/data/takerlongshortRatio', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 4})
ratios = [float(b['buySellRatio']) for b in tbs]
tbr_last = ratios[-1] / (1 + ratios[-1]) * 100

# 1.5 range
lo_band = int((px - 0.75) * 10) / 10
hi_band = int((px + 0.75) * 10) / 10 + 0.10
total_bids = 0
total_asks = 0
band = lo_band
while band < hi_band:
    total_bids += sum(q for p, q in bids if band <= p < band + 0.10)
    total_asks += sum(q for p, q in asks if band <= p < band + 0.10)
    band += 0.10

wall_imbalance = (total_bids - total_asks) / (total_bids + total_asks)
flow_alignment = (tbr_last - 50) / 50
delta = wall_imbalance + flow_alignment

print(f'  Wall imbalance:  {wall_imbalance:+.3f}  ({total_bids:,.0f} bids vs {total_asks:,.0f} asks)')
print(f'  Flow alignment:  {flow_alignment:+.3f}  (TBR {tbr_last:.1f}%)')
print(f'  ─' * 22)
delta_bar = '█' * int(abs(delta) * 20) if abs(delta) > 0 else ''
delta_sign = '+' if delta > 0 else ('-' if delta < 0 else ' ')
print(f'  DELTA: {delta:+.3f}  [{delta_sign}{delta_bar:<20}]')
if delta > 0.5:
    state = 'BUYERS IN CONTROL'
elif delta > 0:
    state = 'BUYERS SLIGHTLY FAVORED'
elif delta > -0.5:
    state = 'SELLERS SLIGHTLY FAVORED'
else:
    state = 'SELLERS IN CONTROL'
print(f'  STATE: {state}')

# ============================================================
# PART 4: SUPPORT/RESISTANCE & KEY ZONES
# ============================================================
print()
print('=' * 31 + ' KEY ZONES ' + '=' * 36)

# Find max bid and ask bands
max_bid_band = max_bid_size = 0
max_ask_band = max_ask_size = 0
band = lo_band
while band < hi_band:
    b = sum(q for p, q in bids if band <= p < band + 0.10)
    a = sum(q for p, q in asks if band <= p < band + 0.10)
    if b > max_bid_size:
        max_bid_size = b
        max_bid_band = band
    if a > max_ask_size:
        max_ask_size = a
        max_ask_band = band
    band += 0.10

print(f'  MAX SUPPORT BAND:     ${max_bid_band:.2f}-${max_bid_band + 0.10:.2f}  ({max_bid_size:,.0f} SOL)')
print(f'  MAX RESISTANCE BAND:  ${max_ask_band:.2f}-${max_ask_band + 0.10:.2f}  ({max_ask_size:,.0f} SOL)')
print(f'  Distance to max support:     ${px - max_bid_band:+.2f} ({(px - max_bid_band) / px * 100:+.2f}%)')
print(f'  Distance to max resistance:  ${max_ask_band - px:+.2f} ({(max_ask_band - px) / px * 100:+.2f}%)')

# ============================================================
# PART 5: EXECUTION ZONES
# ============================================================
print()
print('=' * 30 + ' EXECUTION ZONES ' + '=' * 34)
print()
print('  ENTRY 1: BUY max support (75.70-75.80 zone)')
print(f'    Entry: {max_bid_band + 0.02:.2f}')
print(f'    Stop:  {max_bid_band - 0.20:.2f} (below max support)')
print(f'    Target 1: 75.90 (squeeze trigger)')
print(f'    Target 2: 76.10 (institutional)')
print(f'    Target 3: 76.20 (max resistance - exit here)')
print(f'    Risk: 0.22 | Reward: 0.18 / 0.40 / 0.50 = 0.8R to 2.3R')
print()
print(f'  ENTRY 2: SELL max resistance ({max_ask_band:.2f}-{max_ask_band + 0.10:.2f} zone)')
print(f'    Entry: {max_ask_band - 0.02:.2f}')
print(f'    Stop:  {max_ask_band + 0.30:.2f} (above max resistance)')
print(f'    Target 1: 75.80 (immediate bid)')
print(f'    Target 2: 75.70 (max support - exit here)')
print(f'    Target 3: 75.50 (institutional)')
print(f'    Risk: 0.32 | Reward: 0.42 / 0.52 / 0.72 = 1.3R to 2.3R')
print()
print('  TRIGGER FLIPS:')
print(f'    DELTA flips positive:  buyers break 75.90 (54k ask wall) → long 75.92')
print(f'    DELTA flips negative:  sellers break 75.70 (54k bid wall) → short 75.65')

# ============================================================
# PART 6: CONTEXT
# ============================================================
print()
print('=' * 35 + ' CONTEXT ' + '=' * 37)
fr = g('/fapi/v1/premiumIndex', {'symbol': 'SOLUSDT'})
f_bps = float(fr['lastFundingRate']) * 10000
top = g('/futures/data/topLongShortAccountRatio', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 2})
oi_h = g('/futures/data/openInterestHist', {'symbol': 'SOLUSDT', 'period': '5m', 'limit': 4})
ois = [float(b['sumOpenInterest']) for b in oi_h]
chg3 = (ois[-1] - ois[-3]) / ois[-3] * 100

print(f'  Funding: {f_bps:+.3f} bps')
print(f'  Top trader long: {float(top[-1]["longAccount"]) * 100:.2f}%')
print(f'  OI 3-bar: {chg3:+.3f}%')
print(f'  5M net flow: {bq - sq:+,.0f} SOL')
print(f'  TBR last: {tbr_last:.1f}%')