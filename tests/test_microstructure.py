from __future__ import annotations

from decimal import Decimal
import unittest

from market_service.microstructure.contracts import BestQuoteState, DepthDelta, OrderBookEvent
from market_service.microstructure.ofi import OFIAggregator, event_contribution
from market_service.microstructure.orderbook import BookGapError, OrderBookReconstructor


def quote(
    update_id: int, *, bid_price: str = "100", bid_qty: str = "10",
    ask_price: str = "101", ask_qty: str = "20", ts: int = 100,
) -> BestQuoteState:
    return BestQuoteState(
        symbol="BTCUSDT", venue="spot", update_id=update_id,
        exchange_ts_ms=ts, received_ts_ms=ts,
        bid_price=Decimal(bid_price), bid_qty=Decimal(bid_qty),
        ask_price=Decimal(ask_price), ask_qty=Decimal(ask_qty),
    )


class OFIEquationTests(unittest.TestCase):
    def test_same_price_queue_increases_and_decreases(self):
        self.assertEqual(event_contribution(quote(1), quote(2, bid_qty="15")), Decimal("5"))
        self.assertEqual(event_contribution(quote(1), quote(2, ask_qty="25")), Decimal("-5"))

    def test_best_price_moves_use_the_paper_queue_rules(self):
        self.assertEqual(event_contribution(quote(1), quote(2, bid_price="100.5", bid_qty="7")), Decimal("7"))
        self.assertEqual(event_contribution(quote(1), quote(2, ask_price="100.5", ask_qty="9")), Decimal("-9"))
        self.assertEqual(event_contribution(quote(1), quote(2, bid_price="99", bid_qty="7")), Decimal("-10"))
        self.assertEqual(event_contribution(quote(1), quote(2, ask_price="102", ask_qty="9")), Decimal("20"))

    def test_interval_uses_half_open_boundaries(self):
        first = quote(1, ts=100)
        event_a = OrderBookEvent(first, quote(2, bid_qty="12", ts=900), Decimal("2"))
        event_b = OrderBookEvent(event_a.current, quote(3, bid_qty="13", ts=1000), Decimal("1"))
        aggregate = OFIAggregator(1_000)
        self.assertEqual(aggregate.add(event_a), [])
        closed = aggregate.add(event_b)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].start_ts_ms, 0)
        self.assertEqual(closed[0].end_ts_ms, 1000)
        self.assertEqual(closed[0].event_count, 1)
        self.assertEqual(closed[0].ofi, Decimal("2"))


class LocalBookTests(unittest.TestCase):
    def setUp(self):
        self.book = OrderBookReconstructor("BTCUSDT", "spot")
        self.book.bootstrap({
            "lastUpdateId": 10,
            "bids": [["100", "10"], ["99", "20"]],
            "asks": [["101", "20"], ["102", "30"]],
        }, received_ts_ms=1)

    def test_applies_contiguous_delta_and_emits_event(self):
        event = self.book.apply(DepthDelta(
            symbol="BTCUSDT", venue="spot", first_update_id=11, final_update_id=11,
            exchange_ts_ms=100, received_ts_ms=101,
            bids=((Decimal("100"), Decimal("15")),), asks=(),
        ))
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.contribution, Decimal("5"))
        self.assertEqual(self.book.last_update_id, 11)

    def test_rejects_a_sequence_gap(self):
        with self.assertRaises(BookGapError):
            self.book.apply(DepthDelta(
                symbol="BTCUSDT", venue="spot", first_update_id=12, final_update_id=12,
                exchange_ts_ms=100, received_ts_ms=101, bids=(), asks=(),
            ))

    def test_ignores_already_applied_delta(self):
        self.assertIsNone(self.book.apply(DepthDelta(
            symbol="BTCUSDT", venue="spot", first_update_id=9, final_update_id=10,
            exchange_ts_ms=100, received_ts_ms=101, bids=(), asks=(),
        )))


if __name__ == "__main__":
    unittest.main()
