#!/usr/bin/env python3
"""
continue_monitor.py
-------------------
Continuation of the NY-session 15-minute Binance USD-M perp monitor.
Reads existing /Users/kamii/Documents/crypto-ai-anal/monitor_log.json,
polls BTC/ETH/SOL for 10 more ticks (~10 minutes), appends to log.

Schema (per tick):
{
  "tick_ts": <ms epoch>,
  "snapshots": {
    "BTCUSDT": {"ts", "price_fut", "spot_micro_skew_bps", "spot_obi_top20",
                "fut_micro_skew_bps", "fut_obi_top20",
                "spot_15m_buy_usd", "spot_15m_sell_usd", "spot_15m_buy_share",
                "fut_15m_buy_usd",  "fut_15m_sell_usd",  "fut_15m_buy_share",
                "spot_pct_of_turnover", "funding_rate", "open_interest",
                "24h_change_pct"},
    "ETHUSDT": {...},
    "SOLUSDT": {...}
  }
}
"""
import json, time, urllib.request, urllib.parse, datetime, sys, os, statistics

LOG_PATH    = "/Users/kamii/Documents/crypto-ai-anal/monitor_log.json"
SYMBOLS     = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SPOT_BASE   = "https://api.binance.com"
FUT_BASE    = "https://fapi.binance.com"
INTERVAL_S  = 60
N_TICKS     = 10
WINDOW_MS   = 15 * 60 * 1000   # 15 minutes for buy/sell aggregation


def _http(url, params=None, timeout=10):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-ai-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def spot_depth(sym, limit=20):
    return _http(f"{SPOT_BASE}/api/v3/depth", {"symbol": sym, "limit": limit})


def fut_depth(sym, limit=20):
    return _http(f"{FUT_BASE}/fapi/v1/depth", {"symbol": sym, "limit": limit})


def spot_aggtrades(sym, limit=1000):
    return _http(f"{SPOT_BASE}/api/v3/aggTrades", {"symbol": sym, "limit": limit})


def fut_aggtrades(sym, limit=1000):
    return _http(f"{FUT_BASE}/fapi/v1/aggTrades", {"symbol": sym, "limit": limit})


def fut_24h(sym):
    return _http(f"{FUT_BASE}/fapi/v1/ticker/24hr", {"symbol": sym})


def fut_funding(sym):
    data = _http(f"{FUT_BASE}/fapi/v1/fundingRate", {"symbol": sym, "limit": 1})
    return float(data[-1]["fundingRate"]) if data else 0.0


def fut_oi(sym):
    return float(_http(f"{FUT_BASE}/fapi/v1/openInterest", {"symbol": sym})["openInterest"])


def micro_skew_bps(bids, asks):
    """Top-of-book microprice vs mid, in basis points.
       bids/asks are lists of [price, qty].
    """
    if not bids or not asks:
        return 0.0
    bb_p, bb_q = float(bids[0][0]), float(bids[0][1])
    ba_p, ba_q = float(asks[0][0]), float(asks[0][1])
    if bb_p <= 0 or ba_p <= 0 or (bb_q + ba_q) == 0:
        return 0.0
    mid = 0.5 * (bb_p + ba_p)
    micro = (ba_p * bb_q + bb_p * ba_q) / (bb_q + ba_q)
    return (micro / mid - 1.0) * 1e4


def obi_top20(bids, asks):
    """(bid notional - ask notional) / total notional over top 20 levels."""
    bid_notional = sum(float(p) * float(q) for p, q in bids[:20])
    ask_notional = sum(float(p) * float(q) for p, q in asks[:20])
    total = bid_notional + ask_notional
    if total == 0:
        return 0.0
    return (bid_notional - ask_notional) / total


def agg_buy_sell(trades, now_ms, window_ms):
    """From aggTrades, sum quote volume for buyer-taker (m=false) and seller-taker (m=true)
       over the last window_ms milliseconds.
    """
    cutoff = now_ms - window_ms
    buy = 0.0
    sell = 0.0
    for t in trades:
        ts = int(t.get("T", 0))
        if ts < cutoff:
            continue
        qv = float(t.get("q", 0.0)) * float(t.get("p", 0.0))   # quote volume
        if bool(t.get("m", False)):
            sell += qv
        else:
            buy  += qv
    return buy, sell


def take_snapshot(sym, now_ms):
    sd = spot_depth(sym)
    fd = fut_depth(sym)
    st = spot_aggtrades(sym)
    ft = fut_aggtrades(sym)
    tk = fut_24h(sym)

    price_fut = float(ft[-1]["p"]) if ft else 0.0

    spot_b, spot_a = sd.get("bids", []), sd.get("asks", [])
    fut_b,  fut_a  = fd.get("bids", []), fd.get("asks", [])

    s_buy, s_sell = agg_buy_sell(st, now_ms, WINDOW_MS)
    f_buy, f_sell = agg_buy_sell(ft, now_ms, WINDOW_MS)

    s_total = s_buy + s_sell
    f_total = f_buy + f_sell
    grand = s_total + f_total

    snap = {
        "ts":                  now_ms,
        "price_fut":           price_fut,
        "spot_micro_skew_bps": micro_skew_bps(spot_b, spot_a),
        "spot_obi_top20":      obi_top20(spot_b, spot_a),
        "fut_micro_skew_bps":  micro_skew_bps(fut_b, fut_a),
        "fut_obi_top20":       obi_top20(fut_b, fut_a),
        "spot_15m_buy_usd":    s_buy,
        "spot_15m_sell_usd":   s_sell,
        "spot_15m_buy_share":  (s_buy / s_total) if s_total > 0 else 0.0,
        "fut_15m_buy_usd":     f_buy,
        "fut_15m_sell_usd":    f_sell,
        "fut_15m_buy_share":   (f_buy / f_total) if f_total > 0 else 0.0,
        "spot_pct_of_turnover": (s_total / grand) if grand > 0 else 0.0,
        "funding_rate":        fut_funding(sym),
        "open_interest":       fut_oi(sym),
        "24h_change_pct":      float(tk.get("priceChangePercent", 0.0)),
    }
    return snap


def fmt_tick(n, snap_by_sym, tick_ts):
    parts = []
    for s in SYMBOLS:
        snap = snap_by_sym[s]
        parts.append(f"{s} p={snap['price_fut']:.2f} obi={snap['spot_obi_top20']:+.2f}/{snap['fut_obi_top20']:+.2f} "
                     f"buy%={snap['spot_15m_buy_share']:.2f}/{snap['fut_15m_buy_share']:.2f} "
                     f"fr={snap['funding_rate']:+.5f}")
    iso = datetime.datetime.utcfromtimestamp(tick_ts / 1000).strftime("%H:%M:%S")
    return f"[tick {n:02d} @ {iso}] " + " | ".join(parts)


def main():
    if not os.path.exists(LOG_PATH):
        print(f"FATAL: log file not found at {LOG_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(LOG_PATH) as f:
        log_doc = json.load(f)

    start_ts    = log_doc.get("start")
    interval_s  = log_doc.get("interval_s", INTERVAL_S)
    log_list    = log_doc.get("log", [])
    start_n     = len(log_list)
    print(f"[start] resuming from tick {start_n}, interval={interval_s}s, "
          f"will write {N_TICKS} more ticks", flush=True)

    # Sleep a few seconds so the first appended tick isn't a duplicate ms timestamp
    time.sleep(3.0)

    for i in range(N_TICKS):
        tick_ts = int(time.time() * 1000)
        try:
            snap_by_sym = {s: take_snapshot(s, tick_ts) for s in SYMBOLS}
        except Exception as e:
            print(f"[tick {start_n + i}] ERROR fetching: {e}", flush=True)
            time.sleep(interval_s)
            continue

        log_list.append({"tick_ts": tick_ts, "snapshots": snap_by_sym})

        # Persist after each tick so partial progress survives
        with open(LOG_PATH, "w") as f:
            json.dump(log_doc, f, indent=2)

        print(fmt_tick(start_n + i + 1, snap_by_sym, tick_ts), flush=True)

        if i < N_TICKS - 1:
            time.sleep(interval_s)

    # Final write
    with open(LOG_PATH, "w") as f:
        json.dump(log_doc, f, indent=2)

    end_n = len(log_list)
    first_ts = log_list[0]["tick_ts"]
    last_ts  = log_list[-1]["tick_ts"]
    print(f"[done] total ticks: {end_n} (was {start_n}, added {end_n - start_n}); "
          f"first_ts={first_ts}; last_ts={last_ts}", flush=True)


if __name__ == "__main__":
    main()
