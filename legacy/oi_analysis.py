"""
OI-focused proactiveness analysis for SOL/USDT.

Classifies each 5-min bar by OI direction × price direction × TBR,
aggregates to a proactiveness score, and judges if buyers are
proactive enough to break the seller walls.

All zones/thresholds are derived from live state — no hardcoded prices.
"""
import urllib.request, json
from datetime import datetime, timezone
from collections import defaultdict

BASE = 'https://fapi.binance.com'
SPOT = 'https://api.binance.com'
SYM = 'SOLUSDT'

# Thresholds (operational, not market-state)
TBR_BULL = 0.55         # TBR above this = taker buyers leading
TBR_BEAR = 0.45         # TBR below this = taker sellers leading
OI_FLAT_PCT = 0.05      # OI change within ±0.05% counts as "flat"
PX_FLAT_PCT = 0.05      # price change within ±0.05% counts as "flat"
LOOKBACK_BARS = 48      # 48 × 5min = 4h
HISTORY_FUNDING = 30    # funding rate events


def get(url, params=None):
    full = url
    if params:
        full += '?' + '&'.join('{}={}'.format(k, v) for k, v in params.items())
    with urllib.request.urlopen(full, timeout=10) as r:
        return json.loads(r.read())


# --- Live state for seller wall detection ---
ticker = get(BASE + '/fapi/v1/ticker/price', {'symbol': SYM})
px = float(ticker['price'])
server_time = get(BASE + '/fapi/v1/time')['serverTime']

print('=== OI PROACTIVENESS ANALYSIS — SOL/USDT ===')
print('Time: {} UTC'.format(datetime.fromtimestamp(server_time / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
print('Live price: {:.4f}'.format(px))
print()


# --- Pull supporting data ---
oi_hist = get(BASE + '/futures/data/openInterestHist', {'symbol': SYM, 'period': '5m', 'limit': LOOKBACK_BARS})
tbs = get(BASE + '/futures/data/takerlongshortRatio', {'symbol': SYM, 'period': '5m', 'limit': LOOKBACK_BARS})
klines_5m = get(BASE + '/fapi/v1/klines', {'symbol': SYM, 'interval': '5m', 'limit': LOOKBACK_BARS})
funding_hist = get(BASE + '/fapi/v1/fundingRate', {'symbol': SYM, 'limit': HISTORY_FUNDING})
top_pos = get(BASE + '/futures/data/topLongShortAccountRatio', {'symbol': SYM, 'period': '5m', 'limit': LOOKBACK_BARS})
glb_pos = get(BASE + '/futures/data/globalLongShortAccountRatio', {'symbol': SYM, 'period': '5m', 'limit': LOOKBACK_BARS})
book = get(BASE + '/fapi/v1/depth', {'symbol': SYM, 'limit': 100})
asks = [(float(p), float(q)) for p, q in book['asks']]
bids = [(float(p), float(q)) for p, q in book['bids']]


# ============================================================
# 1. AUTO-DERIVE SELLER WALLS (densest ask clusters above price)
# ============================================================
print('=' * 78)
print('1. SELLER WALL DETECTION (auto-derived from current orderbook)')
print('=' * 78)


def find_walls(asks, lo_dist, hi_dist, window):
    """Find densest rolling ask clusters within [px+lo_dist, px+hi_dist]."""
    lo, hi = px + lo_dist, px + hi_dist
    cands = []
    seen = set()
    for p, _ in asks:
        if not (lo <= p <= hi):
            continue
        c = round(p, 4)
        if c in seen:
            continue
        seen.add(c)
        qty = sum(q for ap, q in asks if ap - window <= p <= ap + window and ap == p)
        # Use the actual asks near the price
        cluster_qty = sum(q for ap, q in asks if p - window / 2 <= ap <= p + window / 2)
        cands.append((p, cluster_qty))
    cands.sort(key=lambda x: x[1], reverse=True)
    return cands[:5]


# Look for walls in two ranges: immediate (0.05-0.30 above) and POC zone (0.20-0.50 above)
near_walls = find_walls(asks, 0.05, 0.30, 0.05)
poc_walls = find_walls(asks, 0.20, 0.50, 0.05)

print()
print('  NEAR walls (0.05-0.30 above price):')
near_wall_total = 0
for p, q in near_walls[:3]:
    print('    @ {:.4f}  cluster qty: {:>10,.0f} SOL  ${:>12,.0f}'.format(p, q, p * q))
    near_wall_total += q
print('  Aggregate near-wall notional: ${:,.0f}'.format(near_wall_total * px))
print()

print('  POC-zone walls (0.20-0.50 above price):')
poc_wall_total = 0
for p, q in poc_walls[:3]:
    print('    @ {:.4f}  cluster qty: {:>10,.0f} SOL  ${:>12,.0f}'.format(p, q, p * q))
    poc_wall_total += q
print('  Aggregate POC-wall notional: ${:,.0f}'.format(poc_wall_total * px))
print()

total_wall_sol = near_wall_total + poc_wall_total
total_wall_notional = total_wall_sol * px
print('  TOTAL WALLS TO BREAK:')
print('    Sol: {:>10,.0f}'.format(total_wall_sol))
print('    $:   ${:>12,.0f}'.format(total_wall_notional))


# ============================================================
# 2. ALIGN ALL DATA INTO 5-MIN BUCKETS
# ============================================================
print()
print('=' * 78)
print('2. 5-MIN BAR CLASSIFICATION (last {} bars = {:.1f}h)'.format(LOOKBACK_BARS, LOOKBACK_BARS * 5 / 60))
print('=' * 78)


# Build maps by 5-min bucket
oi_map = {}  # bucket_ts -> {'oi': float, 'oi_val': float}
for r in oi_hist:
    try:
        ts = int(r['timestamp'])
        bucket = ts // 300000 * 300000
        oi_map[bucket] = {
            'oi': float(r['sumOpenInterest']),
            'val': float(r['sumOpenInterestValue']),
        }
    except Exception:
        pass

tbr_map = {}  # bucket_ts -> {'buy': float, 'sell': float, 'tbr': float}
for r in tbs:
    try:
        ts = int(r['timestamp'])
        bucket = ts // 300000 * 300000
        bv = float(r['buyVol'])
        sv = float(r['sellVol'])
        tbr = bv / max(bv + sv, 1)
        tbr_map[bucket] = {'buy': bv, 'sell': sv, 'tbr': tbr}
    except Exception:
        pass

px_map = {}  # bucket_ts -> {'open': float, 'close': float}
for k in klines_5m:
    bucket = int(k[0]) // 300000 * 300000
    px_map[bucket] = {'open': float(k[1]), 'close': float(k[4]), 'high': float(k[2]), 'low': float(k[3])}

# Build per-bar rows sorted by time
all_buckets = sorted(set(oi_map.keys()) | set(tbr_map.keys()) | set(px_map.keys()))
rows = []
prev_oi = None
prev_px_close = None
for b in all_buckets:
    oi_data = oi_map.get(b)
    tbr_data = tbr_map.get(b)
    px_data = px_map.get(b)
    if not (oi_data and tbr_data and px_data):
        continue
    oi = oi_data['oi']
    oi_val = oi_data['val']
    px_close = px_data['close']
    tbr = tbr_data['tbr']
    buy_vol = tbr_data['buy']
    sell_vol = tbr_data['sell']
    # OI delta
    if prev_oi is not None and prev_oi > 0:
        oi_delta_pct = (oi - prev_oi) / prev_oi * 100
    else:
        oi_delta_pct = 0.0
    # Price delta (vs previous bar close)
    if prev_px_close is not None and prev_px_close > 0:
        px_delta_pct = (px_close - prev_px_close) / prev_px_close * 100
    else:
        px_delta_pct = 0.0
    rows.append({
        'bucket': b,
        'oi': oi,
        'oi_val': oi_val,
        'oi_delta_pct': oi_delta_pct,
        'px_open': px_data['open'],
        'px_close': px_close,
        'px_delta_pct': px_delta_pct,
        'tbr': tbr,
        'buy_vol': buy_vol,
        'sell_vol': sell_vol,
        'net_vol': buy_vol - sell_vol,
    })
    prev_oi = oi
    prev_px_close = px_close


# Classify each row
def classify(r):
    oi_up = r['oi_delta_pct'] > OI_FLAT_PCT
    oi_dn = r['oi_delta_pct'] < -OI_FLAT_PCT
    oi_flat = not oi_up and not oi_dn
    px_up = r['px_delta_pct'] > PX_FLAT_PCT
    px_dn = r['px_delta_pct'] < -PX_FLAT_PCT
    px_flat = not px_up and not px_dn
    tbr_bull = r['tbr'] >= TBR_BULL
    tbr_bear = r['tbr'] <= TBR_BEAR

    if oi_up and px_up and tbr_bull:
        return 'AGGRESSIVE_LONG'
    if oi_up and px_up and tbr_bear:
        return 'DISTRIBUTION'
    if oi_up and px_dn and tbr_bull:
        return 'ABSORPTION'
    if oi_up and px_dn and tbr_bear:
        return 'SHORT_BUILD'
    if oi_dn and px_up:
        return 'SHORT_COVER'
    if oi_dn and px_dn:
        return 'LONG_UNWIND'
    if oi_flat and px_flat:
        return 'TWO_SIDED_FLAT'
    if oi_up and px_up:
        return 'NEUTRAL_LONG_BUILD'
    if oi_up and px_dn:
        return 'NEUTRAL_SHORT_BUILD'
    return 'NEUTRAL_MIXED'


# Proactiveness score per classification
PROACT_SCORE = {
    'AGGRESSIVE_LONG': +2,   # buyers proactive (best)
    'ABSORPTION': +1,        # buyers absorbing selling (good)
    'DISTRIBUTION': -1,      # sellers dominant, buyers passive
    'SHORT_BUILD': -2,       # sellers proactive (worst for buyers)
    'SHORT_COVER': 0,        # passive, not proactive buyers
    'LONG_UNWIND': -1,       # forced selling, not buyer strength
    'NEUTRAL_LONG_BUILD': +1,
    'NEUTRAL_SHORT_BUILD': -1,
    'TWO_SIDED_FLAT': 0,
    'NEUTRAL_MIXED': 0,
}


for r in rows:
    r['class'] = classify(r)
    r['proact'] = PROACT_SCORE[r['class']]


# Print bar-by-bar table
print()
print('  Bar     | OI chg   | Px chg   | TBR    | Buy       Sell       | Class')
print('  ' + '-' * 90)
for r in rows:
    ts_str = datetime.fromtimestamp(r['bucket'] / 1000, tz=timezone.utc).strftime('%H:%M')
    print('  {} | {:>+7.3f}% | {:>+7.3f}% | {:>5.1%} | {:>8,.0f}  {:>8,.0f} | {}'.format(
        ts_str, r['oi_delta_pct'], r['px_delta_pct'], r['tbr'],
        r['buy_vol'], r['sell_vol'], r['class']))


# ============================================================
# 3. HOURLY AGGREGATION + PROACTIVENESS SCORE
# ============================================================
print()
print('=' * 78)
print('3. HOURLY PROACTIVENESS AGGREGATION')
print('=' * 78)


hourly = defaultdict(lambda: {'score': 0, 'count': 0, 'classes': defaultdict(int),
                              'oi_net': 0.0, 'px_net': 0.0, 'tbr_sum': 0.0,
                              'buy_vol': 0.0, 'sell_vol': 0.0, 'first_bucket': None})

for r in rows:
    hr = (r['bucket'] // 3600000) * 3600000
    h = hourly[hr]
    h['score'] += r['proact']
    h['count'] += 1
    h['classes'][r['class']] += 1
    h['oi_net'] += r['oi_delta_pct']
    h['tbr_sum'] += r['tbr']
    h['buy_vol'] += r['buy_vol']
    h['sell_vol'] += r['sell_vol']
    if h['first_bucket'] is None:
        h['first_bucket'] = r['bucket']


print()
print('  Hour    | Score | Class breakdown (count)                                    | Net OI   | Avg TBR')
print('  ' + '-' * 100)
sorted_hours = sorted(hourly.keys())
hour_scores = []
for hr in sorted_hours:
    h = hourly[hr]
    avg_tbr = h['tbr_sum'] / h['count'] if h['count'] > 0 else 0
    hr_label = datetime.fromtimestamp(hr / 1000, tz=timezone.utc).strftime('%H:00')
    # Class summary
    classes_str = ' '.join('{}:{}'.format(k[:3], v) for k, v in sorted(h['classes'].items(), key=lambda x: -x[1]))
    print('  {} | {:>+5} | {:<55} | {:>+6.3f}% | {:>5.1%}'.format(
        hr_label, h['score'], classes_str, h['oi_net'], avg_tbr))
    hour_scores.append((hr_label, h['score'], avg_tbr))


# Compute trend (last 3 hours vs prior 3 hours)
print()
if len(hour_scores) >= 4:
    recent_3 = [s for _, s, _ in hour_scores[-3:]]
    prior_3 = [s for _, s, _ in hour_scores[-6:-3]] if len(hour_scores) >= 6 else [s for _, s, _ in hour_scores[:-3]]
    recent_avg = sum(recent_3) / len(recent_3) if recent_3 else 0
    prior_avg = sum(prior_3) / len(prior_3) if prior_3 else 0
    trend_delta = recent_avg - prior_avg
    print('  RECENT 3-HR AVG PROACTIVENESS: {:+.2f}'.format(recent_avg))
    print('  PRIOR 3-HR AVG PROACTIVENESS:  {:+.2f}'.format(prior_avg))
    print('  TREND:                         {:+.2f}  ({})'.format(
        trend_delta,
        'WARMING' if trend_delta > 1 else ('COOLING' if trend_delta < -1 else 'STABLE')))


# ============================================================
# 4. FLOW RATE vs WALL-BREAK REQUIREMENT
# ============================================================
print()
print('=' * 78)
print('4. WALL-BREAK MATH: Is current buy rate sufficient?')
print('=' * 78)
print()


# Current buy rate (last 15 min = 3 bars)
recent_bars = rows[-3:] if len(rows) >= 3 else rows
recent_buy = sum(r['buy_vol'] for r in recent_bars)
recent_minutes = len(recent_bars) * 5
recent_buy_rate = recent_buy / recent_minutes  # SOL/min

# Peak buy rate in last 4h
peak_buy = max(sum(r['buy_vol'] for r in rows[max(0, i - 3):i]) / 15
               for i in range(3, len(rows) + 1)) if len(rows) >= 3 else recent_buy_rate

# Required rate: clear walls in 15 min (reasonable session time)
target_minutes = 15
required_rate = total_wall_sol / target_minutes

print('  Current buy rate (last {} min): {:>8,.0f} SOL/min'.format(recent_minutes, recent_buy_rate))
print('  Peak buy rate (last 4h):        {:>8,.0f} SOL/min (15-min windows)'.format(peak_buy))
print('  Required to clear walls in {} min: {:>8,.0f} SOL/min'.format(target_minutes, required_rate))
print()
print('  Buyer adequacy ratio (current / required):  {:.2f}x'.format(recent_buy_rate / max(required_rate, 1)))
print('  Buyer adequacy ratio (peak / required):     {:.2f}x'.format(peak_buy / max(required_rate, 1)))
print()

# Time to clear at current vs peak rate
time_at_current = total_wall_sol / max(recent_buy_rate, 1)
time_at_peak = total_wall_sol / max(peak_buy, 1)
print('  Time to clear walls at current rate: {:.1f} min'.format(time_at_current))
print('  Time to clear walls at peak rate:    {:.1f} min'.format(time_at_peak))
print()

# Sustained burst required
if recent_buy_rate >= required_rate:
    burst_verdict = 'BUYERS CAN BREAK — current rate sufficient'
elif peak_buy >= required_rate:
    burst_verdict = 'BUYERS HAVE THE FIREPOWER (peak rate sufficient) — needs sustained aggression'
else:
    burst_verdict = 'BUYERS CANNOT BREAK walls via buy-flow alone — needs short squeeze cascade'
print('  VERDICT: {}'.format(burst_verdict))


# ============================================================
# 5. FUNDING TRAJECTORY (8 days of history)
# ============================================================
print()
print('=' * 78)
print('5. FUNDING RATE TRAJECTORY (last {} events)'.format(HISTORY_FUNDING))
print('=' * 78)
print()

funding_events = []
for r in funding_hist:
    try:
        ts = int(r['fundingTime'])
        rate = float(r['fundingRate'])
        funding_events.append({'ts': ts, 'rate': rate, 'bps': rate * 10000})
    except Exception:
        pass
funding_events.sort(key=lambda x: x['ts'])

print('  Recent funding events (newest first):')
for f in funding_events[-15:]:
    ts_str = datetime.fromtimestamp(f['ts'] / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
    bar = '+' if f['bps'] > 0 else ('-' if f['bps'] < 0 else ' ')
    print('    {}  {:>+7.2f} bps  {}'.format(ts_str, f['bps'], '█' * min(30, int(abs(f['bps']) * 3))))

# Funding trend
if len(funding_events) >= 4:
    recent_4 = funding_events[-4:]
    prior_4 = funding_events[-8:-4] if len(funding_events) >= 8 else funding_events[:-4]
    recent_avg = sum(f['bps'] for f in recent_4) / len(recent_4)
    prior_avg = sum(f['bps'] for f in prior_4) / len(prior_4) if prior_4 else recent_avg
    trend_bps = recent_avg - prior_avg
    print()
    print('  Recent 4 avg:   {:+.2f} bps'.format(recent_avg))
    print('  Prior 4 avg:    {:+.2f} bps'.format(prior_avg))
    print('  Funding trend:  {:+.2f} bps change'.format(trend_bps))
    if trend_bps > 0.5:
        fund_verdict = 'GOING POSITIVE — longs building (or shorts covering)'
    elif trend_bps < -0.5:
        fund_verdict = 'GOING MORE NEGATIVE — shorts building'
    else:
        fund_verdict = 'STABLE — no new directional positioning'
    print('  Verdict: {}'.format(fund_verdict))


# ============================================================
# 6. POSITIONING TREND (top trader + global)
# ============================================================
print()
print('=' * 78)
print('6. POSITIONING TREND (top trader + global)')
print('=' * 78)
print()

top_series = []
for r in top_pos:
    try:
        ts = int(r['timestamp'])
        top_series.append({'ts': ts, 'long_pct': float(r['longAccount']) * 100,
                           'ratio': float(r['longShortRatio'])})
    except Exception:
        pass
top_series.sort(key=lambda x: x['ts'])

glb_series = []
for r in glb_pos:
    try:
        ts = int(r['timestamp'])
        glb_series.append({'ts': ts, 'long_pct': float(r['longAccount']) * 100,
                           'ratio': float(r['longShortRatio'])})
    except Exception:
        pass
glb_series.sort(key=lambda x: x['ts'])

if top_series:
    print('  Top trader L/S (hourly avg):')
    top_hourly = defaultdict(list)
    for t in top_series:
        hr = (t['ts'] // 3600000) * 3600000
        top_hourly[hr].append(t['long_pct'])
    for hr in sorted(top_hourly.keys())[-8:]:
        avg = sum(top_hourly[hr]) / len(top_hourly[hr])
        ts_str = datetime.fromtimestamp(hr / 1000, tz=timezone.utc).strftime('%H:00')
        marker = ' <-- STILL CROWDED' if avg > 73 else (' MODERATE' if avg > 65 else ' BALANCED')
        print('    {}  long: {:>5.2f}%{}'.format(ts_str, avg, marker))

if glb_series:
    print()
    print('  Global L/S (hourly avg):')
    glb_hourly = defaultdict(list)
    for g in glb_series:
        hr = (g['ts'] // 3600000) * 3600000
        glb_hourly[hr].append(g['long_pct'])
    for hr in sorted(glb_hourly.keys())[-8:]:
        avg = sum(glb_hourly[hr]) / len(glb_hourly[hr])
        ts_str = datetime.fromtimestamp(hr / 3600000, tz=timezone.utc).strftime('%H:00')
        print('    {}  long: {:>5.2f}%'.format(ts_str, avg))


# ============================================================
# 7. OI-VALUE vs OI-CONTRACT divergence (institutional signal)
# ============================================================
print()
print('=' * 78)
print('7. OI NOTIONAL vs CONTRACTS — institutional positioning signal')
print('=' * 78)
print()

if len(rows) >= 4:
    print('  Bar     | OI contracts | OI notional ($)  | Implied $ per contract')
    print('  ' + '-' * 70)
    for r in rows[-8:]:
        ts_str = datetime.fromtimestamp(r['bucket'] / 1000, tz=timezone.utc).strftime('%H:%M')
        implied = r['oi_val'] / max(r['oi'], 1)
        print('  {} | {:>13,.0f} | ${:>14,.0f} | ${:>10.2f}'.format(
            ts_str, r['oi'], r['oi_val'], implied))

    # Trend in implied $/contract — if rising while OI flat = longs adding (positive)
    # if falling while OI rising = shorts adding (negative)
    first_implied = rows[-8]['oi_val'] / max(rows[-8]['oi'], 1) if len(rows) >= 8 else 0
    last_implied = rows[-1]['oi_val'] / max(rows[-1]['oi'], 1)
    implied_chg = (last_implied - first_implied) / first_implied * 100 if first_implied > 0 else 0
    print()
    print('  Implied $/contract change (last {} bars): {:+.2f}%'.format(min(8, len(rows)), implied_chg))
    # If $/contract is rising while OI is flat, longs are adding at higher prices (positive for bulls)


# ============================================================
# 8. FINAL VERDICT
# ============================================================
print()
print('=' * 78)
print('8. FINAL VERDICT — ARE BUYERS PROACTIVE ENOUGH?')
print('=' * 78)
print()

# Score components
score = 0
max_score = 10
reasons = []

# Component 1: proactiveness score trend
if len(hour_scores) >= 3:
    recent = sum(s for _, s, _ in hour_scores[-3:]) / 3
    if recent > 4:
        score += 3
        reasons.append('RECENT PROACTIVENESS HIGH (avg score {:+.1f})'.format(recent))
    elif recent > 1:
        score += 2
        reasons.append('Recent proactiveness positive ({:+.1f})'.format(recent))
    elif recent > -1:
        score += 1
        reasons.append('Recent proactiveness neutral ({:+.1f})'.format(recent))
    else:
        reasons.append('Recent proactiveness NEGATIVE ({:+.1f}) — buyers passive'.format(recent))

# Component 2: trend
if len(hour_scores) >= 4:
    if trend_delta > 1:
        score += 2
        reasons.append('Proactiveness WARMING ({:+.1f} trend)'.format(trend_delta))
    elif trend_delta < -1:
        reasons.append('Proactiveness COOLING ({:+.1f} trend)'.format(trend_delta))

# Component 3: adequacy ratio
if recent_buy_rate >= required_rate:
    score += 3
    reasons.append('Current buy rate EXCEEDS required ({:.2f}x)'.format(recent_buy_rate / max(required_rate, 1)))
elif peak_buy >= required_rate:
    score += 1
    reasons.append('Peak buy rate exceeds required ({:.2f}x peak) — not sustained'.format(peak_buy / max(required_rate, 1)))
else:
    reasons.append('Neither current NOR peak buy rate meets required ({:.2f}x current, {:.2f}x peak)'.format(
        recent_buy_rate / max(required_rate, 1), peak_buy / max(required_rate, 1)))

# Component 4: funding direction
if len(funding_events) >= 8:
    if trend_bps > 0.5:
        score += 1
        reasons.append('Funding going positive — longs re-engaging')
    elif trend_bps < -0.5:
        reasons.append('Funding going MORE negative — shorts building')
    else:
        reasons.append('Funding stable — no fresh commitment')

# Component 5: top trader crowding (high crowding = squeeze potential, neutral for proactiveness)
if top_series:
    cur_top = top_series[-1]['long_pct']
    if cur_top > 75:
        reasons.append('Top trader L/S {:.1f}% — extreme long crowding (squeeze fuel if break)'.format(cur_top))

# Verdict
if score >= 7:
    verdict = 'PROACTIVE — buyers have both firepower and intent to break walls'
    confidence = 'HIGH'
elif score >= 4:
    verdict = 'WARMING — buyers showing signs but not yet sustained'
    confidence = 'MEDIUM'
elif score >= 1:
    verdict = 'PASSIVE — buyers defending but not actively driving price'
    confidence = 'MEDIUM'
else:
    verdict = 'ABSENT — buyers not proactive; sellers in control'
    confidence = 'HIGH'

print('  Score: {}/{}'.format(score, max_score))
print('  Verdict: {} (confidence: {})'.format(verdict, confidence))
print()
print('  Reasoning:')
for r in reasons:
    print('    - {}'.format(r))
print()
print('  TRADE IMPLICATION:')
if 'ABSENT' in verdict or 'PASSIVE' in verdict:
    print('    Buyers lack firepower/intent to break walls via buy-flow alone.')
    print('    Walls likely hold unless external catalyst (short squeeze cascade).')
    print('    Bias: sellers favored at walls; long entries only on confirmed break.')
elif 'WARMING' in verdict:
    print('    Buyers building aggression but not yet at threshold.')
    print('    Watch next 2-3 bars: sustained TBR>55% + price through near walls = trigger.')
    print('    Bias: neutral, slight upside lean; trigger waiting.')
elif 'PROACTIVE' in verdict:
    print('    Buyers have enough weight to break walls within session.')
    print('    Trigger: TBR>60% sustained + OI rising.')
    print('    Bias: long; entry on pullbacks to keystone (72.92).')
