"""
Auction dynamics: WHO is winning the current auction?

Markets are continuous double auctions. Every trade is a price discovery
event between two opposing participants. The "winner" of the auction at
any moment is the side whose price is being paid, whose liquidity is
holding, and whose initiated flow is prevailing.

This script measures the auction at three timescales:
  1. Microstructure (instant): order-book shape, microprice, top-of-book depth
  2. Tape (recent 500 prints): initiated flow direction, taker ratio, CVD
  3. Regime (24h): price relative to VWAP, range position, OI trend

Output: a single "AUCTION VERDICT" line + the underlying numbers, so the
verdict can be inspected rather than trusted.

Note: deliberately no directional bias. We measure who is winning the
micro-auction, not who is right.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

import os
import sys

# make the canonical `market_service` package importable when run directly:
#   .venv/bin/python legacy/auction_dynamics.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_service.clients.binance import Binance, normalize_fut_trade, normalize_spot_trade

log = logging.getLogger(__name__)


# ---------- microprice / book shape ----------


def microprice(book: dict) -> dict:
    """
    Compute the weighted mid (microprice) and depth asymmetry.

    Microprice > mid  => aggressive sellers are being absorbed (buyers winning)
    Microprice < mid  => aggressive buyers are being absorbed (sellers winning)
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return {"microprice": None, "mid": None, "skew_bps": None, "depth": {}}

    # bids/asks may be list of [price, qty] pairs (raw) or dicts
    def _row(r):
        if isinstance(r, dict):
            return float(r.get("price") or r[0]), float(r.get("qty") or r[1])
        return float(r[0]), float(r[1])

    bp, bq = _row(bids[0])
    ap, aq = _row(asks[0])
    mid = (bp + ap) / 2
    micro = (bp * aq + ap * bq) / (aq + bq) if (aq + bq) > 0 else mid
    skew_bps = (micro - mid) / mid * 10_000 if mid > 0 else 0.0

    # depth at multiple levels
    def _depth(levels: list[Any]) -> tuple[float, float]:
        bid_sz = ask_sz = 0.0
        for r in levels:
            try:
                _, sz = _row(r)
            except (TypeError, ValueError, IndexError):
                continue
            if r is levels[0] or r in levels[: len(bids)]:
                bid_sz += sz
            else:
                ask_sz += sz
        return bid_sz, ask_sz

    bid_total = sum((_row(r)[1] for r in bids), 0.0)
    ask_total = sum((_row(r)[1] for r in asks), 0.0)

    # depth imbalance at multiple slices
    def _slice(b, a, n):
        b_sum = sum((_row(r)[1] for r in b[:n]), 0.0) if b else 0.0
        a_sum = sum((_row(r)[1] for r in a[:n]), 0.0) if a else 0.0
        return b_sum, a_sum

    b5, a5 = _slice(bids, asks, 5)
    b10, a10 = _slice(bids, asks, 10)
    b20, a20 = _slice(bids, asks, 20)

    def _obi(bs, asz):
        t = bs + asz
        return (bs - asz) / t if t > 0 else 0.0

    return {
        "best_bid": bp, "best_ask": ap,
        "mid": mid, "microprice": micro, "skew_bps": skew_bps,
        "spread_bps": (ap - bp) / mid * 10_000 if mid > 0 else 0.0,
        "depth": {
            "top5":   {"bid": b5,  "ask": a5,  "obi": _obi(b5, a5)},
            "top10":  {"bid": b10, "ask": a10, "obi": _obi(b10, a10)},
            "top20":  {"bid": b20, "ask": a20, "obi": _obi(b20, a20)},
            "total":  {"bid": bid_total, "ask": ask_total,
                       "obi": _obi(bid_total, ask_total)},
        },
    }


def initiated_flow(trades: list[dict]) -> dict:
    """
    Categorize trades by who initiated (taker) vs who provided (maker).

    Binance trade: isBuyerMaker=true => taker SOLD into bid (passive buyers got filled).
                                  false => taker BOUGHT lifting ask (passive sellers got filled).

    So:
      aggressive_buy_qty  = sum(qty where isBuyerMaker==False)
      aggressive_sell_qty = sum(qty where isBuyerMaker==True)
    """
    ab_qty = 0.0
    as_qty = 0.0
    ab_notional = 0.0
    as_notional = 0.0
    n_buy = n_sell = 0
    for t in trades:
        q = float(t.get("qty") or 0)
        p = float(t.get("price") or 0)
        if t.get("is_buyer_maker"):
            as_qty += q
            as_notional += p * q
            n_sell += 1
        else:
            ab_qty += q
            ab_notional += p * q
            n_buy += 1
    cvd = ab_qty - as_qty
    total = ab_notional + as_notional
    return {
        "aggressive_buy_qty":    ab_qty,
        "aggressive_sell_qty":   as_qty,
        "aggressive_buy_notional":  ab_notional,
        "aggressive_sell_notional": as_notional,
        "buy_share": (ab_notional / total) if total else None,
        "cvd_qty":    cvd,
        "cvd_notional": ab_notional - as_notional,
        "n_buy_trades":  n_buy,
        "n_sell_trades": n_sell,
        "trade_count":   len(trades),
    }


def flow_persistence(trades: list[dict]) -> dict:
    """
    Compare recent half of trades vs older half.

    If initiated flow is intensifying in one direction => the auction is
    moving toward that side. If it's flipping => no consensus.
    """
    if len(trades) < 20:
        return {"trend": "insufficient-data"}
    half = len(trades) // 2
    # trades come newest-first from Binance
    recent = trades[:half]
    older  = trades[half:]
    rec = initiated_flow(recent)
    old = initiated_flow(older)
    def _share(f):
        return (f["aggressive_buy_notional"] /
                max(f["aggressive_buy_notional"] + f["aggressive_sell_notional"], 1e-9))
    rs = _share(rec)
    os = _share(old)
    delta = rs - os
    if delta > 0.05:   trend = "accelerating-buy"
    elif delta < -0.05: trend = "accelerating-sell"
    else:               trend = "balanced"
    return {
        "trend": trend,
        "recent_buy_share": rs,
        "older_buy_share":  os,
        "delta":            delta,
        "recent_cvd_notional": rec["cvd_notional"],
        "older_cvd_notional":  old["cvd_notional"],
    }


# ---------- main analysis ----------


async def pull(symbol: str, depth_limit: int) -> dict:
    async with Binance() as b:
        t0 = time.time()
        s_book, f_book, s_trades, f_trades, f_mark, f_fund, f_oi, f_tbs, f_oi_hist = await asyncio.gather(
            b.spot_book(symbol, limit=depth_limit),
            b.fut_book(symbol, limit=depth_limit),
            b.spot_trades(symbol, limit=1000),
            b.fut_trades(symbol, limit=1000),
            b.fut_mark_price(symbol),
            b.fut_funding(symbol),
            b.fut_open_interest(symbol),
            b.fut_taker_buy_sell(symbol, period="15m"),
            b.fut_open_interest_history(symbol, period="15m", limit=10),
            return_exceptions=True,
        )
        latency_ms = round((time.time() - t0) * 1000, 1)
    return {
        "latency_ms": latency_ms,
        "spot": {
            "book":   s_book   if not isinstance(s_book, Exception) else {},
            "trades": [dict(normalize_spot_trade(t), venue="spot") for t in (s_trades if not isinstance(s_trades, Exception) else [])],
            "mp":     f_mark   if not isinstance(f_mark, Exception) else {},
            "fund":   f_fund   if not isinstance(f_fund, Exception) else {},
            "oi":     f_oi     if not isinstance(f_oi, Exception) else {},
            "tbs":    f_tbs    if not isinstance(f_tbs, Exception) else {},
            "oi_hist": f_oi_hist if not isinstance(f_oi_hist, Exception) else {},
        },
        # alias futures-only convenience
        "futures": {
            "book":   f_book   if not isinstance(f_book, Exception) else {},
            "trades": [normalize_fut_trade(t) for t in (f_trades if not isinstance(f_trades, Exception) else [])],
        },
    }


def auction_state(d: dict) -> dict:
    """
    Build the auction state snapshot for both venues, plus a verdict.
    """
    spot_mp  = microprice(d["spot"]["book"])
    fut_mp   = microprice(d["futures"]["book"])
    spot_if  = initiated_flow(d["spot"]["trades"])
    fut_if   = initiated_flow(d["futures"]["trades"])
    spot_per = flow_persistence(d["spot"]["trades"])
    fut_per  = flow_persistence(d["futures"]["trades"])

    # Funding rate from mark_price carries it
    funding = None
    for k in ("last_funding_rate", "lastFundingRate"):
        if isinstance(d["spot"]["mp"], dict) and k in d["spot"]["mp"]:
            try:
                funding = float(d["spot"]["mp"][k]); break
            except (TypeError, ValueError): pass

    # Taker buy ratio (15m)
    tbs_row = d["spot"]["tbs"]
    taker_buy_ratio = None
    if isinstance(tbs_row, dict):
        for k in ("buy_sell_ratio", "buySellRatio"):
            if k in tbs_row and tbs_row[k] is not None:
                try:
                    taker_buy_ratio = float(tbs_row[k]); break
                except (TypeError, ValueError): pass
    elif isinstance(tbs_row, list) and tbs_row:
        last = tbs_row[-1]
        if isinstance(last, dict):
            for k in ("buy_sell_ratio", "buySellRatio"):
                if k in last:
                    try: taker_buy_ratio = float(last[k]); break
                    except (TypeError, ValueError): pass

    # OI delta from history
    oi_change_pct = None
    oi_series = []
    oi_hist = d["spot"]["oi_hist"]
    if isinstance(oi_hist, list):
        for row in oi_hist:
            if isinstance(row, dict):
                ts = row.get("timestamp")
                o  = row.get("sumOpenInterest") or row.get("sum_open_interest") or row.get("openInterest") or row.get("open_interest")
                if ts is not None and o is not None:
                    try: oi_series.append((int(ts), float(o)))
                    except (TypeError, ValueError): pass
    if len(oi_series) >= 2 and oi_series[0][1] > 0:
        oi_change_pct = (oi_series[-1][1] - oi_series[0][1]) / oi_series[0][1] * 100

    return {
        "spot": {
            "microprice":  spot_mp,
            "flow":        spot_if,
            "persistence": spot_per,
        },
        "futures": {
            "microprice":  fut_mp,
            "flow":        fut_if,
            "persistence": fut_per,
        },
        "derivs": {
            "funding":        funding,
            "taker_buy_ratio": taker_buy_ratio,
            "oi_change_pct":  oi_change_pct,
        },
    }


def auction_verdict(state: dict) -> tuple[str, list[str]]:
    """
    WHO IS WINNING THE CURRENT AUCTION?

    Five signals, each +1 buyer / -1 seller / 0 neutral. Sum into verdict.

      1. Microprice skew:
         spot.microprice.skew_bps >  0.5  => +1  (buyers pulling price up via passive bids)
         spot.microprice.skew_bps < -0.5  => -1  (sellers pulling price down via passive asks)

      2. Top-of-book depth imbalance:
         spot.microprice.depth.top20.obi > 0.2  => +1
         spot.microprice.depth.top20.obi < -0.2 => -1

      3. Initiated flow (taker):
         spot.flow.buy_share > 0.55  => +1
         spot.flow.buy_share < 0.45  => -1

      4. Persistence:
         accelerating-buy  => +1
         accelerating-sell  => -1

      5. Futures confirmation (any disagreement flips verdict):
         if spot +1 and futures -1 (or vice versa), the signal is contested
    """
    reasons: list[str] = []
    score = 0

    s = state["spot"]
    f = state["futures"]
    d = state["derivs"]

    # 1. microprice
    s_skew = s["microprice"].get("skew_bps") if isinstance(s["microprice"], dict) else None
    if s_skew is not None:
        if s_skew > 0.5:
            score += 1; reasons.append(f"spot microprice +{s_skew:.2f}bps (buyers dictating)")
        elif s_skew < -0.5:
            score -= 1; reasons.append(f"spot microprice {s_skew:.2f}bps (sellers dictating)")
        else:
            reasons.append(f"spot microprice {s_skew:+.2f}bps (neutral)")

    # 2. depth
    s_obi = (s["microprice"].get("depth") or {}).get("top20", {}).get("obi") if isinstance(s["microprice"], dict) else None
    if s_obi is not None:
        if s_obi > 0.2:
            score += 1; reasons.append(f"spot top-20 OBI {s_obi:+.3f} (bids stacked, buyers offering)")
        elif s_obi < -0.2:
            score -= 1; reasons.append(f"spot top-20 OBI {s_obi:+.3f} (asks stacked, sellers offering)")

    # 3. initiated flow
    s_share = s["flow"].get("buy_share")
    if s_share is not None:
        if s_share > 0.55:
            score += 1; reasons.append(f"spot taker-buy {s_share:.0%} (buyers initiating)")
        elif s_share < 0.45:
            score -= 1; reasons.append(f"spot taker-buy {s_share:.0%} (sellers initiating)")

    # 4. persistence
    s_trend = s["persistence"].get("trend")
    if s_trend == "accelerating-buy":
        score += 1; reasons.append("spot flow ACCELERATING in buy direction")
    elif s_trend == "accelerating-sell":
        score -= 1; reasons.append("spot flow ACCELERATING in sell direction")

    # ----- futures confirmation -----
    f_skew = f["microprice"].get("skew_bps") if isinstance(f["microprice"], dict) else None
    f_share = f["flow"].get("buy_share")
    f_cvd_not = f["flow"].get("cvd_notional") or 0.0
    f_cvd_pos = f_cvd_not > 0

    contested = False
    if s_share is not None and f_share is not None:
        # spot say +1, futures say -1 or vice versa
        spot_buy = s_share > 0.55
        fut_buy  = f_share > 0.55
        if spot_buy != fut_buy:
            contested = True
            reasons.append(
                f"contested: spot taker-buy {s_share:.0%} vs futures taker-buy {f_share:.0%} (no consensus)"
            )
        else:
            reasons.append(f"spot + futures taker-buy aligned ({s_share:.0%} / {f_share:.0%})")

    # 5. derivatives overlay
    taker = d.get("taker_buy_ratio")
    if taker is not None:
        if taker > 1.05:
            score += 1; reasons.append(f"futures taker b/s {taker:.2f} (15m, buyers aggressive)")
        elif taker < 0.95:
            score -= 1; reasons.append(f"futures taker b/s {taker:.2f} (15m, sellers aggressive)")

    funding = d.get("funding")
    if funding is not None:
        if funding > 0.0005:
            reasons.append(f"funding {funding:+.4%} (longs crowded, paying)")
        elif funding < -0.0005:
            reasons.append(f"funding {funding:+.4%} (shorts paying, squeeze fuel if OI rising)")
        else:
            reasons.append(f"funding {funding:+.4%} (neutral)")

    oi_chg = d.get("oi_change_pct")
    if oi_chg is not None:
        if oi_chg > 1.5:
            reasons.append(f"OI {oi_chg:+.2f}% (new positions opening)")
        elif oi_chg < -1.5:
            reasons.append(f"OI {oi_chg:+.2f}% (positions closing)")
        else:
            reasons.append(f"OI {oi_chg:+.2f}% (flat — churn, not conviction)")

    # ----- verdict -----
    if contested:
        verdict = "AUCTION CONTESTED — buyers and sellers at parity, no consensus"
    elif score >= 4:
        verdict = "BUYERS WINNING THE AUCTION (clear dominance)"
    elif score >= 2:
        verdict = "BUYERS EDGE (slight lead)"
    elif score <= -4:
        verdict = "SELLERS WINNING THE AUCTION (clear dominance)"
    elif score <= -2:
        verdict = "SELLERS EDGE (slight lead)"
    else:
        verdict = "AUCTION IN BALANCE (waiting for catalyst)"

    return verdict, reasons


# ---------- renderer ----------


def _f(x, places=4):
    if x is None: return "-"
    if isinstance(x, float): return f"{x:.{places}f}"
    return str(x)


def _usd(n):
    if n is None: return "-"
    return f"${n:,.0f}"


def render(symbol: str, state: dict, verdict: str, reasons: list[str]) -> str:
    L: list[str] = []
    a = L.append
    a(f"=== {symbol}  AUCTION DYNAMICS — who is winning? ===")
    a(f"    snapshot @ {time.strftime('%H:%M:%S')}   latency: {state.get('latency_ms','?')}ms")
    a("")

    s, f, der = state["spot"], state["futures"], state["derivs"]

    for venue_name, v in (("SPOT", s), ("FUTURES", f)):
        a(f"--- {venue_name} micro-auction ---")
        mp = v["microprice"]
        if mp:
            a(f"  best bid/ask : {mp.get('best_bid')} / {mp.get('best_ask')}")
            a(f"  mid          : {_f(mp.get('mid'))}    microprice: {_f(mp.get('microprice'))}")
            a(f"  spread       : {_f(mp.get('spread_bps'), 2)} bps")
            a(f"  microprice skew: {mp.get('skew_bps', 0):+.2f} bps (>0 = buyers dictating)")
            d = mp.get("depth") or {}
            for slice_name in ("top5", "top10", "top20", "total"):
                sl = d.get(slice_name) or {}
                obi = sl.get("obi")
                if obi is not None:
                    a(f"  depth {slice_name:5s}  : bid={_f(sl.get('bid'))}  ask={_f(sl.get('ask'))}   OBI={obi:+.3f}")
        fl = v["flow"]
        if fl:
            a(f"  initiated    : buy {fl['n_buy_trades']} trades / {_usd(fl['aggressive_buy_notional'])}")
            a(f"                 sell {fl['n_sell_trades']} trades / {_usd(fl['aggressive_sell_notional'])}")
            a(f"  buy-share    : {fl.get('buy_share') or 0:.0%}    CVD notional: {_usd(fl.get('cvd_notional'))}")
        per = v["persistence"]
        if per and per.get("trend") != "insufficient-data":
            a(f"  persistence  : {per['trend']}  (recent {per['recent_buy_share']:.0%} vs older {per['older_buy_share']:.0%} buy-share)")
        a("")

    a("--- Derivatives auction ---")
    a(f"  funding rate     : {_f(der.get('funding'), 6)}")
    a(f"  taker b/s (15m)  : {_f(der.get('taker_buy_ratio'))}")
    a(f"  OI change (15m)  : {_f(der.get('oi_change_pct'))}%")
    a("")

    a("--- AUCTION VERDICT ---")
    a(f"  >>> {verdict}")
    for r in reasons:
        a(f"      - {r}")
    a("")
    return "\n".join(L)


# ---------- entry ----------


async def main(symbol: str, depth_limit: int) -> int:
    d = await pull(symbol, depth_limit)
    state = auction_state(d)
    state["latency_ms"] = d["latency_ms"]
    verdict, reasons = auction_verdict(state)
    print(render(symbol, state, verdict, reasons))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--depth", type=int, default=50)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    sys.exit(asyncio.run(main(args.symbol, args.depth)))
