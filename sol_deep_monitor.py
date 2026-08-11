"""
SOL-only deep monitor: 25 minutes of comprehensive data capture.
Pulls every signal discussed in the session, plus EMA structure for the
visual-rejection thesis.

Writes one JSON object per tick to /Users/kamii/Documents/crypto-ai-anal/sol_deep_log.json
so the post-run analyzer has a stable dataset.

Each tick captures:
  - 1m kline (OHLCV + taker buy/sell)
  - Current price / mark / index / funding
  - Top-20 order book depth (both sides)
  - OI history (5m) delta
  - Last 1000 futures trades → 1-min bucket of large-print flow
  - 9/21/50/200 EMA values from 1m and 5m candles
  - Macro: BTC/ETH 1m change for regime context
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import aiohttp

API = "https://fapi.binance.com"
LOG_PATH = "/Users/kamii/Documents/crypto-ai-anal/sol_deep_log.json"
SYMBOL = "SOLUSDT"
INTERVAL_S = 60
DURATION_S = 25 * 60

EMA_PERIODS = [9, 21, 50, 200]


# ---------- helpers ----------


def to_f(x):
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


async def fetch_json(session, path, params=None):
    async with session.get(f"{API}{path}", params=params) as r:
        return await r.json()


def ema(values, period):
    if not values or len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def compute_ema_series(closes, periods=EMA_PERIODS):
    out = {}
    for p in periods:
        v = ema(closes, p)
        if v is not None:
            out[f"ema{p}"] = round(v, 6)
    return out


# ---------- per-tick pull ----------


async def pull_tick(session, sym):
    # Parallel pulls
    klines_1m_t = fetch_json(session, "/fapi/v1/klines", {"symbol": sym, "interval": "1m", "limit": 250})
    klines_5m_t = fetch_json(session, "/fapi/v1/klines", {"symbol": sym, "interval": "5m", "limit": 250})
    book_t      = fetch_json(session, "/fapi/v1/depth",  {"symbol": sym, "limit": 20})
    mark_t      = fetch_json(session, "/fapi/v1/premiumIndex", {"symbol": sym})
    oi_t        = fetch_json(session, "/fapi/v1/openInterest", {"symbol": sym})
    oi_hist_t   = fetch_json(session, "/futures/data/openInterestHist", {"symbol": sym, "period": "5m", "limit": 30})
    trades_t    = fetch_json(session, "/fapi/v1/trades", {"symbol": sym, "limit": 1000})
    btc_1m_t    = fetch_json(session, "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 2})
    eth_1m_t    = fetch_json(session, "/fapi/v1/klines", {"symbol": "ETHUSDT", "interval": "1m", "limit": 2})
    tbs_t       = fetch_json(session, "/futures/data/takerlongshortRatio", {"symbol": sym, "period": "5m", "limit": 12})

    (k1, k5, book, mark, oi_now, oi_hist, trades, btc_1m, eth_1m, tbs) = await asyncio.gather(
        klines_1m_t, klines_5m_t, book_t, mark_t, oi_t, oi_hist_t, trades_t, btc_1m_t, eth_1m_t, tbs_t
    )

    # ----- 1m klines → EMA + structure -----
    closes_1m = [float(k[4]) for k in k1]
    closes_5m = [float(k[4]) for k in k5]
    ema_1m = compute_ema_series(closes_1m)
    ema_5m = compute_ema_series(closes_5m)
    last_1m = k1[-1] if k1 else None
    last_close_1m = float(last_1m[4]) if last_1m else None
    last_close_5m = float(k5[-1][4]) if k5 else None

    # EMA position: is last close above or below each EMA?
    ema_position = {}
    for k, v in ema_1m.items():
        ema_position[f"{k}_pos"] = "ABOVE" if last_close_1m and last_close_1m > v else "BELOW"
    for k, v in ema_5m.items():
        ema_position[f"{k}_pos"] = "ABOVE" if last_close_5m and last_close_5m > v else "BELOW"

    # Rejection check: did the last candle wick reject an EMA?
    rejections = []
    if last_1m:
        h, l, c = float(last_1m[2]), float(last_1m[3]), float(last_1m[4])
        for k, v in ema_1m.items():
            # If price wicked above EMA but closed below → upper rejection
            if h > v and c < v:
                rejections.append(f"{k}_upper_wick_reject")
            # If price wicked below EMA but closed above → lower rejection
            if l < v and c > v:
                rejections.append(f"{k}_lower_wick_reject")
    for k, v in ema_5m.items():
        if last_1m:
            h, l, c = float(last_1m[2]), float(last_1m[3]), float(last_1m[4])
            if h > v and c < v:
                rejections.append(f"5m_{k}_upper_wick_reject")
            if l < v and c > v:
                rejections.append(f"5m_{k}_lower_wick_reject")

    # Recent candle pattern
    candle_seq = []
    for k in k1[-5:]:
        candle_seq.append({
            "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
            "vol": float(k[5]),
            "taker_buy_vol": float(k[9]),
        })

    # ----- Order book -----
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    def _safe_floats(rows):
        out = []
        for r in rows:
            try:
                out.append([float(r[0]), float(r[1])])
            except Exception:
                continue
        return out
    bids = _safe_floats(bids)
    asks = _safe_floats(asks)

    bid_5 = sum(b[1] for b in bids[:5]); ask_5 = sum(a[1] for a in asks[:5])
    bid_10 = sum(b[1] for b in bids[:10]); ask_10 = sum(a[1] for a in asks[:10])
    bid_20 = sum(b[1] for b in bids[:20]); ask_20 = sum(a[1] for a in asks[:20])
    obi_5  = (bid_5 - ask_5) / max(bid_5 + ask_5, 1e-9)
    obi_10 = (bid_10 - ask_10) / max(bid_10 + ask_10, 1e-9)
    obi_20 = (bid_20 - ask_20) / max(bid_20 + ask_20, 1e-9)

    # Bids and asks near 74.95 (the user's key level)
    bids_near_7495 = sum(b[1] for b in bids if 74.50 <= b[0] <= 75.05)
    asks_near_75   = sum(a[1] for a in asks if 75.00 <= a[0] <= 75.50)

    # ----- OI history -----
    oi_series = []
    if isinstance(oi_hist, list):
        for r in oi_hist:
            if isinstance(r, dict):
                try:
                    oi_series.append((int(r["timestamp"]), float(r["sumOpenInterest"])))
                except Exception:
                    continue
    oi_now_val = to_f((oi_now or {}).get("openInterest"))
    oi_first = oi_series[0][1] if oi_series else None
    oi_last  = oi_series[-1][1] if oi_series else None
    oi_window_change_pct = ((oi_last - oi_first) / oi_first * 100) if (oi_first and oi_last and oi_first > 0) else None
    oi_recent_vs_prior_pct = None
    if len(oi_series) >= 10:
        recent = sum(v for _, v in oi_series[-5:]) / 5
        prior  = sum(v for _, v in oi_series[-10:-5]) / 5
        oi_recent_vs_prior_pct = (recent - prior) / prior * 100 if prior > 0 else None

    # ----- Large-trade flow (1-min buckets) -----
    trades_clean = []
    if isinstance(trades, list):
        for t in trades:
            try:
                qty = float(t["qty"]); px = float(t["price"]); ts = int(t["time"])
                is_bm = bool(t["isBuyerMaker"])
                trades_clean.append({"ts": ts, "qty": qty, "px": px, "notional": qty * px, "is_buyer_maker": is_bm})
            except Exception:
                continue

    now_ms = int(time.time() * 1000)
    buckets = {}
    for t in trades_clean:
        m = t["ts"] // 60000 * 60000
        if m not in buckets:
            buckets[m] = {"b": 0, "s": 0, "lb": 0, "ls": 0, "lbn": 0, "lsn": 0, "hb": 0, "hs": 0, "hbn": 0, "hsn": 0, "bigb": 0, "bigs": 0, "bigb_n": 0, "bigs_n": 0, "pxs": []}
        n = t["notional"]
        if t["is_buyer_maker"]:
            buckets[m]["s"] += n
            if t["qty"] >= 50:   buckets[m]["ls"] += n; buckets[m]["lsn"] += 1
            if t["qty"] >= 200:  buckets[m]["hs"] += n; buckets[m]["hsn"] += 1
            if t["qty"] >= 500:  buckets[m]["bigs"] += n; buckets[m]["bigs_n"] += 1
        else:
            buckets[m]["b"] += n
            if t["qty"] >= 50:   buckets[m]["lb"] += n; buckets[m]["lbn"] += 1
            if t["qty"] >= 200:  buckets[m]["hb"] += n; buckets[m]["hbn"] += 1
            if t["qty"] >= 500:  buckets[m]["bigb"] += n; buckets[m]["bigb_n"] += 1
        buckets[m]["pxs"].append(t["px"])

    # 5-min and 15-min large-print aggregates
    def aggregate(window_min):
        cutoff = now_ms - window_min * 60 * 1000
        lb = ls = hb = hs = bigb = bigs = lbn = lsn = hbn = hsn = bigb_n = bigs_n = 0
        for m, b_ in buckets.items():
            if m >= cutoff:
                lb += b_["lb"]; ls += b_["ls"]; hb += b_["hb"]; hs += b_["hs"]
                bigb += b_["bigb"]; bigs += b_["bigs"]
                lbn += b_["lbn"]; lsn += b_["lsn"]; hbn += b_["hbn"]; hsn += b_["hsn"]
                bigb_n += b_["bigb_n"]; bigs_n += b_["bigs_n"]
        return {
            "window_min": window_min,
            "large_buy_usd": round(lb, 0), "large_sell_usd": round(ls, 0),
            "large_buy_n": lbn, "large_sell_n": lsn,
            "huge_buy_usd": round(hb, 0), "huge_sell_usd": round(hs, 0),
            "huge_buy_n": hbn, "huge_sell_n": hsn,
            "whale_buy_usd": round(bigb, 0), "whale_sell_usd": round(bigs, 0),
            "whale_buy_n": bigb_n, "whale_sell_n": bigs_n,
            "large_sell_to_buy_ratio": round(ls / max(lb, 1), 3),
        }

    flow_5 = aggregate(5)
    flow_15 = aggregate(15)

    # Per-minute bucket list (last 10 min)
    bucket_list = []
    for m in sorted(buckets.keys(), reverse=True)[:10]:
        b_ = buckets[m]
        bucket_list.append({
            "minute": datetime.fromtimestamp(m / 1000).strftime("%H:%M"),
            "take_buy": round(b_["b"], 0),
            "take_sell": round(b_["s"], 0),
            "large_buy": round(b_["lb"], 0),
            "large_sell": round(b_["ls"], 0),
            "huge_buy": round(b_["hb"], 0),
            "huge_sell": round(b_["hs"], 0),
            "whale_sell_n": b_["bigs_n"],
        })

    # ----- Taker buy/sell ratio (5m official) -----
    taker_5m = None
    if isinstance(tbs, list) and tbs:
        latest = tbs[-1]
        if isinstance(latest, dict):
            taker_5m = {
                "buy_sell_ratio": to_f(latest.get("buySellRatio")),
                "buy_vol": to_f(latest.get("buyVol")),
                "sell_vol": to_f(latest.get("sellVol")),
            }

    # ----- Mark / funding -----
    funding = to_f((mark or {}).get("lastFundingRate"))
    next_funding_ts = to_f((mark or {}).get("nextFundingTime"))
    mark_price = to_f((mark or {}).get("markPrice"))
    index_price = to_f((mark or {}).get("indexPrice"))

    # ----- BTC/ETH context -----
    btc_chg = None; eth_chg = None
    if btc_1m and len(btc_1m) >= 2:
        btc_chg = (float(btc_1m[-1][4]) - float(btc_1m[0][1])) / float(btc_1m[0][1]) * 100
    if eth_1m and len(eth_1m) >= 2:
        eth_chg = (float(eth_1m[-1][4]) - float(eth_1m[0][1])) / float(eth_1m[0][1]) * 100

    return {
        "ts": now_ms,
        "price": last_close_1m,
        "mark_price": mark_price,
        "index_price": index_price,
        "funding_rate": funding,
        "next_funding_ts": next_funding_ts,
        "open_interest": oi_now_val,
        "oi_window_change_pct": round(oi_window_change_pct, 3) if oi_window_change_pct is not None else None,
        "oi_recent_vs_prior_pct": round(oi_recent_vs_prior_pct, 3) if oi_recent_vs_prior_pct is not None else None,
        "book": {
            "bid_5": round(bid_5, 1), "ask_5": round(ask_5, 1), "obi_5": round(obi_5, 4),
            "bid_10": round(bid_10, 1), "ask_10": round(ask_10, 1), "obi_10": round(obi_10, 4),
            "bid_20": round(bid_20, 1), "ask_20": round(ask_20, 1), "obi_20": round(obi_20, 4),
            "bids_near_7495": round(bids_near_7495, 1),
            "asks_near_75": round(asks_near_75, 1),
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
        },
        "ema_1m": ema_1m,
        "ema_5m": ema_5m,
        "ema_position_1m": {k: ema_position.get(f"{k}_pos", "?") for k in ema_1m},
        "ema_position_5m": {k: ema_position.get(f"{k}_pos", "?") for k in ema_5m},
        "ema_rejections": rejections,
        "last_5_candles": candle_seq,
        "flow_5min": flow_5,
        "flow_15min": flow_15,
        "flow_buckets_recent": bucket_list,
        "taker_5m": taker_5m,
        "macro_btc_1m_chg_pct": round(btc_chg, 3) if btc_chg is not None else None,
        "macro_eth_1m_chg_pct": round(eth_chg, 3) if eth_chg is not None else None,
    }


# ---------- main loop ----------


async def main():
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    start = time.time()
    log = []
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        print(f"[start] {datetime.now(timezone.utc).isoformat()}  duration={DURATION_S}s", flush=True)
        while time.time() - start < DURATION_S:
            tick_ts = int(time.time() * 1000)
            try:
                snap = await pull_tick(session, SYMBOL)
                log.append(snap)
                # Compact one-liner
                px = snap["price"] or 0
                fund = (snap["funding_rate"] or 0) * 10000
                oi_d = snap["oi_recent_vs_prior_pct"] or 0
                obi = snap["book"]["obi_20"]
                ema_p = ",".join(f"{k[3:]}={v[:3]}" for k, v in snap["ema_position_1m"].items() if k.endswith("_pos"))
                fl5 = snap["flow_5min"]
                ratio = fl5["large_sell_to_buy_ratio"]
                bids_7495 = snap["book"]["bids_near_7495"]
                ts_str = datetime.fromtimestamp(tick_ts / 1000).strftime("%H:%M:%S")
                print(
                    f"[{ts_str}] px={px:.2f} fund={fund:+.2f}bp OIΔ={oi_d:+.2f}% obi20={obi:+.3f} "
                    f"f5_L/S={ratio:.2f} bids@74.95={bids_7495:,.0f} rej={','.join(snap['ema_rejections']) or 'none'}",
                    flush=True,
                )
                with open(LOG_PATH, "w") as f:
                    json.dump({"start": start, "interval_s": INTERVAL_S, "log": log}, f, default=str)
            except Exception as e:
                print(f"[err] {e!r}", flush=True)
            await asyncio.sleep(INTERVAL_S)
        print(f"[done] {datetime.now(timezone.utc).isoformat()}  ticks={len(log)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())