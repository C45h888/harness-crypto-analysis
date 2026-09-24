"""Pass-2 analysis workers — substrate foundations (G4/G5/G6) + trigger semantics.

Regression net for the second analysis wave (docs/ANALYSIS_WORKER_SPEC.md §3):
* G6 substrate foundations — ``align_oi_rows`` / ``latest_oi_bar`` /
  ``proactiveness_verdict`` (oi), ``oi_price_divergence`` / ``liquidation_signal``
  (liquidations) are public and deterministic;
* G5 — ``stage_window_inputs`` derives the 4h scalars from the 48 x 5m klines
  the derivative cache already carries (no new fetch, no cache change);
* G4 — ``dx_from_flows`` composes the tape flow summaries + derivative cache
  into the ``dx`` shape ``demand_verdict`` consumes (no raw trades/books needed);
* worker trigger doctrine — probe-against-own-state: a dependency re-fire with
  no semantic change does NOT fire the analysis worker; cold start seeds a
  baseline projection.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from market_service.analysis.demand import demand_verdict, dx_from_flows
from market_service.analysis.liquidations import liquidation_signal, oi_price_divergence
from market_service.analysis.oi import align_oi_rows, latest_oi_bar, proactiveness_verdict
from market_service.analysis.stage import infer_stage, stage_window_inputs
from market_service.analysis_worker.demand_worker import DemandWorker, compose_demand_input
from market_service.analysis_worker.liquidation_worker import LiquidationWorker, compose_liquidation_input
from market_service.analysis_worker.oi_analysis_worker import OIAnalysisWorker, compose_oi_bar_input
from market_service.analysis_worker.stage_worker import StageWorker, compose_stage_input
from market_service.substrate_worker.contracts import SubstrateStatePayload, TriggerDecision
from tests.test_analysis_workers import _AnalysisFakeRedis, _AnalysisFakeStore


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rows(n=1, start_ms=1_700_000_000_000):
    return [{"id": f"{start_ms + i}-0", "fields": {}} for i in range(n)]


# ---- shared 5m-aligned series (bucket T0, T1).
# Timestamps sit on 5m boundaries (real Binance 5m OI/kline alignment) —
# ``oi_price_divergence`` buckets prices to 5m and compares to raw OI timestamps,
# so the series must be 5m-aligned for the join to land.
T0 = 1_700_000_100_000  # divisible by 300000 (a 5m boundary)
T1 = T0 + 300_000

OI_HIST = [
    {"timestamp": T0, "sum_open_interest": 100.0, "sum_open_interest_value": 1000.0},
    {"timestamp": T1, "sum_open_interest": 110.0, "sum_open_interest_value": 1100.0},
]
TBR = [
    {"timestamp": T0, "buy_vol": 60.0, "sell_vol": 40.0},
    {"timestamp": T1, "buy_vol": 70.0, "sell_vol": 30.0},
]
KLINES = [
    [T0, 100.0, 105.0, 99.0, 102.0, 50.0],
    [T1, 102.0, 110.0, 101.0, 108.0, 60.0],
]


class OISubstrateTests(unittest.TestCase):
    """G6: the OI bar grid is public + deterministic."""

    def test_align_oi_rows_classifies_the_bar_grid(self):
        bars = align_oi_rows(OI_HIST, TBR, KLINES)
        self.assertEqual(len(bars), 2)
        # First bar has no prior -> flat deltas -> TWO_SIDED_FLAT.
        self.assertEqual(bars[0]["class"], "TWO_SIDED_FLAT")
        # Second bar: OI +10%, price +5.9%, tbr 0.70 -> AGGRESSIVE_LONG.
        self.assertEqual(bars[1]["class"], "AGGRESSIVE_LONG")
        self.assertEqual(bars[1]["proactiveness"], 2)

    def test_latest_oi_bar_is_the_last_aligned_bar(self):
        bar = latest_oi_bar(OI_HIST, TBR, KLINES)
        self.assertIsNotNone(bar)
        self.assertEqual(bar["class"], "AGGRESSIVE_LONG")

    def test_latest_oi_bar_none_when_no_series(self):
        self.assertIsNone(latest_oi_bar([], [], []))

    def test_proactiveness_verdict_rolls_the_score(self):
        bars = align_oi_rows(OI_HIST, TBR, KLINES)
        verdict = proactiveness_verdict(bars)
        self.assertIn(verdict["label"], ("PROACTIVE", "WARMING", "PASSIVE", "ABSENT"))
        self.assertEqual(verdict["score"], 2)


class LiquidationSubstrateTests(unittest.TestCase):
    """G6: the OI-vs-price divergence grid is public + deterministic."""

    def test_liquidation_signal_grid(self):
        self.assertEqual(liquidation_signal(-0.5, -0.5), "LONG_LIQUIDATION_LIKELY")
        self.assertEqual(liquidation_signal(-0.5, 0.5), "SHORT_LIQUIDATION_LIKELY")
        self.assertEqual(liquidation_signal(0.0, 0.5), "ORGANIC_BUYING")
        self.assertEqual(liquidation_signal(0.0, -0.5), "ORGANIC_SELLING")
        self.assertEqual(liquidation_signal(0.5, 0.5), "LONG_ADDING_INTO_STRENGTH")
        self.assertEqual(liquidation_signal(0.1, 0.1), "MIXED_RANGE_BOUND")

    def test_oi_price_divergence_detects_long_liquidation(self):
        oi = [{"timestamp": T0, "sum_open_interest": 100.0},
              {"timestamp": T1, "sum_open_interest": 95.0}]
        klines = [[T0, 100.0, 101.0, 99.0, 102.0, 50.0],
                  [T1, 102.0, 103.0, 96.0, 97.0, 60.0]]
        result = oi_price_divergence(oi, klines)
        self.assertLess(result["cumulative_oi_change_pct"], 0)
        self.assertLess(result["cumulative_price_change_pct"], 0)
        self.assertEqual(result["signal"], "LONG_LIQUIDATION_LIKELY")

    def test_oi_price_divergence_none_safe(self):
        result = oi_price_divergence([], [])
        self.assertEqual(result["rows"], [])


class StageSubstrateTests(unittest.TestCase):
    """G5: 4h scalars derived from the 48 x 5m klines (no extra fetch)."""

    def test_stage_window_inputs_derives_4h_scalars(self):
        scalars = stage_window_inputs(KLINES, OI_HIST)
        self.assertAlmostEqual(scalars["px_chg_4h"], 8.0)   # (108-100)/100
        self.assertAlmostEqual(scalars["oi_chg_4h"], 10.0)  # (110-100)/100
        self.assertAlmostEqual(scalars["up_pct_4h"], 100.0)  # both bars up

    def test_stage_window_inputs_feeds_infer_stage(self):
        scalars = stage_window_inputs(KLINES, OI_HIST)
        result = infer_stage(**scalars, up_steps=2, down_steps=0)
        self.assertIn("stage", result)
        self.assertIn("MARKUP", result["stage"])  # price + OI rising


class DemandSubstrateTests(unittest.TestCase):
    """G4: dx composed from tape flow summaries + deriv cache (no raw trades)."""

    def test_dx_from_flows_maps_flows_and_derivs(self):
        spot_flow = {"last_price": 100.0, "cvd": 5.0, "buy_share": 0.6,
                     "buy_notional_usd": 60.0, "sell_notional_usd": 40.0}
        fut_flow = {"last_price": 101.0, "cvd": -3.0, "buy_share": 0.45}
        derivs = {"oi": 1_000.0, "funding": 0.0001, "long_pct": 0.6,
                  "top_long_pct": 0.55, "taker_buy_ratio": 1.1}
        dx = dx_from_flows(spot_flow, fut_flow, derivs)
        self.assertEqual(dx["spot"]["cvd"], 5.0)
        self.assertEqual(dx["spot"]["buy_share"], 0.6)
        self.assertEqual(dx["futures"]["buy_share"], 0.45)
        self.assertEqual(dx["derivs"]["oi"], 1_000.0)
        self.assertEqual(dx["derivs"]["funding"], 0.0001)
        self.assertEqual(dx["derivs"]["top_long_pct"], 0.55)

    def test_dx_from_flows_is_none_safe(self):
        dx = dx_from_flows({}, {}, None)
        verdict, _ = demand_verdict(dx)  # does not raise
        self.assertIsInstance(verdict, str)


# ---- worker fixtures ----

def _dep(name, output):
    return {"substrate": name, "symbol": "SOLUSDT", "status": "healthy",
            "computed_at_ms": _now_ms() - 1_000, "output": output}


def _deriv(futures):
    return {"observed_at_ms": _now_ms() - 1_000, "futures": futures}


def _prior(name, output):
    return SubstrateStatePayload.create(
        substrate=name, symbol="SOLUSDT", output=output,
        trigger=TriggerDecision(fired=True, source="probe"),
        computed_at_ms=_now_ms() - 2_000).to_dict()


def _launch(worker_cls, deps, deriv, prior=None, name=None):
    store = _AnalysisFakeStore(_AnalysisFakeRedis(
        deps=deps, derivative=deriv, analysis_latest=prior))
    w = worker_cls(store, symbol="SOLUSDT")
    now = _now_ms()
    _run(w._handle_rows(_rows(), now))
    _run(asyncio.sleep(0.05))
    return w


class OIAnalysisWorkerTests(unittest.TestCase):
    def _deps(self):
        return {"oi": _dep("oi", {"raw_open_interest": 1_000.0}),
                "technicals": _dep("technicals", {"atr_pct": 1.0})}

    def _deriv(self):
        return _deriv({"oi_history": OI_HIST, "taker_buy_sell": TBR, "klines": KLINES})

    def test_cold_start_fires(self):
        w = _launch(OIAnalysisWorker, self._deps(), self._deriv())
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.published[0]["trigger"]["source"], "cold_start")
        self.assertEqual(w.store.redis.published[0]["output"]["bar_class"], "AGGRESSIVE_LONG")

    def test_bar_class_flip_fires(self):
        prior = _prior("oi_analysis", {"bar_class": "TWO_SIDED_FLAT"})
        w = _launch(OIAnalysisWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 1)
        pub = w.store.redis.published[0]
        self.assertEqual(pub["trigger"]["predicates"]["oi_bar_class"]["from"], "TWO_SIDED_FLAT")

    def test_same_bar_class_does_not_fire(self):
        prior = _prior("oi_analysis", {"bar_class": "AGGRESSIVE_LONG"})
        w = _launch(OIAnalysisWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 0)

    def test_compose_null_discipline(self):
        self.assertIsNone(compose_oi_bar_input({"futures": {}}))

    def test_trigger_stream_is_dependency_state_stream(self):
        store = _AnalysisFakeStore(_AnalysisFakeRedis())
        self.assertEqual(OIAnalysisWorker(store, symbol="SOLUSDT")._stream,
                         store.substrate_stream("oi", "SOLUSDT"))


class LiquidationWorkerTests(unittest.TestCase):
    def _deps(self):
        return {"oi": _dep("oi", {"raw_open_interest": 1_000.0}),
                "technicals": _dep("technicals", {"atr_pct": 1.0})}

    def _deriv(self):
        oi = [{"timestamp": T0, "sum_open_interest": 100.0},
              {"timestamp": T1, "sum_open_interest": 95.0}]
        kl = [[T0, 100.0, 101.0, 99.0, 102.0, 50.0],
              [T1, 102.0, 103.0, 96.0, 97.0, 60.0]]
        return _deriv({"oi_history": oi, "klines": kl})

    def test_cold_start_fires(self):
        w = _launch(LiquidationWorker, self._deps(), self._deriv())
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.published[0]["output"]["signal"],
                         "LONG_LIQUIDATION_LIKELY")

    def test_signal_flip_fires(self):
        prior = _prior("liquidation", {"signal": "MIXED_RANGE_BOUND"})
        w = _launch(LiquidationWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.published[0]["trigger"]["predicates"]
                         ["liquidation_signal"]["to"], "LONG_LIQUIDATION_LIKELY")

    def test_same_signal_does_not_fire(self):
        prior = _prior("liquidation", {"signal": "LONG_LIQUIDATION_LIKELY"})
        w = _launch(LiquidationWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 0)

    def test_compose_null_discipline(self):
        self.assertIsNone(compose_liquidation_input({"futures": {}}))


class DemandWorkerTests(unittest.TestCase):
    def _deps(self):
        return {
            "tape": _dep("tape", {
                "futures_flow": {"last_price": 101.0, "cvd": -3.0, "buy_share": 0.45,
                                 "buy_sell_ratio": 0.8},
                "spot_flow": {"last_price": 100.0, "cvd": 5.0, "buy_share": 0.6}}),
            "oi": _dep("oi", {"raw_open_interest": 1_000.0}),
            "delta": _dep("delta", {"tbr_last_pct": 55.0}),
        }

    def _deriv(self):
        return _deriv({"funding": {"lastFundingRate": "0.0001"},
                       "top_ls": [{"longAccount": 0.55}],
                       "global_ls": [{"longAccount": 0.6}],
                       "taker_buy_sell": [{"buySellRatio": 1.1}]})

    def test_cold_start_fires(self):
        w = _launch(DemandWorker, self._deps(), self._deriv())
        self.assertEqual(w.fired_count, 1)
        self.assertIn("verdict", w.store.redis.published[0]["output"])

    def test_verdict_flip_fires(self):
        dx = compose_demand_input(self._window())
        verdict, _ = demand_verdict(dx)
        prior = _prior("demand", {"verdict": "NOT-THE-SAME-VERDICT"})
        w = _launch(DemandWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 1)
        self.assertEqual(w.store.redis.published[0]["trigger"]["predicates"]
                         ["demand_flip"]["to"], verdict)

    def test_same_verdict_does_not_fire(self):
        dx = compose_demand_input(self._window())
        verdict, _ = demand_verdict(dx)
        prior = _prior("demand", {"verdict": verdict})
        w = _launch(DemandWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 0)

    def _window(self):
        return {"substrate_dependencies": self._deps(),
                "futures": self._deriv()["futures"]}

    def test_compose_null_discipline(self):
        self.assertIsNone(compose_demand_input({"substrate_dependencies": {}}))


class StageWorkerTests(unittest.TestCase):
    def _deps(self):
        return {
            "technicals": _dep("technicals", {"atr_pct": 1.0}),
            "oi": _dep("oi", {"raw_open_interest": 1_000.0}),
            "migration": _dep("migration", {"hourly_keystone_migration": {
                "hourly": [{"migration": "UP"}, {"migration": "UP"}, {"migration": "DOWN"}],
                "verdict": "MIGRATING_UP", "net_buckets": 1}}),
        }

    def _deriv(self):
        return _deriv({"klines": KLINES, "oi_history": OI_HIST,
                       "funding": {"lastFundingRate": "0.0001"},
                       "top_ls": [{"longAccount": 0.55}],
                       "global_ls": [{"longAccount": 0.6}]})

    def test_compose_derives_migration_steps(self):
        window = {"substrate_dependencies": self._deps(), "futures": self._deriv()["futures"]}
        kw = compose_stage_input(window)
        self.assertEqual(kw["up_steps"], 2)
        self.assertEqual(kw["down_steps"], 1)
        self.assertAlmostEqual(kw["px_chg_4h"], 8.0)

    def test_cold_start_fires(self):
        w = _launch(StageWorker, self._deps(), self._deriv())
        self.assertEqual(w.fired_count, 1)
        self.assertIn("stage", w.store.redis.published[0]["output"])

    def test_stage_transition_fires(self):
        prior = _prior("stage", {"stage": "NOT-THE-SAME-STAGE"})
        w = _launch(StageWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 1)
        self.assertIn("stage_transition", w.store.redis.published[0]["trigger"]["predicates"])

    def test_same_stage_does_not_fire(self):
        kw = compose_stage_input({"substrate_dependencies": self._deps(),
                                  "futures": self._deriv()["futures"]})
        stage = infer_stage(**kw)["stage"]
        prior = _prior("stage", {"stage": stage})
        w = _launch(StageWorker, self._deps(), self._deriv(), prior)
        self.assertEqual(w.fired_count, 0)

    def test_compose_null_discipline(self):
        self.assertIsNone(compose_stage_input({"futures": {}}))


if __name__ == "__main__":
    unittest.main()
