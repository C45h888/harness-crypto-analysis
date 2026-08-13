"""Shared runtime contracts and infrastructure adapters."""

from .contracts import (
    MARKET_STATE_SCHEMA_VERSION,
    MARKET_RUN_SCHEMA_VERSION,
    MarketEvent,
    MarketRunEnvelope,
    MarketStateEnvelope,
    RefreshCommand,
)

# The NOOA package is intentionally not imported here. The canonical runtime
# must remain usable without the optional analyst dependency installed.

__all__ = [
    "MARKET_STATE_SCHEMA_VERSION",
    "MARKET_RUN_SCHEMA_VERSION",
    "MarketEvent",
    "MarketRunEnvelope",
    "MarketStateEnvelope",
    "RefreshCommand",
]
