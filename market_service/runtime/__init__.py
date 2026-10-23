"""Shared runtime contracts and infrastructure adapters."""

from .contracts import MARKET_RUN_SCHEMA_VERSION

# The NOOA package is intentionally not imported here. The canonical runtime
# must remain usable without the optional analyst dependency installed.
# MarketStateEnvelope (and its ``market_state`` schema version) was removed
# with the domain-state plane: nothing published or read it.

__all__ = [
    "MARKET_RUN_SCHEMA_VERSION",
]
