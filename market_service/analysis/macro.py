"""Reusable broad-market and relative-strength analysis."""

from __future__ import annotations

import asyncio
import statistics

from market_service.clients.binance import Binance
from market_service.clients.coingecko import CoinGecko

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT")


def _returns(klines: list[list]) -> list[float]:
    return [(float(row[4]) - float(row[1])) / float(row[1]) for row in klines if float(row[1])]


def _corr(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 2: return None
    a, b = a[-n:], b[-n:]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    da, db = sum((x - ma) ** 2 for x in a), sum((x - mb) ** 2 for x in b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db) ** 0.5 if da and db else None


def _beta(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 2: return None
    a, b = a[-n:], b[-n:]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    variance = statistics.fmean([(x - mb) ** 2 for x in b])
    return statistics.fmean([(x - ma) * (y - mb) for x, y in zip(a, b)]) / variance if variance else None


def _compound(returns: list[float]) -> float:
    value = 1.0
    for ret in returns: value *= 1 + ret
    return (value - 1) * 100


async def analyze_macro(client: Binance, symbols: tuple[str, ...] = DEFAULT_SYMBOLS) -> dict:
    tickers, klines, funding = await asyncio.gather(
        asyncio.gather(*(client.spot_24h(symbol) for symbol in symbols), return_exceptions=True),
        asyncio.gather(*(client.fut_klines(symbol, interval="1h", limit=25) for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")), return_exceptions=True),
        asyncio.gather(*(client.fut_funding(symbol) for symbol in symbols), return_exceptions=True),
    )
    ticker_rows = [t for t in tickers if isinstance(t, dict)]
    ticker_rows.sort(key=lambda x: float(x.get("price_change_percent", 0)), reverse=True)
    series = {symbol: rows for symbol, rows in zip(("BTCUSDT", "ETHUSDT", "SOLUSDT"), klines) if isinstance(rows, list)}
    returns = {symbol: _returns(rows) for symbol, rows in series.items()}
    funding_rows = [{"symbol": symbol, "funding": row} for symbol, row in zip(symbols, funding) if isinstance(row, dict)]
    btc, eth, sol = returns.get("BTCUSDT", []), returns.get("ETHUSDT", []), returns.get("SOLUSDT", [])
    relationships = {"sol_btc_correlation": _corr(sol, btc), "sol_btc_beta": _beta(sol, btc),
                     "sol_eth_correlation": _corr(sol, eth), "btc_eth_correlation": _corr(btc, eth)}
    return {
        "evidence": {"spot_24h": ticker_rows, "hourly_klines": series, "funding": funding_rows},
        "relative_strength": [{"symbol": x.get("symbol"), "change_percent": float(x.get("price_change_percent", 0)), "last_price": x.get("last_price"), "quote_volume": x.get("quote_volume")} for x in ticker_rows],
        "returns_24h_percent": {"BTCUSDT": _compound(btc), "ETHUSDT": _compound(eth), "SOLUSDT": _compound(sol)},
        "relationships": relationships,
        "idiosyncratic_sol": idiosyncratic(sol, btc, relationships["sol_btc_beta"]),
        "funding": funding_rows,
    }


def idiosyncratic(asset_rets: list[float], benchmark_rets: list[float], beta: float | None) -> dict:
    """Alt-specific move residual = asset 24h return - beta * benchmark return.

    If SOL's move exceeds what BTC-beta explains, that residual is
    idiosyncratic alt-rotation (into or out of SOL). Restrained to the doctrine:
    returns the figure plus a neutral label, never a trade instruction.

    Legacy source: macro.py idiosyncratic-SOL-move block.
    """
    asset_ret = _compound(asset_rets)
    expected = beta * _compound(benchmark_rets) if beta is not None else None
    residual = (asset_ret - expected) if expected is not None else None
    label = None
    if residual is not None:
        if abs(residual) > 1.0:
            label = "OUTPERFORMING" if residual > 0 else "UNDERPERFORMING"
        else:
            label = "TRACKING"
    return {"asset_return_percent": asset_ret, "expected_beta_percent": expected,
            "residual_percent": residual, "label": label}


async def analyze_global_market(coingecko: CoinGecko | None = None) -> dict:
    if coingecko is None:
        async with CoinGecko() as client:
            return await analyze_global_market(client)
    result = await coingecko.global_market()
    return {"evidence": result, "data": result.get("data", {})}
