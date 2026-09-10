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
        # Depth speed: 100ms is the canonical exact-feed stream, but
        # @1000ms is available and gives the bootstrap 10x more headroom.
        # Default stays 100ms (exact-feed contract); opt into 1000ms via env.
        raw_speed = (os.getenv("MICROSTRUCTURE_DEPTH_SPEED_MS") or "100").strip().lower()
        try:
            depth_speed_ms = int(raw_speed)
        except ValueError:
            depth_speed_ms = 100
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
            depth_speed_ms=depth_speed_ms if depth_speed_ms in (100, 1000) else 100,
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
        # (100ms = canonical exact-feed; 1000ms = 10x bootstrap headroom,
        # may reduce exact-feed quality). The default stays 100ms; switching
        # to 1000ms is a deployment-level trade-off, not an autodetect.
        stream = f"{self.settings.symbol.lower()}@depth@{self.settings.depth_speed_ms}ms"
        url = f"{self.settings.websocket_base}/{stream}"
        async with session.ws_connect(url, heartbeat=20, receive_timeout=60) as ws:
            await self._status("connected")
            buffered: list[DepthDelta] = []
            while not buffered:
                delta = await self._receive_delta(ws)
                if delta is not None:
                    buffered.append(delta)
            # Drain the WS into a side channel WHILE bootstrap is in
            # flight. The snapshot REST call takes ~200ms; at 100ms stream
            # cadence that means 2–3 deltas land in aiohttp's receive
            # buffer during bootstrap. Without draining them, the next
            # ``async for message in ws`` picks them up out-of-order
            # (sequence gap from the snapshot) and raises BookGapError.
            # Use a stop_event (not cancel) so the drain task finishes
            # any in-flight message before exiting — cancelling mid-put
            # would lose the message.
            drain_queue: asyncio.Queue[DepthDelta | None] = asyncio.Queue()
            drain_stop = asyncio.Event()
            drain_task = asyncio.create_task(
                self._drain_ws_into_queue(ws, drain_queue, drain_stop),
                name="bootstrap-drain",
            )
            try:
                await self._bootstrap(buffered, drain_queue)
            finally:
                drain_stop.set()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass
            # Pull whatever the drain task accumulated; these are deltas
            # that arrived during bootstrap and must be applied to the
            # live book BEFORE we resume the main loop (otherwise the
            # main loop picks them up with a gap).
            post_bootstrap: list[DepthDelta] = []
            while True:
                try:
                    delta = drain_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if delta is not None:
                    post_bootstrap.append(delta)
            for delta in post_bootstrap:
                await self._apply_and_publish(delta)
                self._record_success()
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
                    self._record_success()

    async def _drain_ws_into_queue(
        self, ws: aiohttp.ClientWebSocketResponse,
        queue: asyncio.Queue[DepthDelta | None],
        stop_event: asyncio.Event,
    ) -> None:
        """Background pump: push WS deltas into ``queue`` until ``stop_event`` set.

        Used during bootstrap to capture deltas that arrive while the
        snapshot REST call is in flight. Honours ``stop_event`` between
        iterations so an in-flight ``ws.receive`` completes its message
        and the delta lands on the queue BEFORE the task exits — the
        caller drains the queue after the task returns. Cancelling
        mid-message would lose the delta and create the exact sequence
        gap this whole path exists to prevent.
        """
        pumped = 0
        try:
            while not stop_event.is_set():
                msg = await ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    delta = self._decode_delta(msg.data)
                    if delta is not None:
                        await queue.put(delta)
                        pumped += 1
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return
                # PING/PONG etc. → continue without queueing.
        except asyncio.CancelledError:
            return
        except Exception:
            return
        finally:
            if pumped:
                log.info(
                    "drain %s: pumped=%d remaining_queue=%d",
                    self.settings.symbol, pumped, queue.qsize(),
                )

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

    async def _bootstrap(
        self,
        buffered: list[DepthDelta],
        drain_queue: asyncio.Queue[DepthDelta | None] | None = None,
    ) -> None:
        """Apply Binance's snapshot/diff bridging rule, draining the buffer.

        The snapshot/diff protocol guarantees: every buffered delta that
        arrives AFTER the snapshot is applied in arrival order; the orderbook
        itself validates continuity (``first_update_id > last_update_id + 1``
        → BookGapError). This means a snapshot whose lastUpdateId is older
        than the head buffered delta does NOT require a single delta to
        "bridge" ``snapshot_id + 1`` — any gap will surface naturally as we
        try to apply.

        ``drain_queue`` (optional) carries deltas that the run-loop's
        background pump is pushing in while the snapshot REST call is in
        flight. The bootstrap pulls them in arrival order after each
        attempt; if any breaks continuity, the attempt fails and the
        remaining deltas are kept for the next snapshot.

        Behaviour per attempt:

          1. Fetch snapshot; if ``snapshot_id < buffered[0].first_update_id``
             the snapshot is too old, retry.
          2. Bootstrap the local book from the snapshot.
          3. Apply every buffered delta in arrival order (and any
             drain-queue deltas already accumulated). If any delta
             breaks the sequence (gap), the orderbook raises BookGapError;
             we abandon this attempt, take a fresh snapshot, retry.
          4. If all deltas apply cleanly, the book is live; any deltas
             older than the snapshot are discarded (their updates are
             already reflected in the snapshot state).

        This collapses the previous "single-delta bridge" heuristic which
        forced a retry whenever the snapshot landed between buffered
        update ranges — a near-certainty on SOL-USDT perps at 100ms.
        """
        first = buffered[0]
        attempts = max(1, self.settings.bootstrap_retries)
        async with Binance() as client:
            for attempt in range(attempts):
                t0 = time.monotonic()
                if self.settings.venue == "futures":
                    snapshot = await client.fut_book(self.settings.symbol, limit=self.settings.snapshot_levels)
                else:
                    snapshot = await client.spot_book(self.settings.symbol, limit=self.settings.snapshot_levels)
                bootstrap_fetch_ms = int((time.monotonic() - t0) * 1000)
                snapshot_id = int(snapshot["lastUpdateId"])
                queued_at_arrival = len(buffered)
                # Drain any background-queued deltas that landed during
                # the snapshot fetch. These are the source of the chronic
                # BookGapError loop at high stream velocity.
                drained: list[DepthDelta] = []
                if drain_queue is not None:
                    while True:
                        try:
                            d = drain_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if d is not None:
                            drained.append(d)
                # Orderbook bootstrap from snapshot.
                if snapshot_id < first.first_update_id:
                    log.info(
                        "bootstrap %s attempt %d: snapshot_id=%d < buffered[0].first=%d, "
                        "fetch_ms=%d queued=%d drained=%d — retrying",
                        self.settings.symbol, attempt + 1,
                        snapshot_id, first.first_update_id,
                        bootstrap_fetch_ms, queued_at_arrival, len(drained),
                    )
                    buffered.extend(drained)
                    continue
                self._book = OrderBookReconstructor(self.settings.symbol, self.settings.venue)
                quote = self._book.bootstrap(snapshot, received_ts_ms=int(time.time() * 1000))
                applied = 0
                last_applied_id = snapshot_id
                sequence_failed = False
                try:
                    # Apply pre-bootstrap buffered deltas first.
                    for d in buffered:
                        if d.final_update_id <= snapshot_id:
                            continue
                        self._book.apply(d)
                        applied += 1
                        last_applied_id = d.final_update_id
                    # Then apply the deltas that landed during the snapshot
                    # fetch. If any one breaks the sequence, BookGapError
                    # surfaces naturally — no silent skip.
                    for d in drained:
                        if d.final_update_id <= last_applied_id:
                            continue
                        self._book.apply(d)
                        applied += 1
                        last_applied_id = d.final_update_id
                except BookGapError:
                    sequence_failed = True
                if sequence_failed:
                    head_velocity_ids_per_sec = (
                        max(0, last_applied_id - snapshot_id)
                        / max(0.001, time.monotonic() - t0)
                    )
                    log.info(
                        "bootstrap %s attempt %d: BookGapError after %d/%d buffered, "
                        "fetch_ms=%d queued=%d drained=%d applied=%d head_velocity≈%.0f id/s",
                        self.settings.symbol, attempt + 1,
                        applied, len(buffered) + len(drained), bootstrap_fetch_ms,
                        queued_at_arrival, len(drained), applied, head_velocity_ids_per_sec,
                    )
                    self._book = None
                    # Drop everything we successfully applied; keep what's
                    # left (the gap tail) so the next snapshot can bridge.
                    kept = buffered[applied:] + drained
                    buffered[:] = [d for d in kept if d.final_update_id > snapshot_id]
                    continue
                # All buffered + drained deltas applied cleanly.
                if self._book.last_quote is not None:
                    await self.store.set_microstructure_book(
                        self.settings.venue, self.settings.symbol,
                        self._book.last_quote.to_dict(),
                    )
                head_velocity_ids_per_sec = (
                    max(0, last_applied_id - snapshot_id)
                    / max(0.001, time.monotonic() - t0)
                )
                drained_id_range = (
                    f"{drained[0].first_update_id}..{drained[-1].final_update_id}"
                    if drained else "n/a"
                )
                log.info(
                    "bootstrap %s succeeded attempt %d: fetch_ms=%d queued=%d "
                    "drained=%d (ids=%s) applied=%d head_velocity≈%.0f id/s "
                    "snapshot_id=%d book_id=%d",
                    self.settings.symbol, attempt + 1,
                    bootstrap_fetch_ms, queued_at_arrival, len(drained),
                    drained_id_range, applied, head_velocity_ids_per_sec,
                    snapshot_id, self._book.last_update_id,
                )
                # Anything older than the snapshot is now redundant; the
                # caller will re-publish what remains in ``buffered`` (deltas
                # newer than the snapshot that we already applied to the
                # book here). The drain-queue deltas the caller didn't yet
                # pull (none in the normal path — bootstrap drains all) go
                # back through the caller's queue-pump below.
                buffered[:] = [d for d in buffered if d.final_update_id > snapshot_id]
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
