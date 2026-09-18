"""Long event-grain capture: Binance bookTicker WS (spot + futures, BTCUSDT).

Push-based top-of-book: every message is one authoritative (bid,ask,qty)
state. No polling, no reconstruction. Both venues concurrently via aiohttp.
Usage: python scripts/capture_bookticker.py <seconds> <out_prefix>
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import aiohttp

STREAMS = {
    "spot": "wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
    "futures": "wss://fstream.binance.com/ws/btcusdt@bookTicker",
}


async def capture_one(url: str, seconds: float) -> list[dict]:
    rows: list[dict] = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(url, heartbeat=20) as ws:
                    async for msg in ws:
                        if time.time() >= end:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = json.loads(msg.data)
                        except ValueError:
                            continue
                        rows.append({
                            "ts": int(time.time() * 1000),
                            "u": d.get("u"),
                            "bid": d.get("b"), "bidQty": d.get("B"),
                            "ask": d.get("a"), "askQty": d.get("A"),
                        })
        except Exception:
            await asyncio.sleep(2)
    return rows


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 540.0
    prefix = sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures/long_tape"
    results = await asyncio.gather(
        *(capture_one(url, seconds) for url in STREAMS.values())
    )
    for (venue, _), rows in zip(STREAMS.items(), results):
        path = f"{prefix}_{venue}_BTCUSDT.json"
        json.dump(rows, open(path, "w"))
        print(f"{venue}: {len(rows)} msgs -> {path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
