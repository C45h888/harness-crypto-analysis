"""Auction dynamics: who is winning the current micro-auction.

Lifted from legacy ``auction_dynamics.py`` into the canonical analysis layer.
Each function is a pure deterministic computation over normalized inputs
(books, trades) so it is reusable across assets and unit-testable.

The verdict answers "WHO is winning the continuous double auction" — buyers
winning, sellers winning, or contested — from microstructure, tape, and
derivatives signals, with the underlying figures preserved alongside.
"""

from __future__ import annotations

from typing import Any


def microprice(book: dict) -> dict:
    """Top-of-book weighted mid (microprice) plus multi-slice depth imbalance.

    ``book`` is a dict with ``bids``/``asks`` rows of ``(price, qty)``.
    skew_bps = (microprice - mid) / mid * 1e4. Positive skew means the
    size-weighted price sits above the simple mid (buyers pulling price up).

    Legacy source: auction_dynamics.microprice.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    def _row(r):
        if isinstance(r, dict):
            return float(r.get("price") or r[0]), float(r.get("qty") or r[1])
        return float(r[0]), float(r[1])

    if not bids or not asks:
        return {"microprice": None, "mid": None, "skew_bps": None, "depth": {}}

    bp, bq = _row(bids[0])
    ap, aq = _row(asks[0])
    mid = (bp + ap) / 2
    micro = (bp * aq + ap * bq) / (aq + bq) if (aq + bq) > 0 else mid
    skew_bps = (micro - mid) / mid * 10000 if mid > 0 else 0.0

    def _slice(b, a, n):
        bs = sum((_row(r)[1] for r in b[:n]), 0.0) if b else 0.0
        asz = sum((_row(r)[1] for r in a[:n]), 0.0) if a else 0.0
        return bs, asz

    def _obi(bs, asz):
        t = bs + asz
        return (bs - asz) / t if t > 0 else 0.0

    depth = {}
    for name, n in (("top5", 5), ("top10", 10), ("top20", 20)):
        bs, asz = _slice(bids, asks, n)
        depth[name] = {"bid": bs, "ask": asz, "obi": _obi(bs, asz)}
    b_total = sum((_row(r)[1] for r in bids), 0.0)
    a_total = sum((_row(r)[1] for r in asks), 0.0)
    depth["total"] = {"bid": b_total, "ask": a_total, "obi": _obi(b_total, a_total)}

    return {
        "best_bid": bp, "best_ask": ap,
        "mid": mid, "microprice": micro, "skew_bps": skew_bps,
        "spread_bps": (ap - bp) / mid * 10000 if mid > 0 else 0.0,
        "depth": depth,
    }


def initiated_flow(trades: list[dict]) -> dict:
    """Taker-aggressor split: aggressive buy vs sell qty + notional + CVD.

    isBuyerMaker=True => taker sold into the bid (aggressive sell).
    Legacy source: auction_dynamics.initiated_flow.
    """
    ab_qty = as_qty = ab_notional = as_notional = 0.0
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
    total = ab_notional + as_notional
    return {
        "aggressive_buy_qty": ab_qty, "aggressive_sell_qty": as_qty,
        "aggressive_buy_notional": ab_notional, "aggressive_sell_notional": as_notional,
        "buy_share": (ab_notional / total) if total else None,
        "cvd_qty": ab_qty - as_qty, "cvd_notional": ab_notional - as_notional,
        "n_buy_trades": n_buy, "n_sell_trades": n_sell, "trade_count": len(trades),
    }


def flow_persistence(trades: list[dict]) -> dict:
    """First-order flow momentum: recent-half vs older-half buy-share delta.

    Trades arrive newest-first (Binance). Accelerating-buy / -sell when the
    recent half's buy-share moves >|5%| relative to the older half.

    Legacy source: auction_dynamics.flow_persistence.
    """
    if len(trades) < 20:
        return {"trend": "insufficient-data"}
    half = len(trades) // 2
    rec = initiated_flow(trades[:half])
    old = initiated_flow(trades[half:])

    def _share(f):
        return f["aggressive_buy_notional"] / max(
            f["aggressive_buy_notional"] + f["aggressive_sell_notional"], 1e-9)

    rs, os_ = _share(rec), _share(old)
    delta = rs - os_
    trend = "accelerating-buy" if delta > 0.05 else ("accelerating-sell" if delta < -0.05 else "balanced")
    return {
        "trend": trend, "recent_buy_share": rs, "older_buy_share": os_, "delta": delta,
        "recent_cvd_notional": rec["cvd_notional"], "older_cvd_notional": old["cvd_notional"],
    }


def auction_verdict(state: dict) -> tuple[str, list[str]]:
    """Five-signal auction winner: microprice skew + top-20 OBI + taker buy +
    persistence, with futures + derivatives overlay. Returns (verdict, reasons).

    ``state`` shape: {spot: {microprice, flow, persistence},
                      futures: {microprice, flow, persistence},
                      derivatives: {funding, taker_buy_ratio, oi_change_pct}}
    Legacy source: auction_dynamics.auction_verdict. Deterministic, no bias.
    """
    reasons: list[str] = []
    score = 0
    s, f, d = state["spot"], state["futures"], state.get("derivatives", {})

    s_skew = s["microprice"].get("skew_bps") if isinstance(s.get("microprice"), dict) else None
    if s_skew is not None:
        if s_skew > 0.5:
            score += 1; reasons.append(f"spot microprice {s_skew:+.2f}bps (buyers dictating)")
        elif s_skew < -0.5:
            score -= 1; reasons.append(f"spot microprice {s_skew:+.2f}bps (sellers dictating)")
        else:
            reasons.append(f"spot microprice {s_skew:+.2f}bps (neutral)")

    s_obi = (s["microprice"].get("depth") or {}).get("top20", {}).get("obi") if isinstance(s.get("microprice"), dict) else None
    if s_obi is not None:
        if s_obi > 0.2:
            score += 1; reasons.append(f"spot top-20 OBI {s_obi:+.3f} (bids stacked)")
        elif s_obi < -0.2:
            score -= 1; reasons.append(f"spot top-20 OBI {s_obi:+.3f} (asks stacked)")

    s_share = s["flow"].get("buy_share")
    if s_share is not None:
        if s_share > 0.55:
            score += 1; reasons.append(f"spot taker-buy {s_share:.0%} (buyers initiating)")
        elif s_share < 0.45:
            score -= 1; reasons.append(f"spot taker-buy {s_share:.0%} (sellers initiating)")

    s_trend = s["persistence"].get("trend")
    if s_trend == "accelerating-buy":
        score += 1; reasons.append("spot flow ACCELERATING in buy direction")
    elif s_trend == "accelerating-sell":
        score -= 1; reasons.append("spot flow ACCELERATING in sell direction")

    f_share = f["flow"].get("buy_share")
    contested = False
    if s_share is not None and f_share is not None:
        spot_buy = s_share > 0.55
        fut_buy = f_share > 0.55
        if spot_buy != fut_buy:
            contested = True
            reasons.append(f"contested: spot taker-buy {s_share:.0%} vs futures {f_share:.0%}")
        else:
            reasons.append(f"spot + futures taker-buy aligned ({s_share:.0%} / {f_share:.0%})")

    taker = d.get("taker_buy_ratio")
    if taker is not None:
        if taker > 1.05:
            score += 1; reasons.append(f"futures taker b/s {taker:.2f} (buyers aggressive)")
        elif taker < 0.95:
            score -= 1; reasons.append(f"futures taker b/s {taker:.2f} (sellers aggressive)")

    funding = d.get("funding")
    if funding is not None:
        reasons.append(f"funding {funding:+.4%}" + (" (longs crowded)" if funding > 0.0005 else " (shorts paying)" if funding < -0.0005 else " (neutral)"))
    oi_chg = d.get("oi_change_pct")
    if oi_chg is not None:
        reasons.append(f"OI {oi_chg:+.2f}%" + (" (new positions)" if oi_chg > 1.5 else " (positions closing)" if oi_chg < -1.5 else " (flat churn)"))

    if contested:
        verdict = "AUCTION CONTESTED — no consensus"
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
