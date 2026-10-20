"""Signal B — cadence-aware narration supervision (contract, checker, shim).

Deadline model, pure cadence judge, failure-kind mapping, heartbeat
admission, and the streaming shim + supervision loop against scripted
streams. No network, no Redis, no sleeps beyond millisecond bounds.
"""

from __future__ import annotations

import asyncio

import pytest

from market_service.nooa_harness.engine import llm as L
from market_service.nooa_harness.engine.failure_fsm import FAILURE_FSM
from market_service.telemetry_kernel.contracts import (
    TelemetryEvent,
    narration_cadence_state,
)


# ---------------------------------------------------------------------------
# Deadline model
# ---------------------------------------------------------------------------

class TestNarrationDeadline:
    def test_floor_for_tiny_prompts(self):
        assert L.narration_deadline_s(0, cap=300) == 90.0
        assert L.narration_deadline_s(5_000, cap=300) == 90.0

    def test_proportional_to_prompt_size(self):
        # Measured live 2026-10-19: 35K chars needs ~78s; rate carries ~2x.
        assert L.narration_deadline_s(34_873, cap=300) == pytest.approx(199.5, abs=1.0)

    def test_clamped_at_absolute_cap(self):
        assert L.narration_deadline_s(90_000, cap=300) == 300.0
        assert L.narration_deadline_s(9_000_000, cap=300) == 300.0


# ---------------------------------------------------------------------------
# Pure cadence judge
# ---------------------------------------------------------------------------

class TestJudgeCadence:
    def test_done_is_complete(self):
        assert L.judge_cadence(bytes_in=10, shape_formed=True, stream_done=True) == "complete"

    def test_no_bytes_is_dangle(self):
        assert L.judge_cadence(bytes_in=0, shape_formed=False, stream_done=False) == "stalled_no_bytes"

    def test_bytes_without_shape_is_thinking_stall(self):
        assert L.judge_cadence(bytes_in=128, shape_formed=False, stream_done=False) == "stalled_thinking"

    def test_bytes_with_shape_is_progressing(self):
        assert L.judge_cadence(bytes_in=128, shape_formed=True, stream_done=False) == "progressing"


# ---------------------------------------------------------------------------
# Failure-kind mapping
# ---------------------------------------------------------------------------

class TestNarrationFailureKind:
    def test_stall_verdicts_become_stall_kinds(self):
        for verdict, kind in (("stalled_no_bytes", "narration_stalled_no_bytes"),
                              ("stalled_thinking", "narration_stalled_thinking")):
            err = L.NarrationStalledError(
                verdict, attempt_id="a1", turn_ref="t", prompt_chars=10,
                deadline_ms=1000, bytes_in=5, elapsed_ms=9,
            )
            assert L.narration_failure_kind(err) == kind

    def test_timeout_and_plain_errors_keep_legacy_kind(self):
        assert L.narration_failure_kind(asyncio.TimeoutError()) == "narration_failed"
        assert L.narration_failure_kind(TimeoutError()) == "narration_failed"
        assert L.narration_failure_kind(ValueError("x")) == "narration_failed"
        timeout_stall = L.NarrationStalledError(
            "timeout", attempt_id="a1", turn_ref="t", prompt_chars=10,
            deadline_ms=1000, bytes_in=5, elapsed_ms=9,
        )
        assert L.narration_failure_kind(timeout_stall) == "narration_failed"

    def test_stall_kinds_classify_transient_narration(self):
        for kind in ("narration_stalled_no_bytes", "narration_stalled_thinking"):
            verdict = FAILURE_FSM.classify(kind)
            assert verdict.failure_class.value == "narration"
            assert verdict.severity.value == "transient"
            assert verdict.remediation.value == "retry"
            assert verdict.terminal.value == "narration_failed"


# ---------------------------------------------------------------------------
# Heartbeat contract admission
# ---------------------------------------------------------------------------

def _heartbeat(**over):
    base = dict(attempt_id="a1", turn_ref="evidence#1", prompt_chars=1_000,
                deadline_ms=120_000, bytes_in=0, elapsed_ms=0, outcome="started")
    base.update(over)
    return narration_cadence_state(**base)


def _emit(state, owner="narration_transport", event_type="agent_narration_cadence"):
    return TelemetryEvent.create(
        plane="agentic", owner=owner, subject_type="narration_turn",
        subject_id="a1", event_type=event_type, source_sequence=0,
        occurred_at_ms=1, state=state,
    )


class TestNarrationCadenceContract:
    @pytest.mark.parametrize("outcome", ["started", "chunk", "complete",
                                         "stalled_no_bytes", "stalled_thinking",
                                         "timeout"])
    def test_all_outcomes_admitted(self, outcome):
        event = _emit(_heartbeat(outcome=outcome, bytes_in=7, elapsed_ms=3))
        event.validate()

    def test_unknown_outcome_rejected(self):
        with pytest.raises(ValueError):
            _heartbeat(outcome="vibing")

    def test_negative_counter_rejected(self):
        with pytest.raises(ValueError):
            _heartbeat(bytes_in=-1)

    def test_foreign_owner_refused(self):
        with pytest.raises(ValueError):
            _emit(_heartbeat(), owner="cycle_controller")

    def test_foreign_event_refused_for_transport_owner(self):
        with pytest.raises(ValueError):
            _emit(_heartbeat(), event_type="agent_cadence")

    def test_prompt_content_refused(self):
        state = _heartbeat()
        state["user_prompt"] = "leak"
        with pytest.raises(ValueError):
            _emit(state)


# ---------------------------------------------------------------------------
# Streaming shim against scripted streams
# ---------------------------------------------------------------------------

class _FakeChunks:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk
        return _gen()


def _msg_chunk(content=None, reasoning=None):
    delta = type("D", (), {"content": content,
                           "reasoning_content": reasoning})()
    return type("C", (), {"choices": [type("X", (), {"delta": delta})()]})()


class _FakeLitellm:
    def __init__(self, chunks=None, response=None):
        self._chunks = chunks
        self._response = response
        self.seen = None

    async def acompletion(self, **params):
        self.seen = params
        if self._response is not None:
            return self._response
        return _FakeChunks(self._chunks)


class _FakeLLM:
    model = "test/model"
    config = {"api_key": "k", "temperature": 0.2}


class TestNarrationStreamShim:
    def test_deltas_extracted_in_order(self, monkeypatch):
        from market_service.nooa_harness.engine import narration_stream as ns
        fake = _FakeLitellm(chunks=[_msg_chunk("{\"a\": "), _msg_chunk("1}")])
        monkeypatch.setattr(ns, "litellm", fake)

        async def _run():
            return [d async for d in ns.stream_deltas(
                _FakeLLM(), messages=[{"role": "user", "content": "hi"}],
                max_tokens=10)]

        deltas = asyncio.run(_run())
        assert [d.text for d in deltas] == ["{\"a\": ", "1}"]
        assert fake.seen["stream"] is True
        assert fake.seen["model"] == "test/model"
        assert fake.seen["max_tokens"] == 10

    def test_thinking_counted_separately(self, monkeypatch):
        from market_service.nooa_harness.engine import narration_stream as ns
        fake = _FakeLitellm(chunks=[_msg_chunk(None, "hmm"), _msg_chunk("ok")])
        monkeypatch.setattr(ns, "litellm", fake)

        async def _run():
            return [d async for d in ns.stream_deltas(
                _FakeLLM(), messages=[], max_tokens=10)]

        deltas = asyncio.run(_run())
        assert deltas[0] == ns.StreamDelta(text="", thinking="hmm")
        assert deltas[1].text == "ok"

    def test_non_streaming_response_yields_once(self, monkeypatch):
        from market_service.nooa_harness.engine import narration_stream as ns
        fake = _FakeLitellm(response=_msg_chunk("whole"))
        monkeypatch.setattr(ns, "litellm", fake)

        async def _run():
            return [d async for d in ns.stream_deltas(
                _FakeLLM(), messages=[], max_tokens=10)]

        assert asyncio.run(_run()) == [ns.StreamDelta(text="whole", thinking="")]

    def test_missing_litellm_raises_runtime_error(self, monkeypatch):
        from market_service.nooa_harness.engine import narration_stream as ns
        monkeypatch.setattr(ns, "litellm", None)
        with pytest.raises(RuntimeError):
            asyncio.run(_collect(ns))

    def test_client_config_reused(self, monkeypatch):
        from market_service.nooa_harness.engine import narration_stream as ns
        fake = _FakeLitellm(chunks=[])
        monkeypatch.setattr(ns, "litellm", fake)

        async def _run():
            return [d async for d in ns.stream_deltas(
                _FakeLLM(), messages=[], max_tokens=10, timeout=7.5)]

        asyncio.run(_run())
        assert fake.seen["api_key"] == "k"
        assert fake.seen["temperature"] == 0.2
        assert fake.seen["timeout"] == 7.5


async def _collect(ns):
    return [d async for d in ns.stream_deltas(
        _FakeLLM(), messages=[], max_tokens=10)]


# ---------------------------------------------------------------------------
# Supervision loop against scripted streams
# ---------------------------------------------------------------------------

class TestSupervisedStreamingLoop:
    def _llm_with(self, monkeypatch, chunks):
        from market_service.nooa_harness.engine import narration_stream as ns
        fake = _FakeLitellm(chunks=chunks)
        monkeypatch.setattr(ns, "litellm", fake)
        return _FakeLLM()

    def test_assembles_text_without_engine(self, monkeypatch):
        llm = self._llm_with(monkeypatch, [_msg_chunk("{\"phase\": "),
                                           _msg_chunk("\"P1\"}")])
        text = asyncio.run(L.call_narration_llm_streaming(
            llm, "do it", system_prompt="sys", engine=None, turn_ref="t1"))
        assert text == "{\"phase\": \"P1\"}"

    def test_byte_gap_stall_carries_evidence(self, monkeypatch):
        monkeypatch.setattr(L, "_CADENCE_BYTE_GAP_S", 0.02)

        async def _hang():
            if False:
                yield
            await asyncio.sleep(5)

        class _HangingLLM(_FakeLLM):
            pass

        from market_service.nooa_harness.engine import narration_stream as ns

        class _HangingLitellm:
            async def acompletion(self, **params):
                return _hang()

        monkeypatch.setattr(ns, "litellm", _HangingLitellm())
        with pytest.raises(L.NarrationStalledError) as exc_info:
            asyncio.run(L.call_narration_llm_streaming(
                _HangingLLM(), "do it", system_prompt="sys",
                engine=None, turn_ref="t2"))
        err = exc_info.value
        assert err.verdict == "stalled_no_bytes"
        assert err.turn_ref == "t2"
        assert err.prompt_chars > 0

    def test_deadline_without_shape_is_thinking_stall(self, monkeypatch):
        monkeypatch.setattr(L, "narration_deadline_s", lambda *a, **k: 0.05)

        async def _drip():
            yield _msg_chunk("zzzz")
            await asyncio.sleep(0.2)
            yield _msg_chunk("zzzz")

        from market_service.nooa_harness.engine import narration_stream as ns

        class _DripLitellm:
            async def acompletion(self, **params):
                return _drip()

        monkeypatch.setattr(ns, "litellm", _DripLitellm())
        with pytest.raises(L.NarrationStalledError) as exc_info:
            asyncio.run(L.call_narration_llm_streaming(
                _FakeLLM(), "do it", system_prompt="sys",
                engine=None, turn_ref="t3"))
        assert exc_info.value.verdict == "stalled_thinking"
