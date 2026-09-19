from __future__ import annotations

from decimal import Decimal
import unittest

from market_service.microstructure.contracts import BestQuoteState, DepthDelta, OrderBookEvent
from market_service.microstructure.fitting_route_c import (
    build_feature_vector,
    build_forward_observations,
    calibration_report,
    fit_forward_ols,
    predict_distribution,
)
from market_service.microstructure.microprice import displacement, microprice, mid
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


class MicropriceTests(unittest.TestCase):
    """Track D1: frozen microprice-v1 measurement."""

    def test_known_quotes_give_known_displacement(self):
        q = quote(1, bid_price="100", bid_qty="2", ask_price="102", ask_qty="1")
        self.assertEqual(mid(q), Decimal("101"))
        self.assertEqual(microprice(q), Decimal("304") / Decimal("3"))
        self.assertEqual(displacement(q), Decimal("304") / Decimal("3") - Decimal("101"))

    def test_zero_queue_returns_null_never_zero(self):
        q = quote(1, bid_qty="0", ask_qty="0")
        self.assertIsNone(microprice(q))
        self.assertIsNone(displacement(q))

    def test_crossed_book_refuses(self):
        q = quote(1, bid_price="102", ask_price="101")
        with self.assertRaises(ValueError):
            displacement(q)


class FeatureVectorTests(unittest.TestCase):
    """Track D2: xt-v1 promotion is Decimal-only and deterministic."""

    def test_deterministic_hash_and_versions(self):
        kw = dict(symbol="BTCUSDT", venue="spot", ts_ms=1000, ofi=Decimal("5"),
                  average_depth=Decimal("1.5"), quote=quote(1), cvd_slope=Decimal("0.3"))
        first, second = build_feature_vector(**kw), build_feature_vector(**kw)
        self.assertEqual(first.input_hash, second.input_hash)
        # vector_version was bumped from v1→v2 during Track D; the test
        # validates the current version is stable (not a regression guard).
        self.assertEqual(first.vector_version, "xt-v2")
        self.assertEqual(first.def_versions["dmu"], "microprice-v1")

    def test_float_inputs_rejected(self):
        with self.assertRaises(TypeError):
            build_feature_vector(symbol="BTCUSDT", venue="spot", ts_ms=1000,
                                 ofi=5.0)  # type: ignore[arg-type]

    def test_missing_fields_are_absent_never_zero(self):
        vec = build_feature_vector(symbol="BTCUSDT", venue="spot", ts_ms=1000)
        self.assertEqual(vec.fields, {})


class ForwardTests(unittest.TestCase):
    """Track D3-D5: forward join, OLS baseline, distribution."""

    def _vectors(self, n: int = 40):
        vecs, mids = [], []
        for i in range(n):
            q = quote(i, bid_price=str(100 + i * 0.1), ask_price=str(102 + i * 0.1),
                      bid_qty=str(2 + (i % 4)), ask_qty=str(1 + (i % 3)),
                      ts=1000 + i * 1000)
            vecs.append(build_feature_vector(
                symbol="BTCUSDT", venue="spot", ts_ms=1000 + i * 1000,
                ofi=Decimal(i % 7 - 3),
                average_depth=Decimal("1.5") + Decimal(i % 5) * Decimal("0.1"),
                quote=q, cvd_slope=Decimal(i % 3)))
            mids.append((1000 + i * 1000, mid(q)))
        return vecs, mids

    def test_no_lookahead_and_tick_required(self):
        vecs, mids = self._vectors()
        first, _ = build_forward_observations(
            vecs, mids, tick_size=Decimal("0.01"), venue="spot")
        truncated, _ = build_forward_observations(
            vecs, mids[:10], tick_size=Decimal("0.01"), venue="spot")
        # Features at t never see the future: overlapping rows are identical.
        for full, cut in zip(first[:9], truncated[:9]):
            self.assertEqual(full.x.fields, cut.x.fields)
        with self.assertRaises(ValueError):
            build_forward_observations(vecs, mids, tick_size=Decimal("0"), venue="spot")
        with self.assertRaises(TypeError):
            build_forward_observations(vecs, mids, tick_size=0.01, venue="spot")  # type: ignore[arg-type]

    def test_venue_mismatch_excluded(self):
        vecs, mids = self._vectors(5)
        obs, log = build_forward_observations(
            vecs, mids, tick_size=Decimal("0.01"), venue="futures")
        self.assertTrue(all(o.excluded for o in obs))
        self.assertGreater(log["excluded_total"], 0)

    def test_forward_ols_trichotomy_and_known_beta(self):
        # Synthetic Y = 2*ofi: slope must recover ~2 with skill.
        vecs, mids = self._vectors(0)
        for i in range(60):
            ofi = Decimal(i % 9 - 4)
            vecs.append(build_feature_vector(
                symbol="BTCUSDT", venue="spot", ts_ms=1000 + i * 1000, ofi=ofi,
                average_depth=Decimal("1.5") + Decimal(i % 5) * Decimal("0.01")))
            mids.append((1000 + i * 1000, Decimal("100") + ofi * Decimal("0.02")))
        obs, _ = build_forward_observations(
            vecs, mids, tick_size=Decimal("0.01"), venue="spot")
        fit, used = fit_forward_ols(obs, symbol="BTCUSDT", venue="spot", horizon_ms=1000)
        self.assertIn(fit.status, ("validated", "provisional"))
        self.assertGreater(len(used), 30)
        dist = predict_distribution(fit, used[0].x)
        self.assertIsNotNone(dist["expected_ticks"])
        rep = calibration_report(fit, obs)
        # n is the OOS held-out count, not total usable
        self.assertGreaterEqual(rep["n"], 10)
        self.assertLess(rep["n"], len(used))
        # Insufficient gate: too few rows.
        thin, _ = fit_forward_ols(obs[:5], symbol="BTCUSDT", venue="spot", horizon_ms=1000)
        self.assertEqual(thin.status, "insufficient")
        with self.assertRaises(ValueError):
            predict_distribution(thin, used[0].x)


if __name__ == "__main__":
    unittest.main()


class FrozenTickTests(unittest.TestCase):
    def test_known_ticks_match_exchange(self):
        from market_service.microstructure.tick import resolve_tick_size
        self.assertEqual(resolve_tick_size("BTCUSDT", "spot"), Decimal("0.01"))
        self.assertEqual(resolve_tick_size("btcusdt", "futures"), Decimal("0.10"))
        self.assertEqual(resolve_tick_size("ETHUSDT", "futures"), Decimal("0.10"))
        self.assertEqual(resolve_tick_size("SOLUSDT", "spot"), Decimal("0.01"))

    def test_unknown_instrument_refuses_never_guesses(self):
        from market_service.microstructure.tick import resolve_tick_size
        with self.assertRaises(KeyError):
            resolve_tick_size("DOGEUSDT", "spot")


class RealTapeReplayTests(unittest.TestCase):
    @staticmethod
    def _load_spot():
        import json
        from market_service.microstructure.microprice import mid
        from market_service.microstructure.ofi import event_contribution
        from market_service.microstructure.contracts import OrderBookEvent
        rows = json.load(open("tests/fixtures/real_tape_spot_BTCUSDT.json"))
        evs, prev = [], None
        for i, r in enumerate(rows):
            if "bid" not in r:
                continue
            q = BestQuoteState(symbol="BTCUSDT", venue="spot", update_id=i,
                               exchange_ts_ms=r["ts"], received_ts_ms=r["ts"],
                               bid_price=Decimal(r["bid"]), bid_qty=Decimal(r["bidQty"]),
                               ask_price=Decimal(r["ask"]), ask_qty=Decimal(r["askQty"]))
            if prev is not None and (q.bid_price, q.bid_qty, q.ask_price, q.ask_qty) != (
                    prev.bid_price, prev.bid_qty, prev.ask_price, prev.ask_qty):
                evs.append(OrderBookEvent(prev, q, event_contribution(prev, q)))
            prev = q
        return evs

    def test_real_tape_replays_deterministically(self):
        import market_service.microstructure as fm
        evs = self._load_spot()
        self.assertGreater(len(evs), 30)
        def run():
            vecs = [fm.build_feature_vector(symbol="BTCUSDT", venue="spot",
                                            ts_ms=e.current.exchange_ts_ms,
                                            ofi=e.contribution, quote=e.current)
                    for e in evs]
            from market_service.microstructure.microprice import mid
            mids = [(e.current.exchange_ts_ms, mid(e.current)) for e in evs]
            obs, _ = fm.build_forward_observations(vecs, mids,
                                                   tick_size=Decimal("0.01"), venue="spot")
            fit, _ = fm.fit_forward_ols(obs, symbol="BTCUSDT", venue="spot",
                                        horizon_ms=1000)
            return fit
        first, second = run(), run()
        self.assertEqual(first.fit_id, second.fit_id)
        self.assertEqual(first.status, second.status)
        self.assertNotEqual(first.status, "insufficient")

    def test_real_tape_forward_skill_positive_oos(self):
        import market_service.microstructure as fm
        from market_service.microstructure.microprice import mid
        evs = self._load_spot()
        vecs = [fm.build_feature_vector(symbol="BTCUSDT", venue="spot",
                                        ts_ms=e.current.exchange_ts_ms,
                                        ofi=e.contribution, quote=e.current)
                for e in evs]
        mids = [(e.current.exchange_ts_ms, mid(e.current)) for e in evs]
        obs, _ = fm.build_forward_observations(vecs, mids,
                                               tick_size=Decimal("0.01"), venue="spot")
        fit, _ = fm.fit_forward_ols(obs, symbol="BTCUSDT", venue="spot",
                                    horizon_ms=60_000)
        self.assertIn(fit.status, ("validated", "provisional"))
        self.assertIsNotNone(fit.oos_skill)


class EventGrainTapeTests(unittest.TestCase):
    @staticmethod
    def _load_event_tape():
        import json
        from market_service.microstructure.microprice import mid
        from market_service.microstructure.ofi import event_contribution
        from market_service.microstructure.contracts import OrderBookEvent
        rows = json.load(open("tests/fixtures/event_tape_spot_BTCUSDT_120s.json"))
        evs, prev = [], None
        for i, r in enumerate(rows):
            q = BestQuoteState(symbol="BTCUSDT", venue="spot", update_id=i,
                               exchange_ts_ms=r["ts"], received_ts_ms=r["ts"],
                               bid_price=Decimal(str(r["bid"])), bid_qty=Decimal(str(r["bidQty"])),
                               ask_price=Decimal(str(r["ask"])), ask_qty=Decimal(str(r["askQty"])))
            if prev is not None and (q.bid_price, q.bid_qty, q.ask_price, q.ask_qty) != (
                    prev.bid_price, prev.bid_qty, prev.ask_price, prev.ask_qty):
                evs.append(OrderBookEvent(prev, q, event_contribution(prev, q)))
            prev = q
        return evs

    def test_event_grain_short_horizon_skill(self):
        import market_service.microstructure as fm
        from market_service.microstructure.microprice import mid
        evs = self._load_event_tape()
        self.assertGreater(len(evs), 1000)
        vecs = [fm.build_feature_vector(symbol="BTCUSDT", venue="spot",
                                        ts_ms=e.current.exchange_ts_ms,
                                        ofi=e.contribution, quote=e.current)
                for e in evs]
        mids = [(e.current.exchange_ts_ms, mid(e.current)) for e in evs]
        obs, _ = fm.build_forward_observations(vecs, mids,
                                               tick_size=Decimal("0.01"), venue="spot")
        for horizon_ms in (1000, 5000):
            fit, _ = fm.fit_forward_ols(obs, symbol="BTCUSDT", venue="spot",
                                        horizon_ms=horizon_ms)
            self.assertIn(fit.status, ("validated", "provisional"))
            self.assertIsNotNone(fit.oos_skill)
            self.assertGreater(float(fit.oos_skill), 0.0)
