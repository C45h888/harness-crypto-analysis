"""Controlled-payload chaos tests through the REAL poller pipeline.

Drives the production ``fetch_binance_evidence`` + ``_safe_poll_symbol`` +
cycle-backoff code path against a scripted fake Binance that injects:

  * 200 with arbitrary X-MBX-USED-WEIGHT-1M headers  → healthy cycle
  * 429 Retry-After:N                                → soft-limit, ladder backoff
  * 418 Retry-After:N                                → IP ban, fail-fast + ban sleep
  * 500                                              → per-endpoint _safe swallow

The substrate is the REAL RateLimitSubstrate (not mocked) so the bounded
worker, header sync, fail-fast, and per-surface isolation are exercised
exactly as in production. The Binance transport is replaced with a
**path-aware** controlled fake (asyncio.gather makes 12 endpoints
concurrent, so a flat script is order-non-deterministic — the fake
matches each request path to the right scripted response).
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any

import aiohttp

from market_service.clients.binance import Binance
from market_service.poller import (
    _ban_cooldown_s,
    _rate_backoff_from_errors,
    _safe_poll_symbol,
    fetch_binance_evidence,
)
from market_service.rate_limit import (
    IpBanError,
    RateLimitError,
    RateLimitSubstrate,
    SurfaceId,
)


# ----------------------------------------------------------------------
# path-aware scripted session
# ----------------------------------------------------------------------


class _FakeReq:
    real_url = "x"
    url = "x"


class _Resp:
    closed = False

    def __init__(self, status: int, headers: dict | None = None, payload: Any = None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=_FakeReq(), history=(),
                status=self.status, message=f"HTTP {self.status}",
            )

    async def json(self, content_type=None):
        return self._payload


class _CM:
    def __init__(self, r: _Resp) -> None:
        self.r = r

    async def __aenter__(self) -> _Resp:
        return self.r

    async def __aexit__(self, *exc) -> bool:
        return False


class PathScriptedSession:
    """Scripted aiohttp stand-in keyed by Binance endpoint path.

    asyncio.gather makes 12 endpoints concurrent; per-call order is not
    deterministic. This fake maps each request path → next scripted
    response (or a default healthy 200). When a path's queue is empty it
    pops from the default queue.
    """

    closed = False

    # The 12 paths the poller issues per cycle
    POLLER_PATHS = {
        # spot (3 endpoints, default surface=SPOT)
        "/api/v3/depth":     "spot",
        "/api/v3/ticker/24hr": "spot",
        "/api/v3/aggTrades": "spot",
        # futures main pool (5 endpoints, default surface=FUTURES)
        "/fapi/v1/depth":        "fut",
        "/fapi/v1/ticker/24hr":  "fut",
        "/fapi/v1/premiumIndex": "fut",
        "/fapi/v1/openInterest": "fut",
        "/fapi/v1/aggTrades":    "fut",
        # futures-data (4 endpoints, surface=FUTURES_DATA)
        "/futures/data/takerlongshortRatio":     "fut_data",
        "/futures/data/topLongShortAccountRatio": "fut_data",
        "/futures/data/globalLongShortAccountRatio": "fut_data",
        "/futures/data/openInterestHist":        "fut_data",
    }

    def __init__(
        self,
        spot: dict[str, list[_Resp]] | None = None,
        fut: dict[str, list[_Resp]] | None = None,
        fut_data: dict[str, list[_Resp]] | None = None,
        default_used_weight: int = 1,
    ) -> None:
        self._spot = {k: list(v) for k, v in (spot or {}).items()}
        self._fut = {k: list(v) for k, v in (fut or {}).items()}
        self._fut_data = {k: list(v) for k, v in (fut_data or {}).items()}
        self._default_used_weight = default_used_weight
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params=None) -> _CM:
        self.calls.append((path, params))
        bucket_name = self.POLLER_PATHS.get(path)
        if bucket_name == "spot":
            queue = self._spot.setdefault(path, [])
        elif bucket_name == "fut":
            queue = self._fut.setdefault(path, [])
        elif bucket_name == "fut_data":
            queue = self._fut_data.setdefault(path, [])
        else:
            queue = None  # unknown path

        if queue and queue:
            return _CM(queue.pop(0))
        # Default healthy response (so a path we didn't script for still
        # returns 200 with the right payload shape).
        return _CM(_Resp(
            200, {"X-MBX-USED-WEIGHT-1M": str(self._default_used_weight)},
            _default_payload_for(path),
        ))

    async def close(self) -> None:
        pass


def _default_payload_for(path: str) -> Any:
    """Shape-correct default payload for each endpoint."""
    if path.endswith("/aggTrades"):
        return [{"T": 1000, "a": 1, "p": 1.0, "q": 1.0, "m": False}]
    if path.endswith(("/premiumIndex",)):
        return {"lastFundingRate": 0.0001, "nextFundingTime": 1, "markPrice": 1}
    if path.endswith(("/openInterest",)):
        return {"openInterest": 1}
    if "/futures/data/" in path:
        return []
    if path.endswith(("/depth",)):
        return {"lastUpdateId": 1, "bids": [], "asks": []}
    return {"ok": True}


# ----------------------------------------------------------------------
# in-memory Redis stand-in
# ----------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.status_payloads: list[dict] = []

    async def publish_poller_status(self, payload: dict[str, Any]) -> None:
        self.status_payloads.append(payload)


class _Settings:
    depth_levels = 100
    flow_window_seconds = 60
    poll_seconds = 5


def _attach_sessions(client: Binance, spot: dict, fut: dict, fut_data: dict | None = None) -> None:
    fake = PathScriptedSession(spot=spot, fut=fut, fut_data=fut_data)
    client._spot._session = fake  # type: ignore[assignment]
    client._fut._session = fake   # type: ignore[assignment]
    return fake


def _run(coro):
    """Run one coroutine in its own event loop."""
    return asyncio.run(coro)


def _run_all(fn):
    """Run one async function in ONE persistent event loop.

    Critical: a fresh ``asyncio.run()`` per cycle would build a new event
    loop, and the substrate's ``asyncio.Lock`` (created on first cycle)
    would be bound to the first loop — subsequent cycles would explode
    with ``RuntimeError: Lock is bound to a different event loop``. This
    mirrors production: the real poller daemon runs every cycle inside
    ONE event loop for its entire lifetime.
    """
    return asyncio.run(fn())


# ----------------------------------------------------------------------
# helpers for building scripted responses
# ----------------------------------------------------------------------


def _ok(path: str, used_weight: int = 1, payload: Any = None) -> _Resp:
    if payload is None:
        payload = _default_payload_for(path)
    return _Resp(200, {"X-MBX-USED-WEIGHT-1M": str(used_weight)}, payload)


def _err(path: str, status: int, retry_after: float | None = None,
         used_weight: int | None = None) -> _Resp:
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    if used_weight is not None:
        headers["X-MBX-USED-WEIGHT-1M"] = str(used_weight)
    return _Resp(status, headers, {})


# ----------------------------------------------------------------------
# chaos scenario 1 — clean cycle through real pipeline
# ----------------------------------------------------------------------


class CleanCycleTests(unittest.TestCase):
    def test_clean_cycle_completes_with_real_substrate(self):
        """Real substrate + scripted 200s → evidence written, no errors."""
        sub = RateLimitSubstrate(enabled=True)
        client = Binance(substrate=sub)
        # All defaults serve healthy responses
        _attach_sessions(client, {}, {}, {})
        ev = _run(fetch_binance_evidence(
            client, symbol="SOLUSDT",
            depth_levels=100, flow_window_seconds=60,
        ))
        self.assertEqual(ev["errors"], [])
        # Substrate saw all 12 calls
        spot_used = sub.worker(SurfaceId.SPOT).state.used_weight
        fut_used = sub.worker(SurfaceId.FUTURES).state.used_weight
        data_used = sub.worker(SurfaceId.FUTURES_DATA).state.used_weight
        print(f"[clean] spot_used={spot_used} fut_used={fut_used} data_used={data_used}")
        self.assertGreaterEqual(spot_used, 1)
        self.assertGreaterEqual(fut_used, 1)
        self.assertGreaterEqual(data_used, 1)


# ----------------------------------------------------------------------
# chaos scenario 2 — soft 429 → ladder backoff
# ----------------------------------------------------------------------


class SoftLimitTests(unittest.TestCase):
    def test_single_429_propagates_through_pipeline(self):
        """Inject 429 on spot_book → fetch raises RateLimitError."""
        sub = RateLimitSubstrate(enabled=True)
        client = Binance(substrate=sub)
        fake = _attach_sessions(
            client,
            spot={"/api/v3/depth": [_err("/api/v3/depth", 429, retry_after=7, used_weight=6100)]},
            fut={}, fut_data={},
        )
        with self.assertRaises(RateLimitError) as ctx:
            _run(fetch_binance_evidence(
                client, symbol="SOLUSDT",
                depth_levels=100, flow_window_seconds=60,
            ))
        self.assertEqual(ctx.exception.retry_after_s, 7.0)
        self.assertEqual(ctx.exception.surface, SurfaceId.SPOT)
        # Bucket paused
        self.assertGreater(sub.worker(SurfaceId.SPOT).state.pause_until, 0)

    def test_consecutive_429_backoff_doubles_to_cap(self):
        """Two 429s → 5s, then 10s, then 20s, capped at 25s."""
        sub = RateLimitSubstrate(enabled=True)
        errors: list[Exception] = []
        current_backoff = 0.0
        ladder = []

        for retry_after in (7.0, 9.0, 3.0, 11.0, 6.0):
            client = Binance(substrate=sub)
            fake = _attach_sessions(
                client,
                spot={"/api/v3/depth": [_err("/api/v3/depth", 429, retry_after=retry_after)]},
                fut={}, fut_data={},
            )
            try:
                _run(fetch_binance_evidence(
                    client, symbol="SOLUSDT",
                    depth_levels=100, flow_window_seconds=60,
                ))
            except RateLimitError as e:
                backoff = _rate_backoff_from_errors([e], 5, current_backoff)
                current_backoff = backoff
                ladder.append(backoff)
                errors.append(e)

        print(f"[ladder] {ladder}")
        self.assertEqual(ladder[0], 5.0)
        self.assertEqual(ladder[1], 10.0)
        self.assertEqual(ladder[2], 20.0)
        self.assertEqual(ladder[3], 25.0)  # cap
        self.assertEqual(ladder[4], 25.0)  # stays


# ----------------------------------------------------------------------
# chaos scenario 3 — 418 IP ban: fail-fast + ban sleep
# ----------------------------------------------------------------------


class IpBanTests(unittest.TestCase):
    def test_418_seals_bucket_blocks_subsequent_http(self):
        """After 418 on SPOT, the NEXT cycle's spot endpoints raise
        IpBanError BEFORE issuing HTTP. Other surfaces are unaffected.

        Both cycles run inside ONE event loop — this matches the
        production poller, which is a daemon with one loop for its
        lifetime. A shared substrate across loops would explode the
        asyncio.Lock binding (separate test below).
        """
        sub = RateLimitSubstrate(enabled=True)

        async def _both_cycles():
            # Cycle 1: 418 on first spot endpoint
            client1 = Binance(substrate=sub)
            _attach_sessions(
                client1,
                spot={"/api/v3/depth": [_err("/api/v3/depth", 418, retry_after=120)]},
                fut={}, fut_data={},
            )
            try:
                await fetch_binance_evidence(
                    client1, symbol="SOLUSDT",
                    depth_levels=100, flow_window_seconds=60,
                )
                raise AssertionError("expected IpBanError on cycle 1")
            except IpBanError as ctx:
                assert ctx.ban_remaining_s == 120.0, ctx.ban_remaining_s
                assert ctx.surface == SurfaceId.SPOT, ctx.surface

            # Cycle 2: completely fresh client, substrate state shared.
            client2 = Binance(substrate=sub)
            fake2 = _attach_sessions(client2, {}, {}, {})
            try:
                await fetch_binance_evidence(
                    client2, symbol="SOLUSDT",
                    depth_levels=100, flow_window_seconds=60,
                )
                raise AssertionError("expected IpBanError on cycle 2")
            except IpBanError as ctx2:
                assert ctx2.ban_remaining_s > 0

            # Fail-fast: ZERO spot calls during the ban.
            # (futures + fut_data calls are expected and correct — those
            # surfaces are not banned.)
            spot_paths = {"/api/v3/depth", "/api/v3/ticker/24hr", "/api/v3/aggTrades"}
            spot_calls_during_ban = [
                (p, params) for (p, params) in fake2.calls if p in spot_paths
            ]
            assert len(spot_calls_during_ban) == 0, (
                f"spot fail-fast broken — {len(spot_calls_during_ban)} spot calls "
                f"fired during ban: {spot_calls_during_ban}"
            )
            # Non-spot surfaces DID get called — surface isolation works
            non_spot_calls = [
                (p, params) for (p, params) in fake2.calls if p not in spot_paths
            ]
            assert len(non_spot_calls) >= 5, (
                f"surface isolation broken — expected ≥5 non-spot calls, got "
                f"{len(non_spot_calls)}"
            )

        _run_all(_both_cycles)

    def test_ban_backoff_uses_full_remainder_plus_cooldown(self):
        ban = IpBanError(SurfaceId.SPOT, 90.0)
        backoff = _rate_backoff_from_errors(
            [ban], poll_seconds=5, current_backoff_s=20.0,
        )
        self.assertEqual(backoff, 90.0 + _ban_cooldown_s())

    def test_ban_dominates_over_soft_in_mixed_batch(self):
        ban = IpBanError(SurfaceId.SPOT, 60.0)
        soft = RateLimitError(SurfaceId.FUTURES, 7.0)
        backoff = _rate_backoff_from_errors(
            [soft, ban], poll_seconds=5, current_backoff_s=10.0,
        )
        self.assertEqual(backoff, 60.0 + _ban_cooldown_s())


# ----------------------------------------------------------------------
# chaos scenario 4 — surface isolation under chaos
# ----------------------------------------------------------------------


class SurfaceIsolationTests(unittest.TestCase):
    def test_futures_data_418_does_not_seal_futures(self):
        """Inject 418 on /futures/data/* only — main FUTURES stays open."""
        sub = RateLimitSubstrate(enabled=True)

        async def _scenario():
            client = Binance(substrate=sub)
            _attach_sessions(
                client, spot={}, fut={},
                fut_data={"/futures/data/openInterestHist":
                          [_err("/futures/data/openInterestHist", 418, retry_after=60)]},
            )
            try:
                await fetch_binance_evidence(
                    client, symbol="SOLUSDT",
                    depth_levels=100, flow_window_seconds=60,
                )
                raise AssertionError("expected IpBanError")
            except IpBanError as ctx:
                assert ctx.surface == SurfaceId.FUTURES_DATA, ctx.surface
            # Main FUTURES is NOT banned
            assert sub.worker(SurfaceId.FUTURES).state.ban_until == 0.0
            # SPOT is NOT banned
            assert sub.worker(SurfaceId.SPOT).state.ban_until == 0.0
            # Data IS banned
            assert sub.worker(SurfaceId.FUTURES_DATA).state.ban_until > 0

        _run_all(_scenario)

    def test_spot_ban_does_not_seal_futures_or_data(self):
        sub = RateLimitSubstrate(enabled=True)

        async def _scenario():
            client = Binance(substrate=sub)
            _attach_sessions(
                client,
                spot={"/api/v3/depth": [_err("/api/v3/depth", 418, retry_after=45)]},
                fut={}, fut_data={},
            )
            try:
                await fetch_binance_evidence(
                    client, symbol="SOLUSDT",
                    depth_levels=100, flow_window_seconds=60,
                )
                raise AssertionError("expected IpBanError")
            except IpBanError:
                pass
            assert sub.worker(SurfaceId.FUTURES).state.ban_until == 0.0
            assert sub.worker(SurfaceId.FUTURES_DATA).state.ban_until == 0.0
            assert sub.worker(SurfaceId.SPOT).state.ban_until > 0

        _run_all(_scenario)


# ----------------------------------------------------------------------
# chaos scenario 5 — 500 passthrough (not a rate event)
# ----------------------------------------------------------------------


class NonRateErrorTests(unittest.TestCase):
    def test_500_swallowed_per_endpoint_no_backoff(self):
        """500 is NOT a rate event — _safe converts it to evidence['errors']."""
        sub = RateLimitSubstrate(enabled=True)
        client = Binance(substrate=sub)
        _attach_sessions(
            client,
            spot={"/api/v3/depth": [_Resp(500, {}, {})]},
            fut={}, fut_data={},
        )
        ev = _run(fetch_binance_evidence(
            client, symbol="SOLUSDT",
            depth_levels=100, flow_window_seconds=60,
        ))
        # The 500 should appear in the errors list, not as RateLimitError
        self.assertEqual(len(ev["errors"]), 1)
        self.assertIn("500", ev["errors"][0]["error"])
        # Substrate did NOT pause or ban
        self.assertEqual(sub.worker(SurfaceId.SPOT).state.pause_until, 0.0)
        self.assertEqual(sub.worker(SurfaceId.SPOT).state.ban_until, 0.0)


# ----------------------------------------------------------------------
# chaos scenario 6 — server-truth header accounting
# ----------------------------------------------------------------------


class DynamicAccountingTests(unittest.TestCase):
    def test_server_used_weight_200_overrides_local(self):
        """Server says used=200 → bucket jumps to 200 (overrides local estimate)."""
        sub = RateLimitSubstrate(enabled=True)

        async def _scenario():
            client = Binance(substrate=sub)
            # ALL spot endpoints return header 200; the worker's
            # max(local, server) sync must keep the bucket at 200
            # even after spot_book's heavier weight-5 reservation.
            _attach_sessions(
                client,
                spot={
                    "/api/v3/depth":      [_ok("/api/v3/depth", used_weight=200)],
                    "/api/v3/ticker/24hr":[_ok("/api/v3/ticker/24hr", used_weight=200)],
                    "/api/v3/aggTrades":  [_ok("/api/v3/aggTrades", used_weight=200)],
                },
                fut={}, fut_data={},
            )
            await fetch_binance_evidence(
                client, symbol="SOLUSDT",
                depth_levels=100, flow_window_seconds=60,
            )
            used = sub.worker(SurfaceId.SPOT).state.used_weight
            print(f"[dynamic] spot used_weight={used}")
            # spot_book reserves 5, then 24h reserves 1, then agg_trades
            # reserves 2 (limit=1000). After each, record_headers does
            # max(local, 200) so the bucket floors at 200 then adds on top.
            # spot_book:    5 → max(5,200)=200
            # spot_24h:     1 → max(200+1=201, 200)=201
            # spot_agg:     2 → max(201+2=203, 200)=203
            assert used == 203, f"expected 203, got {used}"

        _run_all(_scenario)


# ----------------------------------------------------------------------
# chaos scenario 7 — disabled substrate preserves legacy semantics
# ----------------------------------------------------------------------


class DisabledSubstrateTests(unittest.TestCase):
    def test_disabled_substrate_lets_429_become_per_endpoint_error(self):
        """BINANCE_RATE_LIMIT_ENABLED=0 → 429 becomes a per-endpoint error
        in the evidence payload (swallowed by _safe). Substrate workers
        do not even get created — fully inert."""
        sub = RateLimitSubstrate(enabled=False)

        async def _scenario():
            client = Binance(substrate=sub)
            _attach_sessions(
                client,
                spot={"/api/v3/depth": [_err("/api/v3/depth", 429, retry_after=10)]},
                fut={}, fut_data={},
            )
            # Disabled substrate must NOT raise — the gather returns a
            # completed payload, with the 429 recorded as evidence["errors"].
            ev = await fetch_binance_evidence(
                client, symbol="SOLUSDT",
                depth_levels=100, flow_window_seconds=60,
            )
            assert len(ev["errors"]) == 1, ev["errors"]
            assert "429" in ev["errors"][0]["error"], ev["errors"][0]
            assert ev["errors"][0]["endpoint"] == "spot_book"
            # Substrate never created any workers — fully inert
            assert sub._workers == {}, f"disabled substrate created workers: {sub._workers}"

        _run_all(_scenario)


# ----------------------------------------------------------------------
# chaos scenario 8 — full cycle loop simulation
# ----------------------------------------------------------------------


class FullCycleSimulationTests(unittest.TestCase):
    """Mirror the cycle loop: gather → backoff branch → next cycle."""

    def test_three_cycle_simulation_429_429_clean(self):
        """Simulates what poller.main() does: gather → backoff → next cycle.

        All three cycles run inside ONE event loop — same as production.
        """
        sub = RateLimitSubstrate(enabled=True)
        redis = FakeRedis()
        poll_seconds = 5
        current_backoff = 0.0
        outcomes: list[tuple[str, float]] = []

        async def _scenario():
            nonlocal current_backoff

            # Cycle 1: 429 on first spot endpoint
            client1 = Binance(substrate=sub)
            _attach_sessions(
                client1,
                spot={"/api/v3/depth": [_err("/api/v3/depth", 429, retry_after=7)]},
                fut={}, fut_data={},
            )
            try:
                await _safe_poll_symbol(client1, redis, _Settings(), "SOLUSDT")  # type: ignore[arg-type]
                outcomes.append(("clean", 0.0))
            except (RateLimitError, IpBanError) as exc:
                backoff = _rate_backoff_from_errors([exc], poll_seconds, current_backoff)
                current_backoff = backoff
                outcomes.append(("429", backoff))

            # Cycle 2: another 429
            client2 = Binance(substrate=sub)
            _attach_sessions(
                client2,
                spot={"/api/v3/depth": [_err("/api/v3/depth", 429, retry_after=3)]},
                fut={}, fut_data={},
            )
            try:
                await _safe_poll_symbol(client2, redis, _Settings(), "SOLUSDT")  # type: ignore[arg-type]
                outcomes.append(("clean", 0.0))
            except (RateLimitError, IpBanError) as exc:
                backoff = _rate_backoff_from_errors([exc], poll_seconds, current_backoff)
                current_backoff = backoff
                outcomes.append(("429", backoff))

            # Cycle 3: clean — fresh substrate (previous one is paused)
            sub2 = RateLimitSubstrate(enabled=True)
            client3 = Binance(substrate=sub2)
            _attach_sessions(client3, {}, {}, {})
            try:
                await _safe_poll_symbol(client3, redis, _Settings(), "SOLUSDT")  # type: ignore[arg-type]
                outcomes.append(("clean", 0.0))
            except (RateLimitError, IpBanError) as exc:
                outcomes.append(("err", str(exc)))

        _run_all(_scenario)
        print(f"[cycle simulation] {outcomes}")
        self.assertEqual(outcomes[0], ("429", 5.0))
        self.assertEqual(outcomes[1], ("429", 10.0))
        self.assertEqual(outcomes[2][0], "clean")

    def test_full_chaos_429_then_418_then_clean(self):
        """429 → 429 → 418 → ... → clean after ban expires (simulated)."""
        sub = RateLimitSubstrate(enabled=True)
        redis = FakeRedis()
        poll_seconds = 5
        current_backoff = 0.0
        outcomes: list[tuple[str, float]] = []

        async def _scenario():
            nonlocal current_backoff

            # Cycles 1-2: 429s (ladder)
            for retry_after in (7.0, 9.0):
                client = Binance(substrate=sub)
                _attach_sessions(
                    client,
                    spot={"/api/v3/depth": [_err("/api/v3/depth", 429, retry_after=retry_after)]},
                    fut={}, fut_data={},
                )
                try:
                    await _safe_poll_symbol(client, redis, _Settings(), "SOLUSDT")  # type: ignore[arg-type]
                except RateLimitError as exc:
                    b = _rate_backoff_from_errors([exc], poll_seconds, current_backoff)
                    current_backoff = b
                    outcomes.append(("429", b))

            # Cycle 3: 418 (ban)
            client = Binance(substrate=sub)
            _attach_sessions(
                client,
                spot={"/api/v3/depth": [_err("/api/v3/depth", 418, retry_after=90)]},
                fut={}, fut_data={},
            )
            try:
                await _safe_poll_symbol(client, redis, _Settings(), "SOLUSDT")  # type: ignore[arg-type]
            except IpBanError as exc:
                b = _rate_backoff_from_errors([exc], poll_seconds, current_backoff)
                current_backoff = b
                outcomes.append(("418", b))

            # Cycle 4: would be banned on spot; pre-flight raises immediately.
            # Other surfaces remain operational — we measure spot-only.
            client = Binance(substrate=sub)
            fake = _attach_sessions(client, {}, {}, {})
            try:
                await _safe_poll_symbol(client, redis, _Settings(), "SOLUSDT")  # type: ignore[arg-type]
            except IpBanError:
                outcomes.append(("fail-fast", 0.0))
            spot_paths = {"/api/v3/depth", "/api/v3/ticker/24hr", "/api/v3/aggTrades"}
            n_spot_calls_during_ban = sum(
                1 for (p, _) in fake.calls if p in spot_paths
            )
            outcomes.append(("spot-calls-during-ban", float(n_spot_calls_during_ban)))

        _run_all(_scenario)
        print(f"[full chaos] {outcomes}")
        self.assertEqual(outcomes[0][0], "429")
        self.assertEqual(outcomes[0][1], 5.0)
        self.assertEqual(outcomes[1][1], 10.0)
        self.assertEqual(outcomes[2][0], "418")
        self.assertGreater(outcomes[2][1], 90.0)  # 90 + cooldown
        # Last entry is "spot-calls-during-ban"; must be 0
        self.assertEqual(outcomes[-1][1], 0.0,
                         "spot fail-fast broken — spot calls fired during ban")


if __name__ == "__main__":
    unittest.main()
