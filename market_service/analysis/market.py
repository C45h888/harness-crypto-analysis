"""
Combined Binance + CryptoQuant market analysis.

Pulls spot + USD-M futures order flow on the same symbol via the official
Binance SDKs, computes orderflow metrics, correlates the two venues, and
threads in CryptoQuant on-chain metric descriptions from the MCP.

Public API only - no Binance keys required.
CryptoQuant key comes from CRYPTOQUANT_API_KEY env var (already wired in
~/.hermes/config.yaml when running inside Hermes).

Usage:
    python analyze.py BTCUSDT                   # one-shot
    python analyze.py BTCUSDT --json            # JSON output
    python analyze.py BTCUSDT --trades 1000     # more trades per venue
    python analyze.py BTCUSDT --window 10       # 10s bucket correlation
    python analyze.py ETHUSDT                   # any USDT-M pair on Binance
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

from market_service.clients.binance import Binance, normalize_fut_trade, normalize_spot_trade
from market_service.calculations.flow import bucketed_cvd, cvd_series_corr, summarize
from market_service.calculations.signals import deterministic_signals
from market_service.analysis.oi import analyze_open_interest
from market_service.analysis.liquidations import analyze_liquidation_pressure
from market_service.analysis.macro import analyze_macro

log = logging.getLogger(__name__)


# ---------- CryptoQuant side ----------


def cryptoquant_context(asset: str = "btc") -> dict:
    """
    Pull descriptions + thresholds for the most-watched on-chain metrics.
    On the basic plan numeric data is locked but description text still flows.
    """
    try:
        from market_service.clients.cryptoquant import describe_metric
    except Exception as e:
        return {"available": False, "reason": f"import failed: {e}"}

    metrics = {
        "btc": ["mvrv", "sopr", "exchange-netflow", "funding-rates", "whale-ratio"],
        "eth": ["mvrv", "exchange-netflow", "funding-rates"],
        "alt": ["exchange-netflow", "funding-rates"],
        "stablecoin": ["exchange-netflow"],
        "erc20": ["exchange-netflow", "funding-rates"],
        "trx": ["exchange-netflow"],
        "xrp": ["exchange-netflow"],
    }.get(asset, ["exchange-netflow"])

    ctx: dict[str, Any] = {"available": True, "asset": asset, "metrics": {}}
    for m in metrics:
        try:
            d = describe_metric(m)
            if isinstance(d, dict) and d.get("success"):
                ctx["metrics"][m] = d["metric"]
            else:
                ctx["metrics"][m] = {
                    "description": "(locked on basic plan)",
                    "thresholds": None,
                    "interpretation": None,
                }
        except Exception as e:
            ctx["metrics"][m] = {
                "description": f"(error: {e})",
                "thresholds": None,
                "interpretation": None,
            }
    return ctx


def _asset(symbol: str) -> str:
    """Strip quote assets -> base asset string."""
    for q in ("USDT", "USDC", "BUSD", "FDUSD", "USD", "BTC", "ETH"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)].lower()
    return symbol.lower()


def _f(x: Any, places: int = 4) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{places}f}"
    return str(x)


async def _safe(coro_factory, name: str) -> tuple[Any, str | None]:
    """Run an endpoint call, capturing any exception so one failing endpoint
    doesn't kill the whole snapshot. Returns (result, error_string-or-None).
    `coro_factory` must be a zero-arg callable returning a fresh coroutine.
    """
    try:
        return await coro_factory(), None
    except Exception as e:
        log.warning("endpoint %s failed: %s", name, e)
        return None, f"{type(e).__name__}: {e}"


# ---------- core analysis ----------


async def analyze(
    symbol: str,
    trade_limit: int = 500,
    depth_limit: int | None = None,
    bucket_window_s: int = 60,
    include_extended: bool = True,
) -> dict:
    """One-shot combined snapshot with raw Binance evidence and derived metrics.

    ``depth_limit`` defaults to the centralized canonical order-book depth
    (``DEPTH_LEVELS``) when not given, so every assert path uses one source.
    """
    if depth_limit is None:
        from market_service.config import default_depth_levels
        depth_limit = default_depth_levels()
    started_at_ms = int(time.time() * 1000)
    out: dict[str, Any] = {
        "contract": {
            "name": "crypto-ai-market-snapshot",
            "version": 1,
            "raw_evidence_included": True,
            "derived_metrics_are_deterministic": True,
            "null_semantics": "null means the source did not return a value; it is not zero",
        },
        "symbol": symbol,
        "generated_at_ms": started_at_ms,
        "data_source": "binance_public_rest_live",
        "requested": {"trade_limit": trade_limit, "depth_limit": depth_limit, "bucket_window_s": bucket_window_s, "include_extended": include_extended},
        "errors": [],
    }

    async with Binance() as b:
        t0 = time.time()
        # Run every endpoint independently so one 5xx / timeout / 429 doesn't
        # crash the whole snapshot - graceful degradation > total failure.
        # EVERY endpoint error must be tracked - silent drops on the orderbook
        # endpoints hide missing OBI/spread in the rendered output.
        all_results = await asyncio.gather(
            _safe(lambda: b.spot_book(symbol, limit=depth_limit), "spot_book"),
            _safe(lambda: b.fut_book(symbol, limit=depth_limit), "fut_book"),
            _safe(lambda: b.spot_24h(symbol), "spot_24h"),
            _safe(lambda: b.fut_24h(symbol), "fut_24h"),
            _safe(lambda: b.fut_funding(symbol), "fut_funding"),
            _safe(lambda: b.fut_open_interest(symbol), "fut_open_interest"),
            _safe(lambda: b.spot_trades(symbol, limit=trade_limit), "spot_trades"),
            _safe(lambda: b.fut_trades(symbol, limit=trade_limit), "fut_trades"),
        )
        # Unpack every (result, error) pair so nothing gets silently dropped.
        (spot_book, e_book), (fut_book, e_fbook), (spot_24h, e24), (fut_24h, ef24), \
        (fut_fund, efund), (fut_oi, eoi), (spot_trades_raw, est), (fut_trades_raw, eft) = all_results
        endpoint_names = ("spot_book", "fut_book", "spot_24h", "fut_24h",
                          "fut_funding", "fut_open_interest", "spot_trades", "fut_trades")
        endpoint_errs = (e_book, e_fbook, e24, ef24, efund, eoi, est, eft)
        for name, err in zip(endpoint_names, endpoint_errs):
            if err:
                out["errors"].append({"endpoint": name, "error": err})
        if include_extended:
            extended_results = await asyncio.gather(
                _safe(lambda: analyze_open_interest(b, symbol), "open_interest_analysis"),
                _safe(lambda: analyze_liquidation_pressure(b, symbol), "liquidation_pressure"),
                _safe(lambda: analyze_macro(b), "macro_analysis"),
            )
        else:
            extended_results = []
        out["latency_ms"] = round((time.time() - t0) * 1000, 1)

    if include_extended:
        for name, (value, error) in zip(("open_interest_analysis", "liquidation_pressure", "macro_analysis"), extended_results):
            if error:
                out["errors"].append({"endpoint": name, "error": error})
            out[name] = value

    spot_trades = [dict(normalize_spot_trade(t), venue="spot") for t in (spot_trades_raw or [])]
    fut_trades = [normalize_fut_trade(t) for t in (fut_trades_raw or [])]
    spot_flow = summarize(spot_trades, spot_book, depth_levels=depth_limit)
    fut_flow = summarize(fut_trades, fut_book, depth_levels=depth_limit)
    spot_cvd = bucketed_cvd(spot_trades, window_s=bucket_window_s)
    fut_cvd = bucketed_cvd(fut_trades, window_s=bucket_window_s)

    # Preserve successful source payloads alongside the normalized inputs used
    # by the deterministic calculations. Nothing is silently reduced to a label.
    out["evidence"] = {
        "spot": {
            "ticker_24h": spot_24h,
            "order_book": spot_book,
            "trades": spot_trades_raw,
            "normalized_trades": spot_trades,
        },
        "futures": {
            "ticker_24h": fut_24h,
            "order_book": fut_book,
            "trades": fut_trades_raw,
            "normalized_trades": fut_trades,
            "funding": fut_fund,
            "open_interest": fut_oi,
        },
    }

    out["spot"] = {
        "ticker_24h": spot_24h,
        "order_book": {"levels": depth_limit, "raw": spot_book},
        "flow": spot_flow,
        "bucketed_cvd": spot_cvd,
    }

    out["futures"] = {
        "ticker_24h": fut_24h,
        "funding": fut_fund,
        "open_interest": fut_oi,
        "order_book": {"levels": depth_limit, "raw": fut_book},
        "flow": fut_flow,
        "bucketed_cvd": fut_cvd,
    }

    spot_times = [t["ts"] for t in spot_trades if t["ts"] > 0]
    fut_times = [t["ts"] for t in fut_trades if t["ts"] > 0]
    out["coverage"] = {
        "spot_trade_count": len(spot_trades),
        "futures_trade_count": len(fut_trades),
        "spot_oldest_trade_ms": min(spot_times, default=None),
        "futures_oldest_trade_ms": min(fut_times, default=None),
        "spot_newest_trade_ms": max(spot_times, default=None),
        "futures_newest_trade_ms": max(fut_times, default=None),
        "spot_trade_span_seconds": (max(spot_times) - min(spot_times)) / 1000 if len(spot_times) > 1 else 0,
        "futures_trade_span_seconds": (max(fut_times) - min(fut_times)) / 1000 if len(fut_times) > 1 else 0,
        "generated_at_ms": started_at_ms,
        "completed_at_ms": int(time.time() * 1000),
    }
    out["correlation"] = cvd_series_corr(
        out["spot"]["bucketed_cvd"],
        out["futures"]["bucketed_cvd"],
        window_s=bucket_window_s,
    )

    spot_buy_share = spot_flow.get("buy_share", 0.5)
    futures_buy_share = fut_flow.get("buy_share", 0.5)
    signal_snapshot = {
        "spot_buy_share": spot_buy_share,
        "futures_buy_share": futures_buy_share,
        "spot_obi_top_n": spot_flow.get("obi") or 0.0,
        "open_interest": float(fut_oi["open_interest"]) if isinstance(fut_oi, dict) and fut_oi.get("open_interest") is not None else None,
    }
    out["signals"] = deterministic_signals(signal_snapshot, None)
    out["signal_inputs"] = signal_snapshot
    out["status"] = "degraded" if out["errors"] else "healthy"
    out["cryptoquant"] = cryptoquant_context(_asset(symbol))
    return out


# ---------- pretty printing ----------


def _render_venue(L: list[str], v: dict) -> None:
    fl = v["flow"]
    a = L.append
    t = v.get("ticker_24h") or {}
    if isinstance(t, dict) and t:
        a(
            f"  24h                 : open {t.get('open_price')} "
            f"high {t.get('high_price')} low {t.get('low_price')} "
            f"vol {t.get('volume')} quoteVol {t.get('quote_volume')}"
        )
        a(f"  24h change          : {t.get('price_change_percent')}%   trades: {t.get('count')}")
    a(f"  best bid/ask        : {fl.get('best_bid')} / {fl.get('best_ask')}")
    a(f"  spread              : {_f(fl.get('spread_bps'), 2)} bps")
    a(f"  OBI (top-20)        : {_f(fl.get('obi'), 4)}")
    a(f"  last price          : {fl.get('last_price')}")
    a(f"  VWAP (+/- sd)       : {_f(fl.get('vwap'), 2)} (+/- {_f(fl.get('vwap_stddev'), 2)})")
    a(f"  recent trades       : {fl.get('trade_count')}   large: {fl.get('large_trade_count')}")
    a(f"  buy/sell vol        : {_f(fl.get('buy_vol'))} / {_f(fl.get('sell_vol'))}")
    a(f"  CVD                 : {_f(fl.get('cvd'))}")
    bs = fl.get("buy_sell_ratio")
    a(f"  buy:sell ratio      : {bs if bs is None else f'{bs:.3f}'}")
    if fl.get("large_trades"):
        a(f"  top large trade notional (USD):")
        for lt in fl["large_trades"][:5]:
            a(
                f"    {lt['venue']:5s} {lt['side']:4s}  "
                f"qty={lt['qty']:.4f}  px={lt['price']}  notional=${lt['notional_usd']:.0f}"
            )


def render(snap: dict) -> str:
    sym = snap["symbol"]
    s = snap["spot"]
    f = snap["futures"]
    corr = snap["correlation"]
    cq = snap["cryptoquant"]
    errs = snap.get("errors") or []

    L: list[str] = []
    a = L.append
    a(f"=== {sym}  (combined: spot + USD-M futures + CryptoQuant) ===")
    a(
        f"    latency: {snap['latency_ms']}ms  "
        f"aligned buckets: {corr.get('aligned_buckets', 0)}"
    )
    if errs:
        a(f"    {len(errs)} endpoint error(s):")
        for e in errs:
            a(f"      - {e['endpoint']}: {e['error']}")
    a("")
    a("--- Spot ---")
    _render_venue(L, s)
    a("")
    a("--- USD-M Futures ---")
    _render_venue(L, f)

    fund = f.get("funding")
    if isinstance(fund, dict) and fund:
        rate = fund.get("last_funding_rate") or fund.get("lastFundingRate")
        nxt = fund.get("next_funding_time") or fund.get("nextFundingTime")
        if rate is not None and rate != "":
            a(f"  funding rate        : {rate}")
        if nxt is not None:
            a(f"  next funding in     : {nxt} (ms)")

    oi = f.get("open_interest")
    if isinstance(oi, dict) and oi:
        a(f"  open interest       : {oi.get('open_interest')} {oi.get('symbol', '')}")

    a("")
    a("--- Spot vs Futures CVD correlation ---")
    a(f"  aligned buckets     : {corr['aligned_buckets']}")
    a(f"  pearson r           : {_f(corr['spot_vs_futures_corr'], 3)}")
    a("  >0 = both venues agree (directional move)")
    a("  <0 = divergence (often reversal setup)")
    a("")
    a("--- CryptoQuant on-chain context ---")
    if not cq.get("available"):
        a(f"  (unavailable: {cq.get('reason')})")
    else:
        a(f"  asset: {cq.get('asset')}  (basic plan: descriptions only, no numerics)")
        for name, info in (cq.get("metrics") or {}).items():
            if isinstance(info, dict):
                d = info.get("description", "") or ""
                t = info.get("thresholds") or ""
                a(f"  {name:18s} : {d[:90]}{'...' if len(d) > 90 else ''}")
                if t:
                    a(f"  {' ' * 20}thresholds: {t}")
    return "\n".join(L)


# ---------- CLI ----------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Binance + CryptoQuant market analysis")
    p.add_argument("symbol", nargs="?", default="BTCUSDT", help="Binance symbol, e.g. BTCUSDT, ETHUSDT")
    p.add_argument("--trades", type=int, default=500, help="recent trades to pull per venue")
    p.add_argument("--depth", type=int, default=None,
                   help="order book depth (default: centralized DEPTH_LEVELS)")
    p.add_argument("--window", type=int, default=60, help="CVD bucket window in seconds")
    p.add_argument("--json", action="store_true", help="emit JSON instead of pretty text")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    snap = asyncio.run(
        analyze(
            args.symbol,
            trade_limit=args.trades,
            depth_limit=args.depth,
            bucket_window_s=args.window,
        )
    )
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(render(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())