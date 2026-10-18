"""F9 steering: repair prompt leads with the divergent pair (plan item E1)."""

ACCUM = {"calc.forward.fit": {"tool": "calc.forward.fit", "data": {"r2": "0.58"}}}


class _StubController:
    observation = None
    phase_coverage = {"P1": {"calc.ofi.intervals"}}

    def loop_coverage(self):
        return {}

    def congruence(self):
        return None


def _repair(missing):
    from market_service.nooa_harness.engine.kb import compose_repair_prompt
    return compose_repair_prompt(
        controller=_StubController(), loop="reasoning", sub_loop="evidence",
        passes_spent=3, pass_budget=8, dispatches_left=1, missing=missing,
        accumulated=dict(ACCUM), task_reminder="", scenario_reminder="",
        scenario_steer="", phase_guidance={"output": "do output"},
        next_phase="output")


def test_echo_mismatch_leads_with_copy_correction():
    prompt = _repair(["evidence value mismatch on 'r2': cited '0.61' != payload '0.58'"])
    assert prompt.startswith("COPY CORRECTION")
    assert "0.61" in prompt.split("FINAL REJECTED")[0]
    assert "verbatim" in prompt


def test_echo_repair_dump_capped_at_6k():
    big = {f"tool{i}": {"tool": f"t{i}", "data": {"x": "y" * 2000}} for i in range(20)}
    from market_service.nooa_harness.engine.kb import compose_repair_prompt
    prompt = compose_repair_prompt(
        controller=_StubController(), loop="reasoning", sub_loop="evidence",
        passes_spent=3, pass_budget=8, dispatches_left=1,
        missing=["evidence value mismatch on 'r2': cited '0.61' != '0.58'"],
        accumulated=big, task_reminder="", scenario_reminder="",
        scenario_steer="", phase_guidance={"output": "do output"},
        next_phase="output")
    dump = prompt.split("ACCUMULATED TOOL RESULTS")[1].split("PHASE COVERAGE")[0]
    assert len(dump) <= 7_000, len(dump)


def test_tool_absent_repair_keeps_full_dump_and_generic_close():
    prompt = _repair(["phase P3 uncovered: execute its tools before finalizing"])
    assert not prompt.startswith("COPY CORRECTION")
    assert "FINAL REJECTED" in prompt
    assert "tool_calls=[]) only when every missing item is addressed" in prompt


def test_correction_caps_at_three_plus_overflow():
    missing = [f"evidence value mismatch on 'f{i}': cited '1' != '2'" for i in range(5)]
    prompt = _repair(missing)
    head = prompt.split("FINAL REJECTED")[0]
    assert "plus 2 more" in head


def test_cite_missing_leads_with_cite_block_and_small_dump():
    prompt = _repair(["evidence[3] needs path+value — cite the tool+field you read"])
    assert prompt.startswith("CITE MISSING")
    assert "COPY CORRECTION" not in prompt.split("FINAL REJECTED")[0]
    assert "Never invent" in prompt
    dump = prompt.split("ACCUMULATED TOOL RESULTS")[1].split("PHASE COVERAGE")[0]
    assert len(dump) <= 7_000, len(dump)


def test_mixed_mismatch_and_cite_both_lead():
    prompt = _repair([
        "evidence value mismatch on 'r2': cited '0.61' != '0.58'",
        "evidence[4] needs path+value — cite the tool+field you read",
    ])
    head = prompt.split("FINAL REJECTED")[0]
    assert "COPY CORRECTION" in head
    assert "CITE MISSING" in head
