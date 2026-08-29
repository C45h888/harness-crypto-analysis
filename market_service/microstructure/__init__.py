"""Deterministic microstructure measurement and isolated live capture.

This package is intentionally outside the existing five-second REST poller.
It owns Binance depth-event reconstruction and the paper-derived OFI/average
depth measurements; it does not fit models, interpret regimes, or trade.
"""

from .contracts import BestQuoteState, DepthDelta, OFIInterval, OrderBookEvent
from .ofi import OFIAggregator, event_contribution
from .orderbook import BookGapError, OrderBookReconstructor

__all__ = [
    "BestQuoteState",
    "BookGapError",
    "DepthDelta",
    "OFIAggregator",
    "OFIInterval",
    "OrderBookEvent",
    "OrderBookReconstructor",
    "event_contribution",
]
