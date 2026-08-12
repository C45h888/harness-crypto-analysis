"""
Direct async REST client for Binance public market data.

Bypasses both `binance-sdk-spot` and `binance-sdk-derivatives-trading-usds-futures`
because their methods return synchronous ApiResponse wrappers that are NOT
directly awaitable from async code (would raise
`TypeError: object ApiResponse can't be used in 'await' expression`).

Public endpoints only - no API keys required.

  Spot:      https://api.binance.com
  Futures:   https://fapi.binance.com

All methods are genuine coroutines that return native dicts / lists.
"""

from __future__ import annotations

from typing import Any

import aiohttp

SPOT_BASE = "https://api.binance.com"
FUT_BASE = "https://fapi.binance.com"


# ---------- shared helpers ----------


_VALID_KLINE_INTERVALS = frozenset({
    "1s", "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
})


def _require_kline_interval(interval: str | None) -> str | None:
    """Validate a klines interval string. None passes through."""
    if interval is None:
        return None
    if interval not in _VALID_KLINE_INTERVALS:
        valid = ", ".join(repr(v) for v in sorted(_VALID_KLINE_INTERVALS))
        raise ValueError(f"unknown klines interval {interval!r} - valid: {valid}")
    return interval


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    """Strip None values before passing to aiohttp."""
    return {k: v for k, v in params.items() if v is not None}


def _camel_to_snake(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Translate a fixed set of camelCase Binance fields to snake_case.

    Pass-through dict mutation: any of `keys` present in `d` are renamed
    (in place) to their snake_case form. Other keys are untouched.
    """
    for k in keys:
        if k in d and "_" not in k:
            snake = "".join("_" + c.lower() if c.isupper() else c for c in k).lstrip("_")
            d[snake] = d.pop(k)
    return d


# ---------- low-level REST helper ----------


class _Rest:
    """Thin async REST helper around `aiohttp.ClientSession`.

    Every call returns a real coroutine and parses JSON natively.
    No pydantic, no oneOf wrappers, no ApiResponse nonsense.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "_Rest":
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self.base_url,
                headers={"User-Agent": "crypto-ai-anal/1.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get(self, path: str, **params: Any) -> Any:
        await self._ensure_session()
        assert self._session is not None
        async with self._session.get(path, params=_drop_none(params)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


# ---------- combined client ----------


class Binance:
    """Combined Spot + USD-M Futures public client.

    Every method is a real coroutine returning native dicts/lists.
    Spot and futures run on independent aiohttp sessions.
    Binance camelCase fields are normalized to snake_case at the response
    boundary so the rest of the project uses one naming convention.
    """

    def __init__(self) -> None:
        self._spot = _Rest(SPOT_BASE)
        self._fut = _Rest(FUT_BASE)

    async def __aenter__(self) -> "Binance":
        await self._spot.__aenter__()
        await self._fut.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._spot.close()
        await self._fut.close()

    # ---------- spot ----------

    async def spot_book(self, symbol: str, limit: int = 50) -> dict:
        """L2 orderbook. Returns `{lastUpdateId, bids: [[p,q],...], asks: [[p,q],...]}`."""
        self._require_symbol(symbol, "spot_book")
        return await self._spot._get("/api/v3/depth", symbol=symbol, limit=limit)

    async def spot_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        """Recent raw trade prints (oldest-first)."""
        self._require_symbol(symbol, "spot_trades")
        return await self._spot._get("/api/v3/trades", symbol=symbol, limit=limit)

    async def spot_agg_trades(
        self,
        symbol: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        """Aggregated (deduplicated) trades. Supports time-window fetch."""
        self._require_symbol(symbol, "spot_agg_trades")
        return await self._spot._get(
            "/api/v3/aggTrades",
            symbol=symbol,
            limit=limit,
            startTime=start_time,
            endTime=end_time,
        )

    async def spot_book_ticker(self, symbol: str | None = None) -> list[dict] | dict:
        """Best bid/ask across one symbol (wrapped in a list) or all symbols."""
        if symbol:
            data = await self._spot._get("/api/v3/ticker/bookTicker", symbol=symbol)
            return [data] if not isinstance(data, list) else data
        return await self._spot._get("/api/v3/ticker/bookTicker")

    async def spot_24h(self, symbol: str) -> dict:
        """Returns fields are camelCase (priceChangePercent, quoteVolume, etc.).
        Renamed to snake_case at this boundary."""
        self._require_symbol(symbol, "spot_24h")
        r = await self._spot._get("/api/v3/ticker/24hr", symbol=symbol)
        return _camel_to_snake(
            r,
            (
                "priceChange", "priceChangePercent", "weightedAvgPrice",
                "prevClosePrice", "lastPrice", "lastQty",
                "bidPrice", "bidQty", "askPrice", "askQty",
                "openPrice", "highPrice", "lowPrice",
                "quoteVolume", "openTime", "closeTime",
                "firstId", "lastId", "count",
            ),
        )

    async def spot_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 500,
    ) -> list[list]:
        """OHLCV candles. `interval` validated against Binance's allowed set."""
        self._require_symbol(symbol, "spot_klines")
        iv = _require_kline_interval(interval)
        return await self._spot._get(
            "/api/v3/klines",
            symbol=symbol,
            interval=iv,
            limit=limit,
        )

    # ---------- futures (USD-M) ----------

    async def fut_book(self, symbol: str, limit: int = 50) -> dict:
        """L2 orderbook snapshot. `lastUpdateId` field is camelCase."""
        self._require_symbol(symbol, "fut_book")
        return await self._fut._get("/fapi/v1/depth", symbol=symbol, limit=limit)

    async def fut_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        """Recent raw trade prints. Binance fields: `time`, `price`, `qty`, `isBuyerMaker`."""
        self._require_symbol(symbol, "fut_trades")
        return await self._fut._get("/fapi/v1/trades", symbol=symbol, limit=limit)

    async def fut_agg_trades(
        self,
        symbol: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        self._require_symbol(symbol, "fut_agg_trades")
        return await self._fut._get(
            "/fapi/v1/aggTrades",
            symbol=symbol,
            limit=limit,
            startTime=start_time,
            endTime=end_time,
        )

    async def fut_agg_trades_paginated(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
        limit: int = 1000,
        max_pages: int = 200,
    ) -> list[dict]:
        """Pull all USD-M aggTrades in ``[start_time, end_time]`` via fromId walk.

        Migrated from legacy ``long_term_flow.pull_trades``. Rows return the raw
        Binance aggTrades shape (``a/p/q/T/m``) so callers can normalize with
        ``normalize_fut_trade``. Stops when a page is empty or crosses ``end_time``.
        """
        self._require_symbol(symbol, "fut_agg_trades_paginated")
        out: list[dict] = []
        batch = await self.fut_agg_trades(symbol, limit=limit, start_time=start_time, end_time=end_time)
        out.extend(batch)
        last_id = batch[-1]["a"] if batch else None
        page = 1
        while batch and page < max_pages:
            if last_id is None:
                break
            page += 1
            batch = await self._fut._get(
                "/fapi/v1/aggTrades", symbol=symbol, limit=limit,
                fromId=last_id + 1, endTime=end_time,
            )
            if not batch:
                break
            if batch[0]["T"] > end_time:
                break
            out.extend(batch)
            last_id = batch[-1]["a"]
        return out

    async def fut_book_ticker(self, symbol: str) -> list[dict]:
        """Best bid/ask for one symbol. Returns a one-element list (shape
        consistency with `spot_book_ticker`)."""
        self._require_symbol(symbol, "fut_book_ticker")
        data = await self._fut._get("/fapi/v1/ticker/bookTicker", symbol=symbol)
        return [data] if not isinstance(data, list) else data

    async def fut_24h(self, symbol: str) -> dict:
        """24h ticker. All Binance fields normalized to snake_case."""
        self._require_symbol(symbol, "fut_24h")
        r = await self._fut._get("/fapi/v1/ticker/24hr", symbol=symbol)
        return _camel_to_snake(
            r,
            (
                "priceChange", "priceChangePercent", "weightedAvgPrice",
                "lastPrice", "lastQty",
                "bidPrice", "bidQty", "askPrice", "askQty",
                "openPrice", "highPrice", "lowPrice",
                "baseVolume", "quoteVolume",
                "openTime", "closeTime",
                "firstId", "lastId", "count",
            ),
        )

    async def fut_funding(self, symbol: str) -> dict:
        """Mark price + funding rate + next funding time for a single symbol.

        Backed by `/fapi/v1/premiumIndex`. Renames `lastFundingRate` ->
        `last_funding_rate`, `nextFundingTime` -> `next_funding_time`.
        """
        self._require_symbol(symbol, "fut_funding")
        r = await self._fut._get("/fapi/v1/premiumIndex", symbol=symbol)
        if not r:
            raise ValueError(f"fut_funding: no data returned for symbol={symbol!r}")
        return _camel_to_snake(r, ("lastFundingRate", "nextFundingTime", "markPrice",
                                   "indexPrice", "estimatedSettlePrice", "interestRate"))

    async def fut_open_interest(self, symbol: str) -> dict:
        """Current open interest in contracts. `openInterest` -> `open_interest`."""
        self._require_symbol(symbol, "fut_open_interest")
        r = await self._fut._get("/fapi/v1/openInterest", symbol=symbol)
        if not r:
            raise ValueError(f"fut_open_interest: no data for symbol={symbol!r}")
        return _camel_to_snake(r, ("openInterest",))

    async def fut_klines(
        self,
        symbol: str,
        interval: str = "5m",
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list]:
        """USD-M futures OHLCV candles with the same shape as spot klines."""
        self._require_symbol(symbol, "fut_klines")
        _require_kline_interval(interval)
        return await self._fut._get(
            "/fapi/v1/klines", symbol=symbol, interval=interval,
            limit=limit, startTime=start_time, endTime=end_time,
        )

    async def fut_price(self, symbol: str) -> dict:
        """Current USD-M contract price."""
        self._require_symbol(symbol, "fut_price")
        return await self._fut._get("/fapi/v1/ticker/price", symbol=symbol)

    async def fut_funding_history(self, symbol: str, limit: int = 30) -> list[dict]:
        """Historical USD-M funding events, normalized to snake_case."""
        self._require_symbol(symbol, "fut_funding_history")
        rows = await self._fut._get("/fapi/v1/fundingRate", symbol=symbol, limit=limit)
        if not isinstance(rows, list):
            raise ValueError("fut_funding_history: expected a list")
        for row in rows:
            _camel_to_snake(row, ("fundingTime", "fundingRate", "markPrice"))
        return rows

    async def fut_mark_price(self, symbol: str) -> dict:
        """Mark price + funding snapshot. Symbol required."""
        self._require_symbol(symbol, "fut_mark_price")
        r = await self._fut._get("/fapi/v1/premiumIndex", symbol=symbol)
        return _camel_to_snake(r, ("lastFundingRate", "nextFundingTime", "markPrice",
                                   "indexPrice", "estimatedSettlePrice", "interestRate"))

    async def fut_open_interest_history(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 30,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        """Historical OI over time. Backed by `/futures/data/openInterestHist`.

        Each row: `{symbol, sumOpenInterest, sumOpenInterestValue, timestamp}`.
        `sumOpenInterest` is in contracts; `sumOpenInterestValue` is the USD
        notional at the time of the snapshot. `timestamp` is in milliseconds.
        """
        self._require_symbol(symbol, "fut_open_interest_history")
        r = await self._fut._get(
            "/futures/data/openInterestHist",
            symbol=symbol,
            period=period,
            limit=limit,
            startTime=start_time,
            endTime=end_time,
        )
        if not isinstance(r, list):
            raise ValueError(
                f"fut_open_interest_history: expected list, got {type(r).__name__}: {r!r}"
            )
        for row in r:
            _camel_to_snake(row, ("sumOpenInterest", "sumOpenInterestValue"))
        return r

    async def fut_taker_buy_sell(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 30,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        """Taker buy/sell volume split. Backed by `/futures/data/takerlongshortRatio`.

        Each row: `{buySellRatio, buyVol, sellVol, timestamp}`. `buyVol`/`sellVol`
        are base-currency (SOL). `buySellRatio` = buyVol / sellVol.
        """
        self._require_symbol(symbol, "fut_taker_buy_sell")
        r = await self._fut._get(
            "/futures/data/takerlongshortRatio",
            symbol=symbol,
            period=period,
            limit=limit,
            startTime=start_time,
            endTime=end_time,
        )
        if not isinstance(r, list):
            raise ValueError(
                f"fut_taker_buy_sell: expected list, got {type(r).__name__}: {r!r}"
            )
        for row in r:
            _camel_to_snake(row, ("buySellRatio", "buyVol", "sellVol"))
        return r

    async def fut_top_long_short_accounts(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 30,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        """Top-trader accounts long/short ratio. Backed by
        `/futures/data/topLongShortAccountRatio`.

        Each row: `{longAccount, longShortRatio, shortAccount, timestamp}`.
        `longAccount` / `shortAccount` are proportions (0..1).
        """
        self._require_symbol(symbol, "fut_top_long_short_accounts")
        r = await self._fut._get(
            "/futures/data/topLongShortAccountRatio",
            symbol=symbol,
            period=period,
            limit=limit,
            startTime=start_time,
            endTime=end_time,
        )
        if not isinstance(r, list):
            raise ValueError(
                f"fut_top_long_short_accounts: expected list, got {type(r).__name__}: {r!r}"
            )
        for row in r:
            _camel_to_snake(row, ("longAccount", "longShortRatio", "shortAccount"))
        return r

    async def fut_long_short_ratio(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 30,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        """All-trader accounts long/short ratio. Backed by
        `/futures/data/globalLongShortAccountRatio`.

        Each row: `{longAccount, longShortRatio, shortAccount, timestamp}`.
        """
        self._require_symbol(symbol, "fut_long_short_ratio")
        r = await self._fut._get(
            "/futures/data/globalLongShortAccountRatio",
            symbol=symbol,
            period=period,
            limit=limit,
            startTime=start_time,
            endTime=end_time,
        )
        if not isinstance(r, list):
            raise ValueError(
                f"fut_long_short_ratio: expected list, got {type(r).__name__}: {r!r}"
            )
        for row in r:
            _camel_to_snake(row, ("longAccount", "longShortRatio", "shortAccount"))
        return r

    async def fut_premium_kline(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list:
        """Premium index klines. Backed by `/fapi/v1/premiumIndexKlines`.

        Returns rows in the same OHLCV shape as `fut_klines` but with the
        premium index as the price field. Useful for divergence checks
        between perp and spot.
        """
        self._require_symbol(symbol, "fut_premium_kline")
        _require_kline_interval(interval)
        return await self._fut._get(
            "/fapi/v1/premiumIndexKlines",
            symbol=symbol,
            interval=interval,
            limit=limit,
            startTime=start_time,
            endTime=end_time,
        )

    # ---------- guards ----------

    @staticmethod
    def _require_symbol(symbol: str | None, op: str) -> None:
        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"{op}: symbol must be a non-empty string, got {symbol!r}")


# ---------- trade-print normalization ----------


def _normalize_trade(raw: Any, venue: str) -> dict:
    """Trim a spot OR futures trade row into a uniform shape with side.

    Accepts BOTH shapes:
      - recent_trades (camelCase): time, price, qty, isBuyerMaker
      - aggTrades (single-letter): T, p, q, m

    `side` is "buy" when taker was NOT buyer-maker (aggressive buy),
    "sell" when taker WAS buyer-maker (aggressive sell).

    Defensive against malformed rows: missing required fields become 0
    rather than crashing the whole pipeline with KeyError.
    """
    if not isinstance(raw, dict):
        return {"ts": 0, "id": 0, "price": 0.0, "qty": 0.0, "quote_qty": 0.0,
                "is_buyer_maker": False, "side": "buy", "venue": venue}
    # aggTrades single-letter keys: p, q, T, m
    # recent_trades camelCase keys:    price, qty, time, isBuyerMaker
    try:
        price = float(raw.get("p", raw.get("price", 0)))
        qty = float(raw.get("q", raw.get("qty", 0)))
    except (KeyError, TypeError, ValueError):
        return {"ts": 0, "id": 0, "price": 0.0, "qty": 0.0, "quote_qty": 0.0,
                "is_buyer_maker": False, "side": "buy", "venue": venue}
    # is_buyer_maker: aggTrades `m` field, recent_trades `isBuyerMaker` / `is_buyer_maker`
    if "m" in raw:
        is_buyer_maker = bool(raw["m"])
    else:
        is_buyer_maker = bool(raw.get("isBuyerMaker", raw.get("is_buyer_maker", False)))
    ts = int(raw.get("T") or raw.get("time") or 0)
    tid = int(raw.get("a") or raw.get("id") or 0)
    quote_qty = float(
        raw.get("quoteQty") or raw.get("quote_qty")
        or (raw.get("nq") and price * qty)
        or price * qty
    )
    return {
        "ts": ts,
        "id": tid,
        "price": price,
        "qty": qty,
        "quote_qty": quote_qty,
        "is_buyer_maker": is_buyer_maker,
        "side": "sell" if is_buyer_maker else "buy",
        "venue": venue,
    }


def normalize_spot_trade(raw: Any) -> dict:
    """Trim a spot get_trades row into a uniform shape with side."""
    return _normalize_trade(raw, venue="spot")


def normalize_fut_trade(raw: Any) -> dict:
    """Trim a futures recent_trades row into a uniform shape with side."""
    return _normalize_trade(raw, venue="fut")


# ---------- smoke test ----------


if __name__ == "__main__":
    import asyncio
    import inspect

    async def _smoke() -> None:
        async with Binance() as b:
            methods = [m for m in dir(b) if m.startswith(("spot_", "fut_")) and not m.startswith("_")]
            for m in methods:
                fn = getattr(b, m)
                if not callable(fn):
                    continue
                # Per-method arg shape for the awaitability probe.
                if m.endswith("book_ticker") or m == "fut_mark_price":
                    coro = fn("BTCUSDT")
                elif m in {"spot_trades", "spot_agg_trades", "fut_trades",
                           "fut_agg_trades", "spot_klines", "spot_book", "fut_book"}:
                    coro = fn("BTCUSDT", limit=3)
                else:
                    coro = fn("BTCUSDT")
                print(f"  {m:18s} returns {type(coro).__name__:11s} awaitable={inspect.iscoroutine(coro)}")
                coro.close()

            print()
            print("Live calls:")
            print("  spot book_ticker:", (await b.spot_book_ticker("BTCUSDT"))[0])
            print("  fut  book_ticker:", (await b.fut_book_ticker("BTCUSDT"))[0])
            fund = await b.fut_funding("BTCUSDT")
            print(f"  fut  funding    : rate={fund.get('last_funding_rate')} next={fund.get('next_funding_time')}")
            oi = await b.fut_open_interest("BTCUSDT")
            print(f"  fut  OI         : {oi.get('open_interest')} contracts")

    asyncio.run(_smoke())