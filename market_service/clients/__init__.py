"""External live-data clients.

- :mod:`.binance` — REST-only point-in-time slice client (poller/analysis).
- :mod:`.websocket` — WS-owned depth tape surface (+ the REST cut type used
  to seed the local book). The two never overlap: REST has no frames, WS
  has no contiguity.
"""

from .binance import Binance, normalize_fut_trade, normalize_spot_trade
from .websocket import BinanceWebSocket, DepthFrame, DepthSnapshot

__all__ = [
    "Binance",
    "BinanceWebSocket",
    "DepthFrame",
    "DepthSnapshot",
    "normalize_fut_trade",
    "normalize_spot_trade",
]
