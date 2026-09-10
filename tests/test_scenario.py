"""Scenario evaluation tests — pure math + dispatch seams, all LLM-free.

Covers `fitting.evaluate_scenario` (upside/downside math, band ordering,
beta-zero guard, refusal codes, window quality) and the
`calc.scenario.evaluate` registry/routing seams.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from market_service.microstructure import fitting as fm
from market_service.microstructure.contracts import OFIInterval, PriceImpactFit


def _interval(seq: int, ofi: str, quality: str = "exact_feed") -> OFIInterval:
    return OFIInterval(
        symbol="BTCUSDT", venue="spot",
        start_ts_ms=seq * 10_000, end_ts_ms=(seq + 1) * 10_000,
        event_count=2, ofi=Decimal(ofi), average_depth=Decimal("1300"),
        first_update_id=None, last_update_id=None, quality=quality,
    )


def _fit(**over: object) -> PriceImpactFit:
    base: dict = {
        "fit_id": "beta-test", "symbol": "BTCUSDT", "venue": "spot",
        "window_start_ms": 0, "window_end_ms": 1_800_000, "interval_seconds": 10,
        "alpha": Decimal("0"), "beta": Decimal("0.001"),
        "stderr_beta": Decimal("0.0002"), "robust_se_method": "HC0",
        "n_observations": 100, "excluded_observations": 10,
        "r2": Decimal("0.05"), "residual_std": Decimal("0.9"),
        "heteroskedasticity_flag": False, "mean_ad": Decimal("1300"),
        "price_unit": "ticks", "tick_size": Decimal("0.01"),
        "input_hash": "abc", "model_version": "ofi-depth-v1",
        "sensitivity": False, "status": "provisional",
    }
    base.update(over)
    return PriceImpactFit(**base)


class EvaluateScenarioTests(unittest.TestCase):
    def test_upside_math(self):
        # Δ=(100.50−100)/0.01=50 ticks; n=90 (15m@10s); req=50/0.001=50000.
        intervals = [_interval(i, "1000") for i in range(150)]
        out = fm.evaluate_scenario(
            Decimal("100.50"), Decimal("100"), Decimal("0.01"),
            _fit(), intervals, interval_seconds=10, horizon="15m",
        )
        self.assertEqual(out["direction"], "up")
        self.assertEqual(out["delta_req_ticks"], "50")
        self.assertEqual(out["n_intervals"], 90)
        self.assertEqual(out["required_ofi"], "50000")
        # every 90-sum is 90000 ≥ 50000 → exceedance 1 over 61 windows.
        self.assertEqual(out["exceedance"], "1")
        self.assertEqual(out["n_windows_usable"], 61)
        self.assertEqual(out["n_windows_degraded"], 0)

    def test_downside_tail(self):
        # target below current → down; sums 90000, req negative → 0 exceedance
        # (no window sum ≤ −50000) unless flow reverses.
        intervals = [_interval(i, "1000") for i in range(150)]
        out = fm.evaluate_scenario(
            Decimal("99.50"), Decimal("100"), Decimal("0.01"),
            _fit(), intervals, interval_seconds=10, horizon="15m",
        )
        self.assertEqual(out["direction"], "down")
        self.assertEqual(out["required_ofi"], "-50000")
        self.assertEqual(out["exceedance"], "0")
        intervals_neg = [_interval(i, "-1000") for i in range(150)]
        out2 = fm.evaluate_scenario(
            Decimal("99.50"), Decimal("100"), Decimal("0.01"),
            _fit(), intervals_neg, interval_seconds=10, horizon="15m",
        )
        self.assertEqual(out2["exceedance"], "1")

    def test_band_range_ordered(self):
        intervals = [_interval(i, "1000") for i in range(150)]
        out = fm.evaluate_scenario(
            Decimal("100.50"), Decimal("100"), Decimal("0.01"),
            _fit(), intervals, interval_seconds=10, horizon="15m",
        )
        lo, hi = out["required_ofi_range"]
        self.assertLess(Decimal(lo), Decimal(hi))
        self.assertEqual(len(out["exceedance_range"]), 2)

    def test_beta_zero_guard(self):
        intervals = [_interval(i, "1000") for i in range(150)]
        with self.assertRaises(ValueError) as ctx:
            fm.evaluate_scenario(
                Decimal("100.50"), Decimal("100"), Decimal("0.01"),
                _fit(beta=Decimal("0")), intervals,
                interval_seconds=10, horizon="15m",
            )
        self.assertIn("beta_zero", str(ctx.exception))

    def test_refusals(self):
        intervals = [_interval(i, "1000") for i in range(150)]
        fit = _fit()
        with self.assertRaises(ValueError) as c1:
            fm.evaluate_scenario(Decimal("100"), Decimal("100"), Decimal("0.01"),
                                 fit, intervals, interval_seconds=10, horizon="15m")
        self.assertIn("target_eq_current", str(c1.exception))
        with self.assertRaises(ValueError) as c2:
            fm.evaluate_scenario(Decimal("101"), Decimal("100"), Decimal("0.01"),
                                 fit, intervals, interval_seconds=10, horizon="2h")
        self.assertIn("bad_horizon", str(c2.exception))
        with self.assertRaises(ValueError) as c3:
            fm.evaluate_scenario(Decimal("101"), Decimal("100"), Decimal("0.01"),
                                 _fit(status="insufficient"), intervals,
                                 interval_seconds=10, horizon="15m")
        self.assertIn("insufficient", str(c3.exception))
        thin = [_interval(i, "1000") for i in range(100)]  # 100−90+1=11 < 30
        with self.assertRaises(ValueError) as c4:
            fm.evaluate_scenario(Decimal("100.50"), Decimal("100"), Decimal("0.01"),
                                 fit, thin, interval_seconds=10, horizon="15m")
        self.assertIn("insufficient_windows", str(c4.exception))

    def test_degraded_windows_excluded_but_counted(self):
        intervals = [_interval(i, "1000") for i in range(150)]
        intervals[10] = _interval(10, "1000", quality="snapshot_approximation")
        out = fm.evaluate_scenario(
            Decimal("100.50"), Decimal("100"), Decimal("0.01"),
            _fit(), intervals, interval_seconds=10, horizon="15m",
        )
        # windows covering interval 10 (starts 0..10) drop out: 61 − 11 = 50
        # usable, 11 degraded.
        self.assertEqual(out["n_windows_usable"], 50)
        self.assertEqual(out["n_windows_degraded"], 11)
        self.assertEqual(out["exceedance"], "1")

    def test_horizon_sizes(self):
        self.assertEqual(fm.SCENARIO_HORIZONS, {"15m": 900, "1h": 3600, "4h": 14400})
        intervals = [_interval(i, "10") for i in range(3_700)]
        out = fm.evaluate_scenario(
            Decimal("100.10"), Decimal("100"), Decimal("0.01"),
            _fit(), intervals, interval_seconds=10, horizon="1h",
        )
        self.assertEqual(out["n_intervals"], 360)
        self.assertIn("scale_assumption", out)


class ScenarioSemanticsTests(unittest.TestCase):
    SCEN = {"target_price": "100.50", "horizon": "15m"}

    def _final(self, **over):
        base = {
            "phase": "P6", "tool_calls": [],
            "summary": "x" * 250,
            "evidence": [
                {"path": "calc.price.delta → route_a_direct.delta_ticks",
                 "value": "0.27", "interpretation": "derived ΔP"},
                {"path": "calc.scenario.evaluate → exceedance",
                 "value": "0", "interpretation": "no window reaches required flow"},
                {"path": "memory.recall_paper → fact",
                 "value": "OFI impact linear", "interpretation": "paper grounding"},
            ],
            "confidence": "low",
            "limitations": ["provisional fit"],
            "model_separation": "beta and c/lambda read separately",
            "hypothesis": {"H0": "target not reachable", "H1": "target reachable",
                             "paper_refs": ["Cont 1011.6402 §3"],
                             "evidence_refs": ["calc.scenario.evaluate"]},
            "scenario": {
                "target_price": "100.50", "horizon": "15m", "direction": "up",
                "required_ofi": "50000", "required_ofi_range": None,
                "exceedance": "0", "exceedance_range": None,
                "probability": "low", "verdict": "not_reachable",
                "rationale": "required flow outside observed regime",
            },
        }
        base.update(over)
        return base

    def _coverage(self):
        return {p: {"x"} for p in ("P1", "P2", "P3", "P5", "P6")}

    def test_scenario_complete_passes(self):
        from market_service.nooa_harness.engine import _validate_final_turn
        passed, missing = _validate_final_turn(
            self._final(), self._coverage(), scenario=self.SCEN)
        self.assertTrue(passed, missing)

    def test_scenario_absent_unchanged(self):
        from market_service.nooa_harness.engine import _validate_final_turn
        final = self._final()
        del final["scenario"]
        final["hypothesis"] = {"H0": "beta positive", "H1": "",
                                 "paper_refs": [], "evidence_refs": []}
        passed, missing = _validate_final_turn(final, self._coverage())
        self.assertTrue(passed, missing)

    def test_scenario_missing_block_and_h1(self):
        from market_service.nooa_harness.engine import _validate_final_turn
        final = self._final()
        del final["scenario"]
        final["hypothesis"] = {"H0": "x", "H1": "",
                                 "paper_refs": [], "evidence_refs": []}
        passed, missing = _validate_final_turn(
            final, self._coverage(), scenario=self.SCEN)
        self.assertFalse(passed)
        self.assertTrue(any("scenario block" in m for m in missing))
        self.assertTrue(any("TWO hypotheses" in m for m in missing))
        self.assertTrue(any("calc.scenario.evaluate" in m for m in missing))

    def test_scenario_bad_verdict_and_probability(self):
        from market_service.nooa_harness.engine import _validate_final_turn
        scen = dict(self._final()["scenario"])
        scen["verdict"] = "maybe"
        scen["probability"] = "extreme"
        passed, missing = _validate_final_turn(
            self._final(scenario=scen), self._coverage(), scenario=self.SCEN)
        self.assertFalse(passed)
        self.assertTrue(any("scenario.verdict" in m for m in missing))
        self.assertTrue(any("scenario.probability" in m for m in missing))

    def test_verdict_zero_band_invalidated(self):
        from market_service.nooa_harness.engine import _scenario_verdict
        result = {"direction": "up", "target_price": "100.50",
                  "horizon": "15m", "n_windows_usable": 3000,
                  "exceedance": "0", "exceedance_range": ["0", "0"]}
        verdict, reason = _scenario_verdict(result, [], "validated", [])
        self.assertEqual(verdict, "invalidated")
        self.assertIn("H0 holds", reason)

    def test_verdict_high_band_validated(self):
        from market_service.nooa_harness.engine import _scenario_verdict
        result = {"direction": "down", "target_price": "99.50",
                  "horizon": "1h", "n_windows_usable": 500,
                  "exceedance": "0.8", "exceedance_range": ["0.6", "0.9"]}
        verdict, _ = _scenario_verdict(result, [], "validated", [])
        self.assertEqual(verdict, "validated")

    def test_verdict_straddle_inconclusive(self):
        from market_service.nooa_harness.engine import _scenario_verdict
        result = {"direction": "up", "target_price": "100.20",
                  "horizon": "15m", "n_windows_usable": 61,
                  "exceedance": "0.3", "exceedance_range": ["0", "0.8"]}
        verdict, _ = _scenario_verdict(result, [], "validated", [])
        self.assertEqual(verdict, "inconclusive")

    def test_verdict_provisional_ceiling_carries_read(self):
        from market_service.nooa_harness.engine import _scenario_verdict
        result = {"direction": "up", "target_price": "100.50",
                  "horizon": "15m", "n_windows_usable": 3000,
                  "exceedance": "0", "exceedance_range": ["0", "0"]}
        verdict, reason = _scenario_verdict(
            result, [], "provisional", ["89 sequence gap(s)"])
        self.assertEqual(verdict, "inconclusive")
        self.assertIn("0 exceedance", reason)
        self.assertIn("held inconclusive", reason)

    def test_verdict_refusal_names_cause(self):
        from market_service.nooa_harness.engine import _scenario_verdict
        log = [{"capability": "calc.scenario.evaluate", "scope": {},
                "result": "ok",
                "detail": {"status": "refused", "reason": "beta_zero: xyz"}}]
        verdict, reason = _scenario_verdict(None, log, "validated", [])
        self.assertEqual(verdict, "inconclusive")
        self.assertIn("beta_zero", reason)

    def test_verdict_never_called(self):
        from market_service.nooa_harness.engine import _scenario_verdict
        verdict, reason = _scenario_verdict(None, [], "validated", [])
        self.assertEqual(verdict, "inconclusive")
        self.assertIn("never called", reason)


class ScenarioRegistryTests(unittest.TestCase):
    def test_registered_p5(self):
        from market_service.nooa_harness.inference import TOOL_NAMES, TOOL_PHASE
        from market_service.nooa_harness.inference.dispatch import _normalize_tool_name
        self.assertEqual(_normalize_tool_name("calc.scenario.evaluate"),
                         "calc.scenario.evaluate")
        self.assertIn("calc.scenario.evaluate", TOOL_NAMES)
        self.assertEqual(TOOL_PHASE["calc.scenario.evaluate"], "P5")

    def test_unparseable_target_refused_without_store(self):
        import asyncio

        from market_service.nooa_harness.inference import execute_tool
        result, log = asyncio.run(execute_tool(
            None, "calc.scenario.evaluate",
            {"symbol": "BTCUSDT", "venue": "spot", "target_price": "abc",
             "horizon": "1h"},
        ))
        self.assertIsNone(result)
        self.assertEqual(log["result"], "ok")
        self.assertEqual(log["detail"]["status"], "refused")


if __name__ == "__main__":
    unittest.main(verbosity=2)
