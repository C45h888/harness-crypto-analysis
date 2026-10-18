"""Repair prompt: steering removed, structural shape pinned (no half-baked blocks).

The COPY CORRECTION / CITE MISSING mechanism was removed by operator order:
an incomplete steering layer only invites bolted fixes. The repair prompt is
the original shape (header → reminders → missing list → full dump → generic
close); citation enforcement lives in the validator parse gates instead.
These tests pin that contract from both sides.
"""


class _StubController:
    observation = None
    phase_coverage = {"P1": {"calc.ofi.intervals"}}

    def loop_coverage(self):
        return {}

    def congruence(self):
        return None


def _repair(missing, accumulated=None):
    from market_service.nooa_harness.engine.kb import compose_repair_prompt
    return compose_repair_prompt(
        controller=_StubController(), loop="reasoning", sub_loop="evidence",
        passes_spent=3, pass_budget=8, dispatches_left=1, missing=missing,
        accumulated=dict(accumulated or {}), task_reminder="", scenario_reminder="",
        scenario_steer="", phase_guidance={"output": "do output"},
        next_phase="output")


def test_no_steering_blocks_present():
    prompt = _repair(["evidence value mismatch on 'r2': cited '0.61' != '0.58'",
                      "evidence[4] needs path+value — cite the tool+field you read"])
    head = prompt.split("FINAL REJECTED")[0]
    assert "COPY CORRECTION" not in head
    assert "CITE MISSING" not in head


def test_missing_items_listed_verbatim():
    items = ["evidence value mismatch on 'r2': cited '0.61' != '0.58'",
             "phase P3 uncovered: execute its tools"]
    prompt = _repair(items)
    for item in items:
        assert f"- {item}" in prompt


def test_full_dump_preserved():
    big = {f"tool{i}": {"tool": f"t{i}", "data": {"x": "y" * 2000}} for i in range(20)}
    from market_service.nooa_harness.engine.kb import compose_repair_prompt
    prompt = compose_repair_prompt(
        controller=_StubController(), loop="reasoning", sub_loop="evidence",
        passes_spent=3, pass_budget=8, dispatches_left=1,
        missing=["phase P3 uncovered"], accumulated=big,
        task_reminder="", scenario_reminder="", scenario_steer="",
        phase_guidance={"output": "do output"}, next_phase="output")
    dump = prompt.split("ACCUMULATED TOOL RESULTS")[1].split("PHASE COVERAGE")[0]
    assert len(dump) > 24_000, len(dump)


def test_generic_close_intact():
    prompt = _repair(["phase P3 uncovered"])
    assert "tool_calls=[]) only when every missing item is addressed" in prompt
