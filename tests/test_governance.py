"""Governance-chain tests — controller under the membrane, E2E terminals.

Part 1 (controller unit): the controller sits UNDER the membrane. With a
membrane + observation, ``govern``/``advance`` route through the membrane's
verdicts; legacy (no membrane) stays permissive; everything is immutable.

Part 2 (E2E through run_cycle fakes): failures the loop observes are
reported by kind and land as ``cycle_meta["terminal"]`` and
``deterministic_state["terminal"]``:
  - narration parse failure  -> "parse_failed"
  - LLM/tool budget exhaustion -> "budget_exhausted"
  - hard gate insufficient   -> "gate_refused"
  - successful cycle         -> "settled"
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from market_service.nooa_harness.engine import (
    CycleController,
    GOVERNANCE_MEMBRANE,
    GovernanceEvent,
    GovernanceEventKind,
    LoopTerminal,
)
from market_service.nooa_harness.engine.loop_states import (
    NestedLoop,
    SubLoop,
    TaskIntent,
)
from market_service.nooa_harness.engine import InferenceEngine
from market_service.runtime.contracts import InferenceArtifact, WakeEnvelope

M = GOVERNANCE_MEMBRANE


# ---------------------------------------------------------------------------
# Fakes — mirror tests/test_engine.py so the E2E terminal tests run against
# the same deterministic surface (no Redis, no Postgres, no litellm).
# ---------------------------------------------------------------------------


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
            {"name": "micro.capture_status", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
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
    return WakeEnvelope.create(
        symbol="BTCUSDT", venue="spot", trigger_source="watcher",
        predicates_fired={"cold_start": {}},
        counter_snapshot={}, high_water={},
    )



class ControllerGovernanceUnitTests(unittest.TestCase):
    def test_legacy_permissive_without_membrane(self):
        c = CycleController({"target_price": "100", "horizon": "15m"})
        v = c.govern(GovernanceEvent(GovernanceEventKind.SETTLE))
        self.assertTrue(v.allowed)  # legacy: controller is sole authority

    def test_govern_denied_without_observation(self):
        c = CycleController().with_membrane(M)
        v = c.govern(GovernanceEvent(GovernanceEventKind.SETTLE))
        self.assertFalse(v.allowed)

    def test_govern_allowed_move_changes_nothing_on_self(self):
        c = CycleController().with_membrane(M).with_observation(M.initial())
        v = c.govern(
            GovernanceEvent(
                GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE
            )
        )
        self.assertTrue(v.allowed)
        self.assertIsNone(c.terminal)  # self untouched

    def test_advance_is_immutable_and_moves_observation(self):
        c = CycleController().with_membrane(M).with_observation(M.initial())
        c2 = c.advance(
            GovernanceEvent(GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE)
        )
        self.assertIsNone(c.observation.sub_loop)  # original unchanged
        self.assertIs(c2.observation.sub_loop, SubLoop.INTAKE)
        self.assertIsNone(c2.terminal)

    def test_advance_records_terminal_and_clears_observation(self):
        c = (
            CycleController()
            .with_membrane(M)
            .with_observation(M.initial())
        )
        c2 = c.advance(GovernanceEvent(GovernanceEventKind.GATE_REFUSED))
        self.assertEqual(c2.terminal, LoopTerminal.GATE_REFUSED)
        self.assertIsNone(c2.observation)
        self.assertIsNone(c.terminal)  # original untouched

    def test_advance_denied_returns_self(self):
        c = CycleController().with_membrane(M).with_observation(M.initial())
        c2 = c.advance(
            GovernanceEvent(
                GovernanceEventKind.ENTER_LOOP, NestedLoop.REASONING
            )
        )
        self.assertIs(c2, c)

    def test_legal_traversal_to_settled(self):
        c = CycleController().with_membrane(M).with_observation(M.initial())
        moves = [
            (GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE),
            (GovernanceEventKind.CLOSE_SUBLOOP, None),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.EVIDENCE),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.REASONING),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.VALIDATION),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.OUTPUT),
            (GovernanceEventKind.SETTLE, None),
        ]
        for kind, target in moves:
            event = GovernanceEvent(kind, target)
            self.assertTrue(c.govern(event).allowed, kind)
            c = c.advance(event)
            if c.terminal is not None:
                break
        self.assertEqual(c.terminal, LoopTerminal.SETTLED)
        # Ledger (outcomes/coverage) survived the traversal untouched.
        self.assertIsNone(c.observation)


class EvidenceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_gate_reads_run_inside_acquisition(self):
        from unittest.mock import patch

        from market_service.nooa_harness.engine import core

        engine, _s, _p, _m = _engine(llm_responses=[], capture_state="starting")
        govern = core._govern
        execute = core.execute_tool
        observation = None
        requests = []
        reads = []

        def observe_govern(controller, kind, target=None):
            nonlocal observation
            successor, verdict = govern(controller, kind, target)
            observation = successor.observation
            requests.append((kind, target, verdict.allowed))
            return successor, verdict

        async def observe_read(store, name, args, **kwargs):
            reads.append((name, observation))
            return await execute(store, name, args, **kwargs)

        with patch.object(core, "_govern", side_effect=observe_govern), patch.object(
            core, "execute_tool", side_effect=observe_read,
        ):
            artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})

        self.assertIn(
            (GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ACQUISITION, True), requests,
        )
        self.assertEqual([name for name, _ in reads], [
            "micro.capture_status", "micro.fit_beta",
        ])
        for name, position in reads:
            self.assertIs(position.nested_loop, NestedLoop.EVIDENCE, name)
            self.assertIs(position.sub_loop, SubLoop.ACQUISITION, name)
        self.assertEqual(meta["llm_calls"], 0)
        self.assertEqual(artifact.deterministic_state["terminal"], meta["terminal"])


class E2ETerminalTests(unittest.IsolatedAsyncioTestCase):
    """The terminal the membrane routes to lands in BOTH cycle_meta and
    deterministic_state — the graceful-failure contract end to end."""

    async def test_success_cycle_terminals_settled(self):
        engine, _s, _p, _m = _engine(llm_responses=[
            _staged_narration("P1", tools=[
                {"name": "calc.ofi.intervals",
                 "args": {"symbol": "BTCUSDT", "venue": "spot"}},
                {"name": "micro.ofi_intervals",
                 "args": {"symbol": "BTCUSDT", "venue": "spot"}},
            ]),
            _staged_narration("P2", tools=[
                {"name": "calc.depth.average",
                 "args": {"symbol": "BTCUSDT", "venue": "spot"}},
                {"name": "market.derivatives",
                 "args": {"symbol": "BTCUSDT", "venue": "spot"}},
            ]),
            _staged_narration("P5", tools=[
                {"name": "memory.recall_paper",
                 "args": {"symbol": "BTCUSDT", "venue": "spot"}},
                {"name": "calc.price.delta",
                 "args": {"symbol": "BTCUSDT", "venue": "spot", "ofi": "10"}},
            ]),
            _staged_narration("P6", final=True),
        ])
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["terminal"], "settled")
        self.assertEqual(artifact.deterministic_state.get("terminal"), "settled")
        self.assertTrue(meta["final_validation"]["passed"])

    async def test_gate_insufficient_terminals_gate_refused(self):
        engine, _s, _p, _m = _engine(
            llm_responses=[], capture_state="starting",
        )
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["terminal"], "gate_refused")
        self.assertEqual(artifact.deterministic_state.get("terminal"), "gate_refused")
        self.assertEqual(meta["llm_calls"], 0)

    async def test_parse_failure_terminals_parse_failed(self):
        engine, _s, _p, _m = _engine(llm_responses=["no json here at all"])
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["terminal"], "parse_failed")
        self.assertEqual(artifact.deterministic_state.get("terminal"), "parse_failed")
        self.assertIsNone(artifact.interpretation)

    async def test_narration_failure_terminals_narration_failed(self):
        engine, _s, _p, _m = _engine(llm_responses=[])

        async def _boom(messages, max_tokens=None):
            raise RuntimeError("gateway down")

        engine.llm.acall = _boom  # type: ignore[union-attr]
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["terminal"], "narration_failed")
        self.assertEqual(artifact.deterministic_state.get("terminal"), "narration_failed")

    async def test_budget_exhaustion_terminals_budget_exhausted(self):
        # Round follow-up is unparseable -> the loop breaks without a passing
        # final -> BUDGET_EXHAUSTED, honestly recorded beside the meta + state.
        engine, _s, _p, _m = _engine(llm_responses=[
            _good_narration(with_tools=True),
            "unparseable",
        ])
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        self.assertEqual(meta["terminal"], "budget_exhausted")
        self.assertEqual(artifact.deterministic_state.get("terminal"), "budget_exhausted")
        self.assertFalse(meta["final_validation"]["passed"])


if __name__ == "__main__":
    unittest.main()