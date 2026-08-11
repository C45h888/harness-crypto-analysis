import urllib.request, json
from datetime import datetime, timezone
from collections import defaultdict

BINANCE_SPOT = 'https://api.binance.com'
BINANCE_FUT = 'https://fapi.binance.com'
COINGECKO = 'https://api.coingecko.com/api/v3'

def get(url, params=None):
    full = url
    if params:
        full += '?' + '&'.join('{}={}'.format(k, v) for k, v in params.items())
    req = urllib.request.Request(full, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


print('=== MACRO CONTEXT (last 24h) ===')
print('Time: {} UTC'.format(datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
print()

# --- CoinGecko global market data (public, no key) ---
try:
    g = get(COINGECKO + '/global')
    data = g['data']
    print('=== COINGECKO GLOBAL ===')
    print('  Total market cap:  ${:>15,.0f}'.format(data['total_market_cap']['usd']))
    print('  Total 24h vol:     ${:>15,.0f}'.format(data['total_volume']['usd']))
    print('  BTC dominance:     {:>10.2f}%'.format(data['market_cap_percentage']['btc']))
    print('  ETH dominance:     {:>10.2f}%'.format(data['market_cap_percentage']['eth']))
    sol_dom = data['market_cap_percentage'].get('sol', None)
    if sol_dom is not None:
        print('  SOL dominance:     {:>10.2f}%'.format(sol_dom))
    print('  Market cap chg 24h:{:>+10.2f}%'.format(
        data['market_cap_change_percentage_24h_usd']))
    print()
except Exception as e:
    print('CoinGecko global failed: {}'.format(e))
    print()

# --- Binance spot 24h tickers for relative strength ---
symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT']
print('=== 24H RELATIVE STRENGTH (Binance spot) ===')
print('{:<10}  {:>10}  {:>10}  {:>14}  {:>14}  {:>10}'.format(
    'Symbol', 'Last', 'Chg%', 'Volume', 'QuoteVol', 'RS_Rank'))
print('-' * 80)
rows = []
for sym in symbols:
    try:
        t = get(BINANCE_SPOT + '/api/v3/ticker/24hr', {'symbol': sym})
        last = float(t['lastPrice'])
        chg = float(t['priceChangePercent'])
        vol = float(t['volume'])
        qvol = float(t['quoteVolume'])
        rows.append({'sym': sym, 'last': last, 'chg': chg, 'vol': vol, 'qvol': qvol})
    except Exception as e:
        continue

# Rank by 24h change
rows.sort(key=lambda x: x['chg'], reverse=True)
for i, r in enumerate(rows):
    rank_marker = ' <-- TOP' if i == 0 else (' BOTTOM' if i == len(rows) - 1 else '')
    print('{:<10}  {:>10.4f}  {:>+9.2f}%  {:>14,.0f}  ${:>13,.0f}  {:>3}{}'.format(
        r['sym'], r['last'], r['chg'], r['vol'], r['qvol'], len(rows) - i, rank_marker))
print()

# --- 1h returns for beta calculation ---
print('=== HOURLY RETURNS (last 24 bars) ===')
def hourly_returns(sym, market='fut'):
    base = BINANCE_FUT if market == 'fut' else BINANCE_SPOT
    path = '/fapi/v1/klines' if market == 'fut' else '/api/v3/klines'
    kl = get(base + path, {'symbol': sym, 'interval': '1h', 'limit': 25})
    rets = []
    for k in kl:
        o, c = float(k[1]), float(k[4])
        rets.append((c - o) / o)
    return rets

sol_rets = hourly_returns('SOLUSDT', 'fut')
btc_rets = hourly_returns('BTCUSDT', 'fut')
eth_rets = hourly_returns('ETHUSDT', 'fut')

def correlation(a, b):
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = sum((a[i] - ma) ** 2 for i in range(n)) ** 0.5
    db = sum((b[i] - mb) ** 2 for i in range(n)) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)

def beta(a, b):
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / n
    var_b = sum((b[i] - mb) ** 2 for i in range(n)) / n
    if var_b == 0:
        return 0.0
    return cov / var_b

corr_sol_btc = correlation(sol_rets, btc_rets)
corr_sol_eth = correlation(sol_rets, eth_rets)
beta_sol_btc = beta(sol_rets, btc_rets)
beta_sol_eth = beta(sol_rets, eth_rets)
corr_btc_eth = correlation(btc_rets, eth_rets)

print('SOL vs BTC  correlation: {:.3f}   beta: {:.2f}'.format(corr_sol_btc, beta_sol_btc))
print('SOL vs ETH  correlation: {:.3f}   beta: {:.2f}'.format(corr_sol_eth, beta_sol_eth))
print('BTC vs ETH  correlation: {:.3f}'.format(corr_btc_eth))
print()

# --- 24h cumulative moves ---
def cum_ret(rets):
    p = 1.0
    for r in rets:
        p *= (1 + r)
    return (p - 1) * 100

print('24h cumulative return (hourly compounded):')
print('  BTC:  {:+.2f}%'.format(cum_ret(btc_rets)))
print('  ETH:  {:+.2f}%'.format(cum_ret(eth_rets)))
print('  SOL:  {:+.2f}%'.format(cum_ret(sol_rets)))
print()

# --- Idiosyncratic SOL move (SOL return - beta * BTC return) ---
# If beta*btc is the "expected" SOL return, residual is SOL-specific
expected_sol = beta_sol_btc * cum_ret(btc_rets)
idiosyncratic_sol = cum_ret(sol_rets) - expected_sol
print('=== IDIOSYNCRATIC SOL MOVE (residual vs BTC beta) ===')
print('  Expected SOL (beta-driven): {:+.2f}%'.format(expected_sol))
print('  Actual SOL:                 {:+.2f}%'.format(cum_ret(sol_rets)))
print('  Idiosyncratic residual:     {:+.2f}%'.format(idiosyncratic_sol))
if abs(idiosyncratic_sol) > 1.0:
    if idiosyncratic_sol < 0:
        print('  >>> SOL UNDERPERFORMING BTC by {:.2f}% — alt-rotation OUT of SOL'.format(
            abs(idiosyncratic_sol)))
    else:
        print('  >>> SOL OUTPERFORMING BTC by {:.2f}% — alt-rotation INTO SOL'.format(
            idiosyncratic_sol))
else:
    print('  >>> SOL roughly tracking BTC — no idiosyncratic move')
print()

# --- Recent hourly returns table ---
print('=== LAST 12 HOURS: HOURLY % CHANGE ===')
print('{:<10}  {:>10}  {:>10}  {:>10}'.format('Hour', 'BTC', 'ETH', 'SOL'))
print('-' * 44)
for i in range(-12, 0):
    btc_h = btc_rets[i] * 100 if i < len(btc_rets) else 0
    eth_h = eth_rets[i] * 100 if i < len(eth_rets) else 0
    sol_h = sol_rets[i] * 100 if i < len(sol_rets) else 0
    print('{:>4}h ago  {:>+9.2f}%  {:>+9.2f}%  {:>+9.2f}%'.format(-i, btc_h, eth_h, sol_h))
print()

# --- Funding rate comparison across majors ---
print('=== FUNDING RATES (futures, current) ===')
print('{:<10}  {:>12}  {:>10}'.format('Symbol', 'FundingRate', 'Bps'))
print('-' * 36)
for sym in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'DOGEUSDT', 'LINKUSDT']:
    try:
        fr = get(BINANCE_FUT + '/fapi/v1/premiumIndex', {'symbol': sym})
        rate = float(fr['lastFundingRate'])
        bps = rate * 10000
        marker = ' <-- LONG HEAVY' if bps > 1 else (' <-- SHORT HEAVY' if bps < -1 else '')
        print('{:<10}  {:>+12.6f}  {:>+9.2f}{}'.format(sym, rate, bps, marker))
    except Exception as e:
        print('{:<10}  error: {}'.format(sym, e))
print()
