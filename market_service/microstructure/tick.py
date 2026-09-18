"""Frozen per-instrument tick sizes (Track D pre-integration fix #2).

Source: Binance exchangeInfo PRICE_FILTER.tickSize, verified live.
Spot: 0.01 for BTC/ETH/SOL. Futures (USDM): 0.10 for BTC/ETH/SOL.

Frozen: adding a symbol/venue requires a table edit + test, never a
caller-supplied float. Unknown symbol/venue -> KeyError (refuse, never
guess 0.01: the old hardcoded default was 10x wrong for futures).
"""

from __future__ import annotations

from decimal import Decimal

TICK_TABLE_VERSION = "ticks-v1"

TICK_TABLE: dict[tuple[str, str], Decimal] = {
    ("BTCUSDT", "spot"): Decimal("0.01"),
    ("ETHUSDT", "spot"): Decimal("0.01"),
    ("SOLUSDT", "spot"): Decimal("0.01"),
    ("BTCUSDT", "futures"): Decimal("0.10"),
    ("ETHUSDT", "futures"): Decimal("0.10"),
    ("SOLUSDT", "futures"): Decimal("0.10"),
}


def resolve_tick_size(symbol: str, venue: str) -> Decimal:
    """Frozen tick lookup. Raises KeyError on unknown instrument."""
    key = (symbol.upper(), venue)
    try:
        return TICK_TABLE[key]
    except KeyError:
        raise KeyError(
            f"no frozen tick for {key} ({TICK_TABLE_VERSION}); "
            "add it explicitly, never default"
        ) from None
