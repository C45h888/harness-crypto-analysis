"""
SOL monitor + watchlist alert.
Logs each tick to /Users/kamii/Documents/crypto-ai-anal/sol_session_log.json
Emits ALERT to stdout when 73.10 breaks up or 72.80 breaks down with volume.

Threshold for ALERT:
  - Up break: close above 73.10 with 15K+ SOL volume in 1 candle
  - Down break: close below 72.80 with 15K+ SOL volume in 1 candle
  - ALSO alert on cascade: close <72.50 with 20K+ volume
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import aiohttp

API = "https://fapi.binance.com"
LOG_PATH = "/Users/kamii/Documents/crypto-ai-anal/sol_session_log.json"
SYMBOL = "SOLUSDT"
INTERVAL_S = 60
DURATION_S = 15 * 60

LEVEL_UP = 73.10
LEVEL_DOWN = 72.80
LEVEL_CASCADE = 72.50
VOL_ALERT = 15000
VOL_CASCADE = 20000


def fetch(session, path, params=None):
    url = f"{API}{path}"
    return session.get(url, params=params or {})


async def pull_tick(session):
    coros = [
        fetch(session, "/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1m", "limit": 250}),
        fetch(session, "/fapi/v1/depth",  {"symbol": SYMBOL, "limit": 50}),
        fetch(session, "/fapi/v1/premiumIndex", {"symbol": SYMBOL}),
        fetch(session, "/fapi/v1/openInterest", {"symbol": SYMBOL}),
        fetch(session, "/fapi/v1/trades", {"symbol": SYMBOL, "limit": 1000}),
        fetch(session, "/futures/data/openInterestHist", {"symbol": SYMBOL, "period": "5m", "limit": 6}),
        fetch(session, "/futures/data/takerlongshortRatio", {"symbol": SYMBOL, "period": "5m", "limit": 3}),
    ]
    responses = await asyncio.gather(*coros)
    results = []
    for r in responses:
        async with r as resp:
            results.append(await resp.json())
    klines, book, mark, oi_now, trades, oi_hist, tbs = results

    closes = [float(k[4]) for k in klines]
    last = closes[-1]
    last_k = klines[-1]

    def ema(values, period):
        if len(values) < period or period < 1:
            return None
        k = 2 / (period + 1)
        e = sum(values[:period]) / period
        for v in values[period:]:
            e = v * k + e * (1 - k)
        return e

    ema9  = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)
    ema200 = ema(closes[-250:], 200) if len(closes) >= 200 else None

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bids = [[float(b[0]), float(b[1])] for b in bids]
    asks = [[float(a[0]), float(a[1])] for a in asks]

    bid_5  = sum(b[1] for b in bids[:5])
    ask_5  = sum(a[1] for a in asks[:5])
    bid_20 = sum(b[1] for b in bids[:20])
    ask_20 = sum(a[1] for a in asks[:20])
    obi_5  = (bid_5 - ask_5) / max(bid_5 + ask_5, 1e-9)
    obi_20 = (bid_20 - ask_20) / max(bid_20 + ask_20, 1e-9)

    bids_7290 = sum(b[1] for b in bids if 72.80 <= b[0] <= 73.00)
    asks_7310 = sum(a[1] for a in asks if 73.00 <= a[0] <= 73.20)
    asks_7320 = sum(a[1] for a in asks if 73.20 <= a[0] <= 73.40)

    oi_vals = [float(r["sumOpenInterest"]) for r in (oi_hist if isinstance(oi_hist, list) else [])]
    oi_recent_change = None
    if len(oi_vals) >= 2:
        oi_recent_change = (oi_vals[-1] - oi_vals[0]) / oi_vals[0] * 100

    funding = None
    for k in ("lastFundingRate", "last_funding_rate"):
        if k in mark:
            try:
                funding = float(mark[k])
            except (TypeError, ValueError):
                pass
            break

    now_ms = int(time.time() * 1000)
    big_buy = big_sell = huge_buy = huge_sell = large_buy = large_sell = 0.0
    cnt_big_buy = cnt_big_sell = 0
    if isinstance(trades, list):
        for t in trades:
            try:
                qty = float(t["qty"])
                px = float(t["price"])
                n = qty * px
                is_bm = bool(t["isBuyerMaker"])
            except Exception:
                continue
            if is_bm:
                if qty >= 50:    large_sell += n
                if qty >= 200:   huge_sell += n
                if qty >= 500:   big_sell += n; cnt_big_sell += 1
            else:
                if qty >= 50:    large_buy += n
                if qty >= 200:   huge_buy += n
                if qty >= 500:   big_buy += n; cnt_big_buy += 1

    taker_bs = None
    if isinstance(tbs, list) and tbs:
        last_tbs = tbs[-1]
        if isinstance(last_tbs, dict):
            for k in ("buySellRatio", "buy_sell_ratio"):
                if k in last_tbs:
                    try:
                        taker_bs = float(last_tbs[k])
                    except (TypeError, ValueError):
                        pass
                    break

    oi_now_val = None
    if isinstance(oi_now, dict):
        try:
            oi_now_val = float(oi_now.get("openInterest"))
        except (TypeError, ValueError):
            pass

    return {
        "ts": now_ms,
        "time": datetime.fromtimestamp(now_ms / 1000).strftime("%H:%M:%S"),
        "price": last,
        "open": float(last_k[1]),
        "high": float(last_k[2]),
        "low": float(last_k[3]),
        "close": float(last_k[4]),
        "vol": float(last_k[5]),
        "taker_buy_vol": float(last_k[9]),
        "mark_price": mark.get("markPrice"),
        "index_price": mark.get("indexPrice"),
        "funding_rate": funding,
        "oi": oi_now_val,
        "oi_recent_change_pct": oi_recent_change,
        "ema9": ema9, "ema21": ema21, "ema50": ema50, "ema200": ema200,
        "obi_5": obi_5, "obi_20": obi_20,
        "bids_7280_7300": bids_7290,
        "asks_7300_7320": asks_7310,
        "asks_7320_7340": asks_7320,
        "large_buy_usd": large_buy, "large_sell_usd": large_sell,
        "huge_buy_usd": huge_buy, "huge_sell_usd": huge_sell,
        "big_buy_usd": big_buy, "big_sell_usd": big_sell,
        "cnt_big_buy": cnt_big_buy, "cnt_big_sell": cnt_big_sell,
        "taker_5m_bs_ratio": taker_bs,
    }


def check_alerts(prev, curr):
    alerts = []

    close = curr["close"]
    vol = curr["vol"]
    prev_close = prev["close"] if prev else None

    if vol < VOL_ALERT:
        return alerts

    if prev_close is not None and prev_close <= LEVEL_UP and close > LEVEL_UP:
        alerts.append(f"🔴 ALERT UP-BREAK: close {close:.2f} above {LEVEL_UP} (vol={vol:,.0f})")
    if prev_close is not None and prev_close >= LEVEL_DOWN and close < LEVEL_DOWN:
        alerts.append(f"🟢 ALERT DOWN-BREAK: close {close:.2f} below {LEVEL_DOWN} (vol={vol:,.0f})")
    if close < LEVEL_CASCADE and vol >= VOL_CASCADE:
        alerts.append(f"⚫ ALERT CASCADE: close {close:.2f} below {LEVEL_CASCADE} (vol={vol:,.0f})")

    return alerts


async def main():
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)

    start = time.time()
    log = []
    alerts = []

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        print(f"[start] {datetime.now(timezone.utc).isoformat()}  duration={DURATION_S}s interval={INTERVAL_S}s", flush=True)
        print(f"[alerts] UP-break {LEVEL_UP} | DOWN-break {LEVEL_DOWN} | CASCADE <{LEVEL_CASCADE}", flush=True)
        print("=" * 80, flush=True)

        prev = None
        while time.time() - start < DURATION_S:
            tick_ts = int(time.time() * 1000)
            try:
                snap = await pull_tick(session)
                log.append(snap)

                alerts_now = check_alerts(prev, snap)
                prev = snap
                for a in alerts_now:
                    alerts.append({"ts": tick_ts, "alert": a})

                flag = " | ALERT! " + " | ".join(alerts_now) if alerts_now else ""
                bs = snap.get('taker_5m_bs_ratio')
                bs_str = f" bs5m={bs:.2f}" if bs else ""
                print(
                    f"[{snap['time']}] px={snap['price']:.2f} "
                    f"ema9={snap['ema9']:.3f} ema21={snap['ema21']:.3f} ema50={snap['ema50']:.3f} ema200={snap['ema200']:.3f} | "
                    f"obi20={snap['obi_20']:+.3f} | "
                    f"L_B={snap['large_buy_usd']/1000:.0f}K L_S={snap['large_sell_usd']/1000:.0f}K | "
                    f"vol={snap['vol']:,.0f}{bs_str}{flag}",
                    flush=True
                )

                with open(LOG_PATH, "w") as f:
                    json.dump({"start": start, "interval_s": INTERVAL_S, "alerts": alerts, "log": log}, f, default=str)

            except Exception as e:
                print(f"[err] {e!r}", flush=True)

            await asyncio.sleep(INTERVAL_S)

        print("=" * 80, flush=True)
        print(f"[done] {datetime.now(timezone.utc).isoformat()}  ticks={len(log)}  alerts={len(alerts)}", flush=True)
        if alerts:
            print("\nALERTS TRIGGERED:", flush=True)
            for a in alerts:
                print(f"  {datetime.fromtimestamp(a['ts']/1000).strftime('%H:%M:%S')}  {a['alert']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())