"""Regime classification for USD-M perpetuals.

Lifted from legacy ``sol_futures.py`` regime_verdict. Folds price-vs-VWAP, CVD
direction, book imbalance, funding, crowd L/S, smart-money-vs-crowd divergence,
and large-print imbalance into a deterministic regime label — detecting
trends, smart-money traps, squeeze risk, absorption, and chop.

Pure function over a normalized input dict; deterministic and bias-free (it
labels what the data shows; it never instructs a trade).
"""

from __future__ import annotations

from typing import Any


def _row(value: Any) -> Any:
    if isinstance(value, list) and value and isinstance(value[-1], dict):
        return value[-1]
    return value if isinstance(value, dict) else None


def _funding_from(mark: dict, funding: dict) -> float | None:
    for src in (mark, funding):
        if not isinstance(src, dict):
            continue
        for k in ("last_funding_rate", "lastFundingRate"):
            if k in src and src[k] is not None:
                try:
                    return float(src[k])
                except (TypeError, ValueError):
                    continue
    return None


def regime_verdict(d: dict) -> tuple[str, list[str]]:
    """Map futures metrics to a regime label + reasons.

    ``d`` shape: {futures: {flow, open_interest, funding, mark_price,
                            long_short_ratio, top_long_short_accounts}}.
    ``flow`` is the canonical ``flow.summarize()`` output.
    Legacy source: sol_futures.regime_verdict.
    """
    reasons: list[str] = []
    labels: list[str] = []
    fut = d["futures"]
    flow = fut["flow"]
    oi = fut.get("open_interest") or {}
    fund = fut.get("funding") or {}
    mark = fut.get("mark_price") or {}

    last = flow.get("last_price") or 0.0
    vwap = flow.get("vwap") or 0.0
    cvd = flow.get("cvd") or 0.0
    obi = flow.get("obi") or 0.0
    bs = flow.get("buy_sell_ratio") or 0.0
    large = flow.get("large_trades") or []

    if last > vwap * 1.0005:
        labels.append("BULL"); reasons.append(f"last > vwap ({last:.4f} > {vwap:.4f})")
    elif last < vwap * 0.9995:
        labels.append("BEAR"); reasons.append(f"last < vwap ({last:.4f} < {vwap:.4f})")
    else:
        labels.append("CHOP"); reasons.append(f"last ≈ vwap ({last:.4f} ~ {vwap:.4f})")

    if abs(cvd) > 0:
        labels.append("AGGR-BUY" if cvd > 0 else "AGGR-SELL")
        reasons.append(f"CVD {cvd:+.3f} (buy/sell ratio {bs:.2f})")
    if obi > 0.2:
        labels.append("BID-WALL"); reasons.append(f"OBI {obi:+.3f} (bid > ask top 20)")
    elif obi < -0.2:
        labels.append("ASK-WALL"); reasons.append(f"OBI {obi:+.3f} (ask > bid top 20)")

    rate = _funding_from(mark, fund)
    if rate is not None:
        if rate > 0.0005:
            labels.append("LONG-CROWDED"); reasons.append(f"funding {rate:+.4%} (longs paying shorts)")
        elif rate > 0.0001:
            labels.append("LONG-LEAN"); reasons.append(f"funding {rate:+.4%}")
        elif rate < -0.0005:
            labels.append("SHORT-CROWDED"); reasons.append(f"funding {rate:+.4%} (shorts paying longs)")
        elif rate < -0.0001:
            labels.append("SHORT-LEAN"); reasons.append(f"funding {rate:+.4%}")
        else:
            reasons.append(f"funding neutral {rate:+.4%}")

    ls_ratio = long_pct = None
    ls_row = _row(fut.get("long_short_ratio"))
    if isinstance(ls_row, dict):
        for k in ("long_short_ratio", "longShortRatio"):
            if k in ls_row:
                try:
                    ls_ratio = float(ls_row[k]); break
                except (TypeError, ValueError):
                    pass
        for k in ("long_account", "longAccount"):
            if k in ls_row:
                try:
                    long_pct = float(ls_row[k]); break
                except (TypeError, ValueError):
                    pass
    if ls_ratio is not None:
        if ls_ratio > 1.8:
            labels.append("CROWD-LONG"); reasons.append(f"L/S {ls_ratio:.2f} ({(long_pct or ls_ratio/(ls_ratio+1))*100:.1f}% longs)")
        elif ls_ratio < 0.55:
            labels.append("CROWD-SHORT"); reasons.append(f"L/S {ls_ratio:.2f} ({(long_pct or ls_ratio/(ls_ratio+1))*100:.1f}% longs)")
        else:
            reasons.append(f"L/S ratio {ls_ratio:.2f}")

    top_ls_row = _row(fut.get("top_long_short_accounts"))
    top_ls_r = None
    if isinstance(top_ls_row, dict):
        for k in ("long_short_ratio", "longShortRatio"):
            if k in top_ls_row:
                try:
                    top_ls_r = float(top_ls_row[k])
                    reasons.append(f"top-trader L/S {top_ls_r:.2f}")
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

    if large:
        buy_notional = sum(t["notional_usd"] for t in large if t["side"] == "buy")
        sell_notional = sum(t["notional_usd"] for t in large if t["side"] == "sell")
        if buy_notional + sell_notional > 0:
            l_ratio = buy_notional / (buy_notional + sell_notional)
            if l_ratio > 0.65:
                labels.append("LARGE-BUYS"); reasons.append(f"large-print {l_ratio:.0%} buy-side")
            elif l_ratio < 0.35:
                labels.append("LARGE-SELLS"); reasons.append(f"large-print {l_ratio:.0%} buy-side (sellers dominate)")

    if isinstance(oi, dict) and oi.get("open_interest"):
        reasons.append(f"OI {float(oi['open_interest']):,.0f}")

    bull = "BULL" in labels
    bear = "BEAR" in labels
    aggr_b = "AGGR-BUY" in labels
    aggr_s = "AGGR-SELL" in labels
    long_crowd = "CROWD-LONG" in labels
    short_crowd = "CROWD-SHORT" in labels
    long_crowded = "LONG-CROWDED" in labels
    short_crowded = "SHORT-CROWDED" in labels
    smart_bear = "SMART-VS-CROWD-BEAR" in labels
    smart_bull = "SMART-VS-CROWD-BULL" in labels

    if bull and aggr_b:
        regime = "TREND-UP (long build confirmed)"
    elif bear and aggr_s:
        regime = "TREND-DOWN (short build confirmed)"
    elif long_crowd and smart_bear and (aggr_s or bear):
        regime = "TRAP-LONG (smart money fading retail longs)"
    elif short_crowd and smart_bull and (aggr_b or bull):
        regime = "TRAP-SHORT (smart money fading retail shorts)"
    elif long_crowded and aggr_s:
        regime = "LONG-SQUEEZE RISK (crowded + sellers winning)"
    elif short_crowded and aggr_b:
        regime = "SHORT-SQUEEZE RISK (crowded + buyers winning)"
    elif "BID-WALL" in labels or "ASK-WALL" in labels:
        regime = "ABSORPTION / SQUEEZE"
    elif "CHOP" in labels:
        regime = "CHOP / RANGE"
    elif bull:
        regime = "WEAK-UP"
    elif bear:
        regime = "WEAK-DOWN"
    else:
        regime = "MIXED"
    return regime, reasons
