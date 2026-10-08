"""Phase H1 — time-horizon retention + worker cadence horizon tests.

Covers the two-part fix from docs/HORIZON_RETENTION_SPEC.md:

Part 1 (storage):
  - raw stream time-based retention (MINID trim in the publish Lua)
  - slimmed stream bodies (book/tickers latest-only)
  - ``read_raw_oldest_ms`` bounds accessor
  - coverage honesty (``horizon_degraded`` when retention can't serve the window)

Part 2 (cadence):
  - IST-aligned horizon bucket identity (15m/1h/4h at IST day boundaries)
  - fold primitives: monotonic-id dedupe, segment sealing, late-drop
  - fold summary round-trip (bounded, restorable)
  - schema v2 payloads + v1 back-compat read
  - horizon-tagged rollover (IST-aligned bar_4h)
  - pilot migration worker: per-horizon probe predicates + compute outputs
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from datetime import UTC, datetime
from typing import Any

from market_service.runtime.horizons import (
    HORIZON_PERIOD_MS, IST_OFFSET_MS, bucket_start_ms, fold_candles,
    fold_snapshot, fold_state_summary, fold_trades, horizon_bucket,
    horizon_minutes, new_fold_state, restore_fold_state,
)
from market_service.runtime.raw_window import build_raw_window
from market_service.runtime.redis_store import RedisRuntimeStore, _slim_stream_body
from market_service.substrate_worker.contracts import (
    ROLLOVER_PERIOD_MS, CadenceProfile, SubstrateStatePayload, TriggerDecision,
)


def _utc_epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _trade(tid: int, ts_ms: int, price: float = 100.0, qty: float = 1.0,
           maker: bool = False) -> dict[str, Any]:
    return {"id": tid, "ts": ts_ms, "price": price, "qty": qty,
            "is_buyer_maker": maker}


# ---------------------------------------------------------------------------
# Part 2 — IST alignment
# ---------------------------------------------------------------------------


class ISTAlignmentTests(unittest.TestCase):
    """4h buckets start at IST midnight boundaries (UTC 18:30 etc.)."""

    def test_4h_bucket_identity_at_ist_midnight(self):
        # UTC 2026-09-08T18:30:00Z == IST 2026-09-09T00:00:00+05:30 →
        # must be the exact START of a 4h bucket.
        t = _utc_epoch_ms("2026-09-08T18:30:00+00:00")
        self.assertEqual(horizon_bucket("4h", t), t)

    def test_4h_bucket_one_ms_before_boundary(self):
        t = _utc_epoch_ms("2026-09-08T18:29:59.999+00:00")
        bucket = horizon_bucket("4h", t)
        self.assertEqual(bucket, t + 1 - HORIZON_PERIOD_MS["4h"])

    def test_4h_bucket_mid_hour(self):
        start = _utc_epoch_ms("2026-09-08T18:30:00+00:00")
        t = start + 7_200_000  # 2h into the bucket
        self.assertEqual(horizon_bucket("4h", t), start)

    def test_ist_offset_value(self):
        self.assertEqual(IST_OFFSET_MS, 5 * 3_600_000 + 1_800_000)

    def test_15m_alignment_identity_and_1h_ist_hour_boundary(self):
        # 15m: IST offset is an exact multiple → IST-aligned grid == epoch grid.
        t = 1_783_600_000_000
        self.assertEqual(horizon_bucket("15m", t), bucket_start_ms(t, HORIZON_PERIOD_MS["15m"]))
        # 1h: IST-clock hours → UTC 19:30 (== IST 01:00) must be an exact
        # 1h bucket start (30-min offset from epoch hours, by design).
        t_ist_hour = _utc_epoch_ms("2026-09-08T19:30:00+00:00")
        self.assertEqual(horizon_bucket("1h", t_ist_hour), t_ist_hour)

    def test_horizon_minutes(self):
        self.assertEqual(horizon_minutes("15m"), 15)
        self.assertEqual(horizon_minutes("1h"), 60)
        self.assertEqual(horizon_minutes("4h"), 240)

    def test_unknown_horizon_rejected(self):
        with self.assertRaises(ValueError):
            new_fold_state("2h")


# ---------------------------------------------------------------------------
# Part 2 — fold primitives
# ---------------------------------------------------------------------------


class FoldTests(unittest.TestCase):
    def test_fold_dedupes_overlapping_snapshots(self):
        """The same trade arriving in two consecutive snapshots folds ONCE."""
        state = new_fold_state("1h")
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        trades = [_trade(1, t, 100.0, 2.0), _trade(2, t + 1, 100.0, 3.0)]
        self.assertEqual(fold_trades(state, trades, "futures"), 2)
        # A snapshot repeating the same rolling window folds nothing new.
        self.assertEqual(
            fold_snapshot(state, {"futures": {"trades_normalized": trades}}, "futures"),
            0)
        self.assertEqual(state["duplicates_skipped"], 2)
        self.assertEqual(state["segments"][-1]["trade_count"], 2)

    def test_fold_seals_older_segments(self):
        state = new_fold_state("1h")  # 5m segments
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        fold_trades(state, [_trade(1, t, 100.0, 1.0)], "futures")
        fold_trades(state, [_trade(2, t + 6 * 60_000, 100.0, 1.0)], "futures")
        segs = state["segments"]
        self.assertEqual(len(segs), 2)
        self.assertTrue(segs[0]["sealed"])
        self.assertFalse(segs[-1]["sealed"])

    def test_fold_late_data_dropped(self):
        """Out-of-order (late) trades never mutate a sealed segment."""
        state = new_fold_state("1h")
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        fold_trades(state, [_trade(1, t, 100.0, 1.0)], "futures")
        fold_trades(state, [_trade(2, t + 6 * 60_000, 100.0, 1.0)], "futures")
        late = _trade(3, t - 1, 999.0, 999.0)  # newer id, OLDER ts
        self.assertEqual(fold_trades(state, [late], "futures"), 0)
        self.assertEqual(state["late_dropped"], 1)
        self.assertNotIn(999.0, [s["notional"] for s in state["segments"]])

    def test_fold_cvd_direction(self):
        state = new_fold_state("15m")
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        fold_trades(state, [
            _trade(1, t, 100.0, 2.0, maker=False),  # taker buy
            _trade(2, t, 100.0, 1.0, maker=True),   # taker sell
        ], "futures")
        seg = state["segments"][-1]
        self.assertEqual(seg["buy_notional"], 200.0)
        self.assertEqual(seg["sell_notional"], 100.0)
        self.assertEqual(seg["notional"], 300.0)

    def test_fold_bucket_identity_ist_aligned(self):
        """A trade AT IST midnight lands in a segment whose 4h-bucket parent
        starts at the IST boundary."""
        state = new_fold_state("4h")
        t = _utc_epoch_ms("2026-09-08T18:30:00+00:00")  # IST midnight
        fold_trades(state, [_trade(1, t, 100.0, 1.0)], "futures")
        self.assertEqual(state["segments"][-1]["bucket_start_ms"], t)

    def test_summary_round_trip_bounded(self):
        state = new_fold_state("15m")
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        for i in range(10):  # 10 segments spanning 50 minutes
            fold_trades(state, [_trade(i + 1, t + i * 6 * 60_000, 100.0, 1.0)],
                        "futures")
        # Phase A trailing prune: the ring keeps only segments inside the
        # trailing 15m horizon (newest at t+54m → cutoff t+39m). Segment
        # starts are 5m-GRID aligned, not trade-relative.
        self.assertEqual(len(state["segments"]), 3)
        self.assertEqual(
            [s["segment_start_ms"] for s in state["segments"]],
            [((t + i * 360_000) // 300_000) * 300_000 for i in (7, 8, 9)],
        )
        summary = fold_state_summary(state, segment_cap=4)
        self.assertEqual(summary["segments_total"], 3)
        self.assertFalse(summary["__segments_truncated__"])
        restored = restore_fold_state(summary)
        self.assertEqual(restored["hwm"], state["hwm"])
        self.assertEqual(len(restored["segments"]), 3)
        # Restored state resumes dedupe at the high-water.
        self.assertEqual(
            fold_trades(restored, [_trade(5, t + 4 * 6 * 60_000)], "futures"), 0)
        self.assertEqual(
            fold_trades(restored, [_trade(99, t + 60 * 60_000)], "futures"), 1)

    def test_fold_prune_counts_bounded_fallback(self):
        """Even a burst of many segments within the horizon stays capped."""
        state = new_fold_state("15m")
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        # 5m segments over 15 minutes → 4 segments, all within the horizon.
        for i in range(4):
            fold_trades(state, [_trade(i + 1, t + i * 5 * 60_000, 100.0, 1.0)],
                        "futures")
        self.assertEqual(len(state["segments"]), 4)  # ring cap 5 not hit
        self.assertEqual(len(fold_candles(state)), 4)

    def test_fold_per_venue_highwater_independent(self):
        state = new_fold_state("1h")
        t = _utc_epoch_ms("2026-09-08T12:00:00+00:00")
        trades = [_trade(1, t, 100.0, 1.0)]
        fold_trades(state, trades, "spot")
        # Same ids on the futures venue fold independently.
        self.assertEqual(fold_trades(state, trades, "futures"), 1)


# ---------------------------------------------------------------------------
# Part 1 — slimming + retention trim
# ---------------------------------------------------------------------------


def _full_payload(ts_ms: int, book_levels: int = 200) -> dict[str, Any]:
    book = {
        "bids": [[148.20 - i * 0.01, 900.0 + i] for i in range(book_levels)],
        "asks": [[148.32 + i * 0.01, 600.0 + i] for i in range(book_levels)],
    }
    return {
        "observed_at": "2026-09-08T00:00:00+00:00",
        "observed_at_ms": ts_ms,
        "depth_levels": 20,
        "errors": [],
        "coverage": {"requested_window_seconds": 300},
        "spot": {"ticker_24h": {"price": "1"}, "order_book": book,
                 "trades_raw": [{"id": 0}], "trades_normalized": []},
        "futures": {"ticker_24h": None, "order_book": book,
                    "trades_raw": [],
                    "trades_normalized": [_trade(1, ts_ms, 148.25, 2.0)],
                    "funding": {"rate": "0.0001"}, "open_interest": {"oi": 1}},
    }


class SlimmingTests(unittest.TestCase):
    def test_stream_body_drops_point_in_time_surfaces(self):
        slim = _slim_stream_body(_full_payload(1_000))
        for side in ("spot", "futures"):
            self.assertNotIn("order_book", slim[side])
            self.assertNotIn("ticker_24h", slim[side])
            self.assertNotIn("trades_raw", slim[side])
        # Trades + tiny surfaces survive; top-level fields ride along.
        self.assertEqual(len(slim["futures"]["trades_normalized"]), 1)
        self.assertEqual(slim["futures"]["funding"], {"rate": "0.0001"})
        self.assertEqual(slim["futures"]["open_interest"], {"oi": 1})
        self.assertEqual(slim["coverage"], {"requested_window_seconds": 300})
        self.assertEqual(slim["depth_levels"], 20)

    def test_slim_is_substantially_smaller(self):
        full = _full_payload(1_000)
        slim = _slim_stream_body(full)
        full_size = len(json.dumps(full, default=str))
        slim_size = len(json.dumps(slim, default=str))
        self.assertLess(slim_size, full_size * 0.5)

    def test_publish_lua_carries_minid_trim(self):
        """The raw publish script must XTRIM by MINID (time contract) and
        keep the MAXLEN guardrail; the slim body (not the full payload)
        goes into the stream while the latest projection stays FULL."""
        calls: list[tuple] = []

        class _FakeRedis:
            async def eval(self, script, numkeys, *args):
                calls.append((script, args))
                return "1527-0"

        store = RedisRuntimeStore.__new__(RedisRuntimeStore)
        store.redis = _FakeRedis()
        store.prefix = "mkt"
        store.stream_maxlen = 5000
        store.raw_retention_ms = 43_200_000
        store.raw_guardrail_maxlen = 20_000
        sid = asyncio.run(store.publish_raw_evidence("SOLUSDT", _full_payload(1_000)))
        self.assertEqual(sid, "1527-0")
        script, args = calls[0]
        self.assertIn("XTRIM", script)
        self.assertIn("MINID", script)
        self.assertIn("MAXLEN", script)
        # args: KEYS[1..3] then full_body, ts, "600", stream_body, maxlen, minid
        *keys, full_body, ts, _ttl, stream_body, maxlen, minid = args
        self.assertEqual(len(keys), 3)
        self.assertEqual(ts, "1000")
        # The MAXLEN guardrail is the RAW guardrail (20 000), NOT the
        # state-stream maxlen — a count cap below the time retention
        # would silently defeat the MINID contract.
        self.assertEqual(maxlen, "20000")
        # minid = publish instant - 12h → within a second of that math
        now_ms = int(time.time() * 1000)
        self.assertAlmostEqual(int(minid), now_ms - 43_200_000, delta=2_000)
        stream_payload = json.loads(stream_body)
        self.assertNotIn("order_book", stream_payload["futures"])
        self.assertIn("trades_normalized", stream_payload["futures"])
        self.assertIn("order_book", json.loads(full_body)["futures"])


class RawOldestTests(unittest.TestCase):
    def test_read_raw_oldest_ms_parses_entry_id(self):
        class _FakeRedis:
            async def xrange(self, key, min=None, max=None, count=None):
                assert count == 1
                return [("1700000000000-3", {"ts": "x"})]

        store = RedisRuntimeStore.__new__(RedisRuntimeStore)
        store.redis = _FakeRedis()
        store.prefix = "mkt"
        oldest = asyncio.run(store.read_raw_oldest_ms("SOLUSDT"))
        self.assertEqual(oldest, 1_700_000_000_000)


# ---------------------------------------------------------------------------
# Part 1 — coverage honesty
# ---------------------------------------------------------------------------


class _CoverageStore:
    """RedisRuntimeStore-shaped seam for build_raw_window coverage tests."""

    def __init__(self, latest: dict[str, Any], snapshots: list[dict[str, Any]],
                 oldest_ms: int | None):
        self._latest = latest
        self._snapshots = snapshots
        self._oldest = oldest_ms

    async def read_raw_latest(self, symbol):
        return self._latest

    async def read_raw_window(self, symbol, since_ms):
        return list(self._snapshots)

    async def read_raw_oldest_ms(self, symbol):
        return self._oldest


class CoverageHonestyTests(unittest.TestCase):
    def _latest(self, ts_ms: int) -> dict[str, Any]:
        snap = _full_payload(ts_ms)
        snap["errors"] = []
        return snap

    def test_window_within_retention_is_healthy(self):
        now_ms = int(time.time() * 1000)
        latest = self._latest(now_ms)
        oldest = now_ms - 13 * 3_600_000  # 13h retained > 4h window
        out = asyncio.run(
            build_raw_window(_CoverageStore(latest, [latest], oldest), "SOLUSDT", 240))
        ret = out["coverage"]["retention"]
        self.assertFalse(ret["horizon_degraded"])
        self.assertEqual(ret["oldest_entry_ms"], oldest)
        self.assertNotIn("horizon_note", out["coverage"])

    def test_window_beyond_retention_is_degraded(self):
        now_ms = int(time.time() * 1000)
        latest = self._latest(now_ms)
        oldest = now_ms - 2 * 3_600_000  # only 2h retained < 4h window
        out = asyncio.run(
            build_raw_window(_CoverageStore(latest, [latest], oldest), "SOLUSDT", 240))
        ret = out["coverage"]["retention"]
        self.assertTrue(ret["horizon_degraded"])
        self.assertIn("retention", out["coverage"]["horizon_note"])

    def test_empty_stream_reports_no_oldest(self):
        now_ms = int(time.time() * 1000)
        latest = self._latest(now_ms)
        out = asyncio.run(
            build_raw_window(_CoverageStore(latest, [latest], None), "SOLUSDT", 240))
        self.assertIsNone(out["coverage"]["retention"]["oldest_entry_ms"])
        self.assertFalse(out["coverage"]["retention"]["horizon_degraded"])


# ---------------------------------------------------------------------------
# Part 2 — contracts v2 + rollover alignment
# ---------------------------------------------------------------------------


class SchemaV2Tests(unittest.TestCase):
    def test_payload_v2_round_trip_with_horizons(self):
        payload = SubstrateStatePayload.create(
            substrate="migration", symbol="SOLUSDT",
            output={"hourly_keystone_migration": {"hourly": []}},
            trigger=TriggerDecision(fired=True, source="probe"),
            computed_at_ms=1_000,
            horizons={"4h": {"output": {"verdict": "MIGRATING_UP"},
                             "fold": {"horizon": "4h", "hwm": {"futures": 9}}}},
        )
        self.assertEqual(payload.schema_version, 2)
        d = payload.to_dict()
        self.assertEqual(d["horizons"]["4h"]["fold"]["hwm"]["futures"], 9)
        restored = SubstrateStatePayload.from_dict(d)
        self.assertEqual(restored.schema_version, 2)
        self.assertEqual(restored.horizons["4h"]["output"]["verdict"], "MIGRATING_UP")

    def test_v1_payload_reads_as_implicit_base_horizon(self):
        v1 = {
            "schema_version": 1, "substrate": "migration", "symbol": "SOLUSDT",
            "status": "healthy", "observed_at_ms": None, "computed_at_ms": 1,
            "trigger": {"source": "probe", "predicates": {}},
            "freshness": {}, "missing_inputs": [], "output": {"k": 1},
            "provenance": {},
        }
        restored = SubstrateStatePayload.from_dict(v1)
        self.assertEqual(restored.schema_version, 2)
        self.assertEqual(restored.horizons, {})
        self.assertEqual(restored.output, {"k": 1})

    def test_rollover_periods_declared(self):
        self.assertEqual(ROLLOVER_PERIOD_MS["bar_4h"], 14_400_000)
        self.assertEqual(ROLLOVER_PERIOD_MS["bar_15m"], 900_000)

    def test_staleness_for_resolver(self):
        profile = CadenceProfile(cooldown_s=1, staleness_s=900,
                                 horizons=("1h", "4h"),
                                 horizon_staleness_s=(("1h", 1800), ("4h", 14_400)))
        self.assertEqual(profile.staleness_for(None), 900)
        self.assertEqual(profile.staleness_for("1h"), 1800)
        self.assertEqual(profile.staleness_for("4h"), 14_400)
        self.assertEqual(profile.staleness_for("15m"), 900)  # unlisted → base


# ---------------------------------------------------------------------------
# Part 2 — rollover IST alignment
# ---------------------------------------------------------------------------


class _RolloverWorker:
    """Transport-free worker exposing _rollover_decision."""

    SUBSTRATE_NAME = "rollover_test"
    CADENCE = CadenceProfile(cooldown_s=1, staleness_s=10, rollovers=("bar_4h",))


class RolloverISTTests(unittest.TestCase):
    def _worker(self) -> _RolloverWorker:
        from market_service.substrate_worker.core.reader import ReaderMixin
        worker = _RolloverWorker()
        worker.symbol = "SOLUSDT"
        worker._prev_newest_ms = None
        worker._rollover_decision = ReaderMixin._rollover_decision.__get__(worker)
        worker._entry_ms = ReaderMixin._entry_ms  # plain function (staticmethod)
        worker._aligned_bucket = ReaderMixin._aligned_bucket  # plain function (staticmethod)
        return worker

    def test_bar_4h_rollover_uses_ist_aligned_buckets(self):
        worker = self._worker()
        # IST 2026-09-09 00:00:00 = UTC 2026-09-08 18:30:00
        boundary = _utc_epoch_ms("2026-09-08T18:30:00+00:00")
        before = boundary - 5_000
        rows = [{"id": f"{boundary}-0", "fields": {}}]
        worker._prev_newest_ms = before
        decision = worker._rollover_decision(rows)
        self.assertIsNotNone(decision)
        self.assertIn("bar_4h", decision.predicates)
        pred = decision.predicates["bar_4h"]
        self.assertEqual(pred["from_bucket"], (before + IST_OFFSET_MS) // 14_400_000)
        self.assertEqual(pred["to_bucket"], (boundary + IST_OFFSET_MS) // 14_400_000)

    def test_no_rollover_within_bucket(self):
        worker = self._worker()
        boundary = _utc_epoch_ms("2026-09-08T18:30:00+00:00")
        # Both entries INSIDE the same IST-aligned 4h bucket (just after
        # its start) → no boundary crossed.
        rows = [{"id": f"{boundary + 2_000}-0", "fields": {}}]
        worker._prev_newest_ms = boundary + 1_000
        self.assertIsNone(worker._rollover_decision(rows))


# ---------------------------------------------------------------------------
# Part 2 — pilot migration worker horizons
# ---------------------------------------------------------------------------


_ID_SEQ = {"n": 0}


def _hour_trades(start_ms: int, base_price: float, n: int = 8) -> list[dict[str, Any]]:
    """Buy notional concentrated at ``base_price`` inside one hour-span."""
    out = []
    for _ in range(n):
        _ID_SEQ["n"] += 1
        out.append(_trade(_ID_SEQ["n"], start_ms, price=base_price, qty=5.0,
                          maker=False))
    return out


def _hz_evidence(base_trades: list[dict[str, Any]],
                 hz_trades: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ev: dict[str, Any] = {"futures": {"trades_normalized": base_trades}}
    ev["horizons"] = {
        hz: {"futures": {"trades_normalized": trades}}
        for hz, trades in hz_trades.items()
    }
    return ev


class MigrationHorizonTests(unittest.TestCase):
    """Pilot: the 4h horizon is interpreted by the worker, not the math."""

    @staticmethod
    def _bare(horizons: tuple[str, ...] = ("1h", "4h")):
        from market_service.substrate_worker.migration_worker import MigrationWorker
        worker = MigrationWorker.__new__(MigrationWorker)
        worker.horizons = horizons
        worker._horizon_folds = {hz: new_fold_state(hz) for hz in horizons}
        return worker

    def test_compute_base_and_horizons(self):
        worker = self._bare()
        t0 = _utc_epoch_ms("2026-09-08T18:30:00+00:00")   # IST midnight
        t1 = t0 + 3_600_000                                # next epoch hour
        base = _hour_trades(t0, 100.0)
        hz_trades = {
            "1h": _hour_trades(t0, 100.0),
            "4h": _hour_trades(t0, 100.0) + _hour_trades(t1, 103.0),
        }
        out = worker.compute(_hz_evidence(base, hz_trades), 20)
        self.assertIn("hourly_keystone_migration", out)
        # The core extracts "horizons" out of the base output; here we
        # verify the worker emits it for the core to lift into the payload.
        hz_out = out["horizons"]
        self.assertIn("1h", hz_out)
        self.assertIn("4h", hz_out)
        mig4 = hz_out["4h"]["hourly_keystone_migration"]
        self.assertGreaterEqual(len(mig4["hourly"]), 2)
        # Rising keystone across hours (103 vs 100 > width 0.20×bucket grid)
        # → the 4h verdict is MIGRATING_UP.
        self.assertEqual(mig4["verdict"], "MIGRATING_UP")

    def test_compute_no_horizon_evidence_no_crash(self):
        worker = self._bare()
        ev = {"futures": {"trades_normalized": [_trade(1, 1_000, 100.0, 1.0)]}}
        out = worker.compute(ev, 20)
        self.assertNotIn("horizons", out)

    def test_probe_emits_horizon_tagged_predicate(self):
        worker = self._bare()
        t0 = _utc_epoch_ms("2026-09-08T18:30:00+00:00")

        def _one_hour(keystone: float) -> dict[str, Any]:
            return {"hourly_keystone_migration": {"hourly": [
                {"hour_start_ms": t0, "keystone": keystone, "buy_vol": 1,
                 "sell_vol": 0, "migration": None}]}}

        prev_state = {
            "output": _one_hour(100.0),
            "horizons": {"4h": {"output": _one_hour(100.0)}},
        }
        # 4h tape now concentrates buys at 102 — keystone moved on the 4h
        # grid while the base tape is unchanged.
        base = _hour_trades(t0, 100.0)
        ev = _hz_evidence(base, {"1h": _hour_trades(t0, 100.0),
                                 "4h": _hour_trades(t0, 102.0)})
        decision = worker.probe(ev, prev_state, t0 + 60_000)
        self.assertTrue(decision.fired)
        self.assertIn("4h:keystone_changed", decision.predicates)
        self.assertEqual(decision.predicates["4h:keystone_changed"],
                         {"from": 100.0, "to": 102.0})

    def test_probe_silent_when_horizons_unchanged(self):
        worker = self._bare()
        def _prev_state(base_trades, hz4_trades):
            from market_service.calculations.substrates.migration import hourly_keystone_migration
            from market_service.substrate_worker.migration_worker import (
                MIGRATION_BUCKET, MIGRATION_WIDTH,
            )
            base_mig = hourly_keystone_migration(base_trades, MIGRATION_BUCKET, MIGRATION_WIDTH)
            hz4_mig = hourly_keystone_migration(hz4_trades, MIGRATION_BUCKET, MIGRATION_WIDTH)
            return {
                "output": {"hourly_keystone_migration": base_mig},
                "horizons": {"4h": {"output": {
                    "hourly_keystone_migration": hz4_mig,
                }}},
            }

        t0 = _utc_epoch_ms("2026-09-08T18:30:00+00:00")
        base = _hour_trades(t0, 100.0)
        hz4 = _hour_trades(t0, 100.0)
        # Same tape as the previous fire → nothing moved anywhere.
        ev = _hz_evidence(base, {"1h": _hour_trades(t0, 100.0), "4h": hz4})
        decision = worker.probe(ev, _prev_state(base, hz4), t0 + 60_000)
        self.assertFalse(decision.fired)

    def test_pilot_declaration(self):
        from market_service.substrate_worker.migration_worker import MigrationWorker
        self.assertEqual(MigrationWorker.HORIZONS, ("1h", "4h"))
        profile = MigrationWorker.CADENCE
        self.assertEqual(profile.horizons, ("1h", "4h"))
        # A 4h horizon never heartbeats at the 15m-scale base bound.
        self.assertGreater(profile.staleness_for("4h"), profile.staleness_s)


if __name__ == "__main__":
    unittest.main()
