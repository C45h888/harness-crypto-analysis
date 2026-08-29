"""Pure local-book reconstruction for Binance depth update ranges."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .contracts import BestQuoteState, DepthDelta, OrderBookEvent, decimal
from .ofi import event_contribution


class BookGapError(ValueError):
    """A depth update cannot be applied without skipping source sequence IDs."""


class OrderBookReconstructor:
    """Mutable local book whose outputs are immutable quote-event objects."""

    def __init__(self, symbol: str, venue: str):
        self.symbol = symbol.upper()
        self.venue = venue
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self.last_quote: BestQuoteState | None = None

    def bootstrap(self, snapshot: dict[str, Any], *, received_ts_ms: int) -> BestQuoteState:
        if str(snapshot.get("lastUpdateId", "")).strip() == "":
            raise ValueError("snapshot lacks lastUpdateId")
        self.bids = self._levels(snapshot.get("bids") or ())
        self.asks = self._levels(snapshot.get("asks") or ())
        self.last_update_id = int(snapshot["lastUpdateId"])
        quote = self._quote(self.last_update_id, 0, received_ts_ms)
        self.last_quote = quote
        return quote

    def apply(self, delta: DepthDelta) -> OrderBookEvent | None:
        delta.validate()
        if delta.symbol != self.symbol or delta.venue != self.venue:
            raise ValueError("delta belongs to a different book")
        if self.last_update_id is None:
            raise ValueError("book is not bootstrapped")
        if delta.final_update_id <= self.last_update_id:
            return None
        if delta.first_update_id > self.last_update_id + 1:
            raise BookGapError(
                f"missed updates: local={self.last_update_id} incoming={delta.first_update_id}-{delta.final_update_id}"
            )
        self._apply_levels(self.bids, delta.bids)
        self._apply_levels(self.asks, delta.asks)
        self.last_update_id = delta.final_update_id
        current = self._quote(delta.final_update_id, delta.exchange_ts_ms, delta.received_ts_ms)
        previous = self.last_quote
        self.last_quote = current
        if previous is None:
            return None
        if self._same_quote(previous, current):
            return None
        return OrderBookEvent(previous=previous, current=current,
                              contribution=event_contribution(previous, current))

    @staticmethod
    def _levels(levels: Any) -> dict[Decimal, Decimal]:
        out: dict[Decimal, Decimal] = {}
        for row in levels:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price, qty = decimal(row[0]), decimal(row[1])
            if price <= 0 or qty < 0:
                raise ValueError("invalid book level")
            if qty > 0:
                out[price] = qty
        return out

    @staticmethod
    def _apply_levels(book: dict[Decimal, Decimal], changes: tuple[tuple[Decimal, Decimal], ...]) -> None:
        for price, qty in changes:
            if price <= 0 or qty < 0:
                raise ValueError("invalid depth update level")
            if qty == 0:
                book.pop(price, None)
            else:
                book[price] = qty

    def _quote(self, update_id: int, exchange_ts_ms: int, received_ts_ms: int) -> BestQuoteState:
        if not self.bids or not self.asks:
            raise ValueError("book lacks a two-sided best quote")
        bid = max(self.bids)
        ask = min(self.asks)
        quote = BestQuoteState(
            symbol=self.symbol, venue=self.venue, update_id=update_id,
            exchange_ts_ms=exchange_ts_ms, received_ts_ms=received_ts_ms,
            bid_price=bid, bid_qty=self.bids[bid], ask_price=ask, ask_qty=self.asks[ask],
        )
        quote.validate()
        return quote

    @staticmethod
    def _same_quote(left: BestQuoteState, right: BestQuoteState) -> bool:
        return (left.bid_price, left.bid_qty, left.ask_price, left.ask_qty) == (
            right.bid_price, right.bid_qty, right.ask_price, right.ask_qty,
        )
