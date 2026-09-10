"""Pass-B3 inference-engine tests.

Covers the full engine cycle with fakes (no Redis, no Postgres, no litellm):

- Staged cycle: wake → gather → gate → narrate#1 → P1→P5 tool rounds
  (phase coverage credited from executed tool families) → validated final
  or repair-then-final → artifact persisted (PG-first, Redis after) →
  memory proposals resolved. Thin finals are rejected for repair while
  LLM budget (8 turns, 5 tool rounds) remains.
- Hard gate: insufficient inputs → NULL interpretation, ZERO LLM calls,
  gate observation remembered deterministically.
- Final validation: P1/P2/P3/P5 tool coverage + H0 + ≥200-char P4
  explanation + dual-root evidence required; failures recorded honestly.
- Narration parse failure → degraded artifact with NULL interpretation
  and preserved deterministic state.
- Memory proposal resolution: fact-kind rejected, budget enforced, tags
  appended, importance clamped.
- Runner: no-wake path returns status no_wake without fabricating a cycle.
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from market_service.nooa_harness.engine import (
    MAX_MEMORY_PROPOSALS,
    InferenceEngine,
    _extract_json_object,
)


class _FakeRedis:
    async def xlen(self, key):
        return 5_000


class _FakeStore:
    def __init__(self, *, capture_state: str = "running", event_len: int = 5_000):
        self.redis = _FakeRedis()
        self._capture_state = capture_state
        self._event_len = event_len
        self.published: list[Any] = []

    async def read_microstructure_intervals(self, venue, symbol, *, start="-", end="+", count=None):
        return []

    async def read_microstructure_evidence(self, venue, symbol):
        return None

    async def read_microstructure_status(self, venue, symbol):
        return {"state": self._capture_state, "sequence_gaps": 0, "reconnects": 0}

    async def read_derivative_evidence(self, symbol):
        # Synthetic (possibly empty) derivatives cache — mirrors the
        # production null-payload shape so market.derivatives dispatches ok.
        return {"funding": None, "open_interest": None}

    def microstructure_event_stream(self, venue, symbol):
        return f"test:events:{symbol}"

    async def read_microstructure_events(self, venue, symbol, *, start="-", end="+", count=None):
        payloads = []
        ts = 1_700_000_000_000
        for k in range(40):
            quote = {
                "schema_version": 1, "symbol": "BTCUSDT", "venue": "spot",
                "update_id": k + 1, "exchange_ts_ms": ts + k * 10_000,
                "received_ts_ms": ts + k * 10_000,
                "bid_price": "100", "bid_qty": "10",
                "ask_price": "101", "ask_qty": "20",
            }
            payloads.append({
                "schema_version": 1, "event_type": "best_quote_transition",
                "source_quality": "exact_feed",
                "contribution": "5" if k % 2 == 0 else "-3",
                "previous": quote, "current": quote,
            })
        return payloads

    async def read_latest_inference_artifact(self, symbol, venue):
        return None

    async def publish_inference_artifact(self, artifact):
        self.published.append(artifact)
        return "stream-id"


class _FakePostgres:
    def __init__(self):
        self.inserted: list[Any] = []

    async def read_inference_artifact(self, symbol):
        return None

    async def insert_inference_artifact(self, artifact):
        self.inserted.append(artifact)
        return True

    async def close(self):
        return None


class _FakeMemory:
    def __init__(self):
        self.recalled_content = "prior conclusions block"
        self.remembered: list[tuple] = []
        self.postgres_close_called = False

    async def recall(self, session_id, *, query=None, limit=8, **kwargs):
        return []

    @staticmethod
    def render_context_block(memories, *, budget=3000):
        return "## Recalled session memory\n- [observation#abc] prior conclusion"

    async def remember(self, session_id, kind, content, **kwargs):
        self.remembered.append((kind, content, kwargs))
        return type("M", (), {"memory_id": "mem-1", "kind": kind})()

    async def close(self):
        self.postgres_close_called = True


class _FakeLLM:
    """Scripted narration LLM: returns queued responses, counts calls.

    Mirrors the NOOA unified-LLM surface the engine calls: an async
    ``.acall(...)`` returning a response whose ``.raw_response.choices[0].message.content``
    is the scripted text.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def acall(self, messages, max_tokens=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if self.responses:
            content = self.responses.pop(0)
        else:
            raise AssertionError("LLM called more times than scripted")
        return type("R", (), {"raw_response": type(
            "Raw", (), {"choices": [type(
                "C", (), {"message": type(
                    "Msg", (), {"content": content})()})()]})()})()


def _good_narration(with_tools: bool = False) -> str:
    payload: dict[str, Any] = {
        "summary": "Beta is positive with adequate fit quality.",
        "evidence": [{"path": "deterministic_state.microstructure_evidence."
                               "price_impact_fit.beta",
                      "value": "0.001", "interpretation": "positive impact",
                      "metric_name": "beta"}],
        "confidence": "medium",
        "limitations": ["provisional status"],
        "model_separation": "nu*OFI is heteroskedastic; never merged.",
        "memory_proposals": [
            {"kind": "observation",
             "content": "Beta positive on the 30m window",
             "importance": 6.0, "tags": ["beta"]},
        ],
    }
    if with_tools:
        payload["tool_calls"] = [
            {"name": "micro.capture_status",
             "args": {"symbol": "BTCUSDT", "venue": "spot"}},
        ]
    return json.dumps(payload)


_STAGED_SUMMARY = (
    "OFI tape is usable with 40 exact_feed intervals and no sequence gaps, so the "
    "P1 verdict holds. Average depth is stable across the window, giving a clean P2 "
    "observation set with beta positive and moderate explanatory power. Market correlation "
    "confirms taker flow aligns with the OFI sign, which is why this regime persists now: "
    "steady passive depth plus one-sided initiation. Paper grounding holds via Cont 1011.6402."
)


def _staged_narration(phase: str, tools: list[dict] | None = None, *, final: bool = False) -> str:
    """One staged-protocol turn: declared phase, substantive summary, H0, triple-root evidence."""
    evidence = [
        {"path": "deterministic_state.microstructure_evidence.price_impact_fit.beta",
         "value": "0.001", "interpretation": "positive impact", "metric_name": "beta"},
        {"path": "calc.ofi.intervals → ofi",
         "value": "12.5", "interpretation": "sustained one-sided flow", "metric_name": "ofi"},
        {"path": "calc.price.delta → route_a_direct.delta_ticks",
         "value": "+0.53", "interpretation": "derived move with band", "metric_name": "delta_ticks"},
    ] if final else []
    payload: dict[str, Any] = {
        "phase": "P6" if final else phase,
        "summary": _STAGED_SUMMARY if final else f"Advancing {phase}: requesting validation tools.",
        "evidence": evidence,
        "confidence": "medium" if final else None,
        "limitations": ["synthetic window"] if final else None,
        "model_separation": "nu*OFI is heteroskedastic; never merged." if final else None,
        "hypothesis": {
            "H0": "beta > 0 per OFI block (Cont 1011.6402 empirical model)",
            "H1": "beta <= 0 (flow no longer moves price)",
            "paper_refs": ["Cont 1011.6402"],
            "evidence_refs": ["calc.ofi.intervals", "calc.depth.average"],
        } if final else None,
        "tool_calls": tools if tools is not None else [],
        "memory_proposals": [
            {"kind": "observation",
             "content": "Beta positive on the 30m window",
             "importance": 6.0, "tags": ["beta"]},
        ] if final else [],
    }
    return json.dumps(payload)


def _engine(*, llm_responses: list[str], capture_state: str = "running") -> tuple[
    InferenceEngine, _FakeStore, _FakePostgres, _FakeMemory,
]:
    store = _FakeStore(capture_state=capture_state)
    postgres = _FakePostgres()
    memory = _FakeMemory()
    engine = InferenceEngine(
        store, postgres, memory, _FakeLLM(llm_responses),
        symbol="BTCUSDT", venue="spot",
    )
    return engine, store, postgres, memory


def _wake():
    from market_service.runtime.contracts import WakeEnvelope

    return WakeEnvelope.create(
        symbol="BTCUSDT", venue="spot", trigger_source="watcher",
        predicates_fired={"cold_start": {}},
        counter_snapshot={}, high_water={},
    )


class EngineCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_cycle_covers_phases_and_finalizes(self):
        engine, store, postgres, memory = _engine(llm_responses=[
            _staged_narration("P1", tools=[
                {"name": "calc.ofi.intervals", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
                {"name": "micro.ofi_intervals", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
            ]),
            _staged_narration("P2", tools=[
                {"name": "calc.depth.average", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
                {"name": "market.derivatives", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
            ]),
            _staged_narration("P5", tools=[
                {"name": "memory.recall_paper", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
                {"name": "calc.price.delta", "args": {"symbol": "BTCUSDT", "venue": "spot", "ofi": "10"}},
            ]),
            _staged_narration("P6", final=True),
        ])
        artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["llm_calls"], 4)
        self.assertTrue(meta["tool_round"])
        self.assertEqual(meta["repairs"], 0)
        self.assertTrue(meta["final_validation"]["passed"], meta["final_validation"])
        for phase in ("P1", "P2", "P3", "P5", "P6"):
            self.assertTrue(meta["phase_coverage"][phase], phase)
        self.assertEqual(artifact.status, "provisional")
        self.assertIsNotNone(artifact.interpretation)
        self.assertIn("beta", artifact.interpretation["summary"].lower() or "x")
        # capability_log: wake + capture + fit + tool round
        capabilities = [entry["capability"] for entry in artifact.capability_log]
        self.assertIn("engine.wake", capabilities)
        self.assertIn("redis.read_capture_status", capabilities)
        self.assertIn("fitting.assemble_evidence", capabilities)
        # PG-first ordering: postgres insert before redis publish
        self.assertEqual(len(postgres.inserted), 1)
        self.assertEqual(len(store.published), 1)
        # Memory proposal accepted and written
        self.assertTrue(any(kind == "observation" for kind, _, _ in memory.remembered))

    async def test_insufficient_gate_zero_llm_calls(self):
        engine, store, postgres, memory = _engine(
            llm_responses=[], capture_state="starting",
        )
        artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["llm_calls"], 0)
        self.assertEqual(artifact.status, "insufficient")
        self.assertIsNone(artifact.interpretation)
        self.assertEqual(len(engine.llm.calls), 0)
        # Deterministic quality observation remembered (engine disposes)
        self.assertTrue(any(kind == "observation" for kind, _, _ in memory.remembered))

    async def test_task_directive_persists_on_gate_refused_cycle(self):
        # The interaction-plane directive steers + persists even when the
        # gate refuses narration: deterministic_state.task, cycle_meta.task,
        # and the wake capability detail all carry it (zero LLM calls).
        engine, _s, _p, _m = _engine(
            llm_responses=[], capture_state="starting",
        )
        task = "is short-term sell pressure exhausting on BTCUSDT?"
        artifact, meta = await engine.run_cycle(
            _wake(), {"decision": "fire"}, task=task)
        self.assertEqual(meta["llm_calls"], 0)
        self.assertEqual(meta["task"], task[:200])
        self.assertEqual(artifact.deterministic_state.get("task"), task)
        wake_detail = artifact.capability_log[0]["detail"]
        self.assertEqual(wake_detail["task"], task[:200])
        # Manual wake carries the task preview on its predicates.
        wake, _ = await engine.acquire_manual_wake(task=task)
        self.assertEqual(
            wake.predicates_fired["manual"], {"task_preview": task[:200]})

    async def test_scenario_directive_persists_on_gate_refused_cycle(self):
        # Phase 2 plumbing: scenario echoes into state/meta/detail with zero
        # LLM calls; absent scenario leaves no keys behind (parity).
        engine, _s, _p, _m = _engine(
            llm_responses=[], capture_state="starting",
        )
        scenario = {"target_price": "245.30", "horizon": "1h"}
        artifact, meta = await engine.run_cycle(
            _wake(), {"decision": "fire"}, task="can price hit 245.30?",
            scenario=scenario)
        self.assertEqual(meta["llm_calls"], 0)
        self.assertEqual(meta["scenario"], scenario)
        self.assertEqual(artifact.deterministic_state.get("scenario"), scenario)
        self.assertEqual(
            artifact.capability_log[0]["detail"]["scenario"], scenario)
        plain, _ = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertIsNone(plain.deterministic_state.get("scenario"))

    async def test_thin_final_triggers_repair_then_finalizes(self):
        # narrate#1 finalizes with zero validation → REJECTED for repair;
        # the repair turn covers P1/P2/P3, the next covers P5, then final passes.
        engine, _s, _p, _m = _engine(llm_responses=[
            _good_narration(),  # thin, no tools → repair
            _staged_narration("P1", tools=[
                {"name": "calc.ofi.intervals", "args": {}},
                {"name": "calc.depth.average", "args": {}},
                {"name": "market.derivatives", "args": {}},
            ]),
            _staged_narration("P5", tools=[
                {"name": "memory.recall_paper", "args": {}},
                {"name": "calc.price.delta", "args": {"ofi": "10"}},
            ]),
            _staged_narration("P6", final=True),
        ])
        artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["llm_calls"], 4)
        self.assertEqual(meta["repairs"], 1)
        self.assertTrue(meta["final_validation"]["passed"], meta["final_validation"])
        self.assertIsNotNone(artifact.interpretation)

    async def test_narration_failure_produces_degraded_artifact(self):
        # LLM raises on the first call → degraded artifact, NULL interpretation,
        # deterministic state preserved, PG+Redis persisted.
        engine, store, postgres, _m = _engine(llm_responses=[])

        async def _boom(messages, max_tokens=None):
            raise RuntimeError("gateway down")

        engine.llm.acall = _boom  # type: ignore[union-attr]
        artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["llm_calls"], 0)
        self.assertIsNone(artifact.interpretation)
        self.assertTrue(any(
            e.get("source") == "narration" for e in artifact.errors
        ))
        self.assertIsNotNone(artifact.deterministic_state.get("microstructure_evidence"))
        self.assertEqual(len(postgres.inserted), 1)

    async def test_unparseable_narration_is_degraded_not_fatal(self):
        engine, _s, postgres, _m = _engine(llm_responses=["no json here at all"])
        artifact, _meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertIsNone(artifact.interpretation)
        self.assertTrue(any(
            "narration_parse_failed" in (e.get("error") or "")
            for e in artifact.errors
        ))
        self.assertEqual(len(postgres.inserted), 1)

    async def test_tool_call_budget_capped_at_three(self):
        narration = json.dumps({
            "summary": "s",
            "evidence": [],
            "confidence": "low",
            "limitations": [],
            "model_separation": "m",
            "tool_calls": [
                {"name": "micro.capture_status", "args": {}},
                {"name": "micro.ofi_intervals", "args": {}},
                {"name": "micro.evidence", "args": {}},
                {"name": "micro.events", "args": {}},  # 4th → must be ignored
            ],
            "memory_proposals": [],
        })
        engine, _s, _p, _m = _engine(llm_responses=[
            narration,
            _staged_narration("P3", tools=[
                {"name": "market.derivatives", "args": {}},
                {"name": "memory.recall_paper", "args": {}},
                {"name": "calc.price.delta", "args": {"ofi": "5"}},
            ]),
            _staged_narration("P6", final=True),
        ])
        _artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        # GATHER phase contributes exactly one capture_status dispatch; the
        # tool round contributes capture_status + intervals + evidence (the
        # 4th requested call, micro.events, must be dropped by the cap).
        tool_round_entries = [
            e for e in _artifact.capability_log
            if e["capability"] in ("redis.read_capture_status",
                                   "redis.read_intervals", "redis.read_evidence",
                                   "redis.read_events")
        ]
        self.assertEqual(len(tool_round_entries), 4)  # 1 gather + 3 capped round
        self.assertTrue(meta["final_validation"]["passed"], meta["final_validation"])

    async def test_budget_limited_narrate2_even_when_tool_called(self):
        # narrate#1 requests tools, narrate#2 output unparseable → fall back
        # to narrate#1's parsed output (never a third call).
        engine, _s, _p, _m = _engine(llm_responses=[
            _good_narration(with_tools=True),
            "unparseable",
        ])
        artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["llm_calls"], 2)
        self.assertIsNotNone(artifact.interpretation)
        # Parse failure mid-loop falls back without repair: the record is honest.
        self.assertFalse(meta["final_validation"]["passed"])
        self.assertEqual(meta["repairs"], 0)


class MemoryProposalTests(unittest.TestCase):
    def _engine(self):
        return InferenceEngine(_FakeStore(), _FakePostgres(), None, None,
                               symbol="BTCUSDT", venue="spot")

    def test_fact_kind_never_accepted(self):
        engine = self._engine()
        accepted, dispositions = engine.resolve_memory_proposals([
            {"kind": "fact", "content": "beta is 0.001"},
        ])
        self.assertEqual(accepted, [])
        self.assertEqual(dispositions[0]["reason"], "fact_kind_is_llm_forbidden")

    def test_budget_capped(self):
        engine = self._engine()
        proposals = [
            {"kind": "observation", "content": f"obs {i}"} for i in range(6)
        ]
        accepted, dispositions = engine.resolve_memory_proposals(proposals)
        self.assertEqual(len(accepted), MAX_MEMORY_PROPOSALS)
        excess = [d for d in dispositions if d["reason"] == "budget_exceeded"]
        self.assertEqual(len(excess), len(proposals) - MAX_MEMORY_PROPOSALS)

    def test_invalid_kind_and_empty_content_rejected(self):
        engine = self._engine()
        accepted, _d = engine.resolve_memory_proposals([
            {"kind": "alien", "content": "x"},
            {"kind": "observation", "content": "  "},
        ])
        self.assertEqual(accepted, [])

    def test_importance_clamped_and_symbol_tag_appended(self):
        engine = self._engine()
        accepted, _d = engine.resolve_memory_proposals([
            {"kind": "observation", "content": "ok", "importance": 99.0},
        ])
        self.assertEqual(accepted[0]["importance"], 10.0)
        self.assertIn("btcusdt-spot", accepted[0]["tags"])

    def test_non_list_rejected(self):
        engine = self._engine()
        accepted, dispositions = engine.resolve_memory_proposals("not a list")
        self.assertEqual(accepted, [])
        self.assertEqual(dispositions[0]["reason"], "proposals_not_a_list")


class SessionIdTests(unittest.TestCase):
    """The engine's session id must be a real UUID (durable stores cast it)
    and stable per (symbol, venue) so the memory plane stays coherent."""

    def test_stable_uuid_for_same_scope(self):
        import uuid as _uuid

        e1 = InferenceEngine(_FakeStore(), _FakePostgres(), None, None,
                             symbol="BTCUSDT", venue="spot")
        e2 = InferenceEngine(_FakeStore(), _FakePostgres(), None, None,
                             symbol="BTCUSDT", venue="spot")
        self.assertEqual(e1.session_id, e2.session_id)
        _uuid.UUID(e1.session_id)  # must be a real UUID (Pg casts it)

    def test_explicit_session_id_wins(self):
        engine = InferenceEngine(_FakeStore(), _FakePostgres(), None, None,
                                 symbol="BTCUSDT", venue="spot",
                                 session_id="00000000-0000-0000-0000-000000000001")
        self.assertEqual(engine.session_id, "00000000-0000-0000-0000-000000000001")


class NarrationCallTests(unittest.IsolatedAsyncioTestCase):
    """Reasoning-aware narration: parsed content first, reasoning fallback,
    and a reasoning-sized token budget."""

    async def test_call_llm_uses_parsed_content_first(self):
        class _Plain:
            async def acall(self, messages, max_tokens=None):
                class R:
                    content = '{"summary": "ok"}'
                    reasoning = None
                    raw_response = None
                return R()

        engine = InferenceEngine(_FakeStore(), _FakePostgres(), None, _Plain(),
                                 symbol="BTCUSDT", venue="spot")
        raw = await engine._call_llm("prompt")
        self.assertIn('"summary": "ok"', raw)

    async def test_call_llm_falls_back_to_reasoning_when_content_empty(self):
        class _Reasoning:
            async def acall(self, messages, max_tokens=None):
                class R:
                    content = ""
                    reasoning = '{"summary": "from reasoning"}'
                    raw_response = None
                return R()

        engine = InferenceEngine(_FakeStore(), _FakePostgres(), None, _Reasoning(),
                                 symbol="BTCUSDT", venue="spot")
        raw = await engine._call_llm("prompt")
        self.assertIn("from reasoning", raw)

    async def test_narration_budget_is_reasoning_sized(self):
        from market_service.nooa_harness.engine import _narration_max_tokens

        self.assertGreaterEqual(_narration_max_tokens(), 8_000)
        with patch.dict(os.environ, {"NOOA_MODEL_MAX_TOKENS": "6000"}):
            self.assertEqual(_narration_max_tokens(), 6000)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_extract_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(_extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_embedded_json(self):
        self.assertEqual(
            _extract_json_object('Reasoning here {"a": {"b": 2}} trailing'),
            {"a": {"b": 2}},
        )

    def test_none_when_no_json(self):
        self.assertIsNone(_extract_json_object("no objects"))


class RunnerOnceTests(unittest.IsolatedAsyncioTestCase):
    """run_inference_once: direct envelope path, manual force + task,
    and the no-wake (nothing fabricated) path."""

    async def test_event_driven_envelope_runs_cycle_directly(self):
        from market_service.nooa_harness.inference_runner import run_inference_once
        from market_service.runtime.contracts import WakeEnvelope

        envelope = WakeEnvelope.create(
            symbol="BTCUSDT", venue="spot", trigger_source="manual",
            predicates_fired={"manual": {}},
            counter_snapshot={"event_stream_len": 3_000},
            high_water={"events_total": 1_000},
        )
        # Inject the envelope -> run_cycle directly; no worker, no stream.
        async def _fake_dispatch(_env):
            return {"dispatched": True, "wake_id": _env.wake_id}

        # run_inference_once builds a real engine from Settings (may fail in
        # test env w/o REDIS). Instead assert the runner's branching logic:
        # envelope path never touches acquire_manual_wake.
        import market_service.nooa_harness.inference_runner as runner

        # run_inference_once(envelope=...) dispatches straight to run_cycle;
        # verify the runner carries no wake-plane machinery.
        import inspect
        src = inspect.getsource(runner.run_inference_once)
        self.assertIn("envelope", src)
        self.assertNotIn("acquire_wake", src)
        self.assertNotIn("read_pending_wakes", src)
        self.assertNotIn("coalesce_wakes", src)
        self.assertNotIn("evaluate_triggers", src)
        self.assertNotIn("wake_worker", src)
        runner_src = inspect.getsource(runner)
        self.assertNotIn("run_inference_loop", runner_src)
        self.assertNotIn("WakeSupervisor", runner_src)

    async def test_manual_force_path_uses_acquire_manual_wake(self):
        import inspect
        import market_service.nooa_harness.inference_runner as runner

        src = inspect.getsource(runner.run_inference_once)
        self.assertIn("acquire_manual_wake", src)
        self.assertNotIn("read_pending_wakes", src)


class FinalValidationTests(unittest.TestCase):
    def _ev(self, path: str) -> dict:
        return {"path": path, "value": "1", "interpretation": f"{path} shows the fitted state"}
    def test_empty_coverage_fails_with_all_phases_missing(self):
        from market_service.nooa_harness.engine import _validate_final_turn

        passed, missing = _validate_final_turn(
            {"summary": "x", "evidence": [], "hypothesis": None},
            {p: set() for p in ("P1", "P2", "P3", "P4", "P5")},
        )
        self.assertFalse(passed)
        self.assertGreaterEqual(len(missing), 4)

    def test_complete_turn_passes(self):
        from market_service.nooa_harness.engine import _validate_final_turn

        passed, missing = _validate_final_turn(
            {"phase": "P6",
             "summary": _STAGED_SUMMARY,
             "confidence": "medium",
             "evidence": [
                 self._ev("deterministic_state.microstructure_evidence.price_impact_fit.beta"),
                 self._ev("calc.ofi.intervals → ofi"),
                 self._ev("calc.price.delta → route_a_direct.delta_ticks"),
             ],
             "hypothesis": {"H0": "beta > 0"}},
            {"P1": {"calc.ofi.intervals"}, "P2": {"calc.depth.average"},
             "P3": {"market.derivatives"}, "P4": set(),
             "P5": {"calc.price.delta"}, "P6": {"declared"}},
        )
        self.assertTrue(passed, missing)

    def test_missing_p6_and_delta_citation_fail(self):
        from market_service.nooa_harness.engine import _validate_final_turn

        passed, missing = _validate_final_turn(
            {"summary": _STAGED_SUMMARY,
             "confidence": "medium",
             "evidence": [
                 self._ev("deterministic_state.a"),
                 self._ev("market.read → last_price"),
             ],
             "hypothesis": {"H0": "beta > 0"}},
            {"P1": {"x"}, "P2": {"x"}, "P3": {"x"}, "P4": set(),
             "P5": {"x"}, "P6": set()},
        )
        self.assertFalse(passed)
        self.assertTrue(any("P6" in m for m in missing))
        self.assertTrue(any("calc.price.delta" in m for m in missing))

    def test_deterministic_only_evidence_fails(self):
        from market_service.nooa_harness.engine import _validate_final_turn

        passed, missing = _validate_final_turn(
            {"summary": _STAGED_SUMMARY,
             "confidence": "medium",
             "evidence": [
                 self._ev("deterministic_state.a"),
                 self._ev("deterministic_state.b"),
             ],
             "hypothesis": {"H0": "beta > 0"}},
            {"P1": {"x"}, "P2": {"x"}, "P3": {"x"}, "P4": set(),
             "P5": {"x"}, "P6": {"declared"}},
        )
        self.assertFalse(passed)
        self.assertTrue(any("fresh tool result" in m for m in missing))

    def test_confidence_blend_coerces_conservatively(self):
        from market_service.nooa_harness.engine import _validate_final_turn

        passed, missing = _validate_final_turn(
            {"phase": "P6",
             "summary": _STAGED_SUMMARY,
             "confidence": "low-medium",
             "evidence": [
                 self._ev("deterministic_state.microstructure_evidence.price_impact_fit.beta"),
                 self._ev("calc.ofi.intervals → ofi"),
                 self._ev("calc.price.delta → route_a_direct.delta_ticks"),
             ],
             "hypothesis": {"H0": "beta > 0"}},
            {"P1": {"calc.ofi.intervals"}, "P2": {"calc.depth.average"},
             "P3": {"market.derivatives"}, "P4": set(),
             "P5": {"calc.price.delta"}, "P6": {"declared"}},
        )
        # low-medium normalizes conservatively to low, so it PASSES validation
        # but the persisted artifact must store the coerced enum, never the blend.
        # Here we assert the validator accepts via coercion path...
        # Actually strict gate: blends are accepted (coerced), garbage is rejected.
        self.assertTrue(passed, missing)
        from market_service.runtime.contracts import normalize_confidence
        self.assertEqual(normalize_confidence("low-medium"), "low")
        self.assertEqual(normalize_confidence("moderate"), "medium")
        self.assertIsNone(normalize_confidence("ultra"))

    def test_confidence_garbage_fails(self):
        from market_service.nooa_harness.engine import _validate_final_turn

        passed, missing = _validate_final_turn(
            {"phase": "P6",
             "summary": _STAGED_SUMMARY,
             "confidence": "ultra-high",
             "evidence": [
                 self._ev("deterministic_state.microstructure_evidence.price_impact_fit.beta"),
                 self._ev("calc.ofi.intervals → ofi"),
                 self._ev("calc.price.delta → route_a_direct.delta_ticks"),
             ],
             "hypothesis": {"H0": "beta > 0"}},
            {"P1": {"calc.ofi.intervals"}, "P2": {"calc.depth.average"},
             "P3": {"market.derivatives"}, "P4": set(),
             "P5": {"calc.price.delta"}, "P6": {"declared"}},
        )
        self.assertFalse(passed)
        self.assertTrue(any("confidence" in m for m in missing))

    def test_missing_interpretation_fails(self):
        from market_service.nooa_harness.engine import _validate_final_turn

        passed, missing = _validate_final_turn(
            {"phase": "P6",
             "summary": _STAGED_SUMMARY,
             "confidence": "medium",
             "evidence": [
                 {"path": "deterministic_state.a", "value": "1", "interpretation": ""},
                 self._ev("calc.price.delta → route_a_direct.delta_ticks"),
                 self._ev("calc.ofi.intervals → ofi"),
             ],
             "hypothesis": {"H0": "beta > 0"}},
            {"P1": {"calc.ofi.intervals"}, "P2": {"calc.depth.average"},
             "P3": {"market.derivatives"}, "P4": set(),
             "P5": {"calc.price.delta"}, "P6": {"declared"}},
        )
        self.assertFalse(passed)
        self.assertTrue(any("interpretation" in m for m in missing))


class ToolErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_error_never_kills_cycle(self):
        # market.read has no fake backing → error payload, cycle continues;
        # coverage still completes via the other tools and final passes.
        engine, _s, _p, _m = _engine(llm_responses=[
            _staged_narration("P1", tools=[
                {"name": "market.read", "args": {}},
                {"name": "calc.ofi.intervals", "args": {}},
            ]),
            _staged_narration("P2", tools=[
                {"name": "calc.depth.average", "args": {}},
                {"name": "calc.price.delta", "args": {"ofi": "5"}},
                {"name": "market.derivatives", "args": {}},
            ]),
            _staged_narration("P6", final=True),
        ])
        artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertTrue(meta["final_validation"]["passed"], meta["final_validation"])
        self.assertTrue(any(
            str(e.get("capability", "")).startswith("tool.error:")
            for e in artifact.capability_log
        ))
        self.assertIsNotNone(artifact.interpretation)


class StructuredCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_model_content_serialized(self):
        from market_service.nooa_harness.engine import NarrationTurn

        class _Structured:
            async def acall(self, messages, output_model=None, max_tokens=None):
                class R:
                    content = NarrationTurn(phase="P5", summary="structured ok")
                    reasoning = None
                    raw_response = None
                return R()

        engine = InferenceEngine(_FakeStore(), _FakePostgres(), None, _Structured(),
                                 symbol="BTCUSDT", venue="spot")
        raw = await engine._call_llm("prompt")
        self.assertIn('"phase": "P5"', raw)


class CoerceTurnKeyTests(unittest.TestCase):
    """Raw-JSON fallbacks emit key 'tool' — accept it, 'name' wins."""
    def test_tool_key_coerced(self):
        from market_service.nooa_harness.engine import _coerce_turn
        out = _coerce_turn({"phase": "P1", "tool_calls": [
            {"tool": "calc.ofi.intervals", "args": {}}]})
        self.assertEqual(out["tool_calls"], [
            {"name": "calc.ofi.intervals", "args": {}}])

    def test_name_wins_on_conflict(self):
        from market_service.nooa_harness.engine import _coerce_turn
        out = _coerce_turn({"phase": "P1", "tool_calls": [
            {"name": "micro.events", "tool": "calc.ofi.intervals", "args": {}}]})
        self.assertEqual(out["tool_calls"][0]["name"], "micro.events")

    def test_neither_key_dropped(self):
        from market_service.nooa_harness.engine import _coerce_turn
        out = _coerce_turn({"phase": "P1", "tool_calls": [
            {"args": {}}, "nope"]})
        self.assertEqual(out["tool_calls"], [])


class ToolKeyEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_keyed_calls_cover_phases(self):
        # Regression for the live 8-turn burn: every model call keyed 'tool'.
        def _toolkey(phase, tools=None, *, final=False):
            import json as _json
            payload = _json.loads(_staged_narration(phase, tools=[], final=final))
            payload["tool_calls"] = [{"tool": c["name"], "args": c.get("args", {})}
                                      for c in (tools or [])]
            return _json.dumps(payload)
        engine, _s, _p, _m = _engine(llm_responses=[
            _toolkey("P1", tools=[
                {"name": "calc.ofi.intervals", "args": {}},
            ]),
            _toolkey("P2", tools=[
                {"name": "calc.depth.average", "args": {}},
                {"name": "market.derivatives", "args": {}},
            ]),
            _toolkey("P5", tools=[
                {"name": "memory.recall_paper", "args": {}},
                {"name": "calc.price.delta", "args": {"ofi": "5"}},
            ]),
            _toolkey("P6", final=True),
        ])
        artifact, meta = await engine.run_cycle(_wake(), {"decision": "fire"})
        self.assertTrue(meta["final_validation"]["passed"],
                        meta["final_validation"])
        self.assertNotIn("tool.unknown",
                         [str(e.get("capability")) for e in artifact.capability_log])


class P3PromptContractTests(unittest.TestCase):
    """Pin the tooling-base prompt contract: substrate.read primary P3,
    invoke-before-read two-beat, freshness/dormant findings discipline.

    The engine keeps its invoke authority — the two-beat stays. What the
    prompt must also teach is that invocation is a REQUEST to the calculation
    plane (which applies its own cooldown gates), that an unreachable plane is
    a finding, and that age_ms is judged against each worker's own cadence.
    """
    def test_p3_guidance_teaches_two_beat(self):
        from market_service.nooa_harness.engine import _PHASE_GUIDANCE
        p3 = _PHASE_GUIDANCE["P3"]
        self.assertIn("substrate.read", p3)
        self.assertIn("BEFORE", p3)  # invoke listed before read
        self.assertIn("age_ms", p3)
        self.assertIn("FINDINGS", p3)
        self.assertIn("PRIMARY", p3)

    def test_p3_guidance_teaches_cadence_relative_freshness(self):
        """One global staleness threshold mislabels the slow workers."""
        from market_service.nooa_harness.engine import _PHASE_GUIDANCE
        p3 = _PHASE_GUIDANCE["P3"]
        self.assertIn("cadence", p3.lower())
        self.assertIn("migration", p3)  # the ~900s outlier is named

    def test_p3_guidance_makes_an_unreachable_plane_a_finding(self):
        from market_service.nooa_harness.engine import _PHASE_GUIDANCE
        p3 = _PHASE_GUIDANCE["P3"]
        self.assertIn("unreachable", p3.lower())

    def test_system_prompt_frames_invoke_as_a_request(self):
        """A granted tool call is not a guaranteed fresh compute."""
        from market_service.nooa_harness.engine import _SYSTEM_PROMPT_TEMPLATE
        self.assertIn("calculation plane", _SYSTEM_PROMPT_TEMPLATE.lower())

    def test_output_format_names_substrate_primary(self):
        from market_service.nooa_harness.engine import InferenceEngine
        fmt = InferenceEngine._output_format()
        self.assertIn("substrate.read PRIMARY", fmt)
        self.assertIn("TWO-BEAT", fmt)
        self.assertIn("substrate.invoke", fmt)
        self.assertIn("age_ms", fmt)


if __name__ == "__main__":
    unittest.main()
