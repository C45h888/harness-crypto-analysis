"""Membrane (Layer 2 / fsm.py) tests — the governance constitution.

Pins the laws of ``engine/fsm.py``, the primary governance membrane:

- the board is viewable: every admissible observation is enumerable,
  every legal transition exists, every terminal is named;
- legality is derived ONLY from loop_states (no state names defined here);
- ENTER_LOOP / SET_TASK / OPEN_SUBLOOP / CLOSE_SUBLOOP / SETTLE follow the
  constitution exactly;
- failure kinds route to their terminals from EVERY state (graceful);
- every (state x event-kind) pair yields a verdict — never a raise.

The old FSM vocabulary (LoopState / LoopEvent / AgentLoopFSM) is dead.
"""

from __future__ import annotations

import unittest

from market_service.nooa_harness.engine.fsm import (
    FAILURE_EVENT_KINDS,
    GOVERNANCE_MEMBRANE,
    AgenticLoopMembrane,
    GovernanceEvent,
    GovernanceEventKind,
    MembraneVerdict,
)
from market_service.nooa_harness.engine.loop_states import (
    INTENT_LOOP,
    LOOP_INTENTS,
    LOOP_SUBLOOPS,
    AgenticStage,
    LoopObservation,
    LoopTerminal,
    NestedLoop,
    SubLoop,
    TaskIntent,
)

M = GOVERNANCE_MEMBRANE


class BoardViewabilityTests(unittest.TestCase):
    def test_initial_state_is_comprehension(self):
        self.assertEqual(
            M.initial(),
            LoopObservation(
                NestedLoop.COMPREHENSION, TaskIntent.UNDERSTAND_TASK, None
            ),
        )

    def test_board_state_count_is_41(self):
        # 4 + 8 + 16 + 3 + 10 = 41 admissible (loop, intent, sub-loop|None).
        self.assertEqual(len(M.states()), 41)

    def test_every_state_is_legal(self):
        for obs in M.states():
            self.assertTrue(M.is_legal_state(obs), obs)

    def test_states_follow_traversal_loop_order(self):
        loops = [obs.nested_loop for obs in M.states()]
        self.assertIs(loops[0], NestedLoop.COMPREHENSION)
        self.assertIs(loops[-1], NestedLoop.OUTPUT)

    def test_rejects_illegal_observations(self):
        # intent served by a different loop.
        self.assertFalse(
            M.is_legal_state(
                LoopObservation(NestedLoop.EVIDENCE, TaskIntent.VALIDATE_FINAL)
            )
        )
        # sub-loop not a sub-loop of the loop.
        self.assertFalse(
            M.is_legal_state(
                LoopObservation(
                    NestedLoop.EVIDENCE, TaskIntent.INFER_DEPTH, SubLoop.FRAMING
                )
            )
        )
        self.assertFalse(M.is_legal_state("not an observation"))

    def test_all_transitions_land_on_legal_states_or_terminals(self):
        terminals = set(M.terminals())
        for state, _event, outcome in M.all_transitions():
            if isinstance(outcome, LoopTerminal):
                self.assertIn(outcome, terminals)
            else:
                self.assertTrue(M.is_legal_state(outcome), outcome)

    def test_render_contains_every_stage(self):
        rendered = M.render()
        for stage in AgenticStage:
            self.assertIn(stage.value, rendered)

    def test_canonical_instance_is_shared(self):
        import market_service.nooa_harness.engine as engine

        self.assertIs(engine.GOVERNANCE_MEMBRANE, M)


class CanonicalAuthorityTests(unittest.TestCase):
    def test_runtime_uses_fsm_and_scratch_module_is_absent(self):
        from pathlib import Path

        from market_service.nooa_harness.engine import controller, core, fsm

        self.assertFalse(Path(fsm.__file__).with_name("membrane.py").exists())
        self.assertIs(core.GOVERNANCE_MEMBRANE, fsm.GOVERNANCE_MEMBRANE)
        self.assertIs(controller.AgenticLoopMembrane, fsm.AgenticLoopMembrane)
        self.assertIs(controller.GovernanceEvent, fsm.GovernanceEvent)


class LegalEventsTests(unittest.TestCase):
    def test_initial_offers_open_first_subloop_enter_evidence_failure(self):
        events = M.legal_events(M.initial())
        kinds = {e.kind for e in events}
        self.assertIn(GovernanceEventKind.OPEN_SUBLOOP, kinds)
        self.assertIn(GovernanceEventKind.ENTER_LOOP, kinds)
        # No close (nothing open), no settle (not OUTPUT).
        self.assertNotIn(GovernanceEventKind.CLOSE_SUBLOOP, kinds)
        self.assertNotIn(GovernanceEventKind.SETTLE, kinds)
        # Failures are legal everywhere (graceful).
        for kind in FAILURE_EVENT_KINDS:
            self.assertIn(kind, kinds)

    def test_output_offers_settle(self):
        obs = LoopObservation(
            NestedLoop.OUTPUT, TaskIntent.PLACE_OUTPUT, SubLoop.PLACEMENT
        )
        self.assertIn(
            GovernanceEventKind.SETTLE, {e.kind for e in M.legal_events(obs)}
        )

    def test_enter_loop_requires_closed_subloop_in_legal_events(self):
        obs = LoopObservation(
            NestedLoop.EVIDENCE, TaskIntent.INFER_DEPTH, SubLoop.ACQUISITION
        )
        kinds = {e.kind for e in M.legal_events(obs)}
        self.assertNotIn(GovernanceEventKind.ENTER_LOOP, kinds)

    def test_legal_events_never_include_failures_as_progress(self):
        for state in M.states():
            for event in M.legal_events(state):
                self.assertIsInstance(event, GovernanceEvent)


class DecideConstitutionTests(unittest.TestCase):
    def test_enter_loop_only_to_successor(self):
        v = M.decide(
            M.initial(), GovernanceEvent(GovernanceEventKind.ENTER_LOOP, NestedLoop.EVIDENCE)
        )
        self.assertTrue(v.allowed)
        self.assertEqual(v.next, LoopObservation(NestedLoop.EVIDENCE, TaskIntent.INFER_ORDER_FLOW, None))

        # Not the successor -> denied.
        v2 = M.decide(
            M.initial(), GovernanceEvent(GovernanceEventKind.ENTER_LOOP, NestedLoop.REASONING)
        )
        self.assertFalse(v2.allowed)

        # Entering the first loop from initial is not legal (already there).
        v3 = M.decide(
            M.initial(), GovernanceEvent(GovernanceEventKind.ENTER_LOOP, NestedLoop.COMPREHENSION)
        )
        self.assertFalse(v3.allowed)

    def test_enter_loop_requires_closed_subloop(self):
        obs = LoopObservation(
            NestedLoop.EVIDENCE, TaskIntent.INFER_DEPTH, SubLoop.ACQUISITION
        )
        v = M.decide(
            obs, GovernanceEvent(GovernanceEventKind.ENTER_LOOP, NestedLoop.REASONING)
        )
        self.assertFalse(v.allowed)

    def test_open_subloop_only_next_in_order(self):
        # From initial, only INTAKE is openable.
        self.assertTrue(
            M.decide(
                M.initial(),
                GovernanceEvent(GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE),
            ).allowed
        )
        self.assertFalse(
            M.decide(
                M.initial(),
                GovernanceEvent(GovernanceEventKind.OPEN_SUBLOOP, SubLoop.FRAMING),
            ).allowed
        )
        # After INTAKE, only INTERPRETATION.
        obs = LoopObservation(
            NestedLoop.COMPREHENSION, TaskIntent.UNDERSTAND_TASK, SubLoop.INTAKE
        )
        self.assertTrue(
            M.decide(
                obs,
                GovernanceEvent(GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTERPRETATION),
            ).allowed
        )
        self.assertFalse(
            M.decide(
                obs,
                GovernanceEvent(GovernanceEventKind.OPEN_SUBLOOP, SubLoop.FRAMING),
            ).allowed
        )

    def test_set_task_only_intents_served_by_loop(self):
        ev_obs = LoopObservation(NestedLoop.EVIDENCE, TaskIntent.INFER_ORDER_FLOW)
        self.assertTrue(
            M.decide(
                ev_obs, GovernanceEvent(GovernanceEventKind.SET_TASK, TaskIntent.INFER_DEPTH)
            ).allowed
        )
        self.assertFalse(
            M.decide(
                M.initial(), GovernanceEvent(GovernanceEventKind.SET_TASK, TaskIntent.INFER_DEPTH)
            ).allowed
        )

    def test_close_subloop_only_when_open(self):
        self.assertFalse(
            M.decide(
                M.initial(), GovernanceEvent(GovernanceEventKind.CLOSE_SUBLOOP)
            ).allowed
        )
        obs = LoopObservation(
            NestedLoop.EVIDENCE, TaskIntent.INFER_DEPTH, SubLoop.SOURCING
        )
        v = M.decide(obs, GovernanceEvent(GovernanceEventKind.CLOSE_SUBLOOP))
        self.assertTrue(v.allowed)
        self.assertIsNone(v.next.sub_loop)

    def test_settle_only_from_output(self):
        self.assertFalse(
            M.decide(M.initial(), GovernanceEvent(GovernanceEventKind.SETTLE)).allowed
        )
        for loop in (NestedLoop.EVIDENCE, NestedLoop.REASONING, NestedLoop.VALIDATION):
            obs = LoopObservation(loop, LOOP_INTENTS[loop][0], None)
            self.assertFalse(
                M.decide(obs, GovernanceEvent(GovernanceEventKind.SETTLE)).allowed,
                loop,
            )
        obs = LoopObservation(NestedLoop.OUTPUT, TaskIntent.PLACE_OUTPUT, None)
        v = M.decide(obs, GovernanceEvent(GovernanceEventKind.SETTLE))
        self.assertTrue(v.allowed)
        self.assertEqual(v.terminal, LoopTerminal.SETTLED)

    def test_legal_traversal_reaches_output_settled(self):
        # A canonical walk: comprehension -> evidence -> reasoning ->
        # validation -> output -> settle, all adjudicated allowed.
        moves = [
            (GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE),
            (GovernanceEventKind.CLOSE_SUBLOOP, None),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.EVIDENCE),
            (GovernanceEventKind.SET_TASK, TaskIntent.INFER_DEPTH),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.REASONING),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.VALIDATION),
            (GovernanceEventKind.ENTER_LOOP, NestedLoop.OUTPUT),
            (GovernanceEventKind.SETTLE, None),
        ]
        state = M.initial()
        for kind, target in moves:
            event = GovernanceEvent(kind, target)
            verdict = M.decide(state, event)
            self.assertTrue(verdict.allowed, (kind, target, verdict.reason))
            if verdict.terminal is not None:
                self.assertEqual(verdict.terminal, LoopTerminal.SETTLED)
                return
            assert verdict.next is not None
            state = verdict.next
        self.fail("traversal never settled")


class GracefulFailureTests(unittest.TestCase):
    def test_every_state_routes_every_failure_kind_to_its_terminal(self):
        for state in M.states():
            for kind in FAILURE_EVENT_KINDS:
                event = GovernanceEvent(kind)
                verdict = M.decide(state, event)
                self.assertTrue(verdict.allowed, (state, kind))
                self.assertEqual(
                    verdict.terminal, M.terminal_for(kind), (state, kind)
                )

    def test_terminal_for_maps_publicly(self):
        self.assertEqual(
            M.terminal_for(GovernanceEventKind.GATE_REFUSED),
            LoopTerminal.GATE_REFUSED,
        )
        self.assertEqual(
            M.terminal_for(GovernanceEventKind.BUDGET_EXHAUSTED),
            LoopTerminal.BUDGET_EXHAUSTED,
        )
        self.assertEqual(
            M.terminal_for(GovernanceEventKind.VALIDATION_FAILED),
            LoopTerminal.VALIDATION_FAILED,
        )
        self.assertIsNone(M.terminal_for(GovernanceEventKind.SETTLE))

    def test_no_raise_over_all_state_kind_pairs(self):
        for state in M.states():
            for kind in GovernanceEventKind:
                verdict = M.decide(state, GovernanceEvent(kind))
                self.assertIsInstance(verdict, MembraneVerdict)


class SelfCheckTests(unittest.TestCase):
    def test_self_check_passes(self):
        AgenticLoopMembrane().self_check()

    def test_every_progress_event_kind_is_exercised(self):
        exercised = {
            e.kind for state in M.states() for e in M.legal_events(state)
        }
        for kind in (
            GovernanceEventKind.ENTER_LOOP,
            GovernanceEventKind.SET_TASK,
            GovernanceEventKind.OPEN_SUBLOOP,
            GovernanceEventKind.CLOSE_SUBLOOP,
            GovernanceEventKind.SETTLE,
        ):
            self.assertIn(kind, exercised, kind)


if __name__ == "__main__":
    unittest.main()