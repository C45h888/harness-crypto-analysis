"""Pure event contribution and interval aggregation for order-flow imbalance."""

from __future__ import annotations

from decimal import Decimal

from .contracts import BestQuoteState, OFIInterval, OrderBookEvent

# FROZEN depth estimator definition (Pass 3 contract). Changing this string or
# the estimator it describes invalidates every previously produced fit: the
# input_hash embeds the label, so replaying old data with a new definition
# yields a different hash and the old DepthScalingFit inputs no longer match.
#
# Meaning: within one [start, end) interval, average_depth is the arithmetic
# mean of (qB + qA) / 2 taken over the best-quote transition events that fell
# inside the interval. Unit: the instrument's base quantity. Denominator:
# number of best-quote transition events (NOT wall time and NOT depth-update
# message count — intervals whose best quote never moved carry
# average_depth=None and are excluded from depth scaling).
DEPTH_ESTIMATOR = "event_mean_best_bid_ask_v1"


def _mid(quote: BestQuoteState) -> Decimal:
    """Best-quote midprice of one quote state."""
    return (quote.bid_price + quote.ask_price) / Decimal(2)


def event_contribution(previous: BestQuoteState, current: BestQuoteState) -> Decimal:
    """Return the paper's exact best-quote event contribution e_n."""
    previous.validate()
    current.validate()
    if (previous.symbol, previous.venue) != (current.symbol, current.venue):
        raise ValueError("cannot compare quote states from different instruments")
    bid = (current.bid_qty if current.bid_price >= previous.bid_price else Decimal(0))
    bid -= previous.bid_qty if current.bid_price <= previous.bid_price else Decimal(0)
    ask = -(current.ask_qty if current.ask_price <= previous.ask_price else Decimal(0))
    ask += previous.ask_qty if current.ask_price >= previous.ask_price else Decimal(0)
    return bid + ask


class OFIAggregator:
    """Aggregate valid best-quote events into fixed half-open time intervals."""

    def __init__(self, interval_ms: int):
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        self.interval_ms = interval_ms
        self._start: int | None = None
        self._symbol: str | None = None
        self._venue: str | None = None
        self._events: list[OrderBookEvent] = []

    def add(self, event: OrderBookEvent) -> list[OFIInterval]:
        ts = event.current.exchange_ts_ms
        if ts <= 0:
            raise ValueError("event requires a positive exchange timestamp")
        start = (ts // self.interval_ms) * self.interval_ms
        if self._start is None:
            self._start, self._symbol, self._venue = start, event.current.symbol, event.current.venue
        if (self._symbol, self._venue) != (event.current.symbol, event.current.venue):
            raise ValueError("one aggregator accepts one symbol and venue")
        completed: list[OFIInterval] = []
        while start > self._start:
            completed.append(self._close(self._start, self._start + self.interval_ms))
            self._start += self.interval_ms
        self._events.append(event)
        return completed

    def flush(self) -> OFIInterval | None:
        if self._start is None:
            return None
        result = self._close(self._start, self._start + self.interval_ms)
        self._start += self.interval_ms
        return result

    def discard(self) -> None:
        """Drop an incomplete interval after a source sequence discontinuity."""
        self._start = None
        self._symbol = None
        self._venue = None
        self._events = []

    def _close(self, start: int, end: int) -> OFIInterval:
        events, self._events = self._events, []
        if not events:
            return OFIInterval(
                symbol=self._symbol or "", venue=self._venue or "", start_ts_ms=start,
                end_ts_ms=end, event_count=0, ofi=Decimal(0), average_depth=None,
                first_update_id=None, last_update_id=None, quality="insufficient",
                depth_estimator=DEPTH_ESTIMATOR,
            )
        depths = [(e.current.bid_qty + e.current.ask_qty) / Decimal(2) for e in events]
        first_prev, last_curr = events[0].previous, events[-1].current
        return OFIInterval(
            symbol=self._symbol or "", venue=self._venue or "", start_ts_ms=start,
            end_ts_ms=end, event_count=len(events),
            ofi=sum((e.contribution for e in events), Decimal(0)),
            average_depth=sum(depths, Decimal(0)) / Decimal(len(depths)),
            first_update_id=events[0].current.update_id,
            last_update_id=events[-1].current.update_id,
            quality="exact_feed",
            mid_start=_mid(first_prev),
            mid_end=_mid(last_curr),
            depth_estimator=DEPTH_ESTIMATOR,
        )
