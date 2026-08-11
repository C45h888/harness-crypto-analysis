"""
Demand-source diagnostic for a USD-M perp.

Answers the question: "Is the bid being defended by REAL spot demand,
or is it leveraged long/short paper that will evaporate?"

Inputs (all public, no keys needed):
  Binance Spot:        trades, book, 24h ticker
  Binance USD-M Fut:   trades, book, 24h ticker, mark, funding, OI,
                       taker buy/sell, top-trader L/S

Outputs a single verdict:
  SPOT-DEMAND          real bids; durable
  LEV-LONG-BUILD       futures buyers + positive funding; reversible
  LEV-SHORT-BUILD      futures sellers (or absorbing buyers w/ neg funding);
                       squeeze risk if OI is high
  DISTRIBUTION         spot sellers > buyers despite price support
  MIXED-ABSORPTION     both present; ambiguous

Plus a BTC macro overlay so we know if the broader tape supports longs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

from binance import Binance, normalize_fut_trade, normalize_spot_trade
from flow import summarize

log = logging.getLogger(__name__)


# ---------- diagnostic core ----------


async def pull(symbol: str, trade_limit: int, depth_limit: int) -> dict:
    """All reads for one symbol, in parallel. Both venues."""
    async with Binance() as b:
        t0 = time.time()
        (s_trades, s_book, s_24h, f_trades, f_book, f_24h, f_mark, f_fund, f_oi,
         f_oi_hist, f_tbs, f_top_ls, f_ls) = await asyncio.gather(
            b.spot_trades(symbol, limit=trade_limit),
            b.spot_book(symbol, limit=depth_limit),
            b.spot_24h(symbol),
            b.fut_trades(symbol, limit=trade_limit),
            b.fut_book(symbol, limit=depth_limit),
            b.fut_24h(symbol),
            b.fut_mark_price(symbol),
            b.fut_funding(symbol),
            b.fut_open_interest(symbol),
            b.fut_open_interest_history(symbol, period="15m", limit=20),
            b.fut_taker_buy_sell(symbol, period="15m"),
            b.fut_top_long_short_accounts(symbol, period="15m"),
            b.fut_long_short_ratio(symbol, period="15m"),
            return_exceptions=True,
        )
        latency_ms = round((time.time() - t0) * 1000, 1)
    return {
        "latency_ms": latency_ms,
        "spot": {
            "trades": [dict(normalize_spot_trade(t), venue="spot") for t in (s_trades if not isinstance(s_trades, Exception) else [])],
            "book": s_book if not isinstance(s_book, Exception) else {},
            "ticker_24h": s_24h if not isinstance(s_24h, Exception) else {},
        },
        "futures": {
            "trades": [normalize_fut_trade(t) for t in (f_trades if not isinstance(f_trades, Exception) else [])],
            "book": f_book if not isinstance(f_book, Exception) else {},
            "ticker_24h": f_24h if not isinstance(f_24h, Exception) else {},
            "mark_price": f_mark if not isinstance(f_mark, Exception) else {},
            "funding": f_fund if not isinstance(f_fund, Exception) else {},
            "open_interest": f_oi if not isinstance(f_oi, Exception) else {},
            "oi_history": f_oi_hist if not isinstance(f_oi_hist, Exception) else {},
            "taker_buy_sell": f_tbs if not isinstance(f_tbs, Exception) else {},
            "top_ls": f_top_ls if not isinstance(f_top_ls, Exception) else {},
            "global_ls": f_ls if not isinstance(f_ls, Exception) else {},
        },
    }


def _ls_row(x):
    if isinstance(x, list) and x:
        return x[-1] if isinstance(x[-1], dict) else None
    if isinstance(x, dict):
        return x
    return None


def _funding_rate(fut: dict) -> float | None:
    for src in (fut.get("mark_price") or {}, fut.get("funding") or {}):
        for k in ("last_funding_rate", "lastFundingRate"):
            if k in src and src[k] is not None:
                try:
                    return float(src[k])
                except (TypeError, ValueError):
                    pass
    return None


def decompose_demand(d: dict) -> dict:
    """
    Compute the spot-vs-leverage decomposition.

    Diagnostics returned:
      spot_cvd         : spot cumulative volume delta (base units)
      fut_cvd          : futures CVD
      spot_obi         : spot top-20 order-book imbalance [-1, +1]
      fut_obi          : futures top-20 OBI
      spot_lopsided    : spot buy:notional ratio in recent large prints
      fut_lopsided     : futures buy:notional ratio in recent large prints
      oi               : current OI (contracts/coins)
      oi_change_pct    : change in OI over recent history
      oi_trend         : 'rising', 'falling', or 'flat' based on OI delta
      funding_rate     : current funding
      long_crowd_pct   : global long account ratio
      top_long_pct     : top-trader long account ratio
      taker_buy_ratio  : taker buy/sell ratio (15m)
    """
    spot_flow = summarize(d["spot"]["trades"], d["spot"]["book"])
    fut_flow  = summarize(d["futures"]["trades"], d["futures"]["book"])

    # large-print notional split
    def _split_large(trades: list[dict]) -> tuple[float, float]:
        buy = sell = 0.0
        for t in trades:
            n = t["price"] * t["qty"]
            if t["is_buyer_maker"]:
                sell += n
            else:
                buy += n
        return buy, sell

    s_buy, s_sell = _split_large(d["spot"]["trades"])
    f_buy, f_sell = _split_large(d["futures"]["trades"])
    s_tot = s_buy + s_sell
    f_tot = f_buy + f_sell

    oi = d["futures"].get("open_interest") or {}
    oi_val = float(oi["open_interest"]) if isinstance(oi, dict) and oi.get("open_interest") else None

    # OI history -> trend
    oi_hist = d["futures"].get("oi_history") or {}
    oi_change_pct = None
    oi_trend = "n/a"
    oi_series: list[tuple[int, float]] = []

    def _row_extract(row: Any) -> tuple[int | None, float | None]:
        if not isinstance(row, dict):
            return None, None
        ts = row.get("timestamp") or row.get("time")
        o = (row.get("sumOpenInterest") or row.get("sum_open_interest")
             or row.get("openInterest") or row.get("open_interest"))
        try: return (int(ts) if ts is not None else None), (float(o) if o is not None else None)
        except (TypeError, ValueError): return None, None

    if isinstance(oi_hist, list):
        for row in oi_hist:
            ts, o = _row_extract(row)
            if ts is not None and o is not None:
                oi_series.append((ts, o))
    elif isinstance(oi_hist, dict):
        if "data" in oi_hist and isinstance(oi_hist["data"], list):
            for row in oi_hist["data"]:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try: oi_series.append((int(row[0]), float(row[1])))
                    except (TypeError, ValueError): pass
                else:
                    ts, o = _row_extract(row)
                    if ts is not None and o is not None:
                        oi_series.append((ts, o))

    if len(oi_series) >= 2:
        first, last = oi_series[0][1], oi_series[-1][1]
        if first > 0:
            oi_change_pct = (last - first) / first * 100.0
            if oi_change_pct > 1.5: oi_trend = "rising"
            elif oi_change_pct < -1.5: oi_trend = "falling"
            else: oi_trend = "flat"
        else:
            oi_trend = "flat"

    # L/S accounts
    g_row = _ls_row(d["futures"].get("global_ls"))
    t_row = _ls_row(d["futures"].get("top_ls"))
    long_pct = None
    top_long_pct = None
    if isinstance(g_row, dict):
        for k in ("long_account", "longAccount"):
            if k in g_row and g_row[k] is not None:
                try:
                    long_pct = float(g_row[k]); break
                except (TypeError, ValueError): pass
    if isinstance(t_row, dict):
        for k in ("long_account", "longAccount"):
            if k in t_row and t_row[k] is not None:
                try:
                    top_long_pct = float(t_row[k]); break
                except (TypeError, ValueError): pass

    # taker b/s
    tbs_row = _ls_row(d["futures"].get("taker_buy_sell"))
    taker_buy_ratio = None
    if isinstance(tbs_row, dict):
        for k in ("buy_sell_ratio", "buySellRatio"):
            if k in tbs_row and tbs_row[k] is not None:
                try:
                    taker_buy_ratio = float(tbs_row[k]); break
                except (TypeError, ValueError): pass

    return {
        "latency_ms": d["latency_ms"],
        "spot": {
            "last":     spot_flow.get("last_price"),
            "vwap":     spot_flow.get("vwap"),
            "cvd":      spot_flow.get("cvd"),
            "obi":      spot_flow.get("obi"),
            "spread":   spot_flow.get("spread_bps"),
            "trades":   spot_flow.get("trade_count"),
            "large":    spot_flow.get("large_trade_count"),
            "buy_notional": s_buy,
            "sell_notional": s_sell,
            "buy_share": (s_buy / s_tot) if s_tot else None,
            "ticker_24h": d["spot"]["ticker_24h"],
        },
        "futures": {
            "last":     fut_flow.get("last_price"),
            "vwap":     fut_flow.get("vwap"),
            "cvd":      fut_flow.get("cvd"),
            "obi":      fut_flow.get("obi"),
            "spread":   fut_flow.get("spread_bps"),
            "trades":   fut_flow.get("trade_count"),
            "large":    fut_flow.get("large_trade_count"),
            "buy_notional": f_buy,
            "sell_notional": f_sell,
            "buy_share": (f_buy / f_tot) if f_tot else None,
            "mark_price": d["futures"].get("mark_price"),
            "ticker_24h": d["futures"]["ticker_24h"],
        },
        "derivs": {
            "oi":           oi_val,
            "oi_change_pct": oi_change_pct,
            "oi_trend":     oi_trend,
            "funding":      _funding_rate(d["futures"]),
            "long_pct":     long_pct,
            "top_long_pct": top_long_pct,
            "taker_buy_ratio": taker_buy_ratio,
        },
    }


def demand_verdict(dx: dict) -> tuple[str, list[str]]:
    """
    The single most important call: where is the bid coming from?

    Returns BOTH a headline verdict AND a list of "what would change this"
    signals. The interesting case is when spot and futures DISAGREE.

    Signals:
      spot_cvd         : positive => real demand, negative => real distribution
      fut_cvd          : positive => paper buying, negative => paper selling
      funding          : positive => longs paying (long-crowded),
                          negative => shorts paying (short-crowded OR pre-squeeze)
      oi               : open interest (stable vs rising vs falling)
      long_pct         : global retail long %
      top_long_pct     : top-trader long %
      taker_buy_ratio  : aggressive buyer/seller ratio on futures
    """
    s, f, der = dx["spot"], dx["futures"], dx["derivs"]
    reasons: list[str] = []

    s_cvd = s["cvd"] or 0.0
    f_cvd = f["cvd"] or 0.0
    s_share = s["buy_share"] if s["buy_share"] is not None else 0.5
    f_share = f["buy_share"] if f["buy_share"] is not None else 0.5
    funding = der["funding"] or 0.0
    oi = der["oi"]
    long_pct = der["long_pct"]
    top_long_pct = der["top_long_pct"]
    taker_buy_ratio = der["taker_buy_ratio"]

    # ----- raw signal facts -----
    spot_buying = s_cvd > 0 and s_share > 0.55
    spot_selling = s_cvd < 0 and s_share < 0.45
    fut_buying = f_cvd > 0 and f_share > 0.55
    fut_selling = f_cvd < 0 and f_share < 0.45

    # ----- baseline reasons -----
    reasons.append(
        f"spot CVD {s_cvd:+.4f} ({'BUY' if s_cvd > 0 else 'SELL'}); "
        f"spot buy-share {s_share:.0%}"
    )
    reasons.append(
        f"futures CVD {f_cvd:+.4f} ({'BUY' if f_cvd > 0 else 'SELL'}); "
        f"futures buy-share {f_share:.0%}"
    )
    reasons.append(f"funding {funding:+.4%} ({'shorts pay' if funding < 0 else 'longs pay'})")
    if long_pct is not None:
        reasons.append(f"crowd {long_pct:.0%} long, top traders {top_long_pct:.0%} long")

    # ----- spot vs futures divergence -----
    divergence = ""
    if spot_buying and fut_selling:
        # Real buyers vs paper sellers
        if funding <= 0 and top_long_pct and top_long_pct > long_pct:
            divergence = "SPOT-BID / FUTURES-DISTRIBUTION (smart-money short paper to retail longs)"
        else:
            divergence = "SPOT-BID / FUTURES-SELLING (paper friction against real bid)"
    elif spot_selling and fut_buying:
        divergence = "SPOT-DISTRIBUTION / FUTURES-BID (paper support, real sellers)"
    elif spot_buying and fut_buying:
        divergence = "BOTH-BUYING (broad demand, watch for over-leverage)"
    elif spot_selling and fut_selling:
        divergence = "BOTH-SELLING (broad distribution, weak tape)"

    # ----- verdict -----
    if spot_buying and fut_selling and funding <= 0:
        # Real bid defended against paper sellers who pay to be short.
        # This is the classic "smart money accumulating while retail longs get trapped".
        if long_pct and long_pct > 0.65:
            verdict = "SPOT-ACCUMULATION-vs-FUT-DISTRIBUTION (real bid, but retail crowded long — squeeze risk both ways)"
        else:
            verdict = "SPOT-ACCUMULATION-vs-FUT-DISTRIBUTION (real bid, smart-money absorbing leverage)"
    elif spot_buying and not fut_selling:
        verdict = "SPOT-DEMAND (durable bid)"
    elif spot_selling and fut_buying:
        verdict = "DISTRIBUTION-ON-RIP (real sellers to paper buyers — weak tape)"
    elif fut_selling and abs(f_cvd) > 3 * max(abs(s_cvd), 1):
        # Paper-driven move; no real demand
        verdict = "FUTURES-DOMINATED (paper selling, thin spot — fragile)"
    elif spot_buying and fut_buying and funding > 0.0005:
        verdict = "LEVERAGE-OVERHEATED (real + paper demand, long-crowded, funding spiking)"
    elif fut_buying and abs(f_cvd) > 3 * max(abs(s_cvd), 1):
        verdict = "LEV-LONG-BUILD (paper longs only — reversible)"
    elif divergence:
        verdict = divergence
    else:
        verdict = "MIXED-ABSORPTION (no clear direction)"

    # ----- add macro-relevant sub-signals -----
    if top_long_pct is not None and long_pct is not None:
        if top_long_pct < long_pct - 0.05:
            reasons.append(f"  ↳ SMART-MONEY FADING CROWD (top {top_long_pct:.0%} vs crowd {long_pct:.0%})")
        elif top_long_pct > long_pct + 0.05:
            reasons.append(f"  ↳ SMART-MONEY WITH CROWD (top {top_long_pct:.0%} vs crowd {long_pct:.0%})")

    if funding <= -0.0003:
        reasons.append("  ↳ NEGATIVE FUNDING with paper sellers = shorts paying longs = short-squeeze fuel if OI keeps rising")

    return verdict, reasons


# ---------- BTC macro overlay ----------


async def btc_macro(symbol: str = "BTCUSDT", trade_limit: int = 200) -> dict:
    """
    Quick BTC context to know if the broader tape supports longs or risk-off.
    Returns: dict with BTC trend + ETH/SOL correlation in same direction or not.
    """
    async with Binance() as b:
        t0 = time.time()
        btc_24h, btc_fund, btc_oi, eth_24h, eth_fund, eth_oi = await asyncio.gather(
            b.fut_24h(symbol),
            b.fut_funding(symbol),
            b.fut_open_interest(symbol),
            b.fut_24h("ETHUSDT"),
            b.fut_funding("ETHUSDT"),
            b.fut_open_interest("ETHUSDT"),
            return_exceptions=True,
        )

    def _chg(t):
        if isinstance(t, Exception): return None
        if not isinstance(t, dict): return None
        try: return float(t.get("price_change_percent"))
        except (TypeError, ValueError): return None

    def _f(f):
        if isinstance(f, Exception): return None
        if not isinstance(f, dict): return None
        for k in ("lastFundingRate", "last_funding_rate"):
            if k in f and f[k] is not None:
                try: return float(f[k])
                except (TypeError, ValueError): continue
        return None

    return {
        "btc": {"change_pct": _chg(btc_24h), "funding": _f(btc_fund)},
        "eth": {"change_pct": _chg(eth_24h), "funding": _f(eth_fund)},
        "latency_ms": round((time.time() - t0) * 1000, 1),
    }


def macro_climate(macro: dict, target_change_pct: float | None) -> str:
    """
    Is the macro environment supportive of longs / new highs?

    Inputs:
      btc change_pct: 24h change
      btc funding:    funding rate
      eth change_pct: 24h change
      target_change_pct: 24h change of the symbol we're analyzing

    Logic:
      - If BTC and ETH both red and target is red but less red than BTC/ETH
        -> SOL might be defensive, not bullish
      - If BTC red but SOL flat/green
        -> relative strength but counter-trend
      - If BTC and ETH green and SOL green
        -> tailwind for longs
    """
    btc = macro["btc"]
    eth = macro["eth"]

    if btc["change_pct"] is None or eth["change_pct"] is None:
        return "MACRO: data unavailable"

    btc_chg = btc["change_pct"]
    eth_chg = eth["change_pct"]
    sol_chg = target_change_pct or 0.0

    if btc_chg < -1.5 and eth_chg < -1.5:
        if sol_chg > btc_chg + 0.5:
            return f"MACRO RISK-OFF (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); SOL {sol_chg:+.1f}% = RELATIVE BID but counter-trend longs are risky"
        else:
            return f"MACRO RISK-OFF (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); selling pressure broad; longs not supported"
    elif btc_chg > 1.0 and eth_chg > 1.0:
        return f"MACRO RISK-ON (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); tailwind for alts"
    elif -1.5 < btc_chg < 1.0:
        return f"MACRO NEUTRAL-CHOPPY (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); mean-reversion regime, breakouts fickle"
    else:
        return f"MACRO MIXED (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); reduce size"


# ---------- renderer ----------


def _f(x, places=4):
    if x is None: return "-"
    if isinstance(x, float): return f"{x:.{places}f}"
    return str(x)


def _pct(x):
    if x is None: return "-"
    try: v = float(x)
    except Exception: return str(x)
    return f"{v:+.2f}%" if abs(v) > 1 else f"{v:+.3%}"


def _usd(n):
    if n is None: return "-"
    return f"${n:,.0f}"


def render(symbol: str, dx: dict, verdict: str, reasons: list[str], macro: dict, macro_line: str) -> str:
    s, f, der = dx["spot"], dx["futures"], dx["derivs"]
    L: list[str] = []
    a = L.append

    a(f"=== {symbol}  DEMAND-SOURCE DIAGNOSTIC ===")
    a(f"    latency: {dx['latency_ms']}ms   snapshot @ {time.strftime('%H:%M:%S')}")
    a("")

    a("--- Spot (real demand) ---")
    a(f"  last            : {s['last']}   VWAP: {_f(s['vwap'])}")
    a(f"  CVD             : {_f(s['cvd'])}   (signed buy-minus-sell volume)")
    a(f"  OBI top-20      : {_f(s['obi'])}")
    a(f"  large prints    : {s['large']}  buy-share: {s['buy_share']:.0%}" if s['buy_share'] is not None else "")
    a(f"  buy notional    : {_usd(s['buy_notional'])}")
    a(f"  sell notional   : {_usd(s['sell_notional'])}")
    if s.get("ticker_24h"):
        t = s["ticker_24h"]
        a(f"  24h             : {_pct(t.get('price_change_percent'))}   vol={t.get('volume')}  quoteVol={t.get('quote_volume')}")
    a("")

    a("--- Futures (leveraged paper) ---")
    a(f"  last            : {f['last']}   VWAP: {_f(f['vwap'])}")
    a(f"  CVD             : {_f(f['cvd'])}")
    a(f"  OBI top-20      : {_f(f['obi'])}")
    a(f"  large prints    : {f['large']}  buy-share: {f['buy_share']:.0%}" if f['buy_share'] is not None else "")
    a(f"  buy notional    : {_usd(f['buy_notional'])}")
    a(f"  sell notional   : {_usd(f['sell_notional'])}")
    if f.get("ticker_24h"):
        t = f["ticker_24h"]
        a(f"  24h             : {_pct(t.get('price_change_percent'))}   vol={t.get('volume')}")
    a("")

    a("--- Derivatives positioning ---")
    a(f"  open interest   : {_f(der['oi'], 0)}   ({der['oi_trend']}, {der['oi_change_pct']:+.2f}%)" if der.get('oi_change_pct') is not None else f"  open interest   : {_f(der['oi'], 0)}")
    a(f"  funding rate    : {_f(der['funding'], 6)}")
    a(f"  long crowd      : {_pct(der['long_pct'])} of accounts long" if der['long_pct'] is not None else "  long crowd      : n/a")
    a(f"  top-trader long : {_pct(der['top_long_pct'])}" if der['top_long_pct'] is not None else "  top-trader long : n/a")
    a(f"  taker b/s 15m   : {_f(der['taker_buy_ratio'])}")
    a("")

    a("--- Demand source verdict ---")
    a(f"  >>> {verdict}")
    for r in reasons:
        a(f"      - {r}")
    a("")

    a("--- Macro climate (BTC / ETH) ---")
    a(f"  {macro_line}")
    a(f"  btc 24h : {_pct(macro['btc']['change_pct'])}   funding: {_f(macro['btc']['funding'], 6)}")
    a(f"  eth 24h : {_pct(macro['eth']['change_pct'])}   funding: {_f(macro['eth']['funding'], 6)}")
    a("")

    a("--- Trade-set implications ---")
    if "SPOT-DEMAND" in verdict:
        a("  · Bid is durable; lean with the bid; stops under the shelf.")
        a("  · Avoid fading on weak hand; wait for spot CVD to confirm continuation.")
    elif "LEV-LONG-BUILD" in verdict:
        a("  · Bid is paper; reversal risk if funding spikes or OI unwinds.")
        a("  · Use tight stops; if funding > 0.05% consider scaling out.")
    elif "LEV-SHORT-BUILD" in verdict:
        a("  · New shorts absorbing bid; squeeze risk high if catalyst lands.")
        a("  · Could be distribution OR pre-breakout; watch OI delta.")
    elif "DISTRIBUTION" in verdict:
        a("  · Smart money selling to retail; do NOT buy dips.")
        a("  · Look for shorts below the shelf, not longs above.")
    elif "ABSORPTION" in verdict or "MIXED" in verdict:
        a("  · Both sides present; wait for a directional break.")
        a("  · Reduce size until the bid source resolves.")
    a("")

    return "\n".join(L)


# ---------- entry ----------


async def main(symbol: str, trade_limit: int, depth_limit: int) -> int:
    d = await pull(symbol, trade_limit, depth_limit)
    dx = decompose_demand(d)
    verdict, reasons = demand_verdict(dx)
    macro = await btc_macro()
    target_chg = None
    ft = dx["futures"].get("ticker_24h") or {}
    if isinstance(ft, dict):
        try: target_chg = float(ft.get("price_change_percent"))
        except (TypeError, ValueError): pass
    macro_line = macro_climate(macro, target_chg)
    print(render(symbol, dx, verdict, reasons, macro, macro_line))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("symbol", nargs="?", default="SOLUSDT")
    p.add_argument("--trades", type=int, default=500)
    p.add_argument("--depth", type=int, default=50)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    rc = asyncio.run(main(args.symbol, args.trades, args.depth))
    sys.exit(rc)
