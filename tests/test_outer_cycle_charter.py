"""Outer-cycle charter tests — the traversal is the table (Phase S-0/S-1).

docs/STATE_CHARTER_SPEC.md: OUTER_SEQUENCE was dead documentation (defined
in runner.py, never iterated). These tests pin the NEW shape — narrate_cycle
iterates the table through _STAGE_RUNNERS — and tripwire the regression:

1. every receipt resolves to a stage runner, and the FSM-loop coverage is
   the documented order;
2. narrate_cycle's source consumes OUTER_SEQUENCE (the dead-table tripwire);
3. the spy seams survive: every stage wrapper still calls through its module
   attribute at call time;
4. the LoopTag crosswalk is the ONE loop-identity reconciliation: every
   loop_tag literal assigned in the engine, every budget key, and every
   passes_per_loop key resolves to loop_states.LoopTag / LOOP_TAG_CROSSWALK
   (the per-map silent defaults are gone — charter rule: no silent defaults
   map unknown identities to authorized behavior);
5. P1–P6 phase coverage stays a formally separate axis from LoopTag.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import unittest

import market_service.nooa_harness.engine.config as engine_config
import market_service.nooa_harness.engine.core.context as engine_context
import market_service.nooa_harness.engine.core.driver as engine_driver
import market_service.nooa_harness.engine.core.runner as runner
import market_service.nooa_harness.engine.loop_states as loop_states
from market_service.nooa_harness.engine.loop_states import NestedLoop

_ENGINE_DIR = (
    pathlib.Path(runner.__file__).parent  # .../nooa_harness/engine/core
    .parent
)


def _engine_sources():
    for p in sorted(_ENGINE_DIR.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        yield p, p.read_text()


class OuterSequenceDerivationTests(unittest.TestCase):
    """The table is the traversal — not documentation next to it."""

    def test_every_receipt_resolves_to_a_stage_runner(self):
        for receipt in runner.OUTER_SEQUENCE:
            self.assertIn(receipt.name, runner._STAGE_RUNNERS)

    def test_every_stage_runner_is_declared_in_the_table(self):
        self.assertEqual(
            set(runner._STAGE_RUNNERS),
            {receipt.name for receipt in runner.OUTER_SEQUENCE},
        )

    def test_fsm_loop_coverage_is_the_documented_order(self):
        # Pre-FSM wake, then the five merged CONTEXT stages of the re-ordered
        # chain (agent_fetch → data_gate → comprehension → plan-bound
        # acquisition → evidence), then the three terminator stages —
        # exactly the narrate_cycle chain.
        self.assertEqual(
            [r.loop for r in runner.OUTER_SEQUENCE],
            [None] + [NestedLoop.CONTEXT] * 5 + [
                NestedLoop.REASONING, NestedLoop.VALIDATION, NestedLoop.OUTPUT],
        )

    def test_narrate_cycle_consumes_the_table(self):
        """The dead-table tripwire: the traversal MUST iterate
        OUTER_SEQUENCE through _STAGE_RUNNERS."""
        src = inspect.getsource(runner.narrate_cycle)
        self.assertIn("for receipt in OUTER_SEQUENCE", src)
        self.assertIn("_STAGE_RUNNERS[receipt.name]", src)

    def test_spy_seams_survive(self):
        """Stage spies patch core.wake / core.gather / reasoning+output
        owners — each stage wrapper must call through the module attribute
        at call time (never bind a local reference at import time)."""
        sources = {name: inspect.getsource(fn)
                   for name, fn in runner._STAGE_RUNNERS.items()}
        self.assertIn("wake_mod.run_wake(", sources["wake"])
        self.assertIn("gather.run_agent_fetch(", sources["agent_fetch"])
        self.assertIn("gather.run_data_gate(", sources["data_gate"])
        self.assertIn("wake_mod.run_comprehension(", sources["comprehension"])
        self.assertIn("gather.run_plan_bound_acquisition(",
                      sources["plan_bound_acquisition"])
        self.assertIn("reasoning.run_evidence(", sources["evidence"])
        self.assertIn("reasoning.run_reasoning(", sources["reasoning"])
        self.assertIn("reasoning.run_validation(", sources["validation"])
        self.assertIn("output.run_output(", sources["output"])


def _tag_member_values() -> set[str]:
    return {v for k, v in vars(loop_states.LoopTag).items()
            if not k.startswith("_") and isinstance(v, str)}


def _collect_dict_keys(src: str, target: str) -> set[str]:
    """Keys of the module-level ``target`` dict literal (direct, or nested
    inside a field(default_factory=lambda: {...}) ann-assign). Disk-truth
    scan — immune to stale-bytecode import races."""
    keys: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = ([node.target] if isinstance(node, ast.AnnAssign)
                   else node.targets)
        for t in targets:
            name = t.id if isinstance(t, ast.Name) else (
                t.attr if isinstance(t, ast.Attribute) else None)
            if name == target:
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Dict):
                        for k in sub.keys:
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                keys.add(k.value)
    return keys


class LoopTagCrosswalkTests(unittest.TestCase):
    """ONE loop-identity reconciliation — and no silent authorized defaults."""

    def test_crosswalk_covers_every_assigned_loop_tag_literal(self):
        """AST scan: every ``loop_tag = "..."`` assignment and every
        ``st.loop_tag == "..."`` comparison literal in the engine plane
        must be a LoopTag member with a crosswalk entry."""
        literals: set[str] = set()
        for _p, src in _engine_sources():
            tree = ast.parse(src)
            for node in ast.walk(tree):
                # loop_tag = "x"
                if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and any(isinstance(t, ast.Attribute) and t.attr == "loop_tag"
                                for t in node.targets)):
                    literals.add(node.value.value)
                # <x>.loop_tag == "x" (and !=)
                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) \
                        and node.left.attr == "loop_tag":
                    for cmp in node.comparators:
                        if isinstance(cmp, ast.Constant) and isinstance(cmp.value, str):
                            literals.add(cmp.value)
        self.assertTrue(literals, "expected loop_tag literals in the engine plane")
        members = _tag_member_values()
        for lit in literals:
            self.assertIn(lit, members,
                          f"loop_tag literal {lit!r} is not a LoopTag member")
            self.assertIn(lit, loop_states.LOOP_TAG_CROSSWALK,
                          f"loop_tag {lit!r} has no FSM crosswalk entry")

    def test_budget_keys_and_passes_keys_resolve_to_loop_tag(self):
        """Disk-truth AST scan (immune to stale bytecode): the budget dict
        and the carrier's passes_per_loop default resolve to the LoopTag
        namespace."""
        budget_src = pathlib.Path(engine_config.__file__).read_text()
        budget_keys = _collect_dict_keys(budget_src, "LOOP_PASS_BUDGET")
        carrier_src = pathlib.Path(engine_context.__file__).read_text()
        carrier_keys = _collect_dict_keys(carrier_src, "passes_per_loop")
        members = _tag_member_values()
        self.assertEqual(budget_keys, members)
        self.assertEqual(carrier_keys, members)

    def test_crosswalk_is_total_over_loop_tag(self):
        self.assertEqual(set(loop_states.LOOP_TAG_CROSSWALK), _tag_member_values())

    def test_no_silent_authorized_default_in_driver(self):
        """Charter rule: no `.get(st.loop_tag, NestedLoop.X)` anywhere — an
        unknown loop tag routes through GovernanceDenied, never an invented
        authorization."""
        src = inspect.getsource(engine_driver)
        import re
        self.assertIsNone(
            re.search(r"\.get\(\s*st\.loop_tag[^)]*NestedLoop", src),
            "driver.py still maps loop_tag through a silent authorized default",
        )
        self.assertIn("LOOP_TAG_CROSSWALK[st.loop_tag]", src)
        self.assertIn("unknown loop_tag", src)

    def test_registry_home_loops_declared_in_owner(self):
        self.assertEqual(loop_states.REGISTRY_HOME_LOOPS["comprehension"],
                         NestedLoop.CONTEXT)
        self.assertEqual(loop_states.REGISTRY_HOME_LOOPS["evidence"],
                         NestedLoop.CONTEXT)
        self.assertNotIn("_legacy_loop_names", inspect.getsource(engine_driver))

    def test_p_phases_are_a_separate_axis(self):
        """P1–P6 coverage phases are provenance labels — they must never be
        LoopTag members (driver docstring: 'P1–P6 remain coverage/provenance
        labels only')."""
        controller_src = (_ENGINE_DIR / "controller.py").read_text()
        p_literals = set()
        for node in ast.walk(ast.parse(controller_src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in {f"P{i}" for i in range(1, 7)}:
                    p_literals.add(node.value)
        self.assertEqual(p_literals, {f"P{i}" for i in range(1, 7)})
        self.assertFalse(p_literals & _tag_member_values())


if __name__ == "__main__":
    unittest.main()