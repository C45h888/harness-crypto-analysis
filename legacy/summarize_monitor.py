"""
Summarize a 15-minute monitor session saved by /Users/kamii/Documents/crypto-ai-anal/run_monitor.py.

Reads monitor_log.json and prints a per-pair aggregate report.
"""

import json
import statistics
from datetime import datetime

LOG = '/Users/kamii/Documents/crypto-ai-anal/monitor_log.json'

PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

with open(LOG) as f:
    data = json.load(f)
log = data['log']

def to_float(x):
    if x is None: return None
    try: return float(x)
    except: return None

def series(sym):
    out = []
    for t in log:
        s = t['snapshots'].get(sym, {})
        out.append({
            'ts':   t['tick_ts'],
            'time': datetime.fromtimestamp(t['tick_ts']/1000).strftime('%H:%M:%S'),
            'px':       to_float(s.get('price_fut')),
            'spot_b':   to_float(s.get('spot_15m_buy_share')),
            'fut_b':    to_float(s.get('fut_15m_buy_share')),
            'spot_obi': to_float(s.get('spot_obi_top20')),
            'fut_obi':  to_float(s.get('fut_obi_top20')),
            'fund':     to_float(s.get('funding_rate')),
            'spot_pct': to_float(s.get('spot_pct_of_turnover')),
        })
    return out

for sym in PAIRS:
    s = series(sym)
    print(f"\n========== {sym} ==========")
    print(f"{'TIME':>8} {'PX':>10} {'spotB%':>7} {'futB%':>7} {'sOBI':>7} {'fOBI':>7}")
    for r in s:
        px = r['px'] or 0
        sb = (r['spot_b'] or 0) * 100
        fb = (r['fut_b'] or 0) * 100
        sobi = r['spot_obi'] or 0
        fobi = r['fut_obi'] or 0
        print(f"{r['time']:>8} {px:>10.2f} {sb:>6.1f}% {fb:>6.1f}% {sobi:>+7.3f} {fobi:>+7.3f}")