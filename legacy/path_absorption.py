"""
Path absorption analysis — SOLUSDT
Determines if buyers can push price from current level to 74.44 entry,
and if sellers can push it to short-fill zone.

Uses urllib per task spec (no SDK dependency). Pulls live data once,
runs both directions, prints verdict.
"""
import json
import urllib.request
import ssl

BASE = "https://fapi.binance.com"
SYM = "SOLUSDT"

# ---------- urllib fetchers ----------
_ctx = ssl.create_default_context()


def fetch_json(path: str, params: dict | None = None) -> dict | list:
    url = f"{BASE}{path}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10, context=_ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    print("=" * 78)
    print("PATH ABSORPTION ANALYSIS — SOLUSDT")
    print("=" * 78)

    # ---------- PULL DATA ----------
    print("\n[1/5] Pulling live data...")
    depth = fetch_json("/fapi/v1/depth", {"symbol": SYM, "limit": 500})
    trades = fetch_json("/fapi/v1/trades", {"symbol": SYM, "limit": 500})
    ticker = fetch_json("/fapi/v1/ticker/price", {"symbol": SYM})
    tbr = fetch_json("/futures/data/takerlongshortRatio",
                     {"symbol": SYM, "period": "5m", "limit": 24})
    oi_hist = fetch_json("/futures/data/openInterestHist",
                         {"symbol": SYM, "period": "5m", "limit": 24})

    px = float(ticker["price"])
    print(f"  Current price: {px:.4f}")
    print(f"  Bids levels: {len(depth['bids'])} | Asks levels: {len(depth['asks'])}")
    print(f"  Recent trades: {len(trades)}")
    print(f"  TBR bars: {len(tbr)} | OI bars: {len(oi_hist)}")

    # ---------- PARSE BOOK ----------
    bids = [(float(p), float(q)) for p, q in depth["bids"]]
    asks = [(float(p), float(q)) for p, q in depth["asks"]]
    # Sort just in case
    bids.sort(key=lambda x: x[0], reverse=True)  # descending
    asks.sort(key=lambda x: x[0])                # ascending

    # Spread
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 10000
    print(f"  Best bid: {best_bid:.4f} | Best ask: {best_ask:.4f} | Spread: {spread_bps:.1f} bps")

    # Top-of-book imbalance (top 20)
    top_n = 20
    bid_qty_top = sum(q for _, q in bids[:top_n])
    ask_qty_top = sum(q for _, q in asks[:top_n])
    obi = (bid_qty_top - ask_qty_top) / (bid_qty_top + ask_qty_top)
    print(f"  OBI top-{top_n}: {obi:+.4f} (bid {bid_qty_top:.0f} / ask {ask_qty_top:.0f})")

    # ---------- HELPERS ----------
    def cum_ask_to(target: float) -> tuple[float, float]:
        """Return (cum_qty, cum_notional) for all asks with price <= target."""
        cum_q = 0.0
        cum_n = 0.0
        for p, q in asks:
            if p <= target:
                cum_q += q
                cum_n += q * p
            else:
                break
        return cum_q, cum_n

    def cum_bid_to(target: float) -> tuple[float, float]:
        """Return (cum_qty, cum_notional) for all bids with price >= target."""
        cum_q = 0.0
        cum_n = 0.0
        for p, q in bids:
            if p >= target:
                cum_q += q
                cum_n += q * p
            else:
                break
        return cum_q, cum_n

    # ============================================================
    # TASK 1: ASK ABSORPTION LADDER
    # ============================================================
    print("\n" + "=" * 78)
    print(f"TASK 1: ASK ABSORPTION LADDER (current px = {px:.4f})")
    print("=" * 78)
    ask_steps = [
        ("px +0.05", px + 0.05),
        ("px +0.10", px + 0.10),
        ("px +0.20", px + 0.20),
        ("px +0.30", px + 0.30),
        ("74.00",    74.00),
        ("74.10",    74.10),
        ("74.20",    74.20),
        ("74.30",    74.30),
        ("74.40",    74.40),
        ("74.44",    74.44),
    ]
    print(f"{'Step':<12} {'Target px':>10} {'Cum Qty (SOL)':>15} {'Cum Notional ($)':>20}")
    print("-" * 60)
    ask_ladder = {}
    for label, target in ask_steps:
        cq, cn = cum_ask_to(target)
        ask_ladder[target] = cq
        print(f"{label:<12} {target:>10.4f} {cq:>15.2f} {cn:>20,.0f}")

    # ============================================================
    # TASK 2: BID FUEL LADDER
    # ============================================================
    print("\n" + "=" * 78)
    print("TASK 2: BID FUEL LADDER (down to 73.50)")
    print("=" * 78)
    bid_steps = [
        ("px -0.05", px - 0.05),
        ("px -0.10", px - 0.10),
        ("px -0.20", px - 0.20),
        ("74.00",    74.00),
        ("73.80",    73.80),
        ("73.70",    73.70),
        ("73.63",    73.63),
        ("73.60",    73.60),
        ("73.57",    73.57),
        ("73.50",    73.50),
    ]
    print(f"{'Step':<12} {'Target px':>10} {'Cum Qty (SOL)':>15} {'Cum Notional ($)':>20}")
    print("-" * 60)
    bid_ladder = {}
    for label, target in bid_steps:
        cq, cn = cum_bid_to(target)
        bid_ladder[target] = cq
        print(f"{label:<12} {target:>10.4f} {cq:>15.2f} {cn:>20,.0f}")

    # ============================================================
    # TASK 3: SIMULATED ASCENT (buyers push up)
    # ============================================================
    print("\n" + "=" * 78)
    print("TASK 3: SIMULATED PRICE ASCENT (buyers push up from px)")
    print("=" * 78)
    # Bid fuel: total bid qty from px down to 73.50
    # Buyers absorb asks; bids below provide the "fuel" they can pull from if they want to reload
    # Simplified model: ask absorption needed vs available bid fuel
    total_bid_fuel, _ = cum_bid_to(73.50)
    print(f"Bid fuel available (px to 73.50): {total_bid_fuel:.0f} SOL\n")

    ascent_levels = [74.00, 74.05, 74.10, 74.15, 74.20, 74.25, 74.30, 74.35, 74.40, 74.44]
    print(f"{'At level':>9} {'Asks to absorb':>15} {'Bid fuel left':>15} {'Verdict':<25}")
    print("-" * 70)
    ascent_verdicts = []
    for lvl in ascent_levels:
        cq, cn = cum_ask_to(lvl)
        asks_needed = cq
        # Buyers spend bid fuel to lift asks (1 SOL ask = 1 SOL bid used)
        fuel_remaining = total_bid_fuel - asks_needed
        if fuel_remaining > 0:
            verdict = "BUYERS REACH"
        else:
            verdict = "BUYERS EXHAUSTED"
        ascent_verdicts.append((lvl, asks_needed, fuel_remaining, verdict))
        print(f"{lvl:>9.2f} {asks_needed:>15.0f} {fuel_remaining:>15.0f} {verdict:<25}")

    # ============================================================
    # TASK 4: SIMULATED DESCENT (sellers push down — short thesis)
    # ============================================================
    print("\n" + "=" * 78)
    print("TASK 4: SIMULATED PRICE DESCENT (short thesis — sellers push down)")
    print("=" * 78)
    total_ask_fuel, _ = cum_ask_to(74.44)
    print(f"Ask fuel available (px to 74.44): {total_ask_fuel:.0f} SOL\n")

    descent_levels = [73.85, 73.80, 73.75, 73.70, 73.65, 73.63, 73.60, 73.57, 73.55, 73.50]
    print(f"{'At level':>9} {'Bids to absorb':>15} {'Ask fuel left':>15} {'Verdict':<25}")
    print("-" * 70)
    descent_verdicts = []
    for lvl in descent_levels:
        cq, cn = cum_bid_to(lvl)
        bids_to_absorb = cq
        ask_fuel_remaining = total_ask_fuel - bids_to_absorb
        # Sellers consume bids; ask fuel is what shorts have to cover
        if ask_fuel_remaining > 0 and bids_to_absorb < total_bid_fuel * 0.6:
            verdict = "SHORT VALID"
        elif ask_fuel_remaining > 0:
            verdict = "SHORT OK (tight)"
        else:
            verdict = "SQUEEZE RISK"
        descent_verdicts.append((lvl, bids_to_absorb, ask_fuel_remaining, verdict))
        print(f"{lvl:>9.2f} {bids_to_absorb:>15.0f} {ask_fuel_remaining:>15.0f} {verdict:<25}")

    # ============================================================
    # TASK 5: LARGE-PRINT FLOW (last 500 trades)
    # ============================================================
    print("\n" + "=" * 78)
    print("TASK 5: LARGE-PRINT FLOW ANALYSIS (qty >= 100 SOL)")
    print("=" * 78)
    parsed_trades = []
    for t in trades:
        parsed_trades.append({
            "price": float(t["price"]),
            "qty":   float(t["qty"]),
            "time":  int(t["time"]),
            "is_buyer_maker": t["isBuyerMaker"],   # True = buyer is maker => taker sold
        })

    # Large threshold
    qtys = sorted(t["qty"] for t in parsed_trades)
    median_qty = qtys[len(qtys) // 2]
    large_threshold = 100.0  # SOL absolute threshold per task spec
    print(f"  Total trades: {len(parsed_trades)}")
    print(f"  Median qty: {median_qty:.3f} SOL | Large threshold: >= {large_threshold} SOL")

    large_buy_qty = 0.0
    large_sell_qty = 0.0
    large_buy_notional = 0.0
    large_sell_notional = 0.0
    large_trades_in_wall_zone = []  # 74.30 - 74.44

    for t in parsed_trades:
        if t["qty"] >= large_threshold:
            notional = t["qty"] * t["price"]
            if t["is_buyer_maker"]:
                # Taker sold
                large_sell_qty += t["qty"]
                large_sell_notional += notional
            else:
                # Taker bought
                large_buy_qty += t["qty"]
                large_buy_notional += notional
            # Check wall zone
            if 74.30 <= t["price"] <= 74.44:
                large_trades_in_wall_zone.append(t)

    print(f"\n  Large BUY  qty: {large_buy_qty:>10.1f} SOL  (${large_buy_notional:>12,.0f})")
    print(f"  Large SELL qty: {large_sell_qty:>10.1f} SOL  (${large_sell_notional:>12,.0f})")
    net_large = large_buy_qty - large_sell_qty
    if net_large > 0:
        direction = "NET BUY PRESSURE (buyers dominating large tape)"
    elif net_large < 0:
        direction = "NET SELL PRESSURE (sellers dominating large tape)"
    else:
        direction = "BALANCED"
    print(f"  Net: {net_large:+.1f} SOL — {direction}")

    if large_trades_in_wall_zone:
        print(f"\n  >>> Large prints in 74.30-74.44 wall zone: {len(large_trades_in_wall_zone)}")
        for t in large_trades_in_wall_zone[-10:]:
            side = "SELL" if t["is_buyer_maker"] else "BUY"
            print(f"      {t['time']} | {side} {t['qty']:.1f} SOL @ {t['price']:.4f}")
    else:
        print(f"\n  No large prints in 74.30-74.44 wall zone.")

    # Concentration by price bucket
    buckets = {"73.50-73.70": [], "73.70-73.85": [], "73.85-74.00": [],
               "74.00-74.20": [], "74.20-74.44": []}
    for t in parsed_trades:
        if t["qty"] < large_threshold:
            continue
        p = t["price"]
        if 73.50 <= p < 73.70:
            buckets["73.50-73.70"].append(t)
        elif 73.70 <= p < 73.85:
            buckets["73.70-73.85"].append(t)
        elif 73.85 <= p < 74.00:
            buckets["73.85-74.00"].append(t)
        elif 74.00 <= p < 74.20:
            buckets["74.00-74.20"].append(t)
        elif 74.20 <= p <= 74.44:
            buckets["74.20-74.44"].append(t)
    print("\n  Large-print concentration by price bucket:")
    for b, ts in buckets.items():
        bq = sum(t["qty"] for t in ts if not t["is_buyer_maker"])
        sq = sum(t["qty"] for t in ts if t["is_buyer_maker"])
        net = bq - sq
        print(f"    {b}: {len(ts):>3} prints | buy {bq:>7.1f} | sell {sq:>7.1f} | net {net:+7.1f}")

    # ============================================================
    # TASK 6: TBR + OI FLOW
    # ============================================================
    print("\n" + "=" * 78)
    print("TASK 6: TBR + OI INFLOW/OUTFLOW")
    print("=" * 78)
    print("\nTaker Buy/Sell Ratio (last 6 bars):")
    print(f"{'Time':<22} {'BuyVol':>14} {'SellVol':>14} {'Ratio':>8}")
    print("-" * 60)
    tbr_recent = tbr[-6:]
    for bar in tbr_recent:
        ts = bar.get("timestamp", "?")
        bv = float(bar.get("buyVol", 0))
        sv = float(bar.get("sellVol", 0))
        ratio = bv / sv if sv > 0 else 0
        print(f"{str(ts):<22} {bv:>14.0f} {sv:>14.0f} {ratio:>8.3f}")

    # TBR trend
    ratios = []
    for bar in tbr_recent:
        bv = float(bar.get("buyVol", 0))
        sv = float(bar.get("sellVol", 0))
        ratios.append(bv / sv if sv > 0 else 1.0)
    if len(ratios) >= 2:
        tbr_delta = ratios[-1] - ratios[0]
        if tbr_delta > 0.1:
            tbr_trend = "BUY PRESSURE BUILDING"
        elif tbr_delta < -0.1:
            tbr_trend = "SELL PRESSURE BUILDING"
        else:
            tbr_trend = "STABLE"
    else:
        tbr_trend = "INSUFFICIENT DATA"
    print(f"  TBR trend (last 6 bars): {tbr_trend} (Δ={tbr_delta:+.3f})")

    print("\nOpen Interest (last 6 bars):")
    print(f"{'Time':<22} {'OI (SOL)':>14} {'Δ OI':>12}")
    print("-" * 50)
    oi_recent = oi_hist[-6:]
    oi_values = []
    for bar in oi_recent:
        ts = bar.get("timestamp", "?")
        oi = float(bar.get("sumOpenInterest", bar.get("openInterest", 0)))
        oi_values.append(oi)
        print(f"{str(ts):<22} {oi:>14.0f}")
    if len(oi_values) >= 2:
        oi_delta = oi_values[-1] - oi_values[0]
        oi_pct = oi_delta / oi_values[0] * 100 if oi_values[0] > 0 else 0
        if oi_delta > 0:
            oi_trend = "OI RISING (new positions opening)"
        elif oi_delta < 0:
            oi_trend = "OI FALLING (positions closing)"
        else:
            oi_trend = "OI FLAT"
        print(f"  OI delta (6 bars): {oi_delta:+.0f} ({oi_pct:+.2f}%) — {oi_trend}")

    # Smart money direction (top trader long %) — try endpoint if available
    smart_dir = "N/A (endpoint requires different params or unavailable)"
    try:
        # Try global L/S ratio
        gls = fetch_json("/futures/data/globalLongShortAccountRatio",
                         {"symbol": SYM, "period": "5m", "limit": 6})
        if gls:
            print("\nGlobal L/S Ratio (last 6 bars — crowd positioning):")
            print(f"{'Time':<22} {'Long %':>10} {'Short %':>10}")
            print("-" * 50)
            for bar in gls[-6:]:
                ts = bar.get("timestamp", "?")
                la = float(bar.get("longAccount", 0))
                sa = float(bar.get("shortAccount", 0))
                print(f"{str(ts):<22} {la*100:>9.2f}% {sa*100:>9.2f}%")
            last_long = float(gls[-1].get("longAccount", 0))
            first_long = float(gls[-6].get("longAccount", 0)) if len(gls) >= 6 else last_long
            smart_delta = last_long - first_long
            if smart_delta > 0.02:
                smart_dir = f"CROWD LONG-BUILDING (Δ={smart_delta*100:+.2f}%)"
            elif smart_delta < -0.02:
                smart_dir = f"CROWD DE-LONGING (Δ={smart_delta*100:+.2f}%)"
            else:
                smart_dir = f"CROWD STABLE ({last_long*100:.1f}% long)"
    except Exception as e:
        pass

    print(f"\n  Smart-money/crowd direction: {smart_dir}")

    # Combined verdict on TBR + OI
    print("\n  --- TBR + OI COMBINED VERDICT ---")
    if tbr_trend == "BUY PRESSURE BUILDING" and "OI RISING" in oi_trend:
        combo = ">>> NEW LONGS OPENING (real buyer demand)"
    elif tbr_trend == "BUY PRESSURE BUILDING" and "OI FLAT" in oi_trend:
        combo = ">>> SHORT COVERING (paper support, not real demand)"
    elif tbr_trend == "BUY PRESSURE BUILDING" and "OI FALLING" in oi_trend:
        combo = ">>> SHORT COVERS + LIQUIDATIONS"
    elif tbr_trend == "SELL PRESSURE BUILDING" and "OI RISING" in oi_trend:
        combo = ">>> NEW SHORTS OPENING (real paper selling)"
    elif tbr_trend == "SELL PRESSURE BUILDING" and "OI FLAT" in oi_trend:
        combo = ">>> LONG UNWINDING (longs closing)"
    elif tbr_trend == "SELL PRESSURE BUILDING" and "OI FALLING" in oi_trend:
        combo = ">>> LIQUIDATION CASCADE"
    else:
        combo = ">>> MIXED / STABLE"
    print(f"  {combo}")

    # ============================================================
    # FINAL VERDICT
    # ============================================================
    print("\n" + "=" * 78)
    print("FINAL VERDICT — CAN PRICE REACH 74.44 FROM CURRENT PX?")
    print("=" * 78)

    # Required buy volume = asks from px to 74.44
    req_buy_vol, req_buy_notional = cum_ask_to(74.44)
    # Available bid fuel = bids from px down to 73.50
    avail_bid_fuel, avail_bid_notional = cum_bid_to(73.50)
    ratio = avail_bid_fuel / req_buy_vol if req_buy_vol > 0 else 999

    print(f"\n  Required buy volume (asks px→74.44): {req_buy_vol:,.0f} SOL  (${req_buy_notional:,.0f})")
    print(f"  Available bid fuel (bids px→73.50):  {avail_bid_fuel:,.0f} SOL  (${avail_bid_notional:,.0f})")
    print(f"  Fuel ratio: {ratio:.2f}")

    if ratio > 1.5:
        verdict = "BUYERS HAVE AMPLE FUEL"
        confidence = "HIGH"
    elif ratio >= 1.0:
        verdict = "BUYERS CAN REACH"
        confidence = "MEDIUM"
    elif ratio >= 0.5:
        verdict = "BUYERS STRUGGLE"
        confidence = "MEDIUM-LOW"
    else:
        verdict = "BUYERS EXHAUSTED BEFORE TARGET"
        confidence = "HIGH (exhaustion)"

    # Find where buyers fall short
    fall_short_at = None
    for lvl, asks_needed, fuel_remaining, v in ascent_verdicts:
        if fuel_remaining < 0:
            fall_short_at = lvl
            break

    print(f"\n  >>> VERDICT: {verdict}")
    if fall_short_at:
        print(f"      Buyers fall short at: {fall_short_at:.2f}")
    else:
        print(f"      Buyers reach: ALL LEVELS TO 74.44")
    print(f"  Confidence: {confidence}")

    # Also check seller wall concentration in 74.30-74.44
    asks_in_wall = [(p, q) for p, q in asks if 74.30 <= p <= 74.44]
    wall_qty = sum(q for _, q in asks_in_wall)
    wall_levels = len(asks_in_wall)
    wall_notional = sum(q * p for p, q in asks_in_wall)
    print(f"\n  Seller wall concentration (74.30-74.44):")
    print(f"      Levels: {wall_levels} | Total qty: {wall_qty:,.0f} SOL (${wall_notional:,.0f})")
    if wall_qty > req_buy_vol * 0.5:
        print(f"      >>> HEAVY WALL: {wall_qty/req_buy_vol*100:.0f}% of required buy volume sits here")

    # Save summary to log
    summary = {
        "px": px,
        "spread_bps": spread_bps,
        "obi_top20": obi,
        "required_buy_vol_sol": req_buy_vol,
        "available_bid_fuel_sol": avail_bid_fuel,
        "fuel_ratio": ratio,
        "verdict": verdict,
        "confidence": confidence,
        "fall_short_at": fall_short_at,
        "wall_qty_74_30_to_74_44": wall_qty,
        "wall_levels_74_30_to_74_44": wall_levels,
        "large_buy_qty": large_buy_qty,
        "large_sell_qty": large_sell_qty,
        "large_net": net_large,
        "tbr_trend": tbr_trend,
        "oi_trend": oi_trend,
        "combo": combo,
        "smart_dir": smart_dir,
    }
    with open("/Users/kamii/Documents/crypto-ai-anal/path_absorption_log.json", "w") as f:
        json.dump(summary, f, indent=2)

    print("\n  Log written: path_absorption_log.json")


if __name__ == "__main__":
    main()
