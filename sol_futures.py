"""
SOLUSDT USD-M perpetual futures — focused order-flow + market regime analysis.

Pulls the futures tape (no spot), runs every flow primitive in `flow.py`,
threads in CryptoQuant on-chain context (asset="alt", token="sol"), and
renders a single human-readable regime read.

What we capture from Binance USD-M futures:
  - mark price + index price + premium (basis)
  - funding rate + next funding time
  - open interest
  - recent aggregate trades (raw)
  - aggregated taker buy/sell volume
  - global long/short ratio
  - top-trader long/short ratio (accounts + positions)

What we compute locally with `flow.py`:
  - VWAP, stddev, last price, spread, OBI
  - buy/sell volume, CVD, buy:sell ratio, large trades

What we pull from CryptoQuant MCP:
  - descriptions + thresholds for the most-watched alt metrics
    (funding-rates, exchange-netflow, mvrv)
  - live numerics where accessible

Outputs a single "regime verdict" at the bottom:
  TREND / CHOP / SQUEEZE / EXHAUSTION / LIQUIDATION-CASCADE

Usage:
    .venv/bin/python sol_futures.py
    .venv/bin/python sol_futures.py --trades 1000 --depth 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from binance import Binance, normalize_fut_trade
from flow import bucketed_cvd, cvd_series_corr, summarize

log = logging.getLogger(__name__)

SYMBOL = "SOLUSDT"
ASSET = "alt"          # CryptoQuant asset bucket
TOKEN = "sol"          # token filter where supported
WINDOW_S_DEFAULT = 30  # CVD bucket window


# ---------- CryptoQuant side ----------


def cryptoquant_context() -> dict:
    """SOL on-chain context via the CryptoQuant MCP."""
    try:
        from cryptoquant_client import describe_metric, discover_endpoints, query_data
    except Exception as e:
        return {"available": False, "reason": f"import failed: {e}"}

    out: dict[str, Any] = {
        "available": True,
        "asset": ASSET,
        "token": TOKEN,
        "plan": "basic",
        "metrics": {},
        "endpoints_seen": 0,
    }

    # 1. descriptions of the most-watched alt metrics
    for m in ["funding-rates", "exchange-netflow", "mvrv", "sopr", "whale-ratio"]:
        try:
            d = describe_metric(m)
            if isinstance(d, dict) and d.get("success"):
                out["metrics"][m] = d["metric"]
            else:
                out["metrics"][m] = {"description": "(locked on basic plan)", "thresholds": None}
        except Exception as e:
            out["metrics"][m] = {"description": f"(error: {e})", "thresholds": None}

    # 2. try a live numeric pull on the most actionable endpoint
    try:
        live = query_data(
            f"/v1/{ASSET}/market-data/funding-rates",
            {"token": TOKEN, "limit": 10, "window": "day"},
        )
        if isinstance(live, dict) and live.get("success"):
            out["live_funding_rates"] = live.get("data", [])
            out["endpoints_seen"] += 1
        else:
            out["live_funding_rates"] = "(locked on basic plan)"
    except Exception as e:
        out["live_funding_rates"] = f"(error: {e})"

    # 3. count what's actually accessible
    try:
        ep = discover_endpoints(ASSET, "market-data")
        if isinstance(ep, dict):
            out["endpoints_total"] = ep.get("total_endpoints", 0)
            out["endpoints_accessible"] = ep.get("accessible", 0)
    except Exception:
        pass

    return out


# ---------- Binance USD-M futures pulls ----------


async def _pull_futures(b: Binance, symbol: str, trade_limit: int, depth_limit: int) -> dict:
    """All futures reads we care about, in parallel."""
    return await asyncio.gather(
        b.fut_trades(symbol, limit=trade_limit),
        b.fut_book(symbol, limit=depth_limit),
        b.fut_24h(symbol),
        b.fut_funding(symbol),
        b.fut_open_interest(symbol),
        b.fut_mark_price(symbol),
        b.fut_premium_kline(symbol),
        b.fut_long_short_ratio(symbol, period="15m"),
        b.fut_taker_buy_sell(symbol, period="15m"),
        b.fut_top_long_short_accounts(symbol, period="15m"),
        return_exceptions=True,
    )


# ---------- regime classifier ----------


def regime_verdict(d: dict) -> tuple[str, list[str]]:
    """
    Map the metrics to a regime label + reasons.

    Inputs we fold in:
      - price vs vwap
      - CVD direction + magnitude
      - book imbalance (OBI)
      - taker buy/sell ratio (aggressive flow)
      - funding rate + sign + magnitude
      - global L/S ratio (long crowd?)
      - top trader L/S ratio (smart money positioning)
      - large-print imbalance on the recent tape
    """
    reasons: list[str] = []
    labels: list[str] = []

    fut = d["futures"]
    flow = fut["flow"]
    oi  = fut.get("open_interest") or {}
    fund = fut.get("funding") or {}
    mark = fut.get("mark_price") or {}

    last = flow.get("last_price") or 0.0
    vwap = flow.get("vwap") or 0.0
    cvd = flow.get("cvd") or 0.0
    obi = flow.get("obi") or 0.0
    bs = flow.get("buy_sell_ratio") or 0.0
    large = flow.get("large_trades") or []

    # 1. price vs vwap
    if last > vwap * 1.0005:
        labels.append("BULL")
        reasons.append(f"last > vwap ({last:.4f} > {vwap:.4f})")
    elif last < vwap * 0.9995:
        labels.append("BEAR")
        reasons.append(f"last < vwap ({last:.4f} < {vwap:.4f})")
    else:
        labels.append("CHOP")
        reasons.append(f"last ≈ vwap ({last:.4f} ~ {vwap:.4f})")

    # 2. CVD direction
    if abs(cvd) > 0:
        labels.append("AGGR-BUY" if cvd > 0 else "AGGR-SELL")
        reasons.append(f"CVD {cvd:+.3f} (buy/sell ratio {bs:.2f})")

    # 3. book imbalance
    if obi > 0.2:
        labels.append("BID-WALL")
        reasons.append(f"OBI {obi:+.3f} (bid > ask in top 20)")
    elif obi < -0.2:
        labels.append("ASK-WALL")
        reasons.append(f"OBI {obi:+.3f} (ask > bid in top 20)")

    # 4. funding rate (mark_price carries lastFundingRate too)
    rate = None
    for src in (mark, fund):
        for k in ("last_funding_rate", "lastFundingRate"):
            if k in src and src[k] is not None:
                try:
                    rate = float(src[k])
                    break
                except (TypeError, ValueError):
                    continue
        if rate is not None:
            break

    if rate is not None:
        if rate > 0.0005:
            labels.append("LONG-CROWDED")
            reasons.append(f"funding {rate:+.4%} (longs paying shorts)")
        elif rate > 0.0001:
            labels.append("LONG-LEAN")
            reasons.append(f"funding {rate:+.4%}")
        elif rate < -0.0005:
            labels.append("SHORT-CROWDED")
            reasons.append(f"funding {rate:+.4%} (shorts paying longs)")
        elif rate < -0.0001:
            labels.append("SHORT-LEAN")
            reasons.append(f"funding {rate:+.4%}")
        else:
            reasons.append(f"funding neutral {rate:+.4%}")

    # 5. global L/S ratio
    ls = fut.get("long_short_ratio")
    if isinstance(ls, list) and ls:
        ls_row = ls[-1]
    else:
        ls_row = ls if isinstance(ls, dict) else None

    ls_ratio = None
    long_pct = None
    if isinstance(ls_row, dict):
        for k in ("long_short_ratio", "longShortRatio"):
            if k in ls_row:
                try:
                    ls_ratio = float(ls_row[k]); break
                except (TypeError, ValueError): pass
        for k in ("long_account", "longAccount"):
            if k in ls_row:
                try:
                    long_pct = float(ls_row[k]); break
                except (TypeError, ValueError): pass

    if ls_ratio is not None:
        if ls_ratio > 1.8:
            labels.append("CROWD-LONG")
            reasons.append(f"L/S ratio {ls_ratio:.2f} ({(long_pct or ls_ratio/(ls_ratio+1))*100:.1f}% longs)")
        elif ls_ratio < 0.55:
            labels.append("CROWD-SHORT")
            reasons.append(f"L/S ratio {ls_ratio:.2f} ({(long_pct or ls_ratio/(ls_ratio+1))*100:.1f}% longs)")
        else:
            reasons.append(f"L/S ratio {ls_ratio:.2f}")

    # 6. top trader L/S (smart money divergence vs crowd)
    top_ls = fut.get("top_long_short_accounts")
    if isinstance(top_ls, list) and top_ls:
        top_ls_row = top_ls[-1]
    else:
        top_ls_row = top_ls if isinstance(top_ls, dict) else None

    if isinstance(top_ls_row, dict):
        for k in ("long_short_ratio", "longShortRatio"):
            if k in top_ls_row:
                try:
                    top_ls_r = float(top_ls_row[k])
                    reasons.append(f"top-trader L/S {top_ls_r:.2f}")
                    # divergence signal
                    if ls_ratio is not None:
                        if top_ls_r < ls_ratio * 0.8:
                            labels.append("SMART-VS-CROWD-BEAR")
                            reasons.append(f"smart money less long than crowd ({top_ls_r:.2f} vs {ls_ratio:.2f})")
                        elif top_ls_r > ls_ratio * 1.2:
                            labels.append("SMART-VS-CROWD-BULL")
                            reasons.append(f"smart money more long than crowd ({top_ls_r:.2f} vs {ls_ratio:.2f})")
                    break
                except (TypeError, ValueError):
                    pass

    # 7. large-print imbalance
    if large:
        buy_notional = sum(t["notional_usd"] for t in large if t["side"] == "buy")
        sell_notional = sum(t["notional_usd"] for t in large if t["side"] == "sell")
        if buy_notional + sell_notional > 0:
            l_ratio = buy_notional / (buy_notional + sell_notional)
            if l_ratio > 0.65:
                labels.append("LARGE-BUYS")
                reasons.append(f"large-print {l_ratio:.0%} buy-side")
            elif l_ratio < 0.35:
                labels.append("LARGE-SELLS")
                reasons.append(f"large-print {l_ratio:.0%} buy-side (sellers dominate)")

    # 8. OI presence
    if isinstance(oi, dict) and oi.get("open_interest"):
        reasons.append(f"OI {float(oi['open_interest']):,.0f}")

    # === REGIME combination ===
    bull  = "BULL" in labels
    bear  = "BEAR" in labels
    aggr_b = "AGGR-BUY" in labels
    aggr_s = "AGGR-SELL" in labels
    long_crowd  = "CROWD-LONG" in labels
    short_crowd = "CROWD-SHORT" in labels
    long_crowded = "LONG-CROWDED" in labels
    short_crowded = "SHORT-CROWDED" in labels
    smart_bear = "SMART-VS-CROWD-BEAR" in labels
    smart_bull = "SMART-VS-CROWD-BULL" in labels

    # trend with delta confirmation
    if bull and aggr_b:
        regime = "TREND-UP (long build confirmed)"
    elif bear and aggr_s:
        regime = "TREND-DOWN (short build confirmed)"
    # smart money fading the crowd
    elif long_crowd and smart_bear and (aggr_s or bear):
        regime = "TRAP-LONG (smart money fading retail longs)"
    elif short_crowd and smart_bull and (aggr_b or bull):
        regime = "TRAP-SHORT (smart money fading retail shorts)"
    # overleveraged
    elif long_crowded and aggr_s:
        regime = "LONG-SQUEEZE RISK (crowded + sellers winning)"
    elif short_crowded and aggr_b:
        regime = "SHORT-SQUEEZE RISK (crowded + buyers winning)"
    # absorption / squeeze by book
    elif "BID-WALL" in labels or "ASK-WALL" in labels:
        regime = "ABSORPTION / SQUEEZE"
    # chop fallback
    elif "CHOP" in labels:
        regime = "CHOP / RANGE"
    elif bull:
        regime = "WEAK-UP"
    elif bear:
        regime = "WEAK-DOWN"
    else:
        regime = "MIXED"

    return regime, reasons


# ---------- renderer ----------


def _f(x: Any, places: int = 4) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{places}f}"
    return str(x)


def _pct(x: Any) -> str:
    """Render a percent value.
    The Binance ticker gives priceChangePercent as a number in percent units
    (e.g. "-2.912" meaning -2.912%). Anything >1 in absolute value we treat as
    already-percent; smaller values we treat as fractional.
    """
    if x is None:
        return "-"
    try:
        v = float(x)
    except Exception:
        return str(x)
    if abs(v) > 1:
        return f"{v:+.2f}%"
    return f"{v:+.3%}"


def render(d: dict) -> str:
    sym = d["symbol"]
    fut = d["futures"]
    flow = fut["flow"]
    cq = d["cryptoquant"]
    L: list[str] = []
    a = L.append

    a(f"=== {sym} USD-M PERP  (futures-only flow + CryptoQuant context) ===")
    a(f"    latency: {d['latency_ms']}ms   snapshot @ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(d['generated_at_ms']/1000))}")
    a("")

    # ticker
    t = fut.get("ticker_24h") or {}
    a("--- 24h ticker ---")
    a(f"  open        : {t.get('open_price')}   last       : {t.get('last_price')}")
    a(f"  high / low  : {t.get('high_price')} / {t.get('low_price')}")
    a(f"  change      : {_pct(t.get('price_change_percent'))}   trades 24h: {t.get('count'):,}")
    a(f"  vol / quote : {t.get('volume')} / {t.get('quote_volume')}")
    a("")

    # mark / premium / funding / OI
    a("--- Derivatives positioning ---")
    mp = fut.get("mark_price") or {}
    if isinstance(mp, dict):
        a(f"  mark price  : {mp.get('mark_price')}")
        a(f"  index price : {mp.get('index_price')}")
        a(f"  basis       : {mp.get('basis')} ({mp.get('basis_rate')})")

    fund = fut.get("funding") or {}
    rate = None
    for k in ("last_funding_rate", "lastFundingRate"):
        if k in fund:
            rate = fund[k]
            break
    if rate is not None:
        a(f"  funding     : {rate}")
        nxt = fund.get("next_funding_time") or fund.get("nextFundingTime")
        if nxt:
            secs = (int(nxt) - int(time.time() * 1000)) / 1000
            a(f"  next in     : {secs/60:.1f} min")

    oi = fut.get("open_interest") or {}
    if isinstance(oi, dict) and oi:
        a(f"  open int.   : {oi.get('open_interest')} {oi.get('symbol','')}")

    def _last_row(x):
        if isinstance(x, list) and x:
            return x[-1] if isinstance(x[-1], dict) else None
        if isinstance(x, dict):
            return x
        return None

    def _lsfmt(x, label):
        r = _last_row(x)
        if not isinstance(r, dict):
            a(f"  {label:14s}: (n/a)")
            return None
        ratio = r.get("long_short_ratio") or r.get("longShortRatio")
        la = r.get("long_account") or r.get("longAccount")
        sa = r.get("short_account") or r.get("shortAccount")
        parts = []
        if ratio is not None: parts.append(f"ratio={ratio}")
        if la is not None:    parts.append(f"long={float(la)*100:.1f}%")
        if sa is not None:    parts.append(f"short={float(sa)*100:.1f}%")
        a(f"  {label:14s}: " + "  ".join(parts))
        return r

    _lsfmt(fut.get("long_short_ratio"), "global L/S")
    _lsfmt(fut.get("top_long_short_accounts"), "top-trader L/S")

    tbs = fut.get("taker_buy_sell")
    if isinstance(tbs, dict):
        parts = []
        if tbs.get("buy_sell_ratio"): parts.append(f"ratio={tbs['buy_sell_ratio']}")
        if tbs.get("buy_vol"):        parts.append(f"buy_vol={tbs['buy_vol']}")
        if tbs.get("sell_vol"):       parts.append(f"sell_vol={tbs['sell_vol']}")
        a(f"  taker b/s    : " + "  ".join(parts))
    elif isinstance(tbs, list) and tbs:
        last_tbs = tbs[-1] if isinstance(tbs[-1], dict) else None
        if last_tbs:
            parts = []
            if last_tbs.get("buy_sell_ratio"): parts.append(f"ratio={last_tbs['buy_sell_ratio']}")
            if last_tbs.get("buy_vol"):        parts.append(f"buy_vol={last_tbs['buy_vol']}")
            if last_tbs.get("sell_vol"):       parts.append(f"sell_vol={last_tbs['sell_vol']}")
            a(f"  taker b/s    : " + "  ".join(parts))
    a("")

    # flow summary
    a("--- Futures order flow (recent 200-1000 prints) ---")
    a(f"  best bid/ask : {flow.get('best_bid')} / {flow.get('best_ask')}    spread: {_f(flow.get('spread_bps'),2)} bps")
    a(f"  OBI (top-20) : {_f(flow.get('obi'), 4)}")
    a(f"  last         : {flow.get('last_price')}   VWAP: {_f(flow.get('vwap'), 4)}  (+/- {_f(flow.get('vwap_stddev'),4)})")
    a(f"  buy / sell   : {_f(flow.get('buy_vol'))} / {_f(flow.get('sell_vol'))}   CVD: {_f(flow.get('cvd'))}")
    bs = flow.get("buy_sell_ratio")
    a(f"  b:s ratio    : {bs if bs is None else f'{bs:.3f}'}")
    a(f"  trades       : {flow.get('trade_count')}   large: {flow.get('large_trade_count')}")
    if flow.get("large_trades"):
        a("  top large prints (USD notional):")
        for lt in flow["large_trades"][:5]:
            a(f"    {lt['side']:4s}  qty={lt['qty']:.4f}  px={lt['price']}  notional=${lt['notional_usd']:.0f}")
    a("")

    # CryptoQuant context
    a("--- CryptoQuant on-chain context ---")
    if not cq.get("available"):
        a(f"  (unavailable: {cq.get('reason')})")
    else:
        a(f"  asset bucket: {cq.get('asset')}  token: {cq.get('token')}  plan: {cq.get('plan','?')}")
        if "endpoints_accessible" in cq:
            a(f"  endpoints: {cq.get('endpoints_accessible')}/{cq.get('endpoints_total')} accessible")
        for name, info in (cq.get("metrics") or {}).items():
            if isinstance(info, dict):
                dsc = (info.get("description") or "")[:100]
                thr = info.get("thresholds") or ""
                a(f"  {name:18s} : {dsc}{'...' if len(info.get('description') or '')>100 else ''}")
                if thr:
                    a(f"  {' '*20}thresholds: {thr}")
        if cq.get("live_funding_rates") and isinstance(cq["live_funding_rates"], list):
            a("  recent on-chain funding rates (top exchanges):")
            for row in cq["live_funding_rates"][:5]:
                if isinstance(row, dict):
                    a(f"    {row}")
        elif isinstance(cq.get("live_funding_rates"), str):
            a(f"  live funding rates: {cq['live_funding_rates']}")
    a("")

    # regime verdict
    regime, reasons = d["regime"]
    a("--- REGIME ---")
    a(f"  >>> {regime}")
    for r in reasons:
        a(f"      - {r}")
    a("")
    return "\n".join(L)


# ---------- core ----------


async def analyze(symbol: str = SYMBOL, trade_limit: int = 500, depth_limit: int = 50, bucket_window_s: int = WINDOW_S_DEFAULT) -> dict:
    out: dict[str, Any] = {"symbol": symbol, "generated_at_ms": int(time.time() * 1000)}

    async with Binance() as b:
        t0 = time.time()
        (trades_raw, book, fut_24h, funding, oi, mark_price, premium_kline, ls, taker_bs, top_ls_acc) = await _pull_futures(b, symbol, trade_limit, depth_limit)
        out["latency_ms"] = round((time.time() - t0) * 1000, 1)

    # normalize
    if isinstance(trades_raw, Exception):
        log.error("fut_trades failed: %s", trades_raw); trades = []
    else:
        trades = [normalize_fut_trade(t) for t in (trades_raw or [])]

    if isinstance(book, Exception):
        log.error("fut_book failed: %s", book); book = {}

    if isinstance(mark_price, Exception):
        mark_price = {}
    if isinstance(premium_kline, Exception):
        premium_kline = {}
    if isinstance(ls, Exception):
        ls = {}
    if isinstance(taker_bs, Exception):
        taker_bs = {}
    if isinstance(top_ls_acc, Exception):
        top_ls_acc = {}

    out["futures"] = {
        "ticker_24h": fut_24h,
        "mark_price": mark_price,
        "premium_kline": premium_kline,
        "funding": funding,
        "open_interest": oi,
        "long_short_ratio": ls,
        "top_long_short_accounts": top_ls_acc,
        "taker_buy_sell": taker_bs,
        "order_book": {"levels": depth_limit, "raw": book},
        "flow": summarize(trades, book),
        "bucketed_cvd": bucketed_cvd(trades, window_s=bucket_window_s),
    }

    out["cryptoquant"] = cryptoquant_context()
    out["regime"] = regime_verdict(out)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SOLUSDT futures flow + CryptoQuant regime analysis")
    p.add_argument("--symbol", default=SYMBOL, help=f"symbol (default: {SYMBOL})")
    p.add_argument("--trades", type=int, default=500, help="recent trades to pull (default: 500)")
    p.add_argument("--depth", type=int, default=50, help="order book depth (default: 50)")
    p.add_argument("--window", type=int, default=WINDOW_S_DEFAULT, help="CVD bucket window in seconds")
    p.add_argument("--json", action="store_true", help="emit JSON instead of pretty text")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    snap = asyncio.run(analyze(args.symbol, args.trades, args.depth, args.window))
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(render(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
