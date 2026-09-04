"""Controlled-payload failure-injection tests for the rate-limit substrate.

These tests inject synthetic API failure payloads (429 soft limit, 418 IP
ban, 500 passthrough) through a fake aiohttp session into the REAL
``Binance._Rest._get`` transport path, then verify the substrate's bounded
workers and the poller's backoff detection behave exactly as designed.

No network, no live Binance — every response is a controlled payload with
deterministic headers (``X-MBX-USED-WEIGHT-1M``, ``Retry-After``).
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import aiohttp

from market_service.clients.binance import Binance, _depth_weight, _kline_weight
from market_service.poller import (
    _MAX_BACKOFF_MULTIPLIER,
    _ban_cooldown_s,
    _next_rate_backoff,
    _rate_backoff_from_errors,
    _safe,
)
from market_service.rate_limit import (
    IpBanError,
    RateLimitError,
    RateLimitSubstrate,
    SurfaceConfig,
    SurfaceId,
    SurfaceRateWorker,
    default_configs,
)


# ----------------------------------------------------------------------
# deterministic clock / sleep fakes
# ----------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_sleep(clock: FakeClock):
    """Sleep fake that records durations and advances the fake clock."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    return sleep, slept


def _worker(
    *, ceiling: int = 100, limit: int = 120, clock: FakeClock | None = None,
) -> tuple[SurfaceRateWorker, FakeClock, list[float]]:
    clock = clock or FakeClock()
    sleep, slept = make_sleep(clock)
    config = SurfaceConfig(
        SurfaceId.SPOT, weight_limit=limit, weight_ceiling=ceiling,
        default_retry_s=60.0, default_ban_s=120.0,
    )
    return SurfaceRateWorker(config, clock=clock, sleep=sleep), clock, slept


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# worker — pre-flight weight gate
# ----------------------------------------------------------------------


class WorkerAcquireTests(unittest.TestCase):
    def test_acquire_reserves_weight(self):
        worker, _, _ = _worker(ceiling=100)
        _run(worker.acquire(10))
        _run(worker.acquire(5))
        self.assertEqual(worker.state.used_weight, 15)

    def test_acquire_rejects_non_positive_weight(self):
        worker, _, _ = _worker()
        with self.assertRaises(ValueError):
            _run(worker.acquire(0))
        with self.assertRaises(ValueError):
            _run(worker.acquire(-3))

    def test_ceiling_gate_waits_for_window_rollover(self):
        worker, clock, slept = _worker(ceiling=10)
        _run(worker.acquire(6))
        # projected 6+6=12 > ceiling 10 → must wait out the 60s window
        _run(worker.acquire(6))
        self.assertEqual(slept, [60.0])
        # window rolled and reset: only the fresh reservation remains
        self.assertEqual(worker.state.used_weight, 6)
        self.assertEqual(clock.now, 1060.0)

    def test_window_rollover_resets_counter(self):
        worker, clock, slept = _worker(ceiling=100)
        _run(worker.acquire(90))
        clock.advance(61.0)
        _run(worker.acquire(1))
        self.assertEqual(worker.state.used_weight, 1)
        self.assertEqual(slept, [])  # no gate wait; plain rollover

    def test_concurrent_acquires_cannot_overshoot_ceiling(self):
        """A 12-endpoint gather must serialize through the lock."""
        worker, _, slept = _worker(ceiling=10)

        async def burst():
            await asyncio.gather(*[worker.acquire(3) for _ in range(5)])

        _run(burst())
        # 5 x 3 = 15 > ceiling 10 → at least one gate wait fired,
        # and the final reservation is never above ceiling in one window
        self.assertTrue(slept, "expected at least one gate wait")
        self.assertLessEqual(worker.state.used_weight, 10)


# ----------------------------------------------------------------------
# worker — response-path header sync (dynamic accounting)
# ----------------------------------------------------------------------


class WorkerHeaderSyncTests(unittest.TestCase):
    def test_record_headers_syncs_to_server_truth(self):
        worker, _, _ = _worker()
        _run(worker.acquire(2))
        worker.record_headers({"X-MBX-USED-WEIGHT-1M": "50"})
        self.assertEqual(worker.state.used_weight, 50)

    def test_record_headers_never_drops_below_local_reservation(self):
        worker, _, _ = _worker()
        _run(worker.acquire(80))
        worker.record_headers({"X-MBX-USED-WEIGHT-1M": "30"})
        self.assertEqual(worker.state.used_weight, 80)

    def test_absent_headers_are_a_noop(self):
        worker, _, _ = _worker()
        _run(worker.acquire(7))
        worker.record_headers({"Content-Type": "application/json"})
        self.assertEqual(worker.state.used_weight, 7)

    def test_malformed_header_is_a_noop(self):
        worker, _, _ = _worker()
        _run(worker.acquire(7))
        worker.record_headers({"X-MBX-USED-WEIGHT-1M": "not-a-number"})
        self.assertEqual(worker.state.used_weight, 7)

    def test_lowercase_header_name_accepted(self):
        worker, _, _ = _worker()
        worker.record_headers({"x-mbx-used-weight-1m": "42"})
        self.assertEqual(worker.state.used_weight, 42)


# ----------------------------------------------------------------------
# worker — 429 / 418 failure translation
# ----------------------------------------------------------------------


class WorkerFailureTranslationTests(unittest.TestCase):
    def test_429_raises_rate_limit_error_with_retry_after(self):
        worker, clock, _ = _worker()
        with self.assertRaises(RateLimitError) as ctx:
            worker.handle_error(429, {"Retry-After": "7"})
        self.assertEqual(ctx.exception.retry_after_s, 7.0)
        self.assertEqual(ctx.exception.surface, SurfaceId.SPOT)
        self.assertEqual(worker.state.pause_until, clock.now + 7.0)

    def test_429_without_retry_after_uses_default(self):
        worker, _, _ = _worker()
        with self.assertRaises(RateLimitError) as ctx:
            worker.handle_error(429, {})
        self.assertEqual(ctx.exception.retry_after_s, 60.0)

    def test_acquire_waits_out_429_pause(self):
        worker, clock, slept = _worker()
        with self.assertRaises(RateLimitError):
            worker.handle_error(429, {"Retry-After": "7"})
        clock.advance(2.0)
        _run(worker.acquire(1))  # waits the remaining 5s, then proceeds
        self.assertEqual(slept, [5.0])

    def test_418_raises_ip_ban_error_with_ban_duration(self):
        worker, clock, _ = _worker()
        with self.assertRaises(IpBanError) as ctx:
            worker.handle_error(418, {"Retry-After": "120"})
        self.assertEqual(ctx.exception.ban_remaining_s, 120.0)
        self.assertEqual(worker.state.ban_until, clock.now + 120.0)

    def test_418_seals_bucket_fail_fast_no_sleep(self):
        """During a ban, acquire raises immediately — no network, no sleep."""
        worker, _, slept = _worker()
        with self.assertRaises(IpBanError):
            worker.handle_error(418, {"Retry-After": "120"})
        with self.assertRaises(IpBanError) as ctx:
            _run(worker.acquire(1))
        self.assertGreater(ctx.exception.ban_remaining_s, 0)
        self.assertEqual(slept, [])  # fail-fast, never waits into a ban

    def test_ban_expires_and_acquire_recovers(self):
        worker, clock, _ = _worker()
        with self.assertRaises(IpBanError):
            worker.handle_error(418, {"Retry-After": "120"})
        clock.advance(121.0)
        _run(worker.acquire(1))  # ban over → normal operation resumes
        self.assertEqual(worker.state.used_weight, 1)

    def test_non_rate_status_raises_value_error(self):
        worker, _, _ = _worker()
        with self.assertRaises(ValueError):
            worker.handle_error(500, {})
        with self.assertRaises(ValueError):
            worker.handle_error(200, {})


# ----------------------------------------------------------------------
# substrate — routing + disabled mode
# ----------------------------------------------------------------------


class SubstrateTests(unittest.TestCase):
    def test_worker_lazily_created_and_cached_per_surface(self):
        substrate = RateLimitSubstrate(enabled=True)
        w1 = substrate.worker(SurfaceId.SPOT)
        w2 = substrate.worker(SurfaceId.SPOT)
        w3 = substrate.worker(SurfaceId.FUTURES_DATA)
        self.assertIs(w1, w2)
        self.assertIsNot(w1, w3)
        self.assertEqual(w1.surface, SurfaceId.SPOT)
        self.assertEqual(w3.surface, SurfaceId.FUTURES_DATA)

    def test_surfaces_get_independent_buckets(self):
        """A ban on futures-data must NOT seal spot or futures."""
        substrate = RateLimitSubstrate(enabled=True)
        with self.assertRaises(IpBanError):
            substrate.handle_error(SurfaceId.FUTURES_DATA, 418, {"Retry-After": "60"})
        # spot + futures remain fully operational
        _run(substrate.acquire(SurfaceId.SPOT, 1))
        _run(substrate.acquire(SurfaceId.FUTURES, 1))
        with self.assertRaises(IpBanError):
            _run(substrate.acquire(SurfaceId.FUTURES_DATA, 1))

    def test_disabled_mode_is_fully_inert(self):
        substrate = RateLimitSubstrate(enabled=False)
        _run(substrate.acquire(SurfaceId.SPOT, 999999))  # no gate, no raise
        substrate.record(SurfaceId.SPOT, {"X-MBX-USED-WEIGHT-1M": "999999"})
        # 429/418 are no-ops when disabled → legacy raise_for_status path
        substrate.handle_error(SurfaceId.SPOT, 429, {"Retry-After": "10"})
        substrate.handle_error(SurfaceId.SPOT, 418, {"Retry-After": "10"})
        self.assertEqual(substrate._workers, {})

    def test_from_env_disabled_flag(self):
        with patch.dict("os.environ", {"BINANCE_RATE_LIMIT_ENABLED": "0"}):
            substrate = RateLimitSubstrate.from_env()
        self.assertFalse(substrate.enabled)
        with patch.dict("os.environ", {"BINANCE_RATE_LIMIT_ENABLED": "1"}):
            substrate = RateLimitSubstrate.from_env()
        self.assertTrue(substrate.enabled)

    def test_default_configs_env_override(self):
        env = {
            "BINANCE_WEIGHT_LIMIT": "1200",
            "BINANCE_WEIGHT_CEILING": "900",
            "BINANCE_DATA_WEIGHT_LIMIT": "400",
            "BINANCE_DATA_WEIGHT_CEILING": "300",
        }
        with patch.dict("os.environ", env):
            configs = default_configs()
        self.assertEqual(configs[SurfaceId.SPOT].weight_limit, 1200)
        self.assertEqual(configs[SurfaceId.SPOT].weight_ceiling, 900)
        self.assertEqual(configs[SurfaceId.FUTURES_DATA].weight_limit, 400)
        self.assertEqual(configs[SurfaceId.FUTURES_DATA].weight_ceiling, 300)

    def test_default_configs_ceiling_defaults_to_80_percent(self):
        with patch.dict("os.environ", {"BINANCE_WEIGHT_LIMIT": "1000"}, clear=False):
            for var in ("BINANCE_WEIGHT_CEILING", "BINANCE_DATA_WEIGHT_LIMIT",
                        "BINANCE_DATA_WEIGHT_CEILING"):
                import os
                os.environ.pop(var, None)
            configs = default_configs()
        self.assertEqual(configs[SurfaceId.SPOT].weight_ceiling, 800)


# ----------------------------------------------------------------------
# controlled API-failure payloads through the REAL transport path
# ----------------------------------------------------------------------


class _FakeRequestInfo:
    real_url = "http://fake"
    url = "http://fake"


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, payload=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=_FakeRequestInfo(),  # type: ignore[arg-type]
                history=(),
                status=self.status, message=f"HTTP {self.status}",
            )

    async def json(self, content_type=None):
        return self._payload


class _AsyncCM:
    def __init__(self, resp: FakeResponse):
        self.resp = resp

    async def __aenter__(self) -> FakeResponse:
        return self.resp

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """Scripted aiohttp session stand-in: one controlled payload per call."""

    closed = False

    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params=None) -> _AsyncCM:
        self.calls.append((path, params))
        if not self.responses:
            raise AssertionError("FakeSession exhausted — unexpected extra call")
        return _AsyncCM(self.responses.pop(0))

    async def close(self) -> None:
        self.closed = True


def _client_with_payloads(responses: list[FakeResponse], *, enabled: bool = True):
    """Binance client whose spot session replays controlled payloads."""
    substrate = RateLimitSubstrate(enabled=enabled)
    client = Binance(substrate=substrate)
    session = FakeSession(responses)
    client._spot._session = session  # type: ignore[assignment]  # test double
    return client, substrate, session


class TransportFailureInjectionTests(unittest.TestCase):
    """Inject 429/418/500 payloads through _Rest._get and verify behavior."""

    def test_success_payload_passes_and_records_weight_header(self):
        client, substrate, session = _client_with_payloads([
            FakeResponse(200, {"X-MBX-USED-WEIGHT-1M": "17"}, payload={"ok": True}),
        ])
        result = _run(client._spot._get("/api/v3/depth", weight=5,
                                        symbol="SOLUSDT", limit=100))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(substrate.worker(SurfaceId.SPOT).state.used_weight, 17)

    def test_429_payload_raises_rate_limit_error(self):
        client, substrate, session = _client_with_payloads([
            FakeResponse(429, {"Retry-After": "13",
                               "X-MBX-USED-WEIGHT-1M": "6100"}),
        ])
        with self.assertRaises(RateLimitError) as ctx:
            _run(client._spot._get("/api/v3/depth", weight=5,
                                   symbol="SOLUSDT", limit=100))
        self.assertEqual(ctx.exception.retry_after_s, 13.0)
        self.assertEqual(ctx.exception.surface, SurfaceId.SPOT)
        self.assertEqual(len(session.calls), 1)
        # bucket is now paused: next acquire waits out the pause
        clock_pause = substrate.worker(SurfaceId.SPOT).state.pause_until
        self.assertGreater(clock_pause, 0)

    def test_418_payload_seals_bucket_and_blocks_next_request_offline(self):
        """The teapot: after 418, the NEXT request fails fast BEFORE HTTP."""
        client, substrate, session = _client_with_payloads([
            FakeResponse(418, {"Retry-After": "120"}),
        ])
        with self.assertRaises(IpBanError) as ctx:
            _run(client._spot._get("/api/v3/depth", weight=5,
                                   symbol="SOLUSDT", limit=100))
        self.assertEqual(ctx.exception.ban_remaining_s, 120.0)
        self.assertEqual(len(session.calls), 1)
        # Second request: banned → IpBanError from acquire, ZERO new HTTP calls
        with self.assertRaises(IpBanError):
            _run(client._spot._get("/api/v3/ticker/24hr", weight=1,
                                   symbol="SOLUSDT"))
        self.assertEqual(len(session.calls), 1)  # fail-fast, no ban extension

    def test_500_payload_passes_through_legacy_error_path(self):
        """500 is NOT a rate error: raise_for_status raises, not RateLimitError."""
        client, _, session = _client_with_payloads([
            FakeResponse(500, {}),
        ])
        with self.assertRaises(aiohttp.ClientResponseError) as ctx:
            _run(client._spot._get("/api/v3/depth", weight=5,
                                   symbol="SOLUSDT", limit=100))
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(len(session.calls), 1)

    def test_disabled_substrate_preserves_legacy_429_behavior(self):
        """BINANCE_RATE_LIMIT_ENABLED=0: 429 → legacy ClientResponseError."""
        client, _, session = _client_with_payloads(
            [FakeResponse(429, {"Retry-After": "13"})], enabled=False,
        )
        with self.assertRaises(aiohttp.ClientResponseError) as ctx:
            _run(client._spot._get("/api/v3/depth", weight=5,
                                   symbol="SOLUSDT", limit=100))
        self.assertEqual(ctx.exception.status, 429)

    def test_futures_data_surface_routed_to_tight_pool(self):
        """418 on futures-data must not seal the main futures pool."""
        substrate = RateLimitSubstrate(enabled=True)
        client = Binance(substrate=substrate)
        fut_session = FakeSession([FakeResponse(418, {"Retry-After": "60"})])
        client._fut._session = fut_session  # type: ignore[assignment]  # test double
        with self.assertRaises(IpBanError):
            _run(client._fut._get("/futures/data/openInterestHist", weight=1,
                                  surface=SurfaceId.FUTURES_DATA, symbol="SOLUSDT"))
        # tight pool sealed
        with self.assertRaises(IpBanError):
            _run(substrate.acquire(SurfaceId.FUTURES_DATA, 1))
        # main futures pool unaffected
        _run(substrate.acquire(SurfaceId.FUTURES, 1))


# ----------------------------------------------------------------------
# weight model sanity
# ----------------------------------------------------------------------


class WeightModelTests(unittest.TestCase):
    def test_depth_weight_brackets(self):
        self.assertEqual(_depth_weight(50), 1)
        self.assertEqual(_depth_weight(99), 1)
        self.assertEqual(_depth_weight(500), 5)
        self.assertEqual(_depth_weight(1000), 10)
        self.assertEqual(_depth_weight(5000), 50)

    def test_kline_weight_brackets(self):
        self.assertEqual(_kline_weight(100), 1)
        self.assertEqual(_kline_weight(500), 2)
        self.assertEqual(_kline_weight(1000), 5)
        self.assertEqual(_kline_weight(1500), 10)


# ----------------------------------------------------------------------
# poller detection + backoff ladder
# ----------------------------------------------------------------------


class PollerBackoffTests(unittest.TestCase):
    def test_safe_reraises_rate_errors(self):
        async def failing():
            raise RateLimitError(SurfaceId.SPOT, 10.0)

        with self.assertRaises(RateLimitError):
            _run(_safe(failing(), "spot_book"))

    def test_safe_reraises_ip_ban(self):
        async def banned():
            raise IpBanError(SurfaceId.FUTURES, 120.0)

        with self.assertRaises(IpBanError):
            _run(_safe(banned(), "fut_book"))

    def test_safe_swallows_ordinary_errors(self):
        async def boom():
            raise ConnectionError("dns failure")

        result, err = _run(_safe(boom(), "spot_24h"))
        self.assertIsNone(result)
        self.assertIsNotNone(err)
        self.assertIn("ConnectionError", err)

    def test_safe_passes_success(self):
        async def ok():
            return {"lastUpdateId": 1}

        result, err = _run(_safe(ok(), "spot_book"))
        self.assertEqual(result, {"lastUpdateId": 1})
        self.assertIsNone(err)

    def test_backoff_ladder_doubles_to_cap(self):
        poll_s = 5
        b1 = _next_rate_backoff(0.0, poll_s)
        b2 = _next_rate_backoff(b1, poll_s)
        b3 = _next_rate_backoff(b2, poll_s)
        b4 = _next_rate_backoff(b3, poll_s)
        b5 = _next_rate_backoff(b4, poll_s)
        self.assertEqual([b1, b2, b3, b4, b5],
                         [5.0, 10.0, 20.0, 25.0, 25.0])  # capped at 5x
        self.assertEqual(b5, poll_s * _MAX_BACKOFF_MULTIPLIER)

    def test_rate_backoff_prefers_ban_remainder_plus_cooldown(self):
        ban = IpBanError(SurfaceId.SPOT, 120.0)
        soft = RateLimitError(SurfaceId.SPOT, 7.0)
        with patch.dict("os.environ", {"BINANCE_BAN_COOLDOWN_S": "30"}):
            backoff = _rate_backoff_from_errors([soft, ban], poll_seconds=5,
                                                current_backoff_s=20.0)
        self.assertEqual(backoff, 150.0)  # 120 ban + 30 cooldown

    def test_rate_backoff_soft_errors_escalate_ladder(self):
        soft = RateLimitError(SurfaceId.SPOT, 7.0)
        backoff = _rate_backoff_from_errors([soft], poll_seconds=5,
                                            current_backoff_s=10.0)
        self.assertEqual(backoff, 20.0)

    def test_end_to_end_ban_payload_to_poller_backoff(self):
        """Full chain: injected 418 payload → IpBanError → poller backoff."""
        client, _, _ = _client_with_payloads([
            FakeResponse(418, {"Retry-After": "90"}),
        ])
        try:
            _run(client._spot._get("/api/v3/depth", weight=5,
                                   symbol="SOLUSDT", limit=100))
            self.fail("expected IpBanError")
        except IpBanError as exc:
            with patch.dict("os.environ", {"BINANCE_BAN_COOLDOWN_S": "30"}):
                backoff = _rate_backoff_from_errors([exc], poll_seconds=5,
                                                    current_backoff_s=0.0)
            self.assertEqual(backoff, 120.0)  # 90 ban + 30 cooldown

    def test_ban_cooldown_env_garbage_falls_back(self):
        with patch.dict("os.environ", {"BINANCE_BAN_COOLDOWN_S": "not-a-number"}):
            self.assertEqual(_ban_cooldown_s(), 30.0)


if __name__ == "__main__":
    unittest.main()
