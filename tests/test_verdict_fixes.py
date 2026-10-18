"""Verdict fixes: F10 bounded transport, F6 explicit worker, F3 honest fail-open."""

import asyncio
import time
from unittest.mock import patch


class _SlowLLM:
    """Transport that stalls longer than any test timeout."""

    def __init__(self, delay=30.0):
        self.delay = delay
        self.calls = 0

    async def acall(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.delay)
        raise AssertionError("must not return after the bound")


def test_f10_stalled_stream_fails_fast():
    """A dropped stream surfaces in microseconds, not hours (F10)."""
    import market_service.nooa_harness.engine.llm as llm_ns

    started = time.monotonic()
    with patch.object(llm_ns, "narration_timeout_s", return_value=0.05):
        # Skip the structured probe so exactly one bounded transport call runs.
        with patch.object(llm_ns, "_STRUCTURED_OK", False):
            try:
                asyncio.run(llm_ns.call_narration_llm(
                    _SlowLLM(), "hello",
                    system_prompt="sys"))
            except Exception as exc:
                elapsed = time.monotonic() - started
                assert type(exc).__name__ == "TimeoutError", type(exc).__name__
                assert elapsed < 5.0, f"stall not bounded: {elapsed:.1f}s"
                return
    raise AssertionError("stalled transport did not raise inside the bound")


def test_f10_timeout_never_flips_structured_probe_flag():
    """A timeout is transport-transient, not a probe refusal (F10)."""
    import market_service.nooa_harness.engine.llm as llm_ns

    with patch.object(llm_ns, "narration_timeout_s", return_value=0.05):
        with patch.object(llm_ns, "_STRUCTURED_OK", False):
            try:
                asyncio.run(llm_ns.call_narration_llm(
                    _SlowLLM(), "hello", system_prompt="sys"))
            except Exception:
                pass
            assert llm_ns._STRUCTURED_OK is False


def test_f10_default_timeout_is_bounded():
    from market_service.nooa_harness.engine.config import narration_timeout_s
    import os
    os.environ.pop("NOOA_NARRATION_TIMEOUT_S", None)
    assert narration_timeout_s() == 150
    os.environ["NOOA_NARRATION_TIMEOUT_S"] = "5"
    try:
        assert narration_timeout_s() == 30  # clamped to the floor
    finally:
        os.environ.pop("NOOA_NARRATION_TIMEOUT_S", None)


def test_f6_narration_refused_pinned_to_narration_worker():
    from market_service.nooa_harness.engine.failure_substrate.substrate import (
        WORKER_FOR_KIND,
    )
    assert WORKER_FOR_KIND["narration_refused"] == "narration"


def test_f3_dead_store_fails_open_as_all_due_allow():
    """An unreadable store bottoms out as all-due allow, honestly labelled (F3)."""
    from market_service.governance import build_calculation_governor

    class _DeadStore:
        prefix = "marketflow"

        async def read_substrate_latest(self, *a, **k):
            raise ConnectionError("redis down")

        async def read_analysis_latest(self, *a, **k):
            raise ConnectionError("redis down")

        async def read_governance_denials(self, *a, **k):
            return []

        class _Redis:
            async def exists(self, *a, **k):
                return 0

            async def set(self, *a, **k):
                return True

            async def delete(self, *a, **k):
                return 1

        @property
        def redis(self):
            return self._Redis()

    governor = build_calculation_governor(_DeadStore())
    ticket = asyncio.run(governor.authorize_dispatch(
        symbol="SOLUSDT", requested=["tape", "density"]))
    assert ticket.rule == "dispatch-allow"
    assert set(ticket.allowed) == {"tape", "density"}
    assert ticket.deferred is False
