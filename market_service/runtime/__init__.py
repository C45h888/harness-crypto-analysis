"""Shared runtime contracts and infrastructure adapters."""

# The NOOA package is intentionally not imported here. The canonical runtime
# must remain usable without the optional analyst dependency installed.
# MarketStateEnvelope (and its ``market_state`` schema version) was removed
# with the domain-state plane: nothing published or read it.
# MarketRunEnvelope (and its ``market_run`` schema version) was removed
# with the collated-run plane: no writer ever existed.

__all__: list[str] = []
