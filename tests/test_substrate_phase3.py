"""Phase 3 tests — PG-first path, multi-symbol fan-out, healthcheck, parity.

* PG strict/lax behavior uses a fake ledger (no DB needed).
* Multi-symbol pins per-pair consumer groups + supervisor keys.
* Parity gate: every worker's compute() shared keys == the composition
  root's section values on identical evidence (read-only composition
  import — the "steady completion" proof).
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from market_service.substrate_worker import WORKER_REGISTRY
from market_service.substrate_worker.contracts import TriggerDecision
from market_service.substrate_worker.runner import build_workers
from tests.test_density_worker import _NoStore
from tests.test_substrate_worker_core import _FakeRedis, _FakeStore


class _FakeLedger:
    """PostgresRuntimeStore-shaped fake (success or raise)."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.rows: list[tuple[str, str, dict]] = []

    async def record_substrate_state(self, symbol, substrate, payload):
        if self.fail:
            raise ConnectionError("pg down")
        self.rows.append((symbol, substrate, payload))
        return len(self.rows)

    async def close(self):
        return None


def _pg_worker(**kw):
    from market_service.substrate_worker.density_worker import DensityWorker
    store = _FakeStore(_FakeRedis())
    params = {"cooldown_s": 0, "staleness_s": 3600, **kw}
    return DensityWorker(store, symbol="SOLUSDT", **params)


BOOK = {
    "futures": {
        "order_book": {
            "bids": [[148.20, 900.0], [148.15, 500.0], [148.10, 400.0]],
            "asks": [[148.32, 600.0], [148.40, 800.0]],
        },
        "trades_normalized": [
            {"id": i, "ts": 1_700_000_000_000 + i * 45_000, "price": 148.25,
             "qty": 2.0, "is_buyer_maker": bool(i % 3)}
            for i in range(20)
        ],
    },
    "spot": {
        "order_book": {
            "bids": [[148.22, 300.0], [148.10, 120.0]],
            "asks": [[148.30, 250.0]],
        },
        "trades_normalized": [
            {"id": 100 + i, "ts": 1_700_000_000_000 + i * 90_000,
             "price": 148.26, "qty": 1.0, "is_buyer_maker": bool(i % 2)}
            for i in range(10)
        ],
    },
}


class PgFirstPathTests(unittest.TestCase):
    def _fire(self, worker, payload_out=None):
        worker.compute = lambda evidence, depth: dict(payload_out or {"m": 1.0})
        decision = TriggerDecision(fired=True, source="probe")
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                worker._fire_guarded(decision, None, 1_700_000_001_000))
            loop.run_until_complete(asyncio.sleep(0))
        finally:
            loop.close()

    def test_pg_success_records_then_publishes(self):
        ledger = _FakeLedger()
        w = _pg_worker(pg_store=ledger, pg_strict=True)
        with unittest.mock.patch(
            "market_service.substrate_worker.core.build_raw_window",
            return_value={"futures": {"order_book": BOOK["futures"]["order_book"]},
                          "spot": {"order_book": BOOK["spot"]["order_book"]},
                          "coverage": {}},
        ):
            self._fire(w)
        self.assertEqual(len(ledger.rows), 1)
        sym, sub, _recorded = ledger.rows[0]
        self.assertEqual((sym, sub), ("SOLUSDT", "density"))
        self.assertIsNotNone(w.store.redis.latest)

    def test_pg_failure_strict_aborts_publish(self):
        w = _pg_worker(pg_store=_FakeLedger(fail=True), pg_strict=True)
        with unittest.mock.patch(
            "market_service.substrate_worker.core.build_raw_window",
            return_value={"futures": {"order_book": {}}, "spot": {},
                          "coverage": {}},
        ):
            self._fire(w)
        self.assertIsNone(w.store.redis.latest)
        self.assertIn("pg:", w._last_error or "")

    def test_pg_failure_lax_publishes(self):
        w = _pg_worker(pg_store=_FakeLedger(fail=True), pg_strict=False)
        with unittest.mock.patch(
            "market_service.substrate_worker.core.build_raw_window",
            return_value={"futures": {"order_book": {}}, "spot": {},
                          "coverage": {}},
        ):
            self._fire(w)
        self.assertIsNotNone(w.store.redis.latest)
        self.assertIn("lax", w._last_error or "")

    def test_no_pg_store_publishes_directly(self):
        w = _pg_worker()
        with unittest.mock.patch(
            "market_service.substrate_worker.core.build_raw_window",
            return_value={"futures": {"order_book": {}}, "spot": {},
                          "coverage": {}},
        ):
            self._fire(w)
        self.assertIsNotNone(w.store.redis.latest)


import unittest.mock


class MultiSymbolTests(unittest.TestCase):
    def test_per_pair_groups_and_supervisor_keys(self):
        workers = build_workers(_FakeStore(_FakeRedis()),
                                symbols=["SOLUSDT", "BTCUSDT"],
                                names=["density", "tape"])
        self.assertEqual(len(workers), 4)
        groups = [w._group for w in workers]
        self.assertEqual(len(set(groups)), 4)
        sups = [w._supervisor_key for w in workers]
        self.assertEqual(len(set(sups)), 4)
        pairs = {(w.SUBSTRATE_NAME, w.symbol) for w in workers}
        self.assertEqual(pairs, {("density", "SOLUSDT"), ("density", "BTCUSDT"),
                                 ("tape", "SOLUSDT"), ("tape", "BTCUSDT")})


class HealthcheckTests(unittest.TestCase):
    def test_missing_heartbeats_reported(self):
        from market_service.substrate_worker.healthcheck import check_workers

        class _R:
            def __init__(self, keys):
                self._keys = keys

            async def get(self, key):
                return self._keys.get(key)

        class _S:
            def __init__(self, keys):
                self.redis = _R(keys)

            def substrate_supervisor_key(self, sub, sym):
                return f"k:{sub}:{sym}"

        workers = build_workers(_FakeStore(_FakeRedis()), symbols=["SOLUSDT"],
                                names=["density", "tape"])
        present = {w._supervisor_key for w in workers if w.SUBSTRATE_NAME == "density"}
        loop = asyncio.new_event_loop()
        try:
            missing = loop.run_until_complete(check_workers(
                _S({k: "{}" for k in present}), workers))
        finally:
            loop.close()
        self.assertEqual(missing, ["tape:SOLUSDT"])


class MigrationChainTests(unittest.TestCase):
    def test_migration_revision_chain_and_schema_block(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        mig = (root / "alembic" / "versions" / "0011_substrate_calculation.py").read_text()
        self.assertIn('revision = "0011_substrate_calculation"', mig)
        self.assertIn('down_revision = "0010_inference_hypothesis"', mig)
        self.assertIn("CREATE TABLE IF NOT EXISTS substrate_calculation", mig)
        self.assertIn("(symbol, substrate, computed_at DESC)", mig)
        schema = (root / "db" / "init" / "001_schema.sql").read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS substrate_calculation", schema)


class ParityGateTests(unittest.TestCase):
    """Worker.compute shared keys == composition sections, identical evidence."""

    @classmethod
    def setUpClass(cls):
        from market_service.calculations import composition as comp
        from market_service.calculations.substrates.tiers import TierConfig
        cls.comp = comp
        base = 1_700_000_000_000
        evidence: dict[str, Any] = {
            "observed_at_ms": base + 900_000,
            "fetch_window_ms": 900_000,
            "depth_levels": 20,
            "errors": [],
            "coverage": {},
            "spot": {
                "ticker_24h": None,
                "order_book": BOOK["spot"]["order_book"],
                "trades_raw": [],
                "trades_normalized": BOOK["spot"]["trades_normalized"],
            },
            "futures": {
                "ticker_24h": None,
                "order_book": BOOK["futures"]["order_book"],
                "trades_raw": [],
                "trades_normalized": BOOK["futures"]["trades_normalized"],
                "funding": {},
                "open_interest": {"open_interest": 1_000_000.0},
                "taker_buy_sell": [
                    {"buy_vol": 100.0 + i * 10.0, "sell_vol": 100.0,
                     "timestamp": base + i * 300_000} for i in range(3)
                ],
                "oi_history": [
                    {"sum_open_interest": 990_000.0 + i * 1_000.0,
                     "sum_open_interest_value": 148_000_000.0 + i * 148_000.0,
                     "timestamp": base + i * 300_000} for i in range(14)
                ],
                "top_ls": [{"longAccount": "0.55"}],
                "global_ls": [{"long_account": 0.48}],
                "klines": [
                    [base + i * 300_000, 140.0 + i * 0.5 - 0.2,
                     140.0 + i * 0.5 + 0.3, 140.0 + i * 0.5 - 0.4,
                     140.0 + i * 0.5, 100.0 + i] for i in range(20)
                ],
            },
        }
        cls.evidence = evidence
        cls.calc = comp.run_calculations(evidence, 20, 900)
        cls.anal = comp.run_analysis(evidence, cls.calc, depth=20,
                                     tier_config=TierConfig())

    def _worker(self, name):
        return WORKER_REGISTRY[name](_NoStore(), symbol="SOLUSDT")

    def test_tape_parity(self):
        out = self._worker("tape").compute(self.evidence, 20)
        calc = self.calc["calculations"]
        self.assertEqual(out["spot_flow"], calc["flow"]["spot_flow"])
        self.assertEqual(out["futures_flow"], calc["flow"]["futures_flow"])
        self.assertEqual(out["spot_bucketed_cvd"], calc["bucketed_cvd"]["spot_bucketed_cvd"])
        self.assertEqual(out["futures_bucketed_cvd"],
                         calc["bucketed_cvd"]["futures_bucketed_cvd"])
        self.assertEqual(out["cvd_series_corr"], calc["correlation"])
        self.assertEqual(out["spot_turnover_share"],
                         calc["turnover"]["spot_turnover_share"])
        self.assertEqual(out["fut_microprice_skew_bps"],
                         calc["orderbook"]["fut_microprice_skew_bps"])

    def test_density_parity(self):
        out = self._worker("density").compute(self.evidence, 20)
        ob = self.calc["calculations"]["orderbook"]
        for key in ("spot_keystone", "fut_top_density_bids",
                    "fut_significant_levels", "keystone_trade_intensity"):
            self.assertEqual(out[key], ob[key], f"density/{key} mismatch")
        for key in ("keystone", "bid", "ask", "tight", "wide"):
            self.assertEqual(out["fut_keystone"][key], ob["fut_keystone"][key])

    def test_ladders_migration_parity(self):
        out = self._worker("ladders").compute(self.evidence, 20)
        ob = self.calc["calculations"]["orderbook"]
        self.assertEqual(out["fut_absorption_ladder"], ob["fut_absorption_ladder"])
        self.assertEqual(out["ask_wall_ladder"], ob["ask_wall_ladder"])
        mig = self._worker("migration").compute(self.evidence, 20)
        self.assertEqual(mig["hourly_keystone_migration"],
                         ob["hourly_keystone_migration"])

    def test_anchors_tiers_parity(self):
        wm = self.anal["analysis"]["wall_migration"]
        anchors = self._worker("anchors").compute(self.evidence, 20)
        self.assertEqual(anchors["aggregation"], wm["round_anchors"])
        tiers = self._worker("tiers").compute(self.evidence, 20)
        self.assertEqual(tiers["tiers"], wm["tiers"])
        self.assertEqual(tiers["tier_balance"], wm["tier_balance"])

    def test_volume_profile_technicals_large_print_parity(self):
        calc = self.calc["calculations"]
        vp = self._worker("volume_profile").compute(self.evidence, 20)
        self.assertEqual(vp["fut_volume_profile"]["buckets"],
                         calc["volume_profile"]["buckets"])
        self.assertEqual(vp["fut_volume_profile"]["summary"],
                         calc["volume_profile"]["summary"])
        tech = self._worker("technicals").compute(self.evidence, 20)
        self.assertEqual(tech["emas"], calc["technical"]["emas"])
        self.assertEqual(tech["emas_source"], calc["technical"]["emas_source"])
        lp = self._worker("large_print").compute(self.evidence, 20)
        self.assertEqual(lp["tiered_large_flow"], calc["technical"]["tiered_large_flow"])
        self.assertEqual(lp["seller_aggression"], calc["technical"]["seller_aggression"])

    def test_delta_oi_parity(self):
        delta = self._worker("delta").compute(self.evidence, 20)
        adapted = self.anal["analysis"]["delta"]
        for key in ("delta", "delta_raw", "wall_imbalance", "flow_alignment",
                    "tbr_last_pct", "tbr_3avg_pct", "n_bands", "bands", "range"):
            self.assertEqual(delta[key], adapted[key], f"delta/{key} mismatch")
        self.assertEqual(delta["state"], adapted["verdict"])
        oi = self._worker("oi").compute(self.evidence, 20)
        adapted_oi = self.anal["analysis"]["open_interest"]
        for key in ("walls", "weighted_contracts", "inflow_outflow",
                    "implied_value", "raw_open_interest", "bars_available"):
            self.assertEqual(oi[key], adapted_oi[key], f"oi/{key} mismatch")

    def test_signals_parity(self):
        flow = self.calc["calculations"]["flow"]
        evidence = dict(self.evidence)
        evidence["substrate_dependencies"] = {"tape": {"output": {
            "spot_flow": {"buy_share": flow["spot_flow"]["buy_share"],
                          "obi": flow["spot_flow"]["obi"]},
            "futures_flow": {"buy_share": flow["futures_flow"]["buy_share"]},
        }}}
        evidence["own_last_output"] = {}
        out = self._worker("signals").compute(evidence, 20)
        self.assertEqual(out["signals"], self.calc["calculations"]["signals"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
