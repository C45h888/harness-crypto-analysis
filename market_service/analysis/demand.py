"""Demand-source decomposition: where is the bid actually coming from.

Lifted from legacy ``demand_diagnostic.py``. A regime label alone gives false
confidence; this decomposes spot-vs-leverage demand via CVD, funding, OI delta
and smart-money-vs-crowd positioning, and returns a deterministic verdict.

Pure functions over normalized inputs; ``decompose_demand`` consumes a unified
input dict (the collector/analysis shape), and ``demand_verdict`` + 
``macro_climate`` are the reusable interpretation layer.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.flow import summarize


def _ls_row(value: Any) -> Any:
    if isinstance(value, list) and value and isinstance(value[-1], dict):
        return value[-1]
    return value


def _funding_rate(fut: dict) -> float | None:
    for key in ("funding", "last_funding_rate", "lastFundingRate"):
        val = fut.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def decompose_demand(d: dict) -> dict:
    """Spot-vs-leverage decomposition of one combined pull.

    Expected ``d`` shape:
      spot:    {trades, book, ticker_24h}
      futures: {trades, book, open_interest, oi_history, mark_price,
                global_ls, top_ls, taker_buy_sell, ticker_24h}
      latency_ms: float
    Legacy source: demand_diagnostic.decompose_demand.
    """
    spot_flow = summarize(d["spot"]["trades"], d["spot"]["book"])
    fut_flow = summarize(d["futures"]["trades"], d["futures"]["book"])

    def _split_large(trades):
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
    s_tot, f_tot = s_buy + s_sell, f_buy + f_sell

    oi = d["futures"].get("open_interest") or {}
    oi_val = float(oi["open_interest"]) if isinstance(oi, dict) and oi.get("open_interest") else None

    oi_hist = d["futures"].get("oi_history") or {}
    oi_change_pct, oi_trend = None, "n/a"
    oi_series: list[tuple[int, float]] = []
    if isinstance(oi_hist, list):
        for row in oi_hist:
            if isinstance(row, dict):
                ts = row.get("timestamp") or row.get("time")
                o = (row.get("sumOpenInterest") or row.get("sum_open_interest")
                     or row.get("openInterest") or row.get("open_interest"))
                try:
                    if ts is not None and o is not None:
                        oi_series.append((int(ts), float(o)))
                except (TypeError, ValueError):
                    continue
    if len(oi_series) >= 2 and oi_series[0][1] > 0:
        first, last = oi_series[0][1], oi_series[-1][1]
        oi_change_pct = (last - first) / first * 100.0
        oi_trend = "rising" if oi_change_pct > 1.5 else ("falling" if oi_change_pct < -1.5 else "flat")

    long_pct = top_long_pct = taker_buy_ratio = None
    g_row, t_row = _ls_row(d["futures"].get("global_ls")), _ls_row(d["futures"].get("top_ls"))
    for row, attr in ((g_row, "long_pct"), (t_row, "top_long_pct")):
        if isinstance(row, dict):
            for k in ("long_account", "longAccount"):
                if k in row and row[k] is not None:
                    try:
                        if attr == "long_pct":
                            long_pct = float(row[k])
                        else:
                            top_long_pct = float(row[k])
                    except (TypeError, ValueError):
                        pass
                    break
    tbs_row = _ls_row(d["futures"].get("taker_buy_sell"))
    if isinstance(tbs_row, dict):
        for k in ("buy_sell_ratio", "buySellRatio"):
            if k in tbs_row and tbs_row[k] is not None:
                try:
                    taker_buy_ratio = float(tbs_row[k])
                except (TypeError, ValueError):
                    pass
                break

    return {
        "latency_ms": d.get("latency_ms"),
        "spot": {"last": spot_flow.get("last_price"), "vwap": spot_flow.get("vwap"),
                 "cvd": spot_flow.get("cvd"), "obi": spot_flow.get("obi"),
                 "spread": spot_flow.get("spread_bps"), "trades": spot_flow.get("trade_count"),
                 "buy_notional": s_buy, "sell_notional": s_sell,
                 "buy_share": (s_buy / s_tot) if s_tot else None,
                 "ticker_24h": d["spot"].get("ticker_24h")},
        "futures": {"last": fut_flow.get("last_price"), "vwap": fut_flow.get("vwap"),
                    "cvd": fut_flow.get("cvd"), "obi": fut_flow.get("obi"),
                    "spread": fut_flow.get("spread_bps"), "trades": fut_flow.get("trade_count"),
                    "buy_notional": f_buy, "sell_notional": f_sell,
                    "buy_share": (f_buy / f_tot) if f_tot else None,
                    "mark_price": d["futures"].get("mark_price"),
                    "ticker_24h": d["futures"].get("ticker_24h")},
        "derivs": {"oi": oi_val, "oi_change_pct": oi_change_pct, "oi_trend": oi_trend,
                   "funding": _funding_rate(d["futures"]), "long_pct": long_pct,
                   "top_long_pct": top_long_pct, "taker_buy_ratio": taker_buy_ratio},
    }


def demand_verdict(dx: dict) -> tuple[str, list[str]]:
    """Where is the bid coming from? Spot-momentum vs leverage decomposition.

    Verdicts: SPOT-DEMAND, SPOT-ACCUMULATION-vs-FUT-DISTRIBUTION,
    DISTRIBUTION-ON-RIP, FUTURES-DOMINATED, LEVERAGE-OVERHEATED,
    LEV-LONG-BUILD, MIXED-ABSORPTION. Returns (verdict, reasons).
    Legacy source: demand_diagnostic.demand_verdict. Deterministic, no bias.
    """
    s, f, der = dx["spot"], dx["futures"], dx["derivs"]
    reasons: list[str] = []
    s_cvd = s["cvd"] or 0.0
    f_cvd = f["cvd"] or 0.0
    s_share = s["buy_share"] if s["buy_share"] is not None else 0.5
    f_share = f["buy_share"] if f["buy_share"] is not None else 0.5
    funding = der["funding"] or 0.0
    long_pct, top_long_pct, oi = der["long_pct"], der["top_long_pct"], der["oi"]

    spot_buying = s_cvd > 0 and s_share > 0.55
    spot_selling = s_cvd < 0 and s_share < 0.45
    fut_buying = f_cvd > 0 and f_share > 0.55
    fut_selling = f_cvd < 0 and f_share < 0.45

    reasons.append(f"spot CVD {s_cvd:+.4f} ({'BUY' if s_cvd > 0 else 'SELL'}); spot buy-share {s_share:.0%}")
    reasons.append(f"futures CVD {f_cvd:+.4f} ({'BUY' if f_cvd > 0 else 'SELL'}); futures buy-share {f_share:.0%}")
    reasons.append(f"funding {funding:+.4%} ({'shorts pay' if funding < 0 else 'longs pay'})")
    if long_pct is not None:
        reasons.append(f"crowd {long_pct:.0%} long, top traders {top_long_pct:.0%} long")

    if spot_buying and fut_buying:
        divergence = "BOTH-BUYING (broad demand, watch for over-leverage)"
    elif spot_selling and fut_selling:
        divergence = "BOTH-SELLING (broad distribution, weak tape)"
    elif spot_selling and fut_buying:
        divergence = "SPOT-DISTRIBUTION / FUTURES-BID (paper support, real sellers)"
    elif spot_buying and fut_selling:
        divergence = "SPOT-BID / FUTURES-DISTRIBUTION (paper friction against real bid)"
    else:
        divergence = ""

    if spot_buying and fut_selling and funding <= 0:
        if long_pct and long_pct > 0.65:
            verdict = "SPOT-ACCUMULATION-vs-FUT-DISTRIBUTION (real bid, but retail crowded long — squeeze risk both ways)"
        else:
            verdict = "SPOT-ACCUMULATION-vs-FUT-DISTRIBUTION (real bid, smart-money absorbing leverage)"
    elif spot_buying and not fut_selling:
        verdict = "SPOT-DEMAND (durable bid)"
    elif spot_selling and fut_buying:
        verdict = "DISTRIBUTION-ON-RIP (real sellers to paper buyers — weak tape)"
    elif fut_selling and abs(f_cvd) > 3 * max(abs(s_cvd), 1):
        verdict = "FUTURES-DOMINATED (paper selling, thin spot — fragile)"
    elif spot_buying and fut_buying and funding > 0.0005:
        verdict = "LEVERAGE-OVERHEATED (real + paper demand, long-crowded, funding spiking)"
    elif fut_buying and abs(f_cvd) > 3 * max(abs(s_cvd), 1):
        verdict = "LEV-LONG-BUILD (paper longs only — reversible)"
    elif divergence:
        verdict = divergence
    else:
        verdict = "MIXED-ABSORPTION (no clear direction)"

    if top_long_pct is not None and long_pct is not None:
        if top_long_pct < long_pct - 0.05:
            reasons.append(f"  SMART-MONEY FADING CROWD (top {top_long_pct:.0%} vs crowd {long_pct:.0%})")
        elif top_long_pct > long_pct + 0.05:
            reasons.append(f"  SMART-MONEY WITH CROWD (top {top_long_pct:.0%} vs crowd {long_pct:.0%})")
    if funding <= -0.0003:
        reasons.append("  NEGATIVE FUNDING + paper sellers = short-squeeze fuel if OI keeps rising")
    return verdict, reasons


def macro_climate(macro: dict, target_change_pct: float | None) -> str:
    """Is the macro environment supportive of longs / new highs?

    ``macro`` = {btc: {change_pct, funding}, eth: {change_pct, funding}}.
    Legacy source: demand_diagnostic.macro_climate.
    """
    btc, eth = macro["btc"], macro["eth"]
    if btc["change_pct"] is None or eth["change_pct"] is None:
        return "MACRO: data unavailable"
    btc_chg, eth_chg = btc["change_pct"], eth["change_pct"]
    sol_chg = target_change_pct or 0.0
    if btc_chg < -1.5 and eth_chg < -1.5:
        if sol_chg > btc_chg + 0.5:
            return f"MACRO RISK-OFF (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); SOL {sol_chg:+.1f}% = RELATIVE BID but counter-trend longs risky"
        return f"MACRO RISK-OFF (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); selling pressure broad; longs not supported"
    if btc_chg > 1.0 and eth_chg > 1.0:
        return f"MACRO RISK-ON (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); tailwind for alts"
    if -1.5 < btc_chg < 1.0:
        return f"MACRO NEUTRAL-CHOPPY (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); mean-reversion regime, breakouts fickle"
    return f"MACRO MIXED (BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}%); reduce size"
