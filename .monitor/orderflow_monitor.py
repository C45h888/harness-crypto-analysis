"""
SOLUSDT orderflow monitor. Subscribes to marketflow:stream:raw:SOLUSDT
and evaluates seven material triggers against the long hypothesis.

Hypothesis:
  side=LONG, entry=105.98, SL=104.80, TP=106.88
  bias=BEARISH until entry reclaimed.

Triggers:
 1. CVD flip (5m/15m) buy_sell_ratio crosses 0.45 or 0.55
 2. Keystone bid migrates >= 0.30 USDT in one cycle, OR verdict UP/DOWN/FLAT
 3. seller_aggression -> aggressive_selling / distribution OR ask-wall +20%
 4. bid depth within 0.50 USDT mid drops >= 40% in one cycle
 5. OI delta > +-3% on 5m bar
 6. last_price crosses 105.98 from below (entry reclaim) or 106.88 (TP touch)
 7. last_price <= 105.00 (SL threat)
"""
import asyncio, json, os, sys, time
from collections import deque
from typing import Any

import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
STREAM = "marketflow:stream:raw:SOLUSDT"
LATEST = "marketflow:latest:SOLUSDT:raw"

ENTRY = 105.98
SL = 104.80
TP = 106.88
SYMBOL = "SOLUSDT"

# Trigger thresholds
CVD_FLOOR = 0.45
CVD_CEIL = 0.55
KEYSTONE_MIGRATION_USDT = 0.30
LIQUIDITY_VACUUM_DROP_PCT = 40.0
ASK_WALL_GROWTH_PCT = 20.0
OI_SHOCK_PCT = 3.0

# Rolling window (5-second snapshots). 300 ~= 25 minutes.
MAX_HISTORY = 300


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def notify(trigger: str, when: str, price: float, level: str,
           numbers: dict, interp: str):
    payload = {
        "TRIGGER": trigger,
        "WHEN": when,
        "PRICE": price,
        "LEVEL": level,
        "NUMBERS": numbers,
        "INTERPRETATION": interp,
    }
    print(json.dumps(payload), flush=True)


def keystone_from_book(book: dict, side: str) -> tuple[float, float]:
    """Return (keystone_price, total_qty_within_tight_lo_hi).
    Approximation: pick the price level with the max qty in the top 30 levels
    on the given side (bid or ask). For bid keystone we use bids; for ask we
    use asks. We focus on bid keystone (the 'futures keystone bid')."""
    levels = book.get("bids", []) if side == "bid" else book.get("asks", [])
    if not levels:
        return 0.0, 0.0
    best = max(levels, key=lambda lv: float(lv[1]))
    return float(best[0]), float(best[1])


def top_of_book_depth(book: dict, mid: float, band: float = 0.50) -> tuple[float, float]:
    bid = 0.0; ask = 0.0
    for px, qty in book.get("bids", []):
        p = float(px); q = float(qty)
        if mid - p <= band and mid - p >= 0:
            bid += q * p
    for px, qty in book.get("asks", []):
        p = float(px); q = float(qty)
        if p - mid <= band and p - mid >= 0:
            ask += q * p
    return bid, ask


def aggregate_window(deques_spot: deque, deques_fut: deque) -> dict:
    sb = sq = 0.0; fb = fq = 0.0
    for e in deques_spot:
        sb += e["buy_qty"]; sq += e["sell_qty"]
    for e in deques_fut:
        fb += e["buy_qty"]; fq += e["sell_qty"]
    return {
        "spot_buy": sb, "spot_sell": sq,
        "fut_buy": fb, "fut_sell": fq,
        "spot_ratio": sb / (sb + sq + 1e-9),
        "fut_ratio": fb / (fb + fq + 1e-9),
        "spot_cvd": sb - sq, "fut_cvd": fb - fq,
    }


async def main():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    # Start from the latest stream entry id (only new messages after baseline)
    last_id = "$"
    spot_hist: deque = deque(maxlen=60)   # ~5 minutes @ 5s cycle
    fut_hist: deque = deque(maxlen=60)
    full_hist: deque = deque(maxlen=MAX_HISTORY)

    # Baseline read so first cycle has priors for delta triggers
    baseline = await r.get(LATEST)
    if baseline:
        b = json.loads(baseline)
        full_hist.append(_extract(b))
        print(f"[{ts()}] BASELINE loaded: last={b['spot']['ticker_24h']['last_price']} oi={b['futures'].get('open_interest',{}).get('open_interest')}", flush=True)

    print(f"[{ts()}] MONITOR subscribed to {STREAM}", flush=True)

    while True:
        try:
            res = await r.xread({STREAM: last_id}, block=5000, count=1)
        except Exception as exc:
            print(f"[{ts()}] redis error: {exc}", flush=True)
            await asyncio.sleep(1)
            continue
        if not res:
            continue
        _, entries = res[0]
        for eid, fields in entries:
            last_id = eid
            try:
                payload = json.loads(fields["payload"])
            except Exception as exc:
                print(f"[{ts()}] payload decode error: {exc}", flush=True)
                continue
            snap = _extract(payload)
            full_hist.append(snap)
            # update rolling windows: each cycle = 5-second snapshot; one
            # entry's trades_normalized = 5-min fetch window. To build a
            # rolling 5-min view we use only the latest cycle's snapshot;
            # to build 15-min we aggregate the last 3 cycles' buys/sells.
            spot_hist.append({"buy_qty": snap["spot_buy_qty"], "sell_qty": snap["spot_sell_qty"]})
            fut_hist.append({"buy_qty": snap["fut_buy_qty"], "sell_qty": snap["fut_sell_qty"]})

            await evaluate(snap, full_hist, list(spot_hist), list(fut_hist))


def _extract(payload: dict) -> dict:
    sp = payload["spot"]["ticker_24h"]
    fp = payload["futures"].get("ticker_24h") or {}
    fut_book = payload["futures"].get("order_book") or {}
    spot_book = payload["spot"].get("order_book") or {}
    last = float(sp["last_price"])
    oi = float(payload["futures"].get("open_interest", {}).get("open_interest") or 0)
    sb = sum(t["qty"] for t in payload["spot"].get("trades_normalized", []) if t.get("side") == "buy")
    ss = sum(t["qty"] for t in payload["spot"].get("trades_normalized", []) if t.get("side") == "sell")
    fb = sum(t["qty"] for t in payload["futures"].get("trades_normalized", []) if t.get("side") == "buy")
    fs = sum(t["qty"] for t in payload["futures"].get("trades_normalized", []) if t.get("side") == "sell")
    kbid_p, kbid_q = keystone_from_book(fut_book, "bid")
    kask_p, kask_q = keystone_from_book(fut_book, "ask")
    bid_depth, ask_depth = top_of_book_depth(spot_book, last, band=0.50)
    return {
        "ts": payload["observed_at"],
        "last_price": last,
        "fut_last_price": float(fp.get("last_price") or last),
        "oi": oi,
        "spot_buy_qty": sb, "spot_sell_qty": ss,
        "fut_buy_qty": fb, "fut_sell_qty": fs,
        "spot_ratio_5m": sb / (sb + ss + 1e-9),
        "fut_ratio_5m": fb / (fb + fs + 1e-9),
        "keystone_bid_price": kbid_p,
        "keystone_bid_qty": kbid_q,
        "keystone_ask_price": kask_p,
        "keystone_ask_qty": kask_q,
        "bid_depth_band": bid_depth,
        "ask_depth_band": ask_depth,
        "top_long": None,
        "top_short": None,
        "fut_tbs": payload["futures"].get("taker_buy_sell"),
        "fut_oi_history": payload["futures"].get("oi_history"),
    }


async def evaluate(snap: dict, full_hist: deque, spot_hist: list, fut_hist: list):
    price = snap["last_price"]
    when = snap["ts"]

    # 5m CVD / buy-sell ratio
    spot_ratio = snap["spot_ratio_5m"]
    fut_ratio = snap["fut_ratio_5m"]
    # 15m aggregate: use last 3 entries (approx 15s of cycles = 5m window x 3
    # but each cycle's trades_normalized IS the 5m window so the proper 15m
    # aggregate cannot be derived from raw entries alone). Use the 5m snapshot
    # as the primary signal; flag the cross.
    if full_hist and len(full_hist) >= 2:
        prev = full_hist[-2]
        prev_spot_ratio = prev["spot_ratio_5m"]
        prev_fut_ratio = prev["fut_ratio_5m"]
        # TRIGGER 1: CVD flip
        for venue, prev_r, cur_r in (("SPOT", prev_spot_ratio, spot_ratio),
                                    ("FUTURES", prev_fut_ratio, fut_ratio)):
            if (prev_r > CVD_CEIL and cur_r <= CVD_CEIL) or (prev_r < CVD_FLOOR and cur_r >= CVD_FLOOR):
                level = "0.55 (bear flip)" if prev_r > CVD_CEIL else "0.45 (bull flip)"
                notify(
                    "CVD_FLOW_FLIP",
                    when, price, level,
                    {"venue": venue, "prev_ratio": round(prev_r, 3), "new_ratio": round(cur_r, 3)},
                    f"{venue} 5m buy-sell ratio crossed {level} — {'validates' if cur_r >= 0.5 else 'invalidates'} the long.",
                )

    # TRIGGER 2: Keystone migration
    if full_hist and len(full_hist) >= 2:
        prev = full_hist[-2]
        kdelta = snap["keystone_bid_price"] - prev["keystone_bid_price"]
        if abs(kdelta) >= KEYSTONE_MIGRATION_USDT:
            direction = "UP" if kdelta > 0 else "DOWN"
            verdict = "MIGRATING_DOWN" if kdelta < 0 else "MIGRATING_UP"
            notify(
                "KEYSTONE_MIGRATION",
                when, price, f"{prev['keystone_bid_price']}->{snap['keystone_bid_price']}",
                {"keystone_bid_prev": prev["keystone_bid_price"],
                 "keystone_bid_now": snap["keystone_bid_price"],
                 "delta_usdt": round(kdelta, 3),
                 "keystone_bid_qty": snap["keystone_bid_qty"]},
                f"Futures keystone bid migrated {direction} by {abs(kdelta):.2f} USDT — verdict={verdict}; {'bullish' if kdelta>0 else 'bearish'} for the long.",
            )

    # TRIGGER 3: aggressive seller emergence OR ask-wall +20%
    if full_hist and len(full_hist) >= 2:
        prev = full_hist[-2]
        # ask-wall notional growth (top-of-book ask depth within 0.5)
        prev_ask = prev["ask_depth_band"]; cur_ask = snap["ask_depth_band"]
        if prev_ask > 0:
            ask_growth = (cur_ask - prev_ask) / prev_ask * 100
            if ask_growth >= ASK_WALL_GROWTH_PCT:
                notify(
                    "ASK_WALL_GROWTH",
                    when, price, "ask_depth_within_0.5",
                    {"prev_ask_notional": round(prev_ask, 1),
                     "new_ask_notional": round(cur_ask, 1),
                     "growth_pct": round(ask_growth, 2)},
                    f"Ask-wall within 0.5 USDT grew {ask_growth:.1f}% in one cycle — invalidates the long (supply pressing).",
                )
        # seller aggression proxy: fut_ratio drops sharply (e.g. crossing below 0.40)
        if snap["fut_ratio_5m"] < 0.40:
            notify(
                "SELLER_AGGRESSION_FUT",
                when, price, "0.40",
                {"fut_ratio_5m": round(snap["fut_ratio_5m"], 3),
                 "fut_buy_qty": round(snap["fut_buy_qty"], 1),
                 "fut_sell_qty": round(snap["fut_sell_qty"], 1)},
                f"Futures 5m buy-sell ratio fell to {snap['fut_ratio_5m']:.3f} — aggressive selling signature, invalidates long.",
            )

    # TRIGGER 4: liquidity vacuum — bid depth within 0.50 USDT drops >=40%
    if full_hist and len(full_hist) >= 2:
        prev = full_hist[-2]
        prev_bid = prev["bid_depth_band"]
        if prev_bid > 0:
            bid_drop = (prev_bid - snap["bid_depth_band"]) / prev_bid * 100
            if bid_drop >= LIQUIDITY_VACUUM_DROP_PCT:
                notify(
                    "LIQUIDITY_VACUUM",
                    when, price, "bid_depth_within_0.5",
                    {"prev_bid_notional": round(prev_bid, 1),
                     "new_bid_notional": round(snap["bid_depth_band"], 1),
                     "drop_pct": round(bid_drop, 2)},
                    f"Bid depth within 0.5 USDT of mid collapsed {bid_drop:.1f}% — liquidity vacuum below, raises SL risk.",
                )

    # TRIGGER 5: OI shock (using prev cycle to get delta)
    if full_hist and len(full_hist) >= 2:
        prev = full_hist[-2]
        if prev["oi"] > 0:
            oi_delta_pct = (snap["oi"] - prev["oi"]) / prev["oi"] * 100
            if abs(oi_delta_pct) >= OI_SHOCK_PCT:
                notify(
                    "OI_SHOCK",
                    when, price, f"{prev['oi']}->{snap['oi']}",
                    {"oi_prev": prev["oi"], "oi_now": snap["oi"],
                     "delta_pct": round(oi_delta_pct, 3)},
                    f"Open interest shifted {oi_delta_pct:+.2f}% — {'leverage building' if oi_delta_pct>0 else 'position unwinding'}, invalidates near-term longs if negative.",
                )

    # TRIGGER 6: reclaim / retest
    if full_hist and len(full_hist) >= 2:
        prev = full_hist[-2]
        prev_price = prev["last_price"]
        if prev_price < ENTRY <= price:
            notify(
                "ENTRY_RECLAIM",
                when, price, f"{ENTRY}",
                {"prev_price": prev_price, "new_price": price},
                f"Last price reclaimed {ENTRY} from below — long entry zone reactivated, validates the bias shift.",
            )
        if prev_price < TP <= price:
            notify(
                "TP_TOUCH",
                when, price, f"{TP}",
                {"prev_price": prev_price, "new_price": price},
                f"Last price crossed {TP} from below — take-profit level touched, take profit.",
            )

    # TRIGGER 7: SL threat
    if price <= SL + 0.20:
        notify(
            "SL_THREAT",
            when, price, f"{SL}",
            {"distance_to_sl": round(SL - price, 3), "threshold": "<= SL+0.20"},
            f"Price {price} is within 0.20 USDT of SL {SL} — stop-loss imminent, long invalidated.",
        )


if __name__ == "__main__":
    asyncio.run(main())
