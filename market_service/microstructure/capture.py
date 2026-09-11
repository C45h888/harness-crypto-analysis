"""Isolated Binance depth-event capture for the microstructure ledger.

Primary venue is now **futures (SOL-USDT perps)** via fstream WS — spot is
legacy. This process never imports or changes ``market_service.poller``. It
consumes public depth diffs, bootstraps a local book from REST, and emits
raw deltas plus validated best-quote transitions to the dedicated Redis
namespace. Venue is configurable via MICROSTRUCTURE_VENUE (spot|futures|perps).
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

from market_service.clients import BinanceWebSocket, DepthSnapshot
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
    websocket_base: str = "wss://fstream.binance.com/ws"
    depth_speed_ms: int = 100
    bootstrap_retries: int = 3

    @classmethod
    def from_env(cls, symbol: str, venue: str | None = None) -> MicrostructureSettings:
        # Venue: MICROSTRUCTURE_VENUE env wins, else arg, default now futures (perps) per user request.
        # Spot is legacy; futures/perps is primary for SOL-USDT.
        raw_venue = (os.getenv("MICROSTRUCTURE_VENUE") or venue or "futures").lower().strip()
        # Normalize perps aliases
        if raw_venue in ("perps", "perp", "usdm"):
            raw_venue = "futures"
        if raw_venue not in ("spot", "futures"):
            raise ValueError(f"unsupported venue {raw_venue!r} — use spot or futures")
        # WS base per venue
        if raw_venue == "futures":
            default_ws = "wss://fstream.binance.com/ws"
            env_key = "BINANCE_FUTURES_WS_BASE"
        else:
            default_ws = "wss://stream.binance.com:9443/ws"
            env_key = "BINANCE_SPOT_WS_BASE"
        # Snapshot levels — the bootstrap only needs the head of the book to
        # satisfy the snapshot/diff bridging rule; 1000-level fetches are
        # structurally too slow on high-velocity perps (the snapshot arrives
        # after the head has moved thousands of update IDs, which guarantees
        # a BookGapError on the next live delta). 100 for spot, 200 for
        # futures perps — enough to bridge, fast enough to outrun the
        # stream. Operators can still override via env.
        default_snapshot = 200 if raw_venue == "futures" else 100
        # Depth speed: futures exposes @100ms (exact-feed) and @500ms;
        # @1000ms exists ONLY on spot — futures @1000ms subscribes but Binance
        # never sends a frame (verified live: 15s of silence). Default futures
        # goes to @500ms = 2x bootstrap headroom over the exact-feed with
        # still-fresh best-quote events; opt into 100ms via env. Spot keeps
        # @100ms canonical with @1000ms as its own opt-in.
        raw_speed = (os.getenv("MICROSTRUCTURE_DEPTH_SPEED_MS") or "").strip().lower()
        try:
            depth_speed_ms = int(raw_speed) if raw_speed else 0
        except ValueError:
            depth_speed_ms = 0
        if depth_speed_ms <= 0:
            depth_speed_ms = 500 if raw_venue == "futures" else 100
        valid_speeds = (100, 500) if raw_venue == "futures" else (100, 1000)
        if depth_speed_ms not in valid_speeds:
            log.warning(
                "invalid MICROSTRUCTURE_DEPTH_SPEED_MS=%r for venue=%s — "
                "valid=%s, defaulting to %d",
                depth_speed_ms, raw_venue, valid_speeds,
                500 if raw_venue == "futures" else 100,
            )
            depth_speed_ms = 500 if raw_venue == "futures" else 100
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            redis_prefix=os.getenv("REDIS_KEY_PREFIX", "marketflow"),
            symbol=symbol.upper(),
            venue=raw_venue,
            stream_maxlen=_positive_env("MICROSTRUCTURE_STREAM_MAXLEN", 100_000),
            snapshot_levels=_positive_env("MICROSTRUCTURE_SNAPSHOT_LEVELS", default_snapshot),
            reconnect_seconds=_positive_env("MICROSTRUCTURE_RECONNECT_SECONDS", 2),
            interval_seconds=_positive_env("MICROSTRUCTURE_INTERVAL_SECONDS", 10),
            websocket_base=os.getenv(env_key, default_ws).rstrip("/"),
            depth_speed_ms=depth_speed_ms,
            bootstrap_retries=_positive_env("MICROSTRUCTURE_BOOTSTRAP_RETRIES", 3),
        )


class BinanceSpotDepthCapture:
    """One-symbol capture loop with explicit bootstrap and gap recovery."""

    # Backoff bounds (ms). A healthy capture never reconnects — when it
    # does, the backoff escalates with each gap and halves after a stretch
    # of clean deltas, so transient blips cost ~0.5s and chronic issues
    # widen the gap cycle proportionally (capped at 5s).
    _BACKOFF_MIN_MS = 500
    _BACKOFF_MAX_MS = 5_000
    _BACKOFF_SUCCESSES_TO_RESET = 100

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
        self._last_written_status_state: str | None = None
        # Gap-adaptive backoff state (Fix A(iii)). Starts at the floor;
        # doubles per gap, halves per success batch.
        self._backoff_ms = self._BACKOFF_MIN_MS
        self._successes_since_gap = 0

    async def close(self) -> None:
        await self.store.close()

    def _record_success(self) -> None:
        """One clean delta: nudge the backoff down toward the floor."""
        self._successes_since_gap += 1
        if (
            self._successes_since_gap >= self._BACKOFF_SUCCESSES_TO_RESET
            and self._backoff_ms > self._BACKOFF_MIN_MS
        ):
            self._backoff_ms = max(self._BACKOFF_MIN_MS, self._backoff_ms // 2)
            self._successes_since_gap = 0

    def _record_gap(self) -> int:
        """One gap: double the backoff up to the cap. Returns new value."""
        self._backoff_ms = min(self._BACKOFF_MAX_MS, max(self._BACKOFF_MIN_MS, self._backoff_ms * 2))
        self._successes_since_gap = 0
        return self._backoff_ms

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
                        backoff_ms = self._record_gap()
                        log.warning(
                            "microstructure capture %s reconnecting after %s: %s "
                            "(backoff=%dms reconnects=%d)",
                            self.settings.symbol, type(exc).__name__, exc,
                            backoff_ms, self.reconnects,
                        )
                        await self._status("reconnecting", error=f"{type(exc).__name__}: {exc}")
                        await asyncio.sleep(backoff_ms / 1000.0)
        finally:
            await self._status("stopped")
            await self.close()

    async def _run_connection(self, session: aiohttp.ClientSession) -> None:
        # Depth stream speed is configurable via MICROSTRUCTURE_DEPTH_SPEED_MS
        # (futures: 100ms exact / 500ms stable; spot: 100ms / 1000ms).
        stream = f"{self.settings.symbol.lower()}@depth@{self.settings.depth_speed_ms}ms"
        url = f"{self.settings.websocket_base}/{stream}"
        async with session.ws_connect(url, heartbeat=20, receive_timeout=60) as ws:
            await self._status("connected")
            # Single-receiver bootstrap. The previous design ran a concurrent
            # ``_drain_ws_into_queue`` task against the SAME websocket while
            # the main loop awaited on it — aiohttp does not support two
            # concurrent receivers, and on SOL-USDT perps at 100ms cadence
            # that race silently dropped deltas, guaranteeing the
            # ``first_update_id > last_update_id + 1`` BookGapError on every
            # reconnect → the stuck "waiting" cycle seen in production.
            # Now exactly ONE task reads the socket: first the bootstrap
            # (snapshot-first with the canonical straddle rule), then this
            # main loop. No side channel, no queue, no drops.
            await self._bootstrap(ws)
            await self._status("running")
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise ConnectionError("Binance depth websocket closed")
                    continue
                delta = self._decode_delta(message.data)
                if delta is not None:
                    await self._apply_and_publish(delta)
                    self._record_success()

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

    async def _bootstrap(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Snapshot-first, single-receiver sync with the canonical straddle rule.

        The socket is ALREADY connected and THIS method + the main loop are
        the ONLY two receivers over its lifetime — never two at once (the
        previous concurrent drain task was the root cause of the perpetual
        BookGapError / reconnecting loop).

        Sync protocol (canonical Binance diff-depth):

          1. Fetch the REST snapshot FIRST. Deltas that arrive while the
             snapshot request is in flight are held in arrival order inside
             aiohttp's single websocket reader — nothing races them away.
          2. Read deltas from the socket. Skip anything fully inside the
             snapshot (``final_update_id <= snapshot_id`` — already
             reflected in the snapshot state, kept only in the raw ledger).
          3. The first delta whose range extends past the snapshot id is the
             BRIDGE — Binance guarantees contiguity, so it must straddle
             ``snapshot_id + 1`` (``first_update_id <= snapshot_id + 1``).
             Apply it as the bridge; the book is now live. If instead it
             starts past the bridge point (``first > snapshot_id + 1``) the
             stream raced ahead of our snapshot — the bridge was lost and we
             retry with a FRESH snapshot.

        Retries are bounded by ``bootstrap_retries``; exhausting them raises
        BookGapError (which the reconnect loop handles).
        """
        attempts = max(1, self.settings.bootstrap_retries)
        # WS-owned client: this is the ONLY path the capture uses to touch
        # Binance (frame stream + REST snapshot cut). It does not reuse the
        # poller/analysis REST client — that has point-in-time-slice semantics
        # and must NOT be the authority for a WS tape bootstrap.
        ws_client = BinanceWebSocket(venue=self.settings.venue, depth_speed_ms=self.settings.depth_speed_ms)
        for attempt in range(attempts):
            t0 = time.monotonic()
            snapshot: DepthSnapshot = await ws_client.fetch_snapshot(
                self.settings.symbol, levels=self.settings.snapshot_levels,
            )
            bootstrap_fetch_ms = int((time.monotonic() - t0) * 1000)
            snapshot_id = snapshot.update_id
            # Reset (or first-build) the live book from the REST cut.
            self._book = OrderBookReconstructor(self.settings.symbol, self.settings.venue)
            self._book.bootstrap(
                {"lastUpdateId": snapshot.update_id, "bids": snapshot.bids, "asks": snapshot.asks},
                received_ts_ms=int(time.time() * 1000),
            )
            bridge_lost = False
            while True:
                delta = await self._receive_delta(ws)
                if delta is None:
                    continue
                if delta.final_update_id <= snapshot_id:
                    # Already inside the snapshot; keep raw evidence only.
                    await self.store.publish_microstructure_delta(
                        self.settings.venue, self.settings.symbol,
                        delta.to_dict(), maxlen=self.settings.stream_maxlen,
                    )
                    continue
                if self.settings.venue == "futures":
                    # Futures frames are non-contiguous in ID space; the
                    # bridge is the first frame that STRADDLES the snapshot:
                    # its ``pu`` (prev frame's final u) is at-or-before the
                    # snapshot AND its own range extends at-or-past it
                    # (pu <= S <= u). The frame's absolute-quantity levels
                    # are the complete state as of ``u`` — applying it
                    # makes the local book live. A frame whose pu <= S but
                    # whose OWN u <= S is STILL fully inside the snapshot
                    # (its levels predate the snapshot state) and must be
                    # skipped like a spot inside-frame, NOT bridged.
                    pu = delta.previous_update_id
                    if pu is not None and delta.final_update_id <= snapshot_id:
                        # Actually fully inside snapshot; keep raw only.
                        await self.store.publish_microstructure_delta(
                            self.settings.venue, self.settings.symbol,
                            delta.to_dict(), maxlen=self.settings.stream_maxlen,
                        )
                        continue
                    bridge_ok = (
                        pu is not None
                        and pu <= snapshot_id
                        and delta.final_update_id > snapshot_id
                    )
                    if not bridge_ok:
                        # pu > S: the stream head raced past the snapshot.
                        bridge_lost = True
                        break
                else:
                    # Spot: the bridge must straddle snapshot_id + 1.
                    if delta.first_update_id > snapshot_id + 1:
                        bridge_lost = True
                        break
                    bridge_ok = True
                # Bridge: the straddling delta. Applying it makes the
                # local book continuous with the live stream.
                log.info(
                    "bootstrap %s bridge: snapshot_id=%d delta U=%d u=%d pu=%s",
                    self.settings.symbol, snapshot_id,
                    delta.first_update_id, delta.final_update_id,
                    delta.previous_update_id,
                )
                await self._apply_and_publish(delta, bridging=True)
                self._record_success()
                head_velocity_ids_per_sec = (
                    max(0, self._book.last_update_id - snapshot_id)
                    / max(0.001, time.monotonic() - t0)
                )
                log.info(
                    "bootstrap %s bridged at attempt %d: snapshot_id=%d "
                    "book_id=%d fetch_ms=%d head_velocity≈%.0f id/s",
                    self.settings.symbol, attempt + 1, snapshot_id,
                    self._book.last_update_id, bootstrap_fetch_ms,
                    head_velocity_ids_per_sec,
                )
                return
            if bridge_lost:
                self._book = None
                log.info(
                    "bootstrap %s attempt %d: bridge lost (snapshot_id=%d, "
                    "next delta started past it) fetch_ms=%d — retrying",
                    self.settings.symbol, attempt + 1, snapshot_id,
                    bootstrap_fetch_ms,
                )
                continue
        raise BookGapError("could not bootstrap local order book")

    async def _apply_and_publish(
        self, delta: DepthDelta, *, bridging: bool = False,
    ) -> None:
        """Apply a frame to the live book, then publish all evidence.

        ``bridging`` is the bootstrap splice: the straddle frame starts
        before the REST cut (U < S) — a legitimate rewind, not an
        out-of-order arrival. Only the bootstrap path may set it.
        """
        await self.store.publish_microstructure_delta(
            self.settings.venue, self.settings.symbol, delta.to_dict(), maxlen=self.settings.stream_maxlen,
        )
        if self._book is None:
            return
        try:
            event = self._book.apply(delta, bridging=bridging)
        except BookGapError:
            self.gaps += 1
            await self._status("gap", error="depth sequence gap")
            raise
        # BookGapError doesn't fire → clean delta: feed the backoff
        # success counter so the gap-adaptive backoff can come back down.
        self._record_success()
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
        # Event-driven status ledger: append ONE entry only on a state
        # transition (running -> gap -> reconnecting -> running ...). The
        # wake worker blocks on this stream to fire capture-recovery wakes
        # WITHOUT polling — the entry time is carried in the stream id, and
        # the payload carries the transition itself.
        if self._last_written_status_state != state:
            prior = self._last_written_status_state
            self._last_written_status_state = state
            transition_payload = dict(payload)
            transition_payload["from_state"] = prior
            transition_payload["to_state"] = state
            try:
                await self.store.publish_microstructure_status_transition(
                    self.settings.venue, self.settings.symbol, transition_payload,
                    maxlen=self.settings.stream_maxlen,
                )
            except Exception:
                # A failed transition append must NEVER break the capture
                # loop or its status bookkeeping. The latest-key status is
                # already written; the transition ledger is best-effort.
                pass


async def main_async(symbol: str, venue: str | None = None) -> int:
    settings = MicrostructureSettings.from_env(symbol, venue=venue)
    capture = BinanceSpotDepthCapture(settings)
    log.info("capture venue=%s symbol=%s ws=%s", settings.venue, settings.symbol, settings.websocket_base)
    await capture.run_forever()
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Isolated Binance microstructure capture (spot or futures perps)")
    parser.add_argument("symbol", nargs="?", default="SOLUSDT")
    parser.add_argument("--venue", default=None, help="spot or futures (defaults to MICROSTRUCTURE_VENUE or futures)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(main_async(args.symbol, venue=args.venue))


if __name__ == "__main__":
    raise SystemExit(main())
