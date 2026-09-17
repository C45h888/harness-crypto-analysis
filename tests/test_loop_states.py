"""Layer-1 loop-state tests — the in-depth agentic workflow vocabulary.

Pins the invariants of ``engine/loop_states.py``:

- the stage traversal is complete, ordered, and unique;
- every stage hosts exactly one nested loop (injective);
- every nested loop is an IN-DEPTH workflow of >= 2 sub-loops with ordered,
  duplicate-free steps;
- every sub-loop is reachable, every ``LoopStep`` is reachable;
- the agentic core (``PRIMARY_SUBLOOP``) is one of the loop's own sub-loops;
- iterating sub-loops name their exit condition;
- task intents are INTENT-named (no P-ordinals) and bind to both a loop and
  a sub-loop that belongs to that loop;
- the FINAL loop is OUTPUT and its ARCHITECTURE sub-loop grounds output
  placement in an understanding of the agentic surface;
- failure terminals are first-class and ``VALIDATION_FAILED`` is its own
  endpoint;
- the deleted FSM vocabulary does not exist on the engine surface.
"""

from __future__ import annotations

import re
import unittest

from market_service.nooa_harness.engine import (
    FAILURE_TERMINALS,
    INTENT_LOOP,
    INTENT_SUBLOOP,
    LOOP_INTENTS,
    LOOP_STAGE,
    LOOP_STEPS,
    LOOP_SUBLOOPS,
    PRIMARY_SUBLOOP,
    STAGE_LOOP,
    STAGE_ORDER,
    SUBLOOP_SPECS,
    SUBLOOP_STEPS,
    SUCCESS_TERMINALS,
    AgenticStage,
    LoopObservation,
    LoopStep,
    LoopTerminal,
    NestedLoop,
    SubLoop,
    TaskIntent,
    intents_for,
    iterating_sub_loops,
    loop_for_intent,
    loop_for_stage,
    primary_sub_loop,
    spec_for,
    stage_for_loop,
    steps_for,
    steps_for_sub_loop,
    sub_loop_for_intent,
    sub_loops_for,
)
import market_service.nooa_harness.engine.loop_states as loop_states


class StageTraversalTests(unittest.TestCase):
    def test_stage_order_lists_every_stage_exactly_once(self):
        self.assertEqual(len(STAGE_ORDER), len(AgenticStage))
        self.assertEqual(set(STAGE_ORDER), set(AgenticStage))

    def test_stage_order_shape(self):
        self.assertEqual(
            [s.value for s in STAGE_ORDER],
            ["wake", "gather", "reason", "check", "finalize"],
        )

    def test_stage_loop_is_injective(self):
        self.assertEqual(set(STAGE_LOOP), set(AgenticStage))
        self.assertEqual(len(set(STAGE_LOOP.values())), len(AgenticStage))

    def test_stage_loop_expected_wiring(self):
        self.assertIs(STAGE_LOOP[AgenticStage.WAKE], NestedLoop.COMPREHENSION)
        self.assertIs(STAGE_LOOP[AgenticStage.GATHER], NestedLoop.EVIDENCE)
        self.assertIs(STAGE_LOOP[AgenticStage.REASON], NestedLoop.REASONING)
        self.assertIs(STAGE_LOOP[AgenticStage.CHECK], NestedLoop.VALIDATION)
        self.assertIs(STAGE_LOOP[AgenticStage.FINALIZE], NestedLoop.OUTPUT)

    def test_stage_for_loop_round_trips(self):
        for stage in AgenticStage:
            self.assertIs(stage_for_loop(loop_for_stage(stage)), stage)
        for loop in NestedLoop:
            self.assertIs(loop_for_stage(stage_for_loop(loop)), loop)


class NestedLoopTests(unittest.TestCase):
    def test_loops_are_the_five_modes(self):
        self.assertEqual(
            {l.value for l in NestedLoop},
            {"comprehension", "evidence", "reasoning", "validation", "output"},
        )

    def test_persistence_is_renamed_output(self):
        # The final loop places output; it is not merely "persistence".
        self.assertNotIn("persistence", {l.value for l in NestedLoop})


class SubLoopTests(unittest.TestCase):
    def test_loop_subloops_cover_every_loop(self):
        self.assertEqual(set(LOOP_SUBLOOPS), set(NestedLoop))

    def test_every_loop_has_at_least_two_subloops_without_duplicates(self):
        for loop, subs in LOOP_SUBLOOPS.items():
            self.assertGreaterEqual(len(subs), 2, loop)
            self.assertEqual(len(set(subs)), len(subs), loop)

    def test_expected_subloop_wiring(self):
        self.assertEqual(
            sub_loops_for(NestedLoop.COMPREHENSION),
            (SubLoop.INTAKE, SubLoop.INTERPRETATION, SubLoop.FRAMING),
        )
        self.assertEqual(
            sub_loops_for(NestedLoop.EVIDENCE),
            (SubLoop.SOURCING, SubLoop.ACQUISITION, SubLoop.VERIFICATION),
        )
        self.assertEqual(
            sub_loops_for(NestedLoop.REASONING),
            (SubLoop.HYPOTHESIS, SubLoop.ANALYSIS, SubLoop.SYNTHESIS),
        )
        self.assertEqual(
            sub_loops_for(NestedLoop.VALIDATION),
            (SubLoop.GATE, SubLoop.RECOVERY),
        )
        self.assertEqual(
            sub_loops_for(NestedLoop.OUTPUT),
            (
                SubLoop.COMPOSITION,
                SubLoop.ARCHITECTURE,
                SubLoop.PLACEMENT,
                SubLoop.MEMORY,
            ),
        )

    def test_subloop_specs_cover_exactly_used_subloops(self):
        used = {sl for subs in LOOP_SUBLOOPS.values() for sl in subs}
        self.assertEqual(set(SUBLOOP_SPECS), used)


class SubLoopSpecTests(unittest.TestCase):
    def test_every_spec_has_steps_without_duplicates(self):
        for sub_loop, spec in SUBLOOP_SPECS.items():
            self.assertGreaterEqual(len(spec.steps), 2, sub_loop)
            self.assertEqual(len(set(spec.steps)), len(spec.steps), sub_loop)

    def test_spec_for_matches_mapping(self):
        for sub_loop in SubLoop:
            self.assertIs(spec_for(sub_loop), SUBLOOP_SPECS[sub_loop])

    def test_iterating_subloops_name_an_exit_condition(self):
        for sub_loop, spec in SUBLOOP_SPECS.items():
            if spec.iterates:
                self.assertTrue(
                    spec.exit_condition,
                    f"iterating sub-loop {sub_loop.value} lacks exit condition",
                )

    def test_non_iterating_subloops_have_no_exit_condition(self):
        for sub_loop, spec in SUBLOOP_SPECS.items():
            if not spec.iterates:
                self.assertEqual(spec.exit_condition, "", sub_loop)

    def test_evidence_acquisition_shape(self):
        self.assertEqual(
            steps_for_sub_loop(SubLoop.ACQUISITION),
            (LoopStep.READ, LoopStep.CALCULATE, LoopStep.OBSERVE),
        )

    def test_output_architecture_shape(self):
        # Placing output requires understanding the agentic surface first.
        self.assertEqual(
            steps_for_sub_loop(SubLoop.ARCHITECTURE),
            (LoopStep.UNDERSTAND_SURFACE, LoopStep.LOCATE_OUTPUT),
        )
        self.assertIn(
            "surface", spec_for(SubLoop.ARCHITECTURE).purpose.lower()
        )

    def test_output_placement_shape(self):
        self.assertEqual(
            steps_for_sub_loop(SubLoop.PLACEMENT),
            (LoopStep.PERSIST, LoopStep.CONFIRM),
        )

    def test_recovery_shape(self):
        self.assertEqual(
            steps_for_sub_loop(SubLoop.RECOVERY),
            (LoopStep.DIAGNOSE, LoopStep.STEER, LoopStep.REVALIDATE),
        )


class LoopStepTests(unittest.TestCase):
    def test_all_steps_reachable(self):
        reachable = {s for spec in SUBLOOP_SPECS.values() for s in spec.steps}
        self.assertEqual(reachable, set(LoopStep))

    def test_subloop_steps_matches_specs(self):
        for sub_loop, spec in SUBLOOP_SPECS.items():
            self.assertEqual(SUBLOOP_STEPS[sub_loop], spec.steps)

    def test_flattened_loop_steps_is_subloop_order(self):
        for loop, subs in LOOP_SUBLOOPS.items():
            expected = tuple(
                step for sl in subs for step in SUBLOOP_SPECS[sl].steps
            )
            self.assertEqual(LOOP_STEPS[loop], expected)
            self.assertEqual(steps_for(loop), expected)


class PrimarySubLoopTests(unittest.TestCase):
    def test_primary_covers_every_loop(self):
        self.assertEqual(set(PRIMARY_SUBLOOP), set(NestedLoop))

    def test_primary_is_a_subloop_of_its_loop(self):
        for loop, sub_loop in PRIMARY_SUBLOOP.items():
            self.assertIn(sub_loop, LOOP_SUBLOOPS[loop])
            self.assertIs(primary_sub_loop(loop), sub_loop)

    def test_expected_primary_wiring(self):
        self.assertIs(primary_sub_loop(NestedLoop.COMPREHENSION), SubLoop.INTERPRETATION)
        self.assertIs(primary_sub_loop(NestedLoop.EVIDENCE), SubLoop.ACQUISITION)
        self.assertIs(primary_sub_loop(NestedLoop.REASONING), SubLoop.ANALYSIS)
        self.assertIs(primary_sub_loop(NestedLoop.VALIDATION), SubLoop.RECOVERY)
        self.assertIs(primary_sub_loop(NestedLoop.OUTPUT), SubLoop.PLACEMENT)


class IteratingSubLoopTests(unittest.TestCase):
    def test_iterating_sub_loops_matches_specs(self):
        for loop in NestedLoop:
            expected = tuple(
                sl for sl in LOOP_SUBLOOPS[loop] if SUBLOOP_SPECS[sl].iterates
            )
            self.assertEqual(iterating_sub_loops(loop), expected)

    def test_evidence_iterating_core(self):
        self.assertEqual(
            iterating_sub_loops(NestedLoop.EVIDENCE),
            (SubLoop.ACQUISITION, SubLoop.VERIFICATION),
        )

    def test_validation_recovery_iterates(self):
        self.assertEqual(
            iterating_sub_loops(NestedLoop.VALIDATION), (SubLoop.RECOVERY,)
        )


class TaskIntentTests(unittest.TestCase):
    def test_every_intent_maps_to_a_valid_loop(self):
        self.assertEqual(set(INTENT_LOOP), set(TaskIntent))
        for loop in INTENT_LOOP.values():
            self.assertIsInstance(loop, NestedLoop)

    def test_every_intent_maps_to_a_subloop_of_its_loop(self):
        self.assertEqual(set(INTENT_SUBLOOP), set(TaskIntent))
        for intent, sub_loop in INTENT_SUBLOOP.items():
            self.assertIn(sub_loop, LOOP_SUBLOOPS[INTENT_LOOP[intent]])

    def test_every_loop_serves_at_least_one_intent(self):
        self.assertEqual(set(LOOP_INTENTS), set(NestedLoop))
        for loop in NestedLoop:
            self.assertTrue(intents_for(loop), loop)

    def test_loop_intents_is_reverse_of_intent_loop(self):
        for loop in NestedLoop:
            expected = tuple(
                intent for intent, l in INTENT_LOOP.items() if l is loop
            )
            self.assertEqual(intents_for(loop), expected)

    def test_loop_and_subloop_for_intent(self):
        for intent in TaskIntent:
            loop = loop_for_intent(intent)
            self.assertIn(intent, intents_for(loop))
            self.assertIs(sub_loop_for_intent(intent), INTENT_SUBLOOP[intent])

    def test_expected_intent_wiring(self):
        self.assertIs(loop_for_intent(TaskIntent.INFER_ORDER_FLOW), NestedLoop.EVIDENCE)
        self.assertIs(sub_loop_for_intent(TaskIntent.INFER_ORDER_FLOW), SubLoop.ACQUISITION)
        self.assertIs(loop_for_intent(TaskIntent.VALIDATE_FINAL), NestedLoop.VALIDATION)
        self.assertIs(sub_loop_for_intent(TaskIntent.VALIDATE_FINAL), SubLoop.GATE)

    def test_output_loop_intents(self):
        self.assertEqual(
            set(intents_for(NestedLoop.OUTPUT)),
            {TaskIntent.UNDERSTAND_SURFACE, TaskIntent.PLACE_OUTPUT},
        )
        self.assertIs(
            sub_loop_for_intent(TaskIntent.UNDERSTAND_SURFACE),
            SubLoop.ARCHITECTURE,
        )
        self.assertIs(
            sub_loop_for_intent(TaskIntent.PLACE_OUTPUT), SubLoop.PLACEMENT
        )

    def test_intents_are_named_not_p_numbered(self):
        for intent in TaskIntent:
            self.assertIsNone(
                re.fullmatch(r"p\d+", intent.value),
                f"intent {intent.name} looks like a phase ordinal",
            )
            self.assertRegex(intent.value, r"^[a-z][a-z_]+$")


class TerminalTests(unittest.TestCase):
    def test_settled_is_the_only_success_terminal(self):
        self.assertEqual(SUCCESS_TERMINALS, frozenset({LoopTerminal.SETTLED}))

    def test_failure_terminals_are_first_class(self):
        expected = {
            LoopTerminal.GATE_REFUSED,
            LoopTerminal.NARRATION_FAILED,
            LoopTerminal.PARSE_FAILED,
            LoopTerminal.VALIDATION_FAILED,
            LoopTerminal.BUDGET_EXHAUSTED,
            LoopTerminal.INFRA_FAILED,
        }
        self.assertEqual(FAILURE_TERMINALS, frozenset(expected))

    def test_validation_failed_is_its_own_terminal(self):
        self.assertIn(LoopTerminal.VALIDATION_FAILED, FAILURE_TERMINALS)
        self.assertNotIn(LoopTerminal.VALIDATION_FAILED, SUCCESS_TERMINALS)

    def test_success_and_failure_partition_the_terminal_set(self):
        self.assertEqual(
            SUCCESS_TERMINALS | FAILURE_TERMINALS, frozenset(LoopTerminal)
        )
        self.assertEqual(SUCCESS_TERMINALS & FAILURE_TERMINALS, frozenset())

    def test_degraded_is_gone_split_into_failures(self):
        values = {t.value for t in LoopTerminal}
        self.assertNotIn("degraded", values)
        self.assertIn("narration_failed", values)
        self.assertIn("parse_failed", values)


class LoopObservationTests(unittest.TestCase):
    def test_observation_defaults_to_no_subloop(self):
        obs = LoopObservation(NestedLoop.EVIDENCE, TaskIntent.INFER_DEPTH)
        self.assertIsNone(obs.sub_loop)

    def test_observation_carries_subloop(self):
        obs = LoopObservation(
            NestedLoop.OUTPUT, TaskIntent.PLACE_OUTPUT, SubLoop.PLACEMENT
        )
        self.assertIs(obs.nested_loop, NestedLoop.OUTPUT)
        self.assertIs(obs.task, TaskIntent.PLACE_OUTPUT)
        self.assertIs(obs.sub_loop, SubLoop.PLACEMENT)

    def test_observation_is_frozen(self):
        obs = LoopObservation(
            NestedLoop.EVIDENCE, TaskIntent.INFER_ORDER_FLOW, SubLoop.ACQUISITION
        )
        with self.assertRaises(Exception):
            obs.sub_loop = SubLoop.VERIFICATION  # type: ignore[misc]

    def test_observation_equality_and_hash(self):
        a = LoopObservation(NestedLoop.EVIDENCE, TaskIntent.INFER_DEPTH, SubLoop.SOURCING)
        b = LoopObservation(NestedLoop.EVIDENCE, TaskIntent.INFER_DEPTH, SubLoop.SOURCING)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))


class SelfCheckTests(unittest.TestCase):
    def test_module_self_check_passes(self):
        loop_states.self_check()


class DeletedVocabularyTests(unittest.TestCase):
    def test_old_fsm_names_do_not_exist(self):
        import market_service.nooa_harness.engine as engine

        for name in ("LoopState", "LoopEvent", "AgentLoopFSM"):
            self.assertFalse(
                hasattr(engine, name),
                f"deleted FSM name {name} resurfaced on the engine surface",
            )

    def test_fsm_module_exists_as_the_membrane(self):
        # ``engine/fsm.py`` is now the canonical governance membrane (Layer 2
        # """)) — it must import cleanly by its own name.
        from market_service.nooa_harness.engine.fsm import (  # noqa: F401
            AgenticLoopMembrane,
        )

        self.assertIsNotNone(AgenticLoopMembrane)


if __name__ == "__main__":
    unittest.main()
