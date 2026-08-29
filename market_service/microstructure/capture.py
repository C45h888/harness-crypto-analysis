"""Isolated Binance Spot depth-event capture for the microstructure ledger.

This process never imports or changes ``market_service.poller``. It consumes
public depth diffs, bootstraps a local book from REST, and emits raw deltas plus
validated best-quote transitions to the dedicated Redis namespace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from market_service.clients.binance import Binance
from market_service.microstructure.contracts import DepthDelta
from market_service.microstructure.ofi import OFIAggregator
from market_service.microstructure.orderbook import BookGapError, OrderBookReconstructor
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


def _positive_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class MicrostructureSettings:
    redis_url: str
    redis_prefix: str
    symbol: str
    venue: str = "spot"
    stream_maxlen: int = 100_000
    snapshot_levels: int = 1_000
    reconnect_seconds: int = 2
    interval_seconds: int = 10
    websocket_base: str = "wss://stream.binance.com:9443/ws"

    @classmethod
    def from_env(cls, symbol: str, venue: str = "spot") -> MicrostructureSettings:
        if venue.lower() != "spot":
            raise ValueError("Pass 2 capture supports Binance spot only")
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            redis_prefix=os.getenv("REDIS_KEY_PREFIX", "marketflow"),
            symbol=symbol.upper(),
            venue="spot",
            stream_maxlen=_positive_env("MICROSTRUCTURE_STREAM_MAXLEN", 100_000),
            snapshot_levels=_positive_env("MICROSTRUCTURE_SNAPSHOT_LEVELS", 1_000),
            reconnect_seconds=_positive_env("MICROSTRUCTURE_RECONNECT_SECONDS", 2),
            interval_seconds=_positive_env("MICROSTRUCTURE_INTERVAL_SECONDS", 10),
            websocket_base=os.getenv("BINANCE_SPOT_WS_BASE", "wss://stream.binance.com:9443/ws").rstrip("/"),
        )


class BinanceSpotDepthCapture:
    """One-symbol capture loop with explicit bootstrap and gap recovery."""

    def __init__(self, settings: MicrostructureSettings):
        self.settings = settings
        self.store = RedisRuntimeStore(settings.redis_url, settings.redis_prefix)
        self.messages = 0
        self.events = 0
        self.gaps = 0
        self.reconnects = 0
        self.intervals = 0
        self._book: OrderBookReconstructor | None = None
        self._aggregator = OFIAggregator(settings.interval_seconds * 1_000)

    async def close(self) -> None:
        await self.store.close()

    async def run_forever(self) -> None:
        await self._status("starting")
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    try:
                        await self._run_connection(session)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — capture must survive any transport fault and reconnect
                        self.reconnects += 1
                        self._book = None
                        self._aggregator.discard()
                        log.warning("microstructure capture %s reconnecting after %s: %s",
                                    self.settings.symbol, type(exc).__name__, exc)
                        await self._status("reconnecting", error=f"{type(exc).__name__}: {exc}")
                        await asyncio.sleep(self.settings.reconnect_seconds)
        finally:
            await self._status("stopped")
            await self.close()

    async def _run_connection(self, session: aiohttp.ClientSession) -> None:
        stream = f"{self.settings.symbol.lower()}@depth@100ms"
        url = f"{self.settings.websocket_base}/{stream}"
        async with session.ws_connect(url, heartbeat=20, receive_timeout=60) as ws:
            await self._status("connected")
            buffered: list[DepthDelta] = []
            while not buffered:
                delta = await self._receive_delta(ws)
                if delta is not None:
                    buffered.append(delta)
            await self._bootstrap(buffered)
            for delta in buffered:
                await self._apply_and_publish(delta)
            await self._status("running")
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise ConnectionError("Binance depth websocket closed")
                    continue
                delta = self._decode_delta(message.data)
                if delta is not None:
                    await self._apply_and_publish(delta)

    async def _receive_delta(self, ws: aiohttp.ClientWebSocketResponse) -> DepthDelta | None:
        message = await ws.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError("Binance depth websocket did not yield text")
        return self._decode_delta(message.data)

    def _decode_delta(self, raw: str) -> DepthDelta | None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("e") != "depthUpdate":
            return None
        received = int(time.time() * 1000)
        delta = DepthDelta.from_binance(payload, venue=self.settings.venue, received_ts_ms=received)
        self.messages += 1
        return delta

    async def _bootstrap(self, buffered: list[DepthDelta]) -> None:
        """Apply Binance's snapshot/diff bridging rule before publishing events."""
        first = buffered[0]
        async with Binance() as client:
            for _ in range(3):
                snapshot = await client.spot_book(self.settings.symbol, limit=self.settings.snapshot_levels)
                snapshot_id = int(snapshot["lastUpdateId"])
                if snapshot_id < first.first_update_id:
                    continue
                usable = [d for d in buffered if d.final_update_id > snapshot_id]
                if usable:
                    bridge = next(
                        (d for d in usable if d.first_update_id <= snapshot_id + 1 <= d.final_update_id),
                        None,
                    )
                    if bridge is None:
                        # The snapshot landed between buffered update ranges; obtain
                        # a newer snapshot rather than inventing continuity.
                        continue
                self._book = OrderBookReconstructor(self.settings.symbol, self.settings.venue)
                quote = self._book.bootstrap(snapshot, received_ts_ms=int(time.time() * 1000))
                await self.store.set_microstructure_book(self.settings.venue, self.settings.symbol, quote.to_dict())
                buffered[:] = usable
                return
        raise BookGapError("could not bootstrap local order book")

    async def _apply_and_publish(self, delta: DepthDelta) -> None:
        await self.store.publish_microstructure_delta(
            self.settings.venue, self.settings.symbol, delta.to_dict(), maxlen=self.settings.stream_maxlen,
        )
        if self._book is None:
            return
        try:
            event = self._book.apply(delta)
        except BookGapError:
            self.gaps += 1
            await self._status("gap", error="depth sequence gap")
            raise
        quote = self._book.last_quote
        if quote is not None:
            await self.store.set_microstructure_book(self.settings.venue, self.settings.symbol, quote.to_dict())
        if event is not None:
            self.events += 1
            await self.store.publish_microstructure_event(
                self.settings.venue, self.settings.symbol, event.to_dict(), maxlen=self.settings.stream_maxlen,
            )
            for interval in self._aggregator.add(event):
                self.intervals += 1
                await self.store.publish_microstructure_interval(
                    self.settings.venue, self.settings.symbol, interval.to_dict(), maxlen=self.settings.stream_maxlen,
                )
        if self.messages % 100 == 0:
            await self._status("running")

    async def _status(self, state: str, *, error: str | None = None) -> None:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "symbol": self.settings.symbol,
            "venue": self.settings.venue,
            "state": state,
            "updated_at_ms": int(time.time() * 1000),
            "messages": self.messages,
            "best_quote_events": self.events,
            "completed_ofi_intervals": self.intervals,
            "ofi_interval_seconds": self.settings.interval_seconds,
            "sequence_gaps": self.gaps,
            "reconnects": self.reconnects,
            "last_update_id": self._book.last_update_id if self._book else None,
            "error": error,
        }
        await self.store.set_microstructure_status(self.settings.venue, self.settings.symbol, payload)


async def main_async(symbol: str) -> int:
    settings = MicrostructureSettings.from_env(symbol)
    capture = BinanceSpotDepthCapture(settings)
    await capture.run_forever()
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Isolated Binance spot microstructure capture")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(main_async(args.symbol))


if __name__ == "__main__":
    raise SystemExit(main())
