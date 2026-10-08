"""Phase A/B/C rollout tests — horizon workers, PG fold rebuild, budget guard.

Continuation of tests/test_horizon_retention.py:

B — PG-first fold rebuild:
  - v2 payload fold summaries restore fold state (hwm + segments)
  - the raw backfill then folds ONLY the ledger-to-now delta (dedupe-free merge)
  - v1 / absent / malformed PG rows fall back to raw-only rebuild

C — raw-retention budget guard:
  - ``redis_maxmemory_bytes`` accessor (capped / unlimited / unreadable)
  - boot check logs the budget estimate and warns beyond the warn fraction

A — horizon rollout (two interpretation patterns):
  - tape_worker: fold-native per-horizon CVD/buy-share + horizon-tagged
    band-cross predicates
  - technicals_worker: fold-candle EMA/trend/ATR horizons (ema9 leg — the
    only EMA that resolves on the fold rings)
  - density_worker: window-native 4h keystone intensity
"""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from market_service.runtime.horizons import (
    fold_candles, fold_trades, new_fold_state,
)
from market_service.substrate_worker.contracts import CadenceProfile
from market_service.substrate_worker.core import fire as fire_module
from market_service.substrate_worker.core.fire import FireMixin


def _utc_epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _trade(tid: int, ts_ms: int, price: float = 100.0, qty: float = 1.0,
           maker: bool = False) -> dict[str, Any]:
    return {"id": tid, "ts": ts_ms, "price": price, "qty": qty,
            "is_buyer_maker": maker}


# ---------------------------------------------------------------------------
# B — PG fold rebuild
# ---------------------------------------------------------------------------


class _FakePgStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None,
                 exc: Exception | None = None):
        self._rows = rows or []
        self._exc = exc

    async def read_substrate_history(self, symbol, substrate, limit=100):
        if self._exc:
            raise self._exc
        return self._rows


def _bare_migration(pg_store: Any = None) -> Any:
    from market_service.substrate_worker.migration_worker import MigrationWorker
    worker = MigrationWorker.__new__(MigrationWorker)
    worker.SUBSTRATE_NAME = "migration"
    worker.symbol = "SOLUSDT"
    worker.horizons = ("1h", "4h")
    worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
    worker._horizons_backfilled = False
    worker._horizons_rebuilt_from = None
    worker._plane = "substrate"
    worker.pg_store = pg_store
    worker.store = None  # bare seam: _backfill reads through the patched builder
    worker._last_error = None
    return worker


def _pg_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ledger_id": 1, "payload": payload}


def _v2_payload_with_folds(folds: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "computed_at_ms": _utc_epoch_ms("2026-09-08T12:00:00+00:00"),
        "horizons": {hz: {"output": {}, "fold": fold} for hz, fold in folds.items()},
    }


class PGRestoreTests(unittest.TestCase):
    def test_v2_payload_restores_folds_and_folds_only_delta(self):
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        # PG fold: one segment, hwm=5 (trades 1..5 already counted).
        pg_fold = {
            "horizon": "4h", "hwm": {"spot": -1, "futures": 5},
            "late_dropped": 0, "duplicates_skipped": 0,
            "segments_total": 1, "__segments_truncated__": False,
            "segments": [{"segment_start_ms": t, "bucket_start_ms": t,
                          "trade_count": 5, "notional": 500.0,
                          "buy_notional": 500.0, "sell_notional": 0.0,
                          "first_trade_ms": t, "last_trade_ms": t,
                          "sealed": False, "first_px": 100.0, "last_px": 100.0,
                          "high_px": 100.0, "low_px": 100.0,
                          "venues": {"futures": {"trade_count": 5,
                                                  "notional": 500.0,
                                                  "buy_notional": 500.0,
                                                  "sell_notional": 0.0}}}],
        }
        worker = _bare_migration(_FakePgStore(
            [_pg_row(_v2_payload_with_folds({"4h": pg_fold}))]))
        restored = asyncio.run(worker._restore_horizons_from_pg())
        self.assertTrue(restored)
        self.assertEqual(worker._horizon_folds["4h"]["hwm"]["futures"], 5)
        self.assertEqual(len(worker._horizon_folds["4h"]["segments"]), 1)

        # Raw backfill over a window whose trades overlap (3..5) and extend
        # (6..8): only 6..8 may fold — the dedupe makes the merge free.
        raw_window = {"futures": {"trades_normalized": [
            _trade(i, t + i * 1000, 100.0, 2.0) for i in range(3, 9)
        ]}}
        async def _fake_build(redis, symbol, minutes):
            return raw_window
        with patch.object(fire_module, "build_raw_window", _fake_build):
            asyncio.run(worker._backfill_horizons())
        self.assertTrue(worker._horizons_backfilled)
        self.assertEqual(worker._horizons_rebuilt_from, "pg")
        seg = worker._horizon_folds["4h"]["segments"][-1]
        # 5 (PG) + 3 (delta 6,7,8) = 8 trades in the active segment.
        self.assertEqual(seg["trade_count"], 8)
        self.assertEqual(worker._horizon_folds["4h"]["hwm"]["futures"], 8)

    def test_v1_pg_row_falls_back_to_raw_only(self):
        v1 = {"schema_version": 1, "horizons": {}}
        worker = _bare_migration(_FakePgStore([_pg_row(v1)]))
        restored = asyncio.run(worker._restore_horizons_from_pg())
        self.assertFalse(restored)
        raw_window = {"futures": {"trades_normalized": []}}
        async def _fake_build(redis, symbol, minutes):
            return raw_window
        with patch.object(fire_module, "build_raw_window", _fake_build):
            asyncio.run(worker._backfill_horizons())
        self.assertEqual(worker._horizons_rebuilt_from, "raw")

    def test_pg_absent_and_error_paths(self):
        worker = _bare_migration(None)
        self.assertFalse(asyncio.run(worker._restore_horizons_from_pg()))
        worker = _bare_migration(_FakePgStore(exc=RuntimeError("pg down")))
        self.assertFalse(asyncio.run(worker._restore_horizons_from_pg()))
        # Empty PG history.
        worker = _bare_migration(_FakePgStore([]))
        self.assertFalse(asyncio.run(worker._restore_horizons_from_pg()))

    def test_freshness_reports_horizon_rebuild_source(self):
        worker = _bare_migration(None)
        worker._horizons_rebuilt_from = "pg"
        worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
        worker.window_minutes = 15
        out = FireMixin._freshness(worker, {"coverage": {}}, high_water=None)
        self.assertEqual(out["horizons_rebuilt_from"], "pg")
        self.assertEqual(out["horizon_segments"]["4h"], 0)


# ---------------------------------------------------------------------------
# C — raw-retention budget guard
# ---------------------------------------------------------------------------


class _MaxmemFakeRedis:
    def __init__(self, value: str | None, exc: Exception | None = None):
        self._value = value
        self._exc = exc

    async def config_get(self, name):
        if self._exc:
            raise self._exc
        if self._value is None:
            return {}
        return {"maxmemory": self._value}


def _maxmem_store(redis: Any) -> Any:
    from market_service.runtime.redis_store import RedisRuntimeStore
    store = RedisRuntimeStore.__new__(RedisRuntimeStore)
    store.redis = redis
    return store


class MaxmemoryGuardTests(unittest.TestCase):
    def test_accessor_capped_unreadable_and_unlimited(self):
        store = _maxmem_store(_MaxmemFakeRedis("3000000000"))
        self.assertEqual(asyncio.run(store.redis_maxmemory_bytes()), 3_000_000_000)
        store = _maxmem_store(_MaxmemFakeRedis("0"))  # unlimited
        self.assertIsNone(asyncio.run(store.redis_maxmemory_bytes()))
        store = _maxmem_store(_MaxmemFakeRedis(None, exc=RuntimeError("no")))
        self.assertIsNone(asyncio.run(store.redis_maxmemory_bytes()))

    def test_boot_check_within_budget_logs_info_only(self):
        from market_service.poller import _raw_retention_budget_check
        settings = SimpleNamespace(
            redis_raw_guardrail_maxlen=20_000, redis_stream_maxlen=1200,
            redis_raw_retention_ms=43_200_000,
        )
        store = _maxmem_store(_MaxmemFakeRedis("10" * 12))  # 100 GB cap
        with self.assertLogs("market_service.poller", level="INFO") as logs:
            asyncio.run(_raw_retention_budget_check(store, settings, ("SOLUSDT",)))
        joined = "\n".join(logs.output)
        self.assertIn("raw-retention budget", joined)
        self.assertFalse(any("DEGRADED" in line for line in logs.output))

    def test_boot_check_warns_when_over_fraction(self):
        from market_service.poller import _raw_retention_budget_check
        # 3 symbols × 20 000 × 60KB ≈ 3.4GB — cap at 4GB → >70% → warning.
        settings = SimpleNamespace(
            redis_raw_guardrail_maxlen=20_000, redis_stream_maxlen=1200,
            redis_raw_retention_ms=43_200_000,
        )
        store = _maxmem_store(_MaxmemFakeRedis(str(4_000_000_000)))
        with self.assertLogs("market_service.poller", level="WARNING") as logs:
            asyncio.run(_raw_retention_budget_check(
                store, settings, ("SOLUSDT", "ETHUSDT", "BTCUSDT")))
        self.assertTrue(any("DEGRADED" in line for line in logs.output))
        self.assertTrue(any("RAW_RETENTION_MS" in line for line in logs.output))

    def test_boot_check_skips_when_unlimited(self):
        from market_service.poller import _raw_retention_budget_check
        settings = SimpleNamespace(
            redis_raw_guardrail_maxlen=20_000, redis_stream_maxlen=1200,
            redis_raw_retention_ms=43_200_000,
        )
        store = _maxmem_store(_MaxmemFakeRedis("0"))
        with self.assertLogs("market_service.poller", level="INFO") as logs:
            asyncio.run(_raw_retention_budget_check(store, settings, ("SOLUSDT",)))
        self.assertTrue(any("skipped" in line for line in logs.output))


# ---------------------------------------------------------------------------
# A — horizon rollout workers
# ---------------------------------------------------------------------------


class TapeHorizonTests(unittest.TestCase):
    def _worker(self) -> Any:
        from market_service.substrate_worker.tape_worker import TapeWorker
        worker = TapeWorker.__new__(TapeWorker)
        worker.horizons = ("1h", "4h")
        worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
        return worker

    def test_fold_native_horizon_outputs(self):
        worker = self._worker()
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        trades = [
            _trade(1, t, 100.0, 2.0, maker=False),   # futures buy
            _trade(2, t, 100.0, 1.0, maker=True),    # futures sell
        ]
        spot_trades = [_trade(3, t, 100.0, 1.0, maker=False)]
        fold_trades(worker._horizon_folds["4h"], trades, "futures")
        fold_trades(worker._horizon_folds["4h"], spot_trades, "spot")
        evidence = {"spot": {"trades_normalized": []},
                    "futures": {"trades_normalized": []},
                    "fetch_window_ms": 900_000,
                    "horizons": {}}
        out = worker.compute(evidence, 20)
        hz = out["horizons"]["4h"]
        self.assertEqual(hz["cvd_by_venue"]["futures"], 100.0)
        self.assertEqual(hz["cvd_by_venue"]["spot"], 100.0)
        self.assertAlmostEqual(hz["buy_share"]["futures"], 2 / 3, places=5)
        self.assertAlmostEqual(hz["buy_share"]["spot"], 1.0, places=5)

    def test_fold_native_band_cross_predicate(self):
        worker = self._worker()
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        # Previous fold: 40% buy share (below the 0.45 band).
        prev_fold = {
            "horizon": "4h", "hwm": {"futures": 10}, "segments": [
                {"segment_start_ms": t, "bucket_start_ms": t,
                 "trade_count": 10, "notional": 100.0, "buy_notional": 40.0,
                 "sell_notional": 60.0, "first_trade_ms": t,
                 "last_trade_ms": t, "sealed": True,
                 "first_px": 100.0, "last_px": 100.0, "high_px": 100.0,
                 "low_px": 100.0,
                 "venues": {"futures": {"trade_count": 10, "notional": 100.0,
                                         "buy_notional": 40.0,
                                         "sell_notional": 60.0}}}],
        }
        # Current fold: 70% buy share (crossed the 0.55 band).
        cur_trades = [
            _trade(11, t + 60_000, 100.0, 7.0, maker=False),
            _trade(12, t + 60_000, 100.0, 3.0, maker=True),
        ]
        fold_trades(worker._horizon_folds["4h"], cur_trades, "futures")
        window = {"futures": {"trades_normalized": [_trade(1, t)]},
                  "spot": {"trades_normalized": []}}
        last_state = {"horizons": {"4h": {"output": {}, "fold": prev_fold}}}
        decision = worker.probe(window, last_state, t + 120_000)
        self.assertTrue(decision.fired)
        self.assertIn("4h:buy_share_cross:futures", decision.predicates)
        pred = decision.predicates["4h:buy_share_cross:futures"]
        self.assertEqual(pred["horizon"], "4h")
        self.assertEqual(pred["bands"], [0.55, 0.45])


class TechnicalsHorizonTests(unittest.TestCase):
    def _worker(self) -> Any:
        from market_service.substrate_worker.technicals_worker import TechnicalsWorker
        worker = TechnicalsWorker.__new__(TechnicalsWorker)
        worker.horizons = ("1h", "4h")
        worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
        worker.FOLD_ATR_PERIOD = TechnicalsWorker.FOLD_ATR_PERIOD
        return worker

    def _fold_rising(self, worker: Any) -> None:
        """≥9 rising segments so ema9 resolves on the ring."""
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        for i in range(9):
            trades = [_trade(i + 1, t + i * 300_000, 100.0 + i, 1.0)]
            fold_trades(worker._horizon_folds["1h"], trades, "futures")
            fold_trades(worker._horizon_folds["4h"], trades, "futures")

    def test_fold_candle_horizon_outputs(self):
        worker = self._worker()
        self._fold_rising(worker)
        # Base path needs klines; provide a minimal 2-row series.
        klines = [[0, 100.0, 101.0, 99.0, 100.5, 1.0],
                  [1, 100.5, 101.5, 100.0, 101.0, 1.0]]
        evidence = {"futures": {"klines": klines}}
        out = worker.compute(evidence, 20)
        hz = out["horizons"]["1h"]
        self.assertEqual(hz["source"], "fold_candles")
        self.assertEqual(hz["candle_count"], 9)
        self.assertIn("ema9", hz["ema_position"])
        self.assertEqual(hz["ema_position"]["ema9"], "ABOVE")
        self.assertIsNotNone(hz["atr_pct"])

    def test_horizon_ema_flip_predicate(self):
        worker = self._worker()
        self._fold_rising(worker)
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        # Rising tape: price ABOVE ema9 now; previous state had it BELOW.
        prev_state = {"horizons": {"1h": {"output": {
            "ema_position": {"ema9": "BELOW"}}}}}
        klines = [[0, 100.0, 101.0, 99.0, 100.5, 1.0]]
        window = {"futures": {"klines": klines}}
        decision = worker.probe(window, prev_state, t + 300_000)
        self.assertTrue(decision.fired)
        self.assertIn("1h:ema_position_flip", decision.predicates)
        pred = decision.predicates["1h:ema_position_flip"]
        self.assertEqual(pred["leg"], "ema9")
        self.assertEqual(pred["to"], "ABOVE")


class DensityHorizonTests(unittest.TestCase):
    def _worker(self) -> Any:
        from market_service.substrate_worker.density_worker import DensityWorker
        worker = DensityWorker.__new__(DensityWorker)
        worker.horizons = ("4h",)
        worker._horizon_folds = {hz: new_fold_state(hz) for hz in worker.horizons}
        return worker

    def test_window_native_4h_intensity(self):
        from market_service.substrate_worker.density_worker import (
            _pairs,
        )
        worker = self._worker()
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        # Book around 100 so find_keystone produces tight/wide bands.
        book = {"bids": [[99.95, 900.0], [99.90, 800.0], [99.85, 700.0]],
                "asks": [[100.10, 600.0], [100.15, 500.0]]}
        evidence = {
            "spot": {"order_book": {}, "trades_normalized": []},
            "futures": {"order_book": book, "trades_normalized": []},
            "horizons": {"4h": {"futures": {"trades_normalized": [
                _trade(1, t, 99.95, 5.0, maker=False),
                _trade(2, t, 100.10, 3.0, maker=True),
            ]}}},
        }
        out = worker.compute(evidence, 20)
        self.assertIn("horizons", out)
        hz = out["horizons"]["4h"]
        self.assertIn("keystone_trade_intensity", hz)
        self.assertEqual(hz["trade_count"], 2)
        # The base output is untouched by the horizon block.
        self.assertNotIn("horizons", out["fut_keystone"])

    def test_no_4h_window_no_horizon_block(self):
        worker = self._worker()
        book = {"bids": [[99.95, 900.0]], "asks": [[100.10, 600.0]]}
        evidence = {
            "spot": {"order_book": {}, "trades_normalized": []},
            "futures": {"order_book": book, "trades_normalized": []},
        }
        out = worker.compute(evidence, 20)
        self.assertNotIn("horizons", out)

    def test_declaration(self):
        from market_service.substrate_worker.density_worker import DensityWorker
        from market_service.substrate_worker.tape_worker import TapeWorker
        from market_service.substrate_worker.technicals_worker import TechnicalsWorker
        self.assertEqual(DensityWorker.HORIZONS, ("4h",))
        self.assertEqual(TapeWorker.HORIZONS, ("1h", "4h"))
        self.assertEqual(TechnicalsWorker.HORIZONS, ("1h", "4h"))
        for cls in (DensityWorker, TapeWorker, TechnicalsWorker):
            profile: CadenceProfile = cls.CADENCE
            self.assertEqual(profile.horizons, cls.HORIZONS)
            # No horizon heartbeats at a bound below its own horizon span
            # (except the 1h worker's 10-minute warm bound).
            for hz, seconds in profile.horizon_staleness_s:
                if hz == "4h":
                    self.assertGreaterEqual(seconds, 4 * 3600)


if __name__ == "__main__":
    unittest.main()