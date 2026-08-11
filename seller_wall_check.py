#!/usr/bin/env python3
"""
SOLUSDT seller-wall migration + buyer fuel capacity analysis.
Pulls live Binance Futures data with stdlib only.
"""
import json
import urllib.request
import urllib.parse
import time
from collections import defaultdict

BASE = "https://fapi.binance.com"
SYMBOL = "SOLUSDT"
TIMEOUT = 10


def get(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


# ---------- 1. LIVE PRICE + 500-LEVEL ORDERBOOK ----------
ticker = get("/fapi/v1/ticker/price", {"symbol": SYMBOL})
price = float(ticker["price"])
print(f"Live price: {price}")

depth = get("/fapi/v1/depth", {"symbol": SYMBOL, "limit": 500})
bids = [[float(p), float(q)] for p, q in depth["bids"]]
asks = [[float(p), float(q)] for p, q in depth["asks"]]

print(f"Top bid: {bids[0][0]} x {bids[0][1]}")
print(f"Top ask: {asks[0][0]} x {asks[0][1]}")
print(f"Bid levels: {len(bids)}  Ask levels: {len(asks)}")

# ---------- 2. SELLER WALL MIGRATION CHECK ----------
# Prior snapshot ask walls from 30 min ago (per task)
prior_ask_walls = {
    74.30: 12245,
    74.40: 48570,   # 0.10-window reading
    74.44: 10646,
    74.50: 10980,
    74.60: 7039,
    74.71: 10081,
}

# Helper: aggregate ask qty in a (lo, hi) window.
def ask_in_window(asks, lo, hi):
    return sum(q for p, q in asks if lo <= p <= hi)

def ask_exact_at(asks, level, tol=0.005):
    return sum(q for p, q in asks if abs(p - level) <= tol)

def bid_in_window(bids, lo, hi):
    return sum(q for p, q in bids if lo <= p <= hi)

def bid_exact_at(bids, level, tol=0.005):
    return sum(q for p, q in bids if abs(p - level) <= tol)

print("\n=================================================================")
print("SELLER WALL MIGRATION (current vs 30min ago)")
print("=================================================================")

# Compare in 0.02 windows so we capture clusters
window = 0.02
for level, prior in prior_ask_walls.items():
    # current exact level + 0.02 window
    now_exact = ask_exact_at(asks, level)
    now_window = ask_in_window(asks, level - window/2, level + window/2)
    delta_exact = now_exact - prior
    delta_window = now_window - prior

    # Direction: based on whether the wall has migrated closer to current price
    # (down) or further away (up). Use distance to current price.
    prior_dist = level - price if level > price else 0
    if now_window > prior * 1.15:
        direction = "BUILT UP"
    elif now_window < prior * 0.85:
        direction = "ERODED"
    else:
        direction = "STABLE"

    print(f"  Level {level:.2f}: prior={prior:>7,}  now_exact={now_exact:>7,}  "
          f"now±0.01={now_window:>7,}  Δ_w={delta_window:>+7,}  {direction}")

# Also check walls that may have migrated to other levels (74.20, 74.35, 74.55, 74.65, 74.75)
print("\n  Probe other levels (0.02 windows):")
for lvl in [74.20, 74.25, 74.35, 74.45, 74.55, 74.65, 74.75]:
    w = ask_in_window(asks, lvl - window/2, lvl + window/2)
    print(f"    {lvl:.2f}±0.01: {w:>7,}")

# ---------- 3. BUYER FUEL CALCULATION ----------
print("\n=================================================================")
print("BUYER FUEL CALCULATION")
print("=================================================================")

target_entry = 74.44
buyer_floor = 73.50

# Bid depth from current price DOWN to 73.50
bids_to_73_80 = sum(q for p, q in bids if 73.80 <= p <= price)
bids_to_73_70 = sum(q for p, q in bids if 73.70 <= p <= price)
bids_to_73_60 = sum(q for p, q in bids if 73.60 <= p <= price)
bids_to_73_50 = sum(q for p, q in bids if 73.50 <= p <= price)

# Ask depth from current price UP to 74.44
asks_up_to_74_30 = sum(q for p, q in asks if price <= p <= 74.30)
asks_up_to_74_40 = sum(q for p, q in asks if price <= p <= 74.40)
asks_up_to_74_44 = sum(q for p, q in asks if price <= p <= 74.44)

notional_bids = bids_to_73_50 * price
notional_asks = asks_up_to_74_44 * target_entry
ratio = bids_to_73_50 / asks_up_to_74_44 if asks_up_to_74_44 > 0 else float('inf')

print(f"  Bid depth px → 73.80: {bids_to_73_80:>9,.1f} SOL")
print(f"  Bid depth px → 73.70: {bids_to_73_70:>9,.1f} SOL")
print(f"  Bid depth px → 73.60: {bids_to_73_60:>9,.1f} SOL")
print(f"  Bid depth px → 73.50: {bids_to_73_50:>9,.1f} SOL  (~${notional_bids:,.0f})")
print()
print(f"  Ask depth px → 74.30: {asks_up_to_74_30:>9,.1f} SOL")
print(f"  Ask depth px → 74.40: {asks_up_to_74_40:>9,.1f} SOL")
print(f"  Ask depth px → 74.44: {asks_up_to_74_44:>9,.1f} SOL  (~${notional_asks:,.0f})")
print()
print(f"  Fuel ratio (bid_px→73.50 / ask_px→74.44): {ratio:.2f}x")

# Densest bid cluster
clusters = []
for lo_p in [73.50, 73.55, 73.60, 73.65, 73.70, 73.75, 73.80, 73.85]:
    q01 = bid_in_window(bids, lo_p, lo_p + 0.10)
    q02 = bid_in_window(bids, lo_p, lo_p + 0.20)
    clusters.append((lo_p, q01, q02))
clusters.sort(key=lambda x: x[1], reverse=True)
print("\n  Densest bid clusters:")
for lo, q01, q02 in clusters[:5]:
    print(f"    {lo:.2f}-{lo+0.10:.2f} (0.10 win): {q01:,.1f} SOL   "
          f"{lo:.2f}-{lo+0.20:.2f} (0.20 win): {q02:,.1f} SOL")

verdict = "BUYERS HAVE FUEL" if ratio > 1.0 else "SELLERS DOMINATE"
print(f"\n  VERDICT: {verdict}")

# ---------- 4. REACTION AT KEY BID LEVELS ----------
print("\n=================================================================")
print("REACTION AT KEY BID LEVELS")
print("=================================================================")

key_levels = [73.80, 73.70, 73.63, 73.60, 73.57, 73.50]
for lvl in key_levels:
    exact = bid_exact_at(bids, lvl)
    w02 = bid_in_window(bids, lvl - 0.01, lvl + 0.01)
    if w02 == 0:
        print(f"  {lvl:.2f}: NO BID")
        continue
    pct_1k = 1000 / w02 * 100
    pct_5k = 5000 / w02 * 100
    pct_10k = 10000 / w02 * 100
    print(f"  {lvl:.2f}: exact={exact:>7,.1f}  ±0.01={w02:>7,.1f}  "
          f"absorb 1k={pct_1k:5.1f}%  5k={pct_5k:5.1f}%  10k={pct_10k:5.1f}%")

# ---------- 5. SELLER AGGRESSION CHECK ----------
print("\n=================================================================")
print("SELLER AGGRESSION")
print("=================================================================")

# Recent trades
trades = get("/fapi/v1/trades", {"symbol": SYMBOL, "limit": 500})
now_ms = int(time.time() * 1000)
cutoff_ms = now_ms - 5 * 60 * 1000  # last 5 min

big_buys = 0
big_sells = 0
qty_buy = 0.0
qty_sell = 0.0
recent = []
for t in trades:
    qty = float(t["qty"])
    is_buyer_maker = t.get("isBuyerMaker", False)
    ts = t["time"]
    if ts < cutoff_ms:
        continue
    if qty >= 100:
        if is_buyer_maker:
            # buyer is maker = taker sold
            big_sells += 1
            qty_sell += qty
        else:
            # seller is maker = taker bought
            big_buys += 1
            qty_buy += qty
    recent.append((ts, qty, is_buyer_maker))

print(f"  Window: last 5 min ({(now_ms - cutoff_ms)//1000}s)")
print(f"  Large prints >= 100 SOL: {big_buys} buys (taker-buy) vs {big_sells} sells (taker-sell)")
print(f"  Taker-buy qty:  {qty_buy:,.1f} SOL")
print(f"  Taker-sell qty: {qty_sell:,.1f} SOL")
sell_ratio = qty_sell / qty_buy if qty_buy > 0 else float('inf')
print(f"  Sell/Buy qty ratio: {sell_ratio:.2f}")

# TBR
try:
    tbr = get("/futures/data/takerlongshortRatio", {
        "symbol": SYMBOL, "period": "5m", "limit": 12
    })
    tbr_last3 = []
    for row in tbr[-3:]:
        bv = float(row["buyVol"])
        sv = float(row["sellVol"])
        ratio_v = bv / sv if sv > 0 else 0
        tbr_last3.append((bv, sv, ratio_v))
        print(f"  TBR bar {row['timestamp']}: buyVol={bv:,.0f} sellVol={sv:,.0f} ratio={ratio_v:.3f}")
except Exception as e:
    print(f"  TBR error: {e}")
    tbr_last3 = []

# OI history
try:
    oi = get("/futures/data/openInterestHist", {
        "symbol": SYMBOL, "period": "5m", "limit": 12
    })
    oi_last3 = []
    for row in oi[-3:]:
        oi_v = float(row["sumOpenInterest"])
        oi_last3.append(oi_v)
    print(f"  OI last 3 bars: {oi_last3}")
    if len(oi_last3) >= 2:
        chg = (oi_last3[-1] - oi_last3[0]) / oi_last3[0] * 100
        print(f"  OI change over last 3 bars: {chg:+.2f}%")
except Exception as e:
    print(f"  OI error: {e}")

# Aggression classification
if sell_ratio > 1.5:
    agg = "HIGH"
elif sell_ratio > 0.8:
    agg = "MEDIUM"
else:
    agg = "LOW"
print(f"\n  Seller aggression level: {agg}")

# ---------- FINAL VERDICT ----------
print("\n=================================================================")
print("FINAL VERDICT")
print("=================================================================")

# Probability of buyers absorbing asks above:
# - if ratio > 1.3 and ask walls ERODED/STABLE: more likely
# - if ratio < 1.0 or walls BUILT UP: less likely
ask_walls_built = sum(1 for lvl, p in prior_ask_walls.items()
                      if ask_in_window(asks, lvl - window/2, lvl + window/2) > p * 1.15)
ask_walls_eroded = sum(1 for lvl, p in prior_ask_walls.items()
                       if ask_in_window(asks, lvl - window/2, lvl + window/2) < p * 0.85)

if ratio > 1.5 and ask_walls_built <= 1:
    prob = 0.65
elif ratio > 1.2 and ask_walls_built <= 2:
    prob = 0.55
elif ratio > 1.0:
    prob = 0.45
else:
    prob = 0.30

print(f"  Can buyers push 73.89 → 74.44? Fuel ratio {ratio:.2f}x, "
      f"{ask_walls_built} walls built, {ask_walls_eroded} eroded.")
print(f"  Probability of buyers absorbing asks above: {prob*100:.0f}%")

trap = "YES" if ask_walls_built >= 3 or ratio < 1.0 else "NO"
print(f"  Will sellers likely move walls DOWN to trap buyers? {trap}")

# ---------- ENTRY RECOMMENDATION ----------
print("\n=================================================================")
print("ENTRY RECOMMENDATION")
print("=================================================================")

# Identify best short entry: target a level with dense bids BELOW current price
# (price retraces into demand, then sellers wall off 74.40-74.44)
# Short entry best in the 74.40-74.44 ask wall zone if walls have held.
short_zone = "74.40-74.44 (wall zone)"
if ask_walls_eroded >= 3:
    short_zone = "74.30-74.44 (retest of 74.40 ask)"
    risk = "MEDIUM (walls eroded but bid fuel supports a push)"
elif ask_walls_built >= 3:
    short_zone = "74.44-74.50 (wall rebuilt stronger — short the rejection)"
    risk = "LOW (sellers reloaded)"
else:
    short_zone = "74.40-74.44 (current wall zone)"
    risk = "MEDIUM"

print(f"  Best short entry zone: {short_zone}")
print(f"  Reasoning: ask walls {('eroded' if ask_walls_eroded>=3 else 'stable/built')}, "
      f"bid fuel {ratio:.2f}x ratio, aggression {agg}.")
print(f"  Risk level: {risk}")
print(f"  Stop-loss hint: above the highest anchored ask wall "
      f"({max([(p, ask_in_window(asks, p-window/2, p+window/2)) for p in prior_ask_walls.keys()], key=lambda x: x[1])[0]:.2f}).")
print(f"  Take-profit hint: target the bid floor near {buyer_floor:.2f} "
      f"({bids_to_73_50:,.0f} SOL bid pool).")
