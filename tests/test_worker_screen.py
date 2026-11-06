"""Stage-one screen contract: screen quiet => full probe quiet (zero miss).

The six point-in-time workers (anchors, ladders, tiers, density, oi,
signals) gate the heavy window build on screen(): the SAME probe run over
latest-only state. Since their probe inputs ARE latest state (book, OI,
tape:latest), screen and probe read identical inputs.

Pins: quiet screens skip _cached_evidence entirely; screen agrees with
probe over randomized books (shaping-drift tripwire); history workers keep
the default True screen (their probes accumulate window history).
"""

from __future__ import annotations

import asyncio
import random
import unittest
from typing import Any

from market_service.substrate_worker.anchors_worker import AnchorsWorker
from market_service.substrate_worker.density_worker import DensityWorker
from market_service.substrate_worker.ladders_worker import LaddersWorker
from market_service.substrate_worker.oi_worker import OiWorker
from market_service.substrate_worker.signals_worker import SignalsWorker
from market_service.substrate_worker.tiers_worker import TiersWorker
from market_service.substrate_worker.delta_worker import DeltaWorker
from market_service.substrate_worker.tape_worker import TapeWorker
from market_service.substrate_worker.technicals_worker import TechnicalsWorker
from market_service.substrate_worker.migration_worker import MigrationWorker
from market_service.substrate_worker.volume_profile_worker import VolumeProfileWorker
from market_service.substrate_worker.large_print_worker import LargePrintWorker


class _DummyRedis:
    async def script_load(self, source):
        return "sha"

    async def evalsha(self, sha, numkeys, *args):
        return "1"


class _ScreenStore:
    """Minimal store: raw.latest + own latest + optional tape latest."""

    def __init__(self, raw_latest: dict, own_latest: dict | None = None,
                 tape_latest: dict | None = None):
        self._raw = raw_latest
        self._own = own_latest
        self._tape = tape_latest
        self.redis = _DummyRedis()

    def raw_stream(self, symbol):
        return f"s:raw:{symbol}"

    def substrate_supervisor_key(self, substrate, symbol):
        return f"s:sup:{substrate}:{symbol}"

    def analysis_supervisor_key(self, substrate, symbol):
        return f"s:asup:{substrate}:{symbol}"

    async def read(self, namespace: str, *, ids: dict | None = None, **kw):
        if namespace == "raw.latest":
            return self._raw
        if namespace.startswith("substrate."):
            name = namespace.split(".", 1)[1]
            if name == "tape":
                return self._tape
            return self._own
        if namespace.startswith("analysis."):
            return self._own
        return None

    async def publish(self, namespace, payload, *, ids=None, **kw):
        return "9-1"


def _book(mid: float, spread: float = 0.05, depth: int = 12, seed: int = 0):
    rng = random.Random(seed)
    bids = [[round(mid - spread - i * 0.05 - rng.random() * 0.01, 4),
             round(10 + rng.random() * 90, 2)] for i in range(depth)]
    asks = [[round(mid + spread + i * 0.05 + rng.random() * 0.01, 4),
             round(10 + rng.random() * 90, 2)] for i in range(depth)]
    return {"bids": bids, "asks": asks}


def _latest(mid: float, seed: int = 0, oi: float = 7_900_000.0) -> dict:
    return {
        "observed_at_ms": 1_700_000_000_000,
        "spot": {"order_book": _book(mid, seed=seed)},
        "futures": {"order_book": _book(mid, seed=seed + 1),
                    "open_interest": {"open_interest": oi},
                    "trades_normalized": []},
    }


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk(cls, store, **kw):
    return cls(store, symbol="SOLUSDT", window_minutes=15,
               cooldown_s=0, staleness_s=3_600, **kw)


class ScreenSkipsBuildTests(unittest.TestCase):
    """Quiet screen => decide returns before _cached_evidence."""

    def test_oi_quiet_screen_skips_window_build(self):
        now = 1_700_000_000_000
        latest = _latest(150.0, oi=7_900_000.0)
        last = {"schema_version": 2, "computed_at_ms": now,
                "output": {"raw_open_interest": 7_900_000.0}, "horizons": {}}
        store = _ScreenStore(latest, last)
        worker = _mk(OiWorker, store)

        async def _boom(now_ms):
            raise AssertionError("window must not build on quiet screen")
        worker._cached_evidence = _boom  # type: ignore[method-assign]
        rows = [{"id": f"{now}-0", "fields": {}}]
        _run(worker.decide(rows, now))
        self.assertEqual(worker._fired, 0)
        self.assertIn("screen", worker._last_dormant_reason or "")

    def test_density_quiet_screen_skips_window_build(self):
        now = 1_700_000_000_000
        from market_service.calculations.substrates.density import find_keystone
        from market_service.substrate_worker.density_worker import _mid, _pairs
        book = _latest(150.0)["futures"]["order_book"]
        bids = _pairs(book.get("bids"))
        asks = _pairs(book.get("asks"))
        mid = _mid(bids, asks)
        assert mid is not None
        kz = find_keystone(bids, mid, 0.20, -0.30, -0.05, None)
        # Last state carries THIS book's keystone: zero displacement => quiet.
        last = {"schema_version": 2, "computed_at_ms": now,
                "output": {"fut_keystone": {"keystone": kz["keystone"], "mid": mid},
                           "fut_top_density_bids": []}, "horizons": {}}
        store = _ScreenStore(_latest(150.0), last)
        worker = _mk(DensityWorker, store)

        async def _boom(now_ms):
            raise AssertionError("window must not build on quiet screen")
        worker._cached_evidence = _boom  # type: ignore[method-assign]
        rows = [{"id": f"{now}-0", "fields": {}}]
        _run(worker.decide(rows, now))
        self.assertEqual(worker._fired, 0)


class ScreenProbeAgreementTests(unittest.TestCase):
    """screen(latest) == probe(window-from-same-latest) over random books."""

    WORKERS = [AnchorsWorker, LaddersWorker, TiersWorker, DensityWorker, OiWorker]

    def test_agreement_random_books(self):
        now = 1_700_000_000_000
        for cls in self.WORKERS:
            for seed in range(30):
                mid = 150.0 + (seed % 7) * 0.1
                latest = _latest(mid, seed=seed)
                book = latest["futures"]["order_book"]
                last = {"schema_version": 2, "computed_at_ms": now,
                        "output": {"raw_open_interest": 7_900_000.0,
                                   "fut_keystone": {"keystone": 149.5, "mid": 150.0},
                                   "fut_top_density_bids": [],
                                   "anchors": [{"level": 150.0}],
                                   "aggregation": {"anchors": []},
                                   "tier_balance": {"verdict": "balanced", "ratio": 1.0},
                                   "fut_absorption_ladder": [],
                                   "reference": {"mid": 150.0}},
                        "horizons": {}}
                worker = _mk(cls, _ScreenStore(latest, last))
                window = {"futures": {"order_book": book,
                                     "open_interest": {"open_interest": 7_900_000.0}},
                          "spot": {"order_book": _book(mid, seed=seed + 99)}}
                expected = worker.probe(window, last, now).fired
                got = _run(worker.screen(latest, last, now))
                self.assertEqual(got, expected, f"{cls.SUBSTRATE_NAME} seed={seed}")

    def test_signals_screen_uses_tape_dep(self):
        now = 1_700_000_000_000
        tape = {"available": True, "computed_at_ms": now,
                "output": {"spot_flow": {"buy_share": 0.9, "obi": 0.5},
                           "futures_flow": {"buy_share": 0.1, "obi": 0.5}}}
        latest = _latest(150.0)
        last = {"schema_version": 2, "computed_at_ms": now,
                "output": {"snapshot": {"spot_buy_share": 0.5, "futures_buy_share": 0.5,
                                        "spot_obi_top_n": 0.0, "open_interest": 7_900_000.0}},
                "horizons": {}}
        worker = _mk(SignalsWorker, _ScreenStore(latest, last, tape))
        self.assertTrue(_run(worker.screen(latest, last, now)))


class HistoryWorkersDefaultScreenTests(unittest.TestCase):
    def test_history_workers_keep_default_true(self):
        store = _ScreenStore(_latest(150.0), None)
        for cls in (DeltaWorker, TapeWorker, TechnicalsWorker,
                    MigrationWorker, VolumeProfileWorker, LargePrintWorker):
            worker = _mk(cls, store)
            self.assertTrue(_run(worker.screen({}, None, 0)),
                            f"{cls.SUBSTRATE_NAME} must keep default screen")
