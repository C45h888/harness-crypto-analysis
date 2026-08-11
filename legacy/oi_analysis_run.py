import urllib.request, json
from datetime import datetime, timezone

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
print('PRICE: {:.4f}  {}'.format(px, datetime.now(timezone.utc).strftime('%H:%M:%S UTC')))
print()

# OI HISTORY (extensive)
print('=' * 80)
print('OI HISTORY (5m bars, last 12 hours)')
print('=' * 80)
oi_hist = get('/futures/data/openInterestHist', {'symbol': sym, 'period': '5m', 'limit': 144})
oi_now = get('/fapi/v1/openInterest', {'symbol': sym})

oi_series = []
for r in oi_hist:
    try:
        ts = int(r['timestamp'])
        oi = float(r['sumOpenInterest'])
        val = float(r['sumOpenInterestValue'])
        oi_series.append({'ts': ts, 'oi': oi, 'val': val})
    except: pass
oi_series.sort(key=lambda x: x['ts'])

print('{:<10} {:>14} {:>14} {:>8} {:>10}'.format('Time', 'OI(contracts)', 'OI_val($)', 'ChgQty', 'Chg%'))
print('-' * 72)
for i, r in enumerate(oi_series):
    chg_q = 0 if i == 0 else r['oi'] - oi_series[i-1]['oi']
    prev_oi = oi_series[i-1]['oi'] if i > 0 else r['oi']
    chg_p = 0 if i == 0 else chg_q / prev_oi * 100
    flag = ''
    if chg_p > 0.5: flag = ' <<< BUILD'
    elif chg_p > 0.2: flag = ' << build'
    elif chg_p < -0.5: flag = ' UNWIND >>>'
    elif chg_p < -0.2: flag = ' unwind >>'
    print('{:<10} {:>14,.0f} {:>14,.0f} {:>+8,.0f} {:>+10.3f}%{}'.format(
        datetime.fromtimestamp(r['ts']/1000, tz=timezone.utc).strftime('%H:%M'),
        r['oi'], r['val'], chg_q, chg_p, flag))
print()

# OI summary stats
oi_vals = [r['oi'] for r in oi_series]
print('OI STATS (last 12h):')
print('  Max OI:  {:>14,.0f}'.format(max(oi_vals)))
print('  Min OI:  {:>14,.0f}'.format(min(oi_vals)))
print('  Range:   {:>14,.0f} ({:+.2f}% of min)'.format(max(oi_vals)-min(oi_vals), (max(oi_vals)-min(oi_vals))/min(oi_vals)*100))
print('  First:   {:>14,.0f}'.format(oi_vals[0]))
print('  Last:    {:>14,.0f}'.format(oi_vals[-1]))
print('  Net 12h: {:>+14,.0f} ({:+.3f}%)'.format(oi_vals[-1]-oi_vals[0], (oi_vals[-1]-oi_vals[0])/oi_vals[0]*100))
print('  Current: {:>14,.0f}'.format(float(oi_now.get('openInterest', 0))))
print()

# Major OI events
print('=' * 80)
print('MAJOR OI EVENTS (|chg%| > 0.4%)')
print('=' * 80)
print('{:<10} {:>14} {:>10} {:>12}'.format('Time', 'OI', 'Chg%', 'Type'))
print('-' * 52)
events = []
for i, r in enumerate(oi_series):
    if i == 0: continue
    chg_p = (r['oi'] - oi_series[i-1]['oi']) / oi_series[i-1]['oi'] * 100
    if abs(chg_p) > 0.4:
        etype = 'BUILD' if chg_p > 0 else 'UNWIND'
        events.append({'ts': r['ts'], 'oi': r['oi'], 'chg': chg_p, 'type': etype})
        print('{:<10} {:>14,.0f} {:>+10.3f}% {:>12}'.format(
            datetime.fromtimestamp(r['ts']/1000, tz=timezone.utc).strftime('%H:%M'),
            r['oi'], chg_p, etype))
print()
print('Total major events: {}'.format(len(events)))
build_e = [e for e in events if e['type'] == 'BUILD']
unwind_e = [e for e in events if e['type'] == 'UNWIND']
print('Build events:  {}'.format(len(build_e)))
print('Unwind events: {}'.format(len(unwind_e)))
print()

# OI vs PRICE CORRELATION
print('=' * 80)
print('OI vs PRICE (last 2 hours, aligned)')
print('=' * 80)
klines = get('/fapi/v1/klines', {'symbol': sym, 'interval': '5m', 'limit': 24})
kline_dict = {}
for k in klines:
    ts = int(k[0])
    o = float(k[1])
    h = float(k[2])
    l = float(k[3])
    c = float(k[4])
    kline_dict[ts] = {'o': o, 'h': h, 'l': l, 'c': c}

print('{:<10} {:>8} {:>8} {:>14} {:>8}'.format('Time', 'Close', 'Chg%', 'OI', 'OI Chg%'))
print('-' * 56)
for r in oi_series[-24:]:
    ts = r['ts']
    if ts in kline_dict:
        c = kline_dict[ts]['c']
        idx = oi_series.index(r)
        prev_oi_v = oi_series[idx-1]['oi'] if idx > 0 else r['oi']
        chg_p = (r['oi'] - prev_oi_v) / prev_oi_v * 100
        prev_c = kline_dict[ts]['o']
        px_chg = (c - prev_c) / prev_c * 100
        flag = ''
        if chg_p > 0.3 and px_chg > 0: flag = ' LONG BUILD'
        elif chg_p > 0.3 and px_chg < 0: flag = ' SHORT BUILD'
        elif chg_p < -0.3 and px_chg > 0: flag = ' SHORT COVER'
        elif chg_p < -0.3 and px_chg < 0: flag = ' LONG LIQUID'
        print('{:<10} {:>8.3f} {:>+7.2f}% {:>14,.0f} {:>+7.3f}% {}'.format(
            datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%H:%M'),
            c, px_chg, r['oi'], chg_p, flag))
print()

# OI vs PRICE summary
print('=' * 80)
print('OI vs PRICE KEY SIGNALS')
print('=' * 80)
recent_oi = oi_series[-12:]
recent_chg_total = (recent_oi[-1]['oi'] - recent_oi[0]['oi']) / recent_oi[0]['oi'] * 100
print('Last 1h OI change: {:+.3f}%'.format(recent_chg_total))

if recent_oi[0]['ts'] in kline_dict and recent_oi[-1]['ts'] in kline_dict:
    start_px = kline_dict[recent_oi[0]['ts']]['o']
    end_px = kline_dict[recent_oi[-1]['ts']]['c']
    px_chg = (end_px - start_px) / start_px * 100
    print('Last 1h price change: {:+.2f}%'.format(px_chg))
    print()
    if recent_chg_total > 0.3 and px_chg < 0:
        print('>>> SHORT POSITIONS BUILDING (OI up + price down)')
    elif recent_chg_total > 0.3 and px_chg > 0:
        print('>>> LONG POSITIONS BUILDING (OI up + price up)')
    elif recent_chg_total < -0.3 and px_chg > 0:
        print('>>> SHORT COVERING (OI down + price up)')
    elif recent_chg_total < -0.3 and px_chg < 0:
        print('>>> LONG LIQUIDATING (OI down + price down)')
    else:
        print('>>> OI / price movement unclear')
print()

# Funding history
print('=' * 80)
print('FUNDING RATE HISTORY (last 24h)')
print('=' * 80)
fund_hist = get('/fapi/v1/fundingRate', {'symbol': sym, 'limit': 50})
print('{:<12} {:>12} {:>10}'.format('Time', 'Rate', 'Bps'))
print('-' * 38)
for r in fund_hist[:24]:
    ts = int(r['fundingTime'])
    rate = float(r['fundingRate'])
    print('{:<12} {:>12.6f} {:>+10.2f}'.format(
        datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
        rate, rate * 10000))
print()

rates = [float(r['fundingRate']) for r in fund_hist[:24]]
print('Funding stats (last 24h):')
print('  Current: {:+.4f}%  ({:+.2f} bps)'.format(rates[0], rates[0]*10000))
print('  Avg:     {:+.4f}%  ({:+.2f} bps)'.format(sum(rates)/len(rates), sum(rates)/len(rates)*10000))
print('  Max:     {:+.4f}%'.format(max(rates)))
print('  Min:     {:+.4f}%'.format(min(rates)))
print()

# OI composition
print('=' * 80)
print('OI COMPOSITION (top trader vs global)')
print('=' * 80)
top = get('/futures/data/topLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 36})
glb = get('/futures/data/globalLongShortAccountRatio', {'symbol': sym, 'period': '5m', 'limit': 36})

top_dict = {int(r['timestamp']): r for r in top}
glb_dict = {int(r['timestamp']): r for r in glb}

print('{:<10} {:>8} {:>8} {:>9} {:>9} {:>9}'.format(
    'Time', 'TopLong%', 'TopShrt%', 'GlbLong%', 'GlbShrt%', 'OI'))
print('-' * 60)
common_ts = sorted(set(top_dict.keys()) & set(glb_dict.keys()) & set(r['ts'] for r in oi_series))
for ts in common_ts[-12:]:
    tp = top_dict[ts]
    gp = glb_dict[ts]
    oi_r = next((r for r in oi_series if r['ts'] == ts), None)
    tlp = float(tp['longAccount']) * 100
    tsp_v = float(tp['shortAccount']) * 100
    glp = float(gp['longAccount']) * 100
    gsp = float(gp['shortAccount']) * 100
    oi_v = oi_r['oi'] if oi_r else 0
    print('{:<10} {:>8.2f} {:>8.2f} {:>9.2f} {:>9.2f} {:>9,.0f}'.format(
        datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%H:%M'),
        tlp, tsp_v, glp, gsp, oi_v))
print()

# OI-weighted positioning
print('=' * 80)
print('POSITIONING x OI (last hour) - implied contract counts')
print('=' * 80)
print('OI x long% = approximate long contracts')
print()
for ts in common_ts[-12:]:
    tp = top_dict[ts]
    gp = glb_dict[ts]
    oi_r = next((r for r in oi_series if r['ts'] == ts), None)
    if oi_r:
        tlp = float(tp['longAccount'])
        glp = float(gp['longAccount'])
        oi_v = oi_r['oi']
        top_long_contracts = oi_v * tlp
        top_short_contracts = oi_v * (1 - tlp)
        global_long_contracts = oi_v * glp
        global_short_contracts = oi_v * (1 - glp)
        print('{:<10} OI={:>10,.0f}  TopL={:>9,.0f}  TopS={:>9,.0f}  GlbL={:>9,.0f}  GlbS={:>9,.0f}'.format(
            datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%H:%M'),
            oi_v, top_long_contracts, top_short_contracts, global_long_contracts, global_short_contracts))
print()

# OI inflow/outflow windows
print('=' * 80)
print('OI INFLOW/OUTFLOW BY WINDOW')
print('=' * 80)
windows = [12, 24, 48, 72, 144]
for n in windows:
    if len(oi_series) >= n:
        first = oi_series[-n]['oi']
        last = oi_series[-1]['oi']
        chg = last - first
        chg_p = chg / first * 100
        window_label = '{}h'.format(n * 5 / 60)
        print('Last {}: OI {:+,.0f} ({:+.3f}%)'.format(window_label, chg, chg_p))
print()

# Current OI summary
print('=' * 80)
print('CURRENT OI SUMMARY')
print('=' * 80)
print()
print('Current OI:       {:,.0f} contracts'.format(float(oi_now.get('openInterest', 0))))
print('Current OI value: ${:,.0f}'.format(float(oi_now.get('openInterest', 0)) * px))
print()

last_oi = oi_series[-1]
prev_oi = oi_series[-2]
chg = last_oi['oi'] - prev_oi['oi']
chg_p = chg / prev_oi['oi'] * 100
print('Last 5m bar:')
print('  OI change: {:+,.0f} contracts ({:+.3f}%)'.format(chg, chg_p))
print('  Dollar chg: ${:+,.0f}'.format(chg * px))
print()

print('Last 12 bars (1h):')
for r in oi_series[-12:]:
    idx = oi_series.index(r)
    if idx > 0:
        prev = oi_series[idx-1]['oi']
        chg_p = (r['oi'] - prev) / prev * 100
        print('  {}  OI={:>11,.0f}  chg={:+8,.0f} ({:+.2f}%)'.format(
            datetime.fromtimestamp(r['ts']/1000, tz=timezone.utc).strftime('%H:%M'),
            r['oi'], r['oi'] - prev, chg_p))
print()

# Top trader trajectory
print('=' * 80)
print('TOP TRADER LONG% TRAJECTORY (last 2h)')
print('=' * 80)
top_series = [(int(r['timestamp']), float(r['longAccount'])) for r in top[-24:]]
top_series.sort()
prev = None
for ts, lp in top_series:
    chg = '' if prev is None else '{:+.4f}'.format(lp - prev)
    flag = ''
    if prev is not None and lp - prev > 0.02: flag = ' <<< ADD'
    elif prev is not None and lp - prev < -0.02: flag = ' >>> REDUCE'
    print('{}  Long%={:.2f}  chg={}{}'.format(
        datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%H:%M'),
        lp * 100, chg, flag))
    prev = lp
print()

# OI regime classification
print('=' * 80)
print('OI REGIME CLASSIFICATION')
print('=' * 80)
last_hour = oi_series[-12:]
last_hour_chg = (last_hour[-1]['oi'] - last_hour[0]['oi']) / last_hour[0]['oi'] * 100

kline_start = None
kline_end = None
for k in klines:
    if k[0] == last_hour[0]['ts']:
        kline_start = float(k[1])
    if k[0] == last_hour[-1]['ts']:
        kline_end = float(k[4])

if kline_start and kline_end:
    px_chg = (kline_end - kline_start) / kline_start * 100
    print('1h summary:')
    print('  OI change:    {:+.3f}%'.format(last_hour_chg))
    print('  Price change: {:+.2f}%'.format(px_chg))
    print()
    if last_hour_chg > 0.3 and px_chg < -0.3:
        print('  >>> REGIME: SHORT BUILDING (new shorts entering)')
    elif last_hour_chg > 0.3 and px_chg > 0.3:
        print('  >>> REGIME: LONG BUILDING (new longs entering)')
    elif last_hour_chg < -0.3 and px_chg > 0.3:
        print('  >>> REGIME: SHORT COVERING (shorts closing)')
    elif last_hour_chg < -0.3 and px_chg < -0.3:
        print('  >>> REGIME: LONG LIQUIDATION (longs closing)')
    elif abs(last_hour_chg) < 0.3:
        print('  >>> REGIME: RANGING (no clear positioning)')
    else:
        print('  >>> REGIME: DIVERGENCE (OI and price disagree)')

current_fr = rates[0]
if current_fr > 0.0005:
    print('  Funding: LONG-CROWDED (longs paying >5 bps)')
elif current_fr > 0.0001:
    print('  Funding: mildly long-crowded')
elif current_fr < -0.0005:
    print('  Funding: SHORT-CROWDED (shorts paying >5 bps)')
elif current_fr < -0.0001:
    print('  Funding: mildly short-crowded')
else:
    print('  Funding: NEUTRAL')
print()