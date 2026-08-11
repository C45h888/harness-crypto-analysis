"""One-shot session regime read for Sat/Sun move reaction + current orderbook.

Pulls macro snapshot across BTC/ETH/SOL, then SOL orderbook + recent trades +
futures flow + positioning. Data-only output.
"""
import asyncio, sys, json
from datetime import datetime, timezone
from binance import Binance, normalize_spot_trade, normalize_fut_trade
import flow as F

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


async def main() -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n=== SNAPSHOT {ts} ===\n")
    async with Binance() as b:

        # ---------- 1. MACRO SNAPSHOT (Spot 24h + Perp Funding/OI) ----------
        print("--- MACRO SNAPSHOT (Spot 24h + Perp Funding/OI) ---")
        for sym in SYMS:
            t = await b.spot_24h(sym)
            mp = await b.fut_mark_price(sym)
            oi = await b.fut_open_interest(sym)
            print(
                f"  {sym:9s} spot_last={t['last_price']:>10s} "
                f"chg24h={float(t['price_change_percent']):+.2f}% "
                f"hi={t['high_price']} lo={t['low_price']} "
                f"quoteVol={t['quote_volume']}"
            )
            print(
                f"  {'':9s} perp_mark={mp['mark_price']:>10s} "
                f"funding={float(mp['last_funding_rate'])*100:+.4f}% "
                f"OI={oi['open_interest']}"
            )

        # ---------- 2. SOL WEEKEND PRICE ACTION (1h klines Fri-Mon) ----------
        print("\n--- SOLUSDT 1h klines (last 96 = 4d, weekend in middle) ---")
        klines = await b.spot_klines("SOLUSDT", interval="1h", limit=96)
        # Print compact: date, o/h/l/c, vol
        for k in klines[-30:]:  # last 30h ~ Fri evening onward
            d = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%a %d %H:%M")
            print(
                f"  {d}  o={k[1]}  h={k[2]}  l={k[3]}  c={k[4]}  v={k[5]}"
            )

        # ---------- 3. SOLUSDT RECENT 1m klines (last 30m) ----------
        print("\n--- SOLUSDT 1m klines (last 30 min) ---")
        k1m = await b.spot_klines("SOLUSDT", interval="1m", limit=30)
        for k in k1m:
            d = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%H:%M")
            mid = (float(k[2]) + float(k[3])) / 2
            print(
                f"  {d}  o={k[1]}  h={k[2]}  l={k[3]}  c={k[4]}  v={k[5]}"
            )

        # ---------- 4. SOL ORDERBOOK (Spot + Fut, depth 50) ----------
        spot_book = await b.spot_book("SOLUSDT", limit=50)
        fut_book = await b.fut_book("SOLUSDT", limit=50)

        def obi(bk: dict) -> tuple[float, float, float]:
            bids = [(float(x[0]), float(x[1])) for x in bk["bids"]]
            asks = [(float(x[0]), float(x[1])) for x in bk["asks"]]
            bq = sum(q for _, q in bids)
            aq = sum(q for _, q in asks)
            obi_v = (bq - aq) / (bq + aq) if (bq + aq) else 0.0
            return bq, aq, obi_v

        sb, sa, sobi = obi(spot_book)
        fb, fa, fobi = obi(fut_book)
        print("\n--- SOLUSDT ORDERBOOK (top 50 each side) ---")
        print(f"  SPOT  bid_qty={sb:>10.0f}  ask_qty={sa:>10.0f}  OBI={sobi:+.4f}")
        print(f"  FUT   bid_qty={fb:>10.0f}  ask_qty={fa:>10.0f}  OBI={fobi:+.4f}")

        # Top 8 levels each side, both venues
        print("\n  SPOT TOP 8 BIDS:")
        for px, q in [(float(x[0]), float(x[1])) for x in spot_book["bids"][:8]]:
            print(f"    {px:.4f}  {q:>9.2f} SOL  ({px*q:>10.0f} USD)")
        print("  SPOT TOP 8 ASKS:")
        for px, q in [(float(x[0]), float(x[1])) for x in spot_book["asks"][:8]]:
            print(f"    {px:.4f}  {q:>9.2f} SOL  ({px*q:>10.0f} USD)")

        print("\n  FUT TOP 8 BIDS:")
        for px, q in [(float(x[0]), float(x[1])) for x in fut_book["bids"][:8]]:
            print(f"    {px:.4f}  {q:>9.2f} SOL  ({px*q:>10.0f} USD)")
        print("  FUT TOP 8 ASKS:")
        for px, q in [(float(x[0]), float(x[1])) for x in fut_book["asks"][:8]]:
            print(f"    {px:.4f}  {q:>9.2f} SOL  ({px*q:>10.0f} USD)")

        # ---------- 5. SOLUSDT FUTURES FLOW (last 1000 aggTrades) ----------
        fut_tr = await b.fut_agg_trades("SOLUSDT", limit=1000)
        spot_tr = await b.spot_agg_trades("SOLUSDT", limit=1000)
        fut_norm = [normalize_fut_trade(t) for t in fut_tr]
        spot_norm = [normalize_spot_trade(t) for t in spot_tr]

        fsum = F.summarize(fut_norm, fut_book)
        ssum = F.summarize(spot_norm, spot_book)

        print("\n--- SOLUSDT FUTURES FLOW (last 1000 aggTrades) ---")
        for k, v in fsum.items():
            print(f"  {k:>20s}: {v}")

        print("\n--- SOLUSDT SPOT FLOW (last 1000 aggTrades) ---")
        for k, v in ssum.items():
            print(f"  {k:>20s}: {v}")

        # Bucketed CVD
        fbuckets = F.bucketed_cvd(fut_norm, window_s=60)
        sbuckets = F.bucketed_cvd(spot_norm, window_s=60)
        if len(fbuckets) >= 2 and len(sbuckets) >= 2:
            corr = F.cvd_series_corr(sbuckets, fbuckets, window_s=60)
            print(f"\n  spot-vs-fut CVD correlation (60s buckets): {corr}")

        # ---------- 6. SOL POSITIONING ----------
        oi_h = await b.fut_open_interest_history("SOLUSDT", period="5m", limit=12)
        ls = await b.fut_long_short_ratio("SOLUSDT", period="5m", limit=2)
        tla = await b.fut_top_long_short_accounts("SOLUSDT", period="5m", limit=2)
        tbs = await b.fut_taker_buy_sell("SOLUSDT", period="5m", limit=2)

        print("\n--- SOLUSDT POSITIONING ---")
        print("  OI history (5m, last 12):")
        for row in oi_h:
            d = datetime.fromtimestamp(row["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M")
            print(f"    {d}  OI={row['sum_open_interest']}  OI_value={row['sum_open_interest_value']}")

        print("  Global L/S ratio (5m, last 2):")
        for row in ls:
            print(f"    {row}")

        print("  Top trader L/S (5m, last 2):")
        for row in tla:
            print(f"    {row}")

        print("  Taker buy/sell (5m, last 2):")
        for row in tbs:
            print(f"    {row}")

        # ---------- 7. LARGE PRINTS (institutional footprint) ----------
        # find threshold: top 5% by notional
        fnotionals = [abs(float(t["price"]) * float(t["qty"])) for t in fut_norm]
        fnotionals.sort()
        if fnotionals:
            thr = fnotionals[int(len(fnotionals) * 0.95)]
            large_fut = [t for t in fut_norm if abs(float(t["price"]) * float(t["qty"])) >= thr]
            lb = sum(1 for t in large_fut if t["side"] == "buy")
            ls_ = sum(1 for t in large_fut if t["side"] == "sell")
            lb_n = sum(float(t["price"]) * float(t["qty"]) for t in large_fut if t["side"] == "buy")
            ls_n = sum(float(t["price"]) * float(t["qty"]) for t in large_fut if t["side"] == "sell")
            print(f"\n--- SOLUSDT LARGE PRINTS (fut, top 5% by notional, threshold>={thr:.0f}) ---")
            print(f"  count: {len(large_fut)}  buy={lb}  sell={ls_}  ratio_b/s={lb/ls_ if ls_ else 99:.2f}")
            print(f"  notional: buy=${lb_n:,.0f}  sell=${ls_n:,.0f}  ratio_b/s={lb_n/ls_n if ls_n else 99:.2f}")
            # Show the actual large prints
            for t in sorted(large_fut, key=lambda x: float(x["price"]) * float(x["qty"]), reverse=True)[:10]:
                print(
                    f"    {datetime.fromtimestamp(t['ts']/1000, tz=timezone.utc).strftime('%H:%M:%S')} "
                    f"{t['side'].upper():4s} {float(t['price']):.4f}  "
                    f"{float(t['qty']):.2f} SOL  "
                    f"${float(t['price'])*float(t['qty']):,.0f}"
                )

        # ---------- 8. KEYSTONE ZONE - find deepest bid walls ----------
        print("\n--- KEYSTONE IDENTIFICATION (largest bid walls in current book) ---")
        all_spot_bids = sorted(
            [(float(x[0]), float(x[1])) for x in spot_book["bids"]], key=lambda x: -x[1]
        )[:5]
        all_fut_bids = sorted(
            [(float(x[0]), float(x[1])) for x in fut_book["bids"]], key=lambda x: -x[1]
        )[:5]
        all_spot_asks = sorted(
            [(float(x[0]), float(x[1])) for x in spot_book["asks"]], key=lambda x: -x[1]
        )[:5]
        all_fut_asks = sorted(
            [(float(x[0]), float(x[1])) for x in fut_book["asks"]], key=lambda x: -x[1]
        )[:5]

        print("  SPOT deepest 5 bid walls:")
        for px, q in all_spot_bids:
            print(f"    {px:.4f}  {q:>9.2f} SOL  (${px*q:>10,.0f})")
        print("  FUT deepest 5 bid walls:")
        for px, q in all_fut_bids:
            print(f"    {px:.4f}  {q:>9.2f} SOL  (${px*q:>10,.0f})")
        print("  SPOT deepest 5 ask walls:")
        for px, q in all_spot_asks:
            print(f"    {px:.4f}  {q:>9.2f} SOL  (${px*q:>10,.0f})")
        print("  FUT deepest 5 ask walls:")
        for px, q in all_fut_asks:
            print(f"    {px:.4f}  {q:>9.2f} SOL  (${px*q:>10,.0f})")

    print("\n=== END SNAPSHOT ===")


if __name__ == "__main__":
    asyncio.run(main())
