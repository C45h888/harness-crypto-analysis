"""
WS-owned Binance depth surface — the ordered event tape.

SEMANTIC JURISDICTION (file-level boundary):
- This module OWNS the Binance depth *tape*: the diff-depth websocket, its
  frame shape (``U/u/pu``), and the typed REST-cut that seeds the local book.
- It is the ONLY module that knows how a REST snapshot (a cut) splices into
  the WS tape. Nothing outside this file may assume WS frames are
  contiguous in update-ID space (they are NOT) or that a REST ``lastUpdateId``
  is a tape cursor (it is a cut).
- The REST poller and analysis consume `Binance` (``binance.py``) — a
  POINT-IN-TIME slice model. That module is REST-only and never yields
  WS frames.

Rule: if a caller needs depth *events* (per-frame diffs), it must go through
this WS module. If it needs a *snapshot* (a cut every N seconds), it stays on
Binance. No cross-over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

import aiohttp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WS tape types — the ONLY contracts the WS plane exposes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepthSnapshot:
    """A REST point-in-time depth cut (the bootstrap splice token).

    ``update_id`` is ``lastUpdateId`` — a *cut*: the book state is valid as
    of this id. Do NOT treat it as a tape cursor; the WS tape is
    non-contiguous by design (frames chain via ``pu``).
    """

    symbol: str
    venue: str
    update_id: int
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]

    @classmethod
    def from_rest(cls, raw: dict[str, Any], *, symbol: str, venue: str) -> "DepthSnapshot":
        return cls(
            symbol=symbol.upper(),
            venue=venue,
            update_id=int(raw["lastUpdateId"]),
            bids=tuple((str(p), str(q)) for p, q in raw.get("bids") or ()),
            asks=tuple((str(p), str(q)) for p, q in raw.get("asks") or ()),
        )


@dataclass(frozen=True)
class DepthFrame:
    """One WS depth diff frame (a tape element, NOT a contiguity unit).

    ``first_update_id``/``final_update_id`` (U/u) delimit the frame's
    internal update range; ``previous_update_id`` (pu) links this frame to
    the previous one's ``u`` — THE ordering proof. Consecutive frames are
    non-contiguous in U-space by design; only ``pu``-chaining matters.
    """

    symbol: str
    venue: str
    first_update_id: int
    final_update_id: int
    previous_update_id: int | None
    exchange_ts_ms: int
    received_ts_ms: int
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]

    @classmethod
    def from_binance(cls, payload: dict[str, Any], *, venue: str, received_ts_ms: int) -> "DepthFrame":
        return cls(
            symbol=str(payload["s"]).upper(),
            venue=venue,
            first_update_id=int(payload["U"]),
            final_update_id=int(payload["u"]),
            previous_update_id=int(payload["pu"]) if payload.get("pu") is not None else None,
            exchange_ts_ms=int(payload.get("E") or 0),
            received_ts_ms=received_ts_ms,
            bids=tuple((str(p), str(q)) for p, q in payload.get("b") or ()),
            asks=tuple((str(p), str(q)) for p, q in payload.get("a") or ()),
        )


# ---------------------------------------------------------------------------
# WS-owned client — yields typed frames / typed cuts. No REST contiguity.
# ---------------------------------------------------------------------------

# Valid depth speeds per venue (Binance): futures 100/500, spot 100/1000.
_VALID_DEPTH_SPEEDS = {
    "spot": (100, 1000),
    "futures": (100, 500),
}


class BinanceWebSocket:
    """WS depth tape client — the ONLY entry to Binance's diff-depth feed.

    Latched: speeds are validated per venue (futures @1000ms does not
    exist — it subscribes but never sends a frame). Yields typed
    :class:`DepthFrame` objects; the caller owns bootstrapping via a
    :class:`DepthSnapshot` cut (see ``microstructure.capture``).
    """

    def __init__(self, venue: str = "futures", depth_speed_ms: int | None = None) -> None:
        if venue not in _VALID_DEPTH_SPEEDS:
            raise ValueError(f"unsupported venue {venue!r} — use spot or futures")
        self.venue = venue
        speeds = _VALID_DEPTH_SPEEDS[venue]
        if depth_speed_ms is None:
            depth_speed_ms = 500 if venue == "futures" else 100
        if depth_speed_ms not in speeds:
            raise ValueError(
                f"invalid depth speed {depth_speed_ms}ms for {venue} — "
                f"valid: {', '.join(map(str, speeds))}ms"
            )
        self.depth_speed_ms = depth_speed_ms
        default_ws = {
            "spot": "wss://stream.binance.com:9443/ws",
            "futures": "wss://fstream.binance.com/ws",
        }[venue]
        self.websocket_base = default_ws

    @property
    def stream_name(self) -> str:
        return ""  # stream is symbol-specific; see depth_stream()

    async def depth_stream(
        self, symbol: str, *, heartbeat: int = 20, receive_timeout: float = 60,
    ) -> AsyncIterator[DepthFrame]:
        """Yield typed depth frames in arrival order (tape semantics)."""
        import time
        stream = f"{symbol.lower()}@depth@{self.depth_speed_ms}ms"
        url = f"{self.websocket_base}/{stream}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, heartbeat=heartbeat, receive_timeout=receive_timeout) as ws:
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("Binance depth websocket closed")
                        continue
                    payload = message.json()
                    if not isinstance(payload, dict) or payload.get("e") != "depthUpdate":
                        continue
                    yield DepthFrame.from_binance(
                        payload, venue=self.venue,
                        received_ts_ms=int(time.time() * 1000),
                    )

    async def fetch_snapshot(self, symbol: str, levels: int = 200) -> DepthSnapshot:
        """Fetch a REST depth cut via the WS-owned wire path (never Binance).

        THIS module owns its own REST call for the bootstrap splice token —
        it must not reach through the REST client (``clients.binance``),
        which is the poller/analysis surface. The result is immediately
        boxed into :class:`DepthSnapshot` here; no bare dict crosses the
        WS layer.
        """
        import aiohttp as _aiohttp

        from market_service.rate_limit import RateLimitSubstrate, SurfaceId

        base = "https://fapi.binance.com" if self.venue == "futures" else "https://api.binance.com"
        surface = SurfaceId.FUTURES if self.venue == "futures" else SurfaceId.SPOT
        substrate = RateLimitSubstrate.from_env()
        await substrate.acquire(surface, 10)  # depth ≤1000 weight is 10
        async with _aiohttp.ClientSession() as session:
            async with session.get(
                f"{base}/fapi/v1/depth" if self.venue == "futures" else f"{base}/api/v3/depth",
                params={"symbol": symbol, "limit": levels},
            ) as resp:
                substrate.record(surface, resp.headers)
                resp.raise_for_status()
                raw = await resp.json(content_type=None)
        return DepthSnapshot.from_rest(raw, symbol=symbol, venue=self.venue)