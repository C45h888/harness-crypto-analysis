"""Pure microprice measurement (Track D1, theory §4).

Frozen definition MICROPRICE_ESTIMATOR="microprice-v1":
  M    = (bid + ask) / 2
  P_mu = (ask*qBid + bid*qAsk) / (qBid + qAsk)
  D_mu = P_mu - M

Decimal-only. Zero-queue -> None (NULL, never 0). Crossed/locked books are
refused by BestQuoteState.validate(); dict-replay callers that bypass
validation must treat a None return as not-provided.
"""

from __future__ import annotations

from decimal import Decimal

from .contracts import BestQuoteState

MICROPRICE_ESTIMATOR = "microprice-v1"
_BPS = Decimal(10000)


def mid(quote: BestQuoteState) -> Decimal:
    """Best-quote midprice (bid+ask)/2."""
    return (quote.bid_price + quote.ask_price) / Decimal(2)


def microprice(quote: BestQuoteState) -> Decimal | None:
    """Size-weighted microprice, or None when total top-queue is zero."""
    total = quote.bid_qty + quote.ask_qty
    if total == 0:
        return None
    return (quote.ask_price * quote.bid_qty + quote.bid_price * quote.ask_qty) / total


def displacement(quote: BestQuoteState) -> Decimal | None:
    """Microprice displacement D_mu = P_mu - M, or None when undefined."""
    quote.validate()
    mu = microprice(quote)
    if mu is None:
        return None
    return mu - mid(quote)


def displacement_bps(quote: BestQuoteState) -> Decimal | None:
    """D_mu expressed in basis points of mid, or None when undefined."""
    quote.validate()
    mu = microprice(quote)
    if mu is None:
        return None
    m = mid(quote)
    if m == 0:
        return None
    return (mu / m - Decimal(1)) * _BPS
