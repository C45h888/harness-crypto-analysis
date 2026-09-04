"""Small async CoinGecko public client for broad market context."""

from __future__ import annotations

import aiohttp

BASE_URL = "https://api.coingecko.com/api/v3"


class CoinGecko:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *args):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, **params):
        assert self._session is not None
        async with self._session.get(BASE_URL + path, params=params, headers={"User-Agent": "crypto-ai-anal/1.0"}) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def global_market(self) -> dict:
        return await self._get("/global")
