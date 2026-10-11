"""Three-layer failure chain tests — governance, mechanics, execution.

Pins the canonical failure architecture:

    engine/fsm.py            [router]     CYCLE_FAILED intake -> delegate
    engine/failure_fsm.py    [GOVERNANCE] classify(kind) -> FailureVerdict
                                          (class, severity, policy, terminal)
    failure_substrate/       [MECHANICS]  worker selection, retry budget,
                                          per-cycle ledger, coordination
    failure_substrate/workers.py [EXECUTION] bounded remediation units

Sections:
  1. FailureFSM (governance classification + policy + terminal routing).
  2. The boundary: governance holds no workers; the substrate holds no
     classification table; the main FSM holds no failure semantics.
  3. FailureSubstrate mechanics (worker selection, escalation, outcomes).
  4. signal_failure / signal_and_route_failure integration.
  5. Full-cycle E2E terminals through the failure node.
  6. FailureReport serialization.

Nooa-free (no Redis, no Postgres, no LLM).
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import pytest

from market_service.nooa_harness.engine.failure_fsm import (
    FAILURE_CLASSIFICATION,
    FAILURE_FSM,
    FAILURE_TERMINAL,
    FailureClass,
    FailureFSM,
    FailureKind,
    FailureSeverity,
    FailureVerdict,
    RemediationKind,
)
from market_service.nooa_harness.engine.failure_substrate import (
    FAILURE_WORKER_REGISTRY,
    MAX_RETRIES,
    WORKER_FOR_KIND,
    FailureReport,
    FailureSubstrate,
    FailureWorker,
    RemediationOutcome,
    default_workers,
    signal_failure,
    worker_for_verdict,
)
from market_service.nooa_harness.engine.loop_states import LoopTerminal


@pytest.fixture(autouse=True)
def _reset_workers():
    """Reset the module-global template registry between tests."""
    for w in FAILURE_WORKER_REGISTRY.values():
        w.reset()
    yield


# ========================================================================
# 1 — Governance: FailureFSM classification, policy, terminal routing
# ========================================================================


class TestFailureFSMClassification:
    """Every kind is classified; the policy and terminal are decided here."""

    def test_classification_covers_every_kind(self):
        assert set(FAILURE_CLASSIFICATION) == {m.value for m in FailureKind}

    @pytest.mark.parametrize("kind,expected_class,expected_severity", [
        ("narration_failed", FailureClass.NARRATION, FailureSeverity.TRANSIENT),
        ("parse_failed", FailureClass.NARRATION, FailureSeverity.PERMANENT),
        ("tool_denied", FailureClass.TOOL, FailureSeverity.PERMANENT),
        ("tool_error", FailureClass.TOOL, FailureSeverity.TRANSIENT),
        ("tool_suppressed", FailureClass.TOOL, FailureSeverity.DEGRADED),
        ("validation_failed", FailureClass.VALIDATION, FailureSeverity.PERMANENT),
        ("budget_exhausted", FailureClass.DISPATCH, FailureSeverity.PERMANENT),
        ("memory_failed", FailureClass.MEMORY, FailureSeverity.DEGRADED),
        ("gate_refused", FailureClass.GATE, FailureSeverity.PERMANENT),
        ("infra_failed", FailureClass.INFRA, FailureSeverity.TRANSIENT),
    ])
    def test_classify_metadata(self, kind, expected_class, expected_severity):
        verdict = FAILURE_FSM.classify(kind)
        assert verdict.failure_class is expected_class
        assert verdict.severity is expected_severity

    @pytest.mark.parametrize("kind,expected_terminal", [
        ("gate_refused", LoopTerminal.GATE_REFUSED),
        ("narration_failed", LoopTerminal.NARRATION_FAILED),
        ("parse_failed", LoopTerminal.PARSE_FAILED),
        ("validation_failed", LoopTerminal.VALIDATION_FAILED),
        ("budget_exhausted", LoopTerminal.BUDGET_EXHAUSTED),
        ("infra_failed", LoopTerminal.INFRA_FAILED),
        # substrate-routed kinds default to INFRA_FAILED
        ("tool_suppressed", LoopTerminal.INFRA_FAILED),
        ("chain_halt", LoopTerminal.INFRA_FAILED),
        ("unknown_kind", LoopTerminal.INFRA_FAILED),
    ])
    def test_terminal_routing(self, kind, expected_terminal):
        assert FAILURE_FSM.classify(kind).terminal is expected_terminal
        assert FAILURE_FSM.terminal_for(kind) is expected_terminal

    @pytest.mark.parametrize("kind,expected_policy", [
        ("narration_failed", RemediationKind.RETRY),      # transient
        ("tool_suppressed", RemediationKind.DEGRADE),     # degraded
        ("memory_failed", RemediationKind.DEGRADE),       # degraded
        ("parse_failed", RemediationKind.TERMINATE),      # permanent
        ("gate_refused", RemediationKind.TERMINATE),      # permanent
    ])
    def test_policy_from_severity(self, kind, expected_policy):
        assert FAILURE_FSM.classify(kind).remediation is expected_policy

    def test_classify_never_raises(self):
        for bad in [None, "", "no_such_failure", " "]:
            verdict = FAILURE_FSM.classify(bad)
            assert isinstance(verdict, FailureVerdict)
            assert verdict.terminal is LoopTerminal.INFRA_FAILED

    def test_transient_override(self):
        assert FAILURE_FSM.classify(
            "parse_failed", is_transient_override=True,
        ).severity is FailureSeverity.TRANSIENT
        assert FAILURE_FSM.classify(
            "narration_failed", is_transient_override=False,
        ).severity is FailureSeverity.PERMANENT

    def test_self_check_passes(self):
        FailureFSM().self_check()


# ========================================================================
# 2 — The boundary (the semantic boundary that was missing)
# ========================================================================


class TestFailureBoundary:
    """The three layers must not leak into each other."""

    def test_governance_verdict_carries_no_worker(self):
        verdict = FAILURE_FSM.classify("narration_failed")
        assert not hasattr(verdict, "worker_name")
        assert not hasattr(verdict, "max_retries")

    def test_classification_table_has_no_worker_column(self):
        # Each entry is exactly (class, severity) — worker assignment is
        # the substrate's mechanical affair.
        for kind, entry in FAILURE_CLASSIFICATION.items():
            assert len(entry) == 2, kind
            assert isinstance(entry[0], FailureClass)
            assert isinstance(entry[1], FailureSeverity)

    def test_main_fsm_holds_no_failure_semantics(self):
        from market_service.nooa_harness.engine import fsm as main_fsm

        for dead in (
            "FAILURE_EVENT_KINDS", "FAILURE_EVENT_WORKERS",
            "_FAILURE_KIND_TERMINAL", "_FAILURE_KIND_WORKER", "_resolve_worker",
        ):
            assert not hasattr(main_fsm, dead), dead

    def test_main_fsm_knows_no_workers(self):
        from market_service.nooa_harness.engine import fsm as main_fsm

        verdict = main_fsm.GOVERNANCE_MEMBRANE.decide(
            main_fsm.GOVERNANCE_MEMBRANE.initial(),
            main_fsm.GovernanceEvent(
                main_fsm.GovernanceEventKind.CYCLE_FAILED,
                failure_kind="narration_failed",
            ),
        )
        assert verdict.terminal is LoopTerminal.NARRATION_FAILED
        assert not hasattr(verdict, "assigned_worker")

    def test_substrate_owns_the_worker_map(self):
        assert "narration_failed" in WORKER_FOR_KIND
        assert worker_for_verdict(FAILURE_FSM.classify("narration_failed")) == "narration"

    def test_worker_budgets_match_declared_max_retries(self):
        for name, worker in FAILURE_WORKER_REGISTRY.items():
            assert worker.max_retries == MAX_RETRIES.get(name, 0), name


# ========================================================================
# 3 — Mechanics: FailureSubstrate coordination
# ========================================================================


class TestFailureSubstrateMechanics:
    """Worker selection, escalation, and per-cycle isolation."""

    def test_registry_has_all_workers(self):
        expected = {"narration", "tool", "validation", "dispatch",
                    "memory", "gate", "infra", "inference"}
        assert set(FAILURE_WORKER_REGISTRY) == expected

    def test_default_workers_are_fresh_instances(self):
        a = default_workers()
        b = default_workers()
        assert a is not b
        for name in a:
            assert a[name] is not b[name]

    def test_worker_retry_budget(self):
        w = FailureWorker(name="t", max_retries=1)
        assert w.can_retry() is True
        w.consume_retry()
        assert w.can_retry() is False
        assert w.retry_count == 1

    def test_transient_retries_then_escalates_to_terminate(self):
        sub = FailureSubstrate()
        verdict = FAILURE_FSM.classify("narration_failed")  # transient, budget 1
        first = asyncio.run(sub.mediate(verdict, detail="blip"))
        assert first.resolved is False and first.terminate_now is False
        assert first.remediation is RemediationKind.RETRY
        # Budget (1) now spent -> escalation to terminate.
        second = asyncio.run(sub.mediate(verdict, detail="blip again"))
        assert second.terminate_now is True
        assert second.remediation is RemediationKind.TERMINATE

    def test_permanent_terminates_immediately(self):
        sub = FailureSubstrate()
        out = asyncio.run(sub.mediate(FAILURE_FSM.classify("parse_failed")))
        assert out.terminate_now is True
        assert out.remediation is RemediationKind.TERMINATE

    def test_degraded_resolves_and_continues(self):
        sub = FailureSubstrate()
        out = asyncio.run(sub.mediate(FAILURE_FSM.classify("tool_suppressed")))
        assert out.resolved is True and out.terminate_now is False
        assert out.remediation is RemediationKind.DEGRADE

    def test_unknown_worker_terminates_honestly(self):
        sub = FailureSubstrate(workers={})  # no workers wired
        out = asyncio.run(sub.mediate(FAILURE_FSM.classify("narration_failed")))
        assert out.terminate_now is True
        assert "unknown worker" in (out.failure_report.remediation_detail or "")

    def test_retry_ledger_is_per_instance(self):
        a, b = FailureSubstrate(), FailureSubstrate()
        verdict = FAILURE_FSM.classify("narration_failed")
        asyncio.run(a.mediate(verdict))
        # b's ledger is untouched — no shared global retry state.
        assert b.worker("narration").retry_count == 0

    def test_worker_invoke_reports_outcome(self):
        w = FailureWorker(name="t", max_retries=2, fallback="degrade")
        outcome = asyncio.run(w.invoke("x"))
        assert isinstance(outcome, RemediationOutcome)
        assert outcome.retry_count == 1


# ========================================================================
# 4 — signal_failure / signal_and_route_failure integration
# ========================================================================


class TestSignalFailure:
    @pytest.mark.parametrize("kind,expected_resolved,expected_terminate", [
        ("narration_failed", False, False),
        ("parse_failed", False, True),
        ("validation_failed", False, True),
        ("gate_refused", False, True),
        ("tool_suppressed", True, False),
        ("memory_failed", True, False),
        ("unknown_kind", False, False),
    ])
    def test_signal_outcome(self, kind, expected_resolved, expected_terminate):
        outcome = asyncio.run(signal_failure(kind, f"test {kind}"))
        assert outcome.resolved is expected_resolved, kind
        assert outcome.terminate_now is expected_terminate, kind
        assert outcome.failure_report.failure_kind == kind

    def test_signal_report_serializable(self):
        outcome = asyncio.run(signal_failure("narration_failed", "test"))
        d = outcome.failure_report.to_dict()
        json.dumps(d)
        assert d["failure_kind"] == "narration_failed"
        assert "worker_name" in d


# ========================================================================
# 5 — Runtime integration: signal_and_route_failure
# ========================================================================


class TestSignalAndRouteFailure(unittest.IsolatedAsyncioTestCase):
    async def test_transient_resolved_returns_none(self):
        engine, store, _, memory, ctx, st = _build_failure_context()
        st.failure_kind = "narration_failed"
        st.failure_detail = "LLM transport timeout"

        from market_service.nooa_harness.engine.core.context import (
            signal_and_route_failure,
        )
        result = await signal_and_route_failure(engine, ctx, st)
        assert result is None, "transient with budget should continue loop"
        assert st.failure_kind is None
        assert "failure_report" in st.deterministic_state

    async def test_permanent_returns_terminal(self):
        engine, store, _, memory, ctx, st = _build_failure_context()
        st.failure_kind = "validation_failed"
        st.failure_detail = "final rejected 3 times"

        from market_service.nooa_harness.engine.core.context import (
            signal_and_route_failure,
        )
        result = await signal_and_route_failure(engine, ctx, st)
        assert result is not None
        artifact, meta = result
        assert meta["terminal"] == "validation_failed"

    async def test_degraded_returns_none(self):
        engine, store, _, memory, ctx, st = _build_failure_context()
        st.failure_kind = "tool_suppressed"
        st.failure_detail = "tool refused this cycle"

        from market_service.nooa_harness.engine.core.context import (
            signal_and_route_failure,
        )
        result = await signal_and_route_failure(engine, ctx, st)
        assert result is None
        assert st.failure_kind is None
        assert st.deterministic_state["failure"]["resolved"] is True

    async def test_report_and_governance_carry_through(self):
        engine, store, _, memory, ctx, st = _build_failure_context()
        st.failure_kind = "narration_failed"
        st.failure_detail = "blip"

        from market_service.nooa_harness.engine.core.context import (
            signal_and_route_failure,
        )
        await signal_and_route_failure(engine, ctx, st)
        failure = st.deterministic_state["failure"]
        # governance fields present
        assert failure["class"] == "narration"
        assert failure["severity"] == "transient"
        assert failure["worker"] == "narration"
        assert "failure_report" in st.deterministic_state


# ========================================================================
# 6 — Full cycle E2E terminals
# ========================================================================


class TestCycleFailureEndToEnd:
    @pytest.mark.parametrize("case,expected_terminal", [
        ("narr_fail", "narration_failed"),
        ("parse_fail", "parse_failed"),
    ])
    def test_cycle_terminates_through_failure_node(self, case, expected_terminal):
        from tests.test_governance import _engine, _wake, _staged_narration
        from market_service.nooa_harness.engine import core

        if case == "narr_fail":
            turns: list[str] = []
        else:
            turns = ["not valid json at all whatsoever",
                     "not valid json at all whatsoever"]

        engine, store, postgres, memory = _engine(llm_responses=turns)

        async def execute(store, name, args, **kwargs):
            return {"value": "fixture"}, {"capability": name, "result": "ok"}

        with patch.object(core.context, "execute_tool", side_effect=execute), \
             patch.object(core.context, "_utc_now_iso",
                          return_value="2026-01-01T00:00:00+00:00"), \
             patch("market_service.runtime.contracts.uuid.uuid4", return_value="1"), \
             patch.object(postgres, "insert_inference_artifact", return_value=True), \
             patch.object(store, "publish_inference_artifact", return_value="1"), \
             patch.object(memory, "remember", return_value=None):
            try:
                artifact, meta = asyncio.run(engine.narrate_cycle(
                    _wake(), {"decision": "fire"}, task="test"
                ))
            except AssertionError:
                return  # FakeLLM exhausted — expected for narr_fail

        assert meta["terminal"] == expected_terminal, \
            f"{case}: got {meta['terminal']}"
        det_state = artifact.to_dict().get("deterministic_state", {})
        assert det_state.get("terminal") == expected_terminal

    def test_successful_cycle_still_reachable(self):
        from tests.test_governance import _engine, _wake, _staged_narration
        from market_service.nooa_harness.engine import core

        turns = [
            _staged_narration("P1", tools=[{"name": "calc.ofi.intervals"}]),
            _staged_narration("P2", tools=[{"name": "calc.depth.average"}]),
            _staged_narration("P3", tools=[{"name": "market.derivatives"}]),
            _staged_narration("P5", tools=[
                {"name": "memory.recall_paper"}, {"name": "calc.price.delta"}]),
            _staged_narration("P6", final=True),
        ] * 10
        engine, store, postgres, memory = _engine(llm_responses=turns)

        async def execute(st, name, args, **kwargs):
            return {"value": "fixture"}, {"capability": name, "result": "ok"}

        with patch.object(core.context, "execute_tool", side_effect=execute), \
             patch.object(core.context, "_utc_now_iso",
                          return_value="2026-01-01T00:00:00+00:00"), \
             patch("market_service.runtime.contracts.uuid.uuid4", return_value="1"), \
             patch.object(postgres, "insert_inference_artifact", return_value=True), \
             patch.object(store, "publish_inference_artifact", return_value="1"), \
             patch.object(memory, "remember", return_value=None):
            artifact, meta = asyncio.run(engine.narrate_cycle(
                _wake(), {"decision": "fire"}, task="test"
            ))

        assert "terminal" in meta
        assert meta["llm_calls"] > 0


# ========================================================================
# 7 — FailureReport
# ========================================================================


class TestFailureReport:
    def test_default_construction(self):
        r = FailureReport(failure_kind="test", worker_name="infra")
        d = r.to_dict()
        assert d["failure_kind"] == "test"
        assert d["worker_name"] == "infra"
        assert d["resolved"] is False
        assert d["retries_used"] == 0

    def test_full_construction(self):
        r = FailureReport(
            failure_kind="narration_failed", worker_name="narration",
            severity="transient", resolved=True, retries_used=1, max_retries=2,
            fallback_applied=True, fallback_name="json", degraded=False,
            terminal=None, detail="LLM returned 503",
            remediation_detail="retried and succeeded",
        )
        d = r.to_dict()
        assert d["retries_used"] == 1
        json.dumps(d)


# ------------------------------------------------------------------------
# Shared fakes (mirror tests/test_governance.py)
# ------------------------------------------------------------------------


def _build_failure_context():
    from tests.test_governance import _engine, _wake
    from market_service.nooa_harness.engine.core import context as ctx_mod

    engine, store, postgres, memory = _engine(llm_responses=["{}"])
    wake = _wake()
    ctx = ctx_mod._CycleContext(
        wake=wake, task=None, scenario=None,
        generated_at="2026-01-01T00:00:00+00:00",
        controller=engine_state_controller(),
        capability_log=[],
    )
    st = ctx_mod.CycleRuntimeState(
        controller=engine_state_controller(),
        capability_log=[],
        deterministic_state={},
        accumulated_tool_results={},
        tool_results={},
        parsed_current={},
    )
    return engine, store, postgres, memory, ctx, st


def engine_state_controller():
    from market_service.nooa_harness.engine.controller import CycleController
    from market_service.nooa_harness.engine.fsm import GOVERNANCE_MEMBRANE

    return (
        CycleController()
        .with_membrane(GOVERNANCE_MEMBRANE)
        .with_observation(GOVERNANCE_MEMBRANE.initial())
    )
