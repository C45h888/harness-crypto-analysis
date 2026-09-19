"""REASON stage — one function per loop over a shared pass carrier.

Comprehension understands once; evidence gathers (narrate#1 + budgeted
follow-ups); reasoning tests and compares; validation gates with one bounded
retry then emits to the FSM terminal. Shared mechanics (dispatch round,
follow-up turn) live in _dispatch_turn/_followup_turn so every loop spends
passes the same way: tool packing is bounded by the canonical per-round
call cap and overflow is recorded as unexecuted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import context
from .. import narration as narration_mod
from ..config import (
    AGENTIC_MAX_LLM_TURNS,
    AGENTIC_MAX_TOOL_ROUNDS,
    LOOP_PASS_BUDGET,
    MAX_DISPATCHES_PER_PASS,
    VALIDATION_RETRY_PASSES,
)
from .context import _CycleContext, _ReasonedCycle, _PHASE_INTENT
from ..controller import CycleController
from ..fsm import GovernanceEvent, GovernanceEventKind
from ..kb import (
    TURN_CONTRACT_LINE,
    TOOL_WINDOWS,
    build_loop_state_block,
    build_output_format,
    build_task_workflow,
    chain_status,
)
from .chain import chain_completion, chain_steer
from ..loop_states import NestedLoop, SubLoop, TaskIntent

log = logging.getLogger(__name__)


def seed_evidence_plan(
    task: str | None, scenario: dict[str, Any] | None,
) -> dict[str, Any]:
    """Deterministic evidence seed: task shape -> suggested tools.

    Autonomy inside a planned frame: the engine suggests, the agent
    disposes. Seeds never execute by themselves — every dispatch is an
    agent tool_call classified by the controller. Pure over injected
    task/scenario (no I/O, no LLM).
    """
    seeds: list[str] = [
        "calc.forward.forecast",
    ]
    rationale = ["canonical deterministic forward ForecastResult always seeded"]
    blob = f"{task or ''} {json.dumps(scenario or {})}".lower()
    if scenario is not None:
        seeds += ["calc.scenario.evaluate", "calc.forward.scenario"]
        rationale.append("scenario present: legacy + forward scenario paths")
    if any(k in blob for k in ("horizon", "1s", "5s", "30s", "60s",
                                "target", "theta", "probab",
                                "hypothes", "h0", "h1")):
        seeds += ["calc.forward.distribution", "calc.hypothesis.test",
                    "calc.decay.report"]
        rationale.append("horizon/hypothesis language: distribution + test + decay")
    if any(k in blob for k in ("wall", "absorb", "liquid", "reversal",
                                "support", "resist")):
        seeds += ["calc.events.absorption", "calc.events.walls"]
        rationale.append("liquidity language: typed event detectors")
    seeds.append("memory.recall_paper")
    rationale.append("paper grounding always seeded (evidence home)")
    seen: list[str] = []
    for seed in seeds:
        if seed not in seen:
            seen.append(seed)
    return {"seeds": seen, "rationale": rationale}


def default_comprehension(
    task: str | None, scenario: dict[str, Any] | None,
    seed: dict[str, Any], reason: str,
) -> dict[str, Any]:
    """Fallback understanding block when the comprehension pass fails.

    Never kills the cycle: a defaulted block plus the seeded plan keeps
    the loop moving, and the parse note records what happened.
    """
    return {
        "intent": (task[:200] if task else "autonomous microstructure inference"),
        "constraints": {
            "scenario": scenario,
            "pass_budgets": dict(LOOP_PASS_BUDGET),
        },
        "questions": ["what is the current microstructure state?",
                        "what does the forward evidence support?"],
        "evidence_plan": list(seed.get("seeds") or []),
        "parse_note": reason,
    }


@dataclass
class CycleRuntimeState:
    """Mutable carrier threading one reason-stage run through its loops."""

    controller: Any
    capability_log: list[dict[str, Any]]
    deterministic_state: dict[str, Any]
    accumulated_tool_results: dict[str, Any]
    tool_results: dict[str, Any]
    parsed_current: dict[str, Any]
    parsed_1: dict[str, Any] | None = None
    llm_calls: int = 0
    # Dispatch rounds remain separately visible from LLM/pass budgets.
    tool_rounds_used: int = 0
    repairs_sent: int = 0
    validation_retries_used: int = 0
    validation_entered: bool = False
    opened_subs: set[str] = field(default_factory=set)
    passes_per_loop: dict[str, int] = field(default_factory=lambda: {
        "comprehension": 0, "evidence": 0, "reasoning": 0,
        "validation": 0, "output": 0,
    })
    dispatched_in_pass: int = 0
    loop_tag: str = "evidence"
    unexecuted: list[str] = field(default_factory=list)
    task_reminder: str = ""
    scenario_reminder: str = ""
    phase_guidance: dict[str, str] = field(default_factory=dict)
    comprehension: dict[str, Any] | None = None
    seed_plan: dict[str, Any] | None = None
    task_workflow: dict[str, Any] | None = None
    finalize_now: bool = False
    final_validation: dict[str, Any] = field(default_factory=lambda: {
        "passed": False, "missing": ["loop_not_run"],
    })
    failure_kind: str | None = None
    failure_detail: str | None = None

    def to_reasoned(self) -> _ReasonedCycle:
        """Freeze the handoff the OUTPUT loop consumes."""
        return _ReasonedCycle(
            parsed_first=self.parsed_1 or {},
            parsed_final=self.parsed_current,
            llm_calls=self.llm_calls,
            tool_rounds_used=self.tool_rounds_used,
            repairs_sent=self.repairs_sent,
            finalize_now=self.finalize_now,
            final_validation=self.final_validation,
            unexecuted=list(self.unexecuted),
            tool_results=self.tool_results,
            comprehension=self.comprehension,
            passes_per_loop=dict(self.passes_per_loop),
        )


def _mark_declared(
    ctrl: CycleController, parsed: dict[str, Any],
) -> CycleController:
    # PURE helper — returns the successor controller; the caller rebinds.
    return ctrl.mark_declared(parsed)


def _open_once(st: CycleRuntimeState, sub: SubLoop) -> None:
    """Open a sub-loop unless already opened (membrane advances siblings)."""
    if sub.value in st.opened_subs:
        return
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, sub,
    )
    st.opened_subs.add(sub.value)


async def _dispatch_turn(engine: Any, ctx: Any, st: CycleRuntimeState) -> None:
    """Dispatch st.parsed_current's tool_calls under the pass ceiling."""
    st._round_had_execution = False  # type: ignore[attr-defined]
    from market_service.nooa_harness.inference import (
        _normalize_tool_name,
        capability_log_entry,
        tool_homes,
    )

    tool_calls = st.parsed_current.get("tool_calls")
    if not (isinstance(tool_calls, list) and tool_calls):
        return
    pass_remaining = MAX_DISPATCHES_PER_PASS - st.dispatched_in_pass
    dict_calls = [c for c in tool_calls if isinstance(c, dict)]
    if st.tool_rounds_used >= AGENTIC_MAX_TOOL_ROUNDS or pass_remaining <= 0:
        # Budget spent: name everything pending (never silently dropped).
        for call in dict_calls:
            raw_name = str(call.get("name") or call.get("tool") or "").strip()
            if raw_name:
                st.unexecuted.append(raw_name)
        return
    # The canonical per-round cap is enforced here; overflow is named on
    # unexecuted, never silently dropped.
    to_execute = dict_calls[:pass_remaining]
    for call in dict_calls[pass_remaining:]:
        raw_name = str(call.get("name") or call.get("tool") or "").strip()
        if raw_name:
            st.unexecuted.append(raw_name)
            log.warning("pass ceiling: deferring %s to unexecuted", raw_name)
    if not to_execute:
        return
    st.tool_rounds_used += 1
    round_results: dict[str, Any] = {}
    for call in to_execute:
        raw_name = str(call.get("name", ""))
        args = dict(call.get("args") or {})
        args.setdefault("symbol", engine.symbol)
        args.setdefault("venue", engine.venue)
        canonical = _normalize_tool_name(raw_name) or raw_name
        expected_loop = {
            "evidence": NestedLoop.EVIDENCE,
            "reasoning": NestedLoop.REASONING,
            "validation": NestedLoop.VALIDATION,
        }.get(st.loop_tag, NestedLoop.REASONING)
        expected_sub_loop = {
            "evidence": SubLoop.ACQUISITION,
            "reasoning": SubLoop.ANALYSIS,
            "validation": SubLoop.RECOVERY,
        }.get(st.loop_tag)
        # The dispatch registry owns the allowed work homes; the FSM owns
        # whether the current observation is one of them.  A model cannot
        # move a tool into a convenient phase by naming it differently.
        authorization = None
        for home_loop, home_sub_loop in tool_homes(canonical):
            try:
                home_nested_loop = NestedLoop(home_loop)
                home_sub_loop_enum = SubLoop(home_sub_loop)
            except ValueError:
                continue
            candidate = context._authorize_work(
                st.controller,
                nested_loop=home_nested_loop,
                sub_loop=home_sub_loop_enum,
            )
            if candidate.allowed:
                authorization = candidate
                break
        if authorization is None:
            authorization = context._authorize_work(
                st.controller,
                nested_loop=expected_loop,
                sub_loop=expected_sub_loop,
            )
        if not authorization.allowed:
            # The FSM is the work boundary.  A model request made from the
            # wrong loop becomes an explicit finding and is never dispatched.
            tool_log = capability_log_entry(
                f"tool.denied:{canonical}",
                {"symbol": engine.symbol, "venue": engine.venue},
                "denied",
                detail={"reason": "unauthorized_loop_work", "fsm": authorization.reason},
            )
            st.dispatched_in_pass += 1
            st.capability_log.append(tool_log)
            st.controller = st.controller.record_outcome(
                canonical, tool_log, None, raw_name=raw_name, args=args,
            )
            round_results[canonical] = None
            st.accumulated_tool_results[canonical] = None
            continue
        # Controller authority: redundant re-dispatch of a tool
        # that already refused this cycle is suppressed
        # structurally (logged, never executed).
        if st.controller.is_redundant(canonical):
            log.warning(
                "pass loop: suppressing redundant dispatch of "
                "%s (refused earlier this cycle)", canonical,
            )
            _supp_log = capability_log_entry(
                f"tool.suppressed:{canonical}",
                {"symbol": engine.symbol, "venue": engine.venue},
                "denied",
                detail={"reason": "refused_deterministically",
                        "refusal_reason": st.controller.scenario_refusal_reason()},
            )
            st.capability_log.append(_supp_log)
            st.controller = st.controller.record_outcome(
                canonical, _supp_log, None, raw_name=raw_name, args=args,
            )
            st.dispatched_in_pass += 1
            round_results[canonical] = None
            st.accumulated_tool_results[canonical] = None
            continue
        try:
            st._round_had_execution = True  # type: ignore[attr-defined]
            result, tool_log = await context.execute_tool(
                engine.store, raw_name, args, postgres=engine.postgres,
                memory=engine.memory, settings=engine.settings,
            )
        except Exception as exc:  # one bad tool never kills the cycle
            log.exception("pass loop: tool %s raised", raw_name)
            result, tool_log = None, capability_log_entry(
                f"tool.error:{canonical}",
                {"symbol": engine.symbol, "venue": engine.venue},
                "error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        st.dispatched_in_pass += 1
        st.capability_log.append(tool_log)
        # Controller classifies the outcome (the ONLY place the
        # ok-with-refused-detail null-discipline shape is
        # interpreted), applies phase-coverage credit, and returns
        # a NEW immutable controller — rebound here.
        st.controller = st.controller.record_outcome(
            canonical, tool_log, result,
            raw_name=raw_name, args=args,
        )
        if result is None:
            result_payload = None
        else:
            rendered = json.dumps(result, default=str)
            if len(rendered) < 40_000:
                result_payload = json.loads(rendered)
            else:
                result_payload = {"_truncated": True, "preview": rendered[:4_000]}
        round_results[canonical] = result_payload
        st.accumulated_tool_results[canonical] = result_payload
    st.tool_results.update(round_results)
    st._round_results = round_results  # type: ignore[attr-defined]


async def _followup_turn(engine: Any, ctx: Any, st: CycleRuntimeState) -> dict[str, Any] | None:
    """One follow-up LLM turn for st.loop_tag. Returns parsed or None."""
    coverage = st.controller.phase_coverage
    next_phase = narration_mod.next_uncovered_phase(coverage)
    # P1–P6 remain coverage/provenance labels only.  They do not retask the
    # FSM observation; the prompt below derives its actual intent from the
    # controller and uses the next phase only for evidence guidance.
    # Phase labels guide evidence selection, but never become the prompt's
    # semantic state.  The controller observation supplies the intent.
    # LOOP STATE relay: the agent sees its loop, budget, and what
    # came before — observation-backed via the controller ledger.
    loop_header = build_loop_state_block(
        controller=st.controller,
        loop=st.loop_tag,
        sub_loop=("acquisition" if st.loop_tag == "evidence"
                  else "analysis" if st.loop_tag == "reasoning"
                  else "recovery"),
        intent=None,
        passes_spent=st.passes_per_loop.get(st.loop_tag, 0),
        pass_budget=LOOP_PASS_BUDGET.get(st.loop_tag, 0),
        dispatches_left=MAX_DISPATCHES_PER_PASS - st.dispatched_in_pass,
        traversal=st.controller.loop_coverage(),
        gate=st.deterministic_state.get("gate"),
        congruence=st.controller.congruence() if st.loop_tag == "reasoning" else None,
        comprehension=(st.deterministic_state.get("comprehension")
                       if st.loop_tag == "reasoning" else None),
    )
    chain = (st.task_workflow or {}).get("chain") or []
    round_results = getattr(st, "_round_results", {})
    chain_block = ""
    if st.loop_tag == "reasoning":
        st_chain = chain_completion(
            st.controller, task=ctx.task, scenario=ctx.scenario)
        st.deterministic_state["statistical_chain"] = {
            "links": st_chain["links"], "missing": st_chain["missing"],
            "refused": st_chain["refused"], "complete": st_chain["complete"],
        }
        chain_block = f"\n{st_chain['render']}\n"
        steer = chain_steer(st_chain)
        if steer:
            chain_block += f"{steer}\n"
    user_prompt_next = (
        f"{loop_header}\n\n"
        f"{TOOL_WINDOWS.get(st.loop_tag, '')}\n\n"
        f"{chain_status(chain, list(st.accumulated_tool_results))}\n"
        f"{chain_block}\n"
        f"{st.task_reminder}"
        f"{st.scenario_reminder}"
        f"PASS RESULTS (pass {st.passes_per_loop.get(st.loop_tag, 0)} of "
        f"{LOOP_PASS_BUDGET.get(st.loop_tag, 0)} for {st.loop_tag}; cite paths):\n"
        f"{json.dumps(round_results, default=str)[:40_000]}\n\n"
        f"PRIOR PASSES (earlier results, for citation):\n"
        f"{json.dumps({k: v for k, v in st.accumulated_tool_results.items() if k not in round_results}, default=str)[:40_000]}\n\n"
        "PHASE COVERAGE (families with ≥1 ok tool): "
        f"{json.dumps({p: sorted(s) for p, s in coverage.items()})}\n"
        f"{st.phase_guidance[next_phase]}\n"
        f"{TURN_CONTRACT_LINE}\n"
        f"Passes remaining in {st.loop_tag}: "
        f"{LOOP_PASS_BUDGET.get(st.loop_tag, 0) - st.passes_per_loop.get(st.loop_tag, 0)}. "
        "Declare \"phase\" every turn; call this loop's tools, or advance with tool_calls=[]."
    )
    try:
        raw_next = await engine._call_llm(user_prompt_next)
    except Exception as exc:
        log.exception("pass narration round failed; ending loop early")
        st.failure_kind = "narration_failed"
        st.failure_detail = f"{type(exc).__name__}: {exc}"
        return None
    st.llm_calls += 1
    st.passes_per_loop[st.loop_tag] = st.passes_per_loop.get(st.loop_tag, 0) + 1
    parsed_next = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_next))
    if parsed_next is None:
        log.warning("pass round parse failed, ending loop")
        st.failure_kind = "parse_failed"
        st.failure_detail = "no JSON object in follow-up narration"
        return None
    st.parsed_current = parsed_next
    st.controller = _mark_declared(st.controller, parsed_next)
    return parsed_next


def _name_pending(st: CycleRuntimeState) -> None:
    """Name undispatched calls on a loop that ends with work pending."""
    pending = st.parsed_current.get("tool_calls")
    if isinstance(pending, list):
        for call in pending:
            if isinstance(call, dict):
                raw_name = str(call.get("name") or call.get("tool") or "").strip()
                if raw_name and raw_name not in st.unexecuted:
                    st.unexecuted.append(raw_name)


async def run_comprehension(
    engine: Any, ctx: Any,
) -> tuple[CycleRuntimeState | None, tuple | None]:
    """COMPREHENSION loop (1 pass, no tools). Returns (st, None) or (None, finished)."""
    gathered = ctx.gathered
    if gathered is None:
        raise RuntimeError("reason/check requires gathered evidence")
    wake, task, scenario = ctx.wake, ctx.task, ctx.scenario
    st = CycleRuntimeState(
        controller=ctx.controller,
        capability_log=ctx.capability_log,
        deterministic_state=gathered.deterministic_state,
        accumulated_tool_results=gathered.accumulated_tool_results,
        tool_results=gathered.tool_results,
        parsed_current={},
    )

    # --- CONTEXT: memory + prior diff ---
    _memories, memory_block = await engine._recall_memory()
    prior_note = await engine._prior_change_note()

    wake_block = json.dumps(
        {"trigger_source": wake.trigger_source,
         "predicates": wake.predicates_fired},
        default=str,
    )
    task_block = (
        f"TASK (interactive-plane directive — frame H0/H1 to ANSWER this; "
        f"cite it in hypothesis.evidence_refs as 'task'):\n{task[:4_000]}\n\n"
        if task else ""
    )
    scenario_block = (
        f"SCENARIO (price-target question — evaluate with the "
        f"calc.scenario.evaluate tool at the given horizon; cite its "
        f"→ … paths, never compute the requirement yourself):\n"
        f"{json.dumps(scenario)}\n\n"
        if scenario else ""
    )
    st.task_reminder = (
        f"TASK reminder (answer this): {task[:500]}\n"
        if task else ""
    )
    st.task_block = task_block  # type: ignore[attr-defined]
    st.scenario_block = scenario_block  # type: ignore[attr-defined]
    st.wake_block = wake_block  # type: ignore[attr-defined]
    st.memory_block = memory_block  # type: ignore[attr-defined]
    st.prior_note = prior_note  # type: ignore[attr-defined]
    st.scenario_reminder = (
        f"SCENARIO reminder (evaluate with calc.scenario.evaluate at the "
        f"given horizon; frame H0/H1 as not-reachable/reachable): "
        f"{json.dumps(scenario)}\n"
        if scenario else ""
    )
    st.phase_guidance = dict(narration_mod.PHASE_GUIDANCE)
    if scenario:
        st.phase_guidance["P5"] = (
            st.phase_guidance["P5"]
            + f" SCENARIO GIVEN ({json.dumps(scenario)}): call "
            "calc.scenario.evaluate with that target_price/horizon IN ADDITION "
            "to recall_paper + price.delta — the scenario verdict is the primary output. "
            "The final scenario block must echo fit_status + n_windows_usable + r2 "
            "and its rationale (≥80 chars) must name them beside the verdict."
        )
    user_prompt_1 = (
        f"{task_block}"
        f"{scenario_block}"
        f"WAKE: {wake_block}\n\n"
        "ENVELOPE STATE (gate reads + wake identity — READ-PLANE; pull "
        "everything else yourself via tools; never recompute values, "
        "COMMAND the read tools and cite their paths):\n"
        f"{json.dumps(st.deterministic_state, default=str)[:60_000]}\n\n"
        f"{prior_note}\n\n"
    )
    if memory_block:
        user_prompt_1 += (
            "RECALLED MEMORY (provenance-tagged priors; subordinate to the "
            f"ledger):\n{memory_block}\n\n"
        )
    user_prompt_1 += engine._output_format()
    st.user_prompt_1 = user_prompt_1  # type: ignore[attr-defined]

    # --- COMPREHENSION PASS (1 LLM call, no tools) ---
    st.seed_plan = seed_evidence_plan(task, scenario)
    st.deterministic_state["seeded_plan"] = st.seed_plan
    st.task_workflow = build_task_workflow(task, scenario)
    st.deterministic_state["task_workflow"] = st.task_workflow
    workflow_block = (
        f"REQUIRED WORKFLOW FOR THIS TASK (kind: {st.task_workflow['kind']} — "
        "walk it in order; this is the shape a complete answer takes):\n"
        + "\n".join(f"- {step}" for step in st.task_workflow["steps"])
        + f"\n{st.task_workflow['handoff']}\n"
    )
    st.workflow_block = workflow_block  # type: ignore[attr-defined]
    # COMPREHENSION is a real governed traversal, but it is deterministic:
    # envelope normalization, task classification and evidence framing do
    # not spend an additional model turn before the agent's first proposal.
    # This keeps the hard-gate and narration budgets unambiguous.
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTAKE
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.INTERPRETATION
    )
    st.passes_per_loop["comprehension"] += 1
    st.comprehension = default_comprehension(
        task, scenario, st.seed_plan, "deterministic",
    )
    st.deterministic_state["comprehension"] = st.comprehension
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.FRAMING
    )
    # Framing is deterministic in the current runtime: the task workflow and
    # acceptance material have been constructed above.  Close it explicitly;
    # leaving the observation open would make the next ENTER_LOOP illegal.
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )
    st.controller = st.controller.record_loop_visit(
        "comprehension", 1,
        ("intake", "interpretation", "framing"), True,
    )
    return st, None


async def run_evidence(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple | None:
    """EVIDENCE loop: narrate#1 + budgeted follow-ups. Ends early on empty
    calls or a failed round (candidate stands; validation decides)."""
    st.loop_tag = "evidence"
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.ENTER_LOOP, NestedLoop.EVIDENCE
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.SOURCING
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ACQUISITION
    )
    # First-turn prompt: envelope state + output contract + seeded plan +
    # required workflow, headed by this loop's relayed state.
    user_prompt_1 = (
        f"{st.task_block}"
        f"{st.scenario_block}"
        f"WAKE: {st.wake_block}\n\n"
        "ENVELOPE STATE (gate reads + wake identity — READ-PLANE; pull "
        "everything else yourself via tools; never recompute values, "
        "COMMAND the read tools and cite their paths):\n"
        f"{json.dumps(st.deterministic_state, default=str)[:60_000]}\n\n"
        f"{st.prior_note}\n\n"
    )
    if st.memory_block:
        user_prompt_1 += (
            "RECALLED MEMORY (provenance-tagged priors; subordinate to the "
            f"ledger):\n{st.memory_block}\n\n"
        )
    user_prompt_1 += engine._output_format()
    st.user_prompt_1 = user_prompt_1  # type: ignore[attr-defined]
    # LOOP STATE relay (evidence pass 1) + seeded plan + required workflow.
    user_prompt_1 = (
        build_loop_state_block(
            controller=st.controller,
            loop="evidence", sub_loop="acquisition",
            intent=TaskIntent.INFER_ORDER_FLOW.value,
            passes_spent=0, pass_budget=LOOP_PASS_BUDGET["evidence"],
            dispatches_left=MAX_DISPATCHES_PER_PASS,
            traversal=st.controller.loop_coverage(),
            gate=st.deterministic_state.get("gate"),
            seeds=list(st.seed_plan["seeds"]),
        )
        + "\n\n" + TOOL_WINDOWS["evidence"]
        + "\n\n" + st.workflow_block  # type: ignore[attr-defined]
        + "\n\n" + st.user_prompt_1  # type: ignore[attr-defined]
    )
    try:
        raw_1 = await engine._call_llm(user_prompt_1)
    except Exception as exc:
        log.exception("narration#1 failed")
        st.controller = st.controller.advance(
            GovernanceEvent(GovernanceEventKind.NARRATION_FAILED)
        )
        terminal = (
            st.controller.terminal.value if st.controller.terminal
            else "narration_failed"
        )
        ctx.controller = st.controller
        st.deterministic_state["terminal"] = terminal
        return await engine._degraded_artifact(
            st.deterministic_state, st.capability_log,
            f"narration_failed: {type(exc).__name__}: {exc}",
        ), {"llm_calls": st.llm_calls, "terminal": terminal}
    st.llm_calls += 1
    st.passes_per_loop["evidence"] += 1
    st.dispatched_in_pass = 0
    parsed_1 = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_1))
    if parsed_1 is None:
        st.controller = st.controller.advance(
            GovernanceEvent(GovernanceEventKind.PARSE_FAILED)
        )
        terminal = (
            st.controller.terminal.value if st.controller.terminal
            else "parse_failed"
        )
        ctx.controller = st.controller
        st.deterministic_state["terminal"] = terminal
        return await engine._degraded_artifact(
            st.deterministic_state, st.capability_log,
            "narration_parse_failed: no JSON object in output",
        ), {"llm_calls": st.llm_calls, "terminal": terminal}
    st.parsed_1 = parsed_1
    st.parsed_current = parsed_1
    st.controller = _mark_declared(st.controller, parsed_1)
    while (st.passes_per_loop["evidence"] < LOOP_PASS_BUDGET["evidence"]
           and st.llm_calls < AGENTIC_MAX_LLM_TURNS):
        if not (isinstance(st.parsed_current.get("tool_calls"), list)
                and st.parsed_current.get("tool_calls")):
            break  # candidate stands; reasoning may still pull
        st.dispatched_in_pass = 0
        await _dispatch_turn(engine, ctx, st)
        if await _followup_turn(engine, ctx, st) is None:
            if st.failure_kind:
                return await run_runtime_failure(engine, ctx, st)
            break
    _name_pending(st)
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.VERIFICATION
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )
    st.controller = st.controller.record_loop_visit(
        "evidence", st.passes_per_loop["evidence"],
        ("sourcing", "acquisition", "verification"),
        completed=True,
    )
    return None


async def run_reasoning(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple | None:
    """REASONING loop: budgeted follow-ups (test → compare → synthesize)."""
    st.loop_tag = "reasoning"
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.ENTER_LOOP, NestedLoop.REASONING
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.HYPOTHESIS
    )
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.ANALYSIS
    )
    while (st.passes_per_loop["reasoning"] < LOOP_PASS_BUDGET["reasoning"]
           and st.llm_calls < AGENTIC_MAX_LLM_TURNS):
        if not (isinstance(st.parsed_current.get("tool_calls"), list)
                and st.parsed_current.get("tool_calls")):
            break  # candidate stands; validation decides
        st.dispatched_in_pass = 0
        await _dispatch_turn(engine, ctx, st)
        if await _followup_turn(engine, ctx, st) is None:
            if st.failure_kind:
                return await run_runtime_failure(engine, ctx, st)
            break
    _name_pending(st)
    _open_once(st, SubLoop.SYNTHESIS)
    # Freeze the steady-track reading at reasoning exit: the validator
    # judges this, not the agent's self-report.
    st_chain = chain_completion(st.controller, task=ctx.task, scenario=ctx.scenario)
    st.deterministic_state["statistical_chain"] = {
        "links": st_chain["links"], "missing": st_chain["missing"],
        "refused": st_chain["refused"], "complete": st_chain["complete"],
    }
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
    )
    st.controller = st.controller.record_loop_visit(
        "reasoning", st.passes_per_loop["reasoning"],
        ("hypothesis", "analysis", "synthesis"),
        completed=True,
    )
    return None


async def run_validation(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple | None:
    """VALIDATION loop: gate + one bounded retry, else FSM terminal."""
    st.loop_tag = "validation"
    if not st.validation_entered:
        st.validation_entered = True
        st.passes_per_loop["validation"] = st.passes_per_loop.get("validation", 0) + 1
        st.controller, _ = context._must_govern(
            st.controller, GovernanceEventKind.ENTER_LOOP, NestedLoop.VALIDATION
        )
        st.controller, _ = context._must_govern(
            st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.GATE
        )
    elif (
        st.controller.observation is not None
        and st.controller.observation.sub_loop is None
    ):
        # A re-entry after a completed recovery starts a fresh gate without
        # pretending that the parent loop was re-entered.
        st.controller, _ = context._must_govern(
            st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.GATE
        )
    passed, missing = narration_mod.validate_final_turn(
        st.parsed_current, st.controller.phase_coverage, scenario=ctx.scenario,
        scenario_status=st.controller.scenario_state(),
        scenario_refusal_reason=st.controller.scenario_refusal_reason(),
    )
    # Steady-track gate (core-owned): required-but-never-attempted chain
    # links join missing[] and ride the existing repair path. Refused links
    # are findings — they never block, and the controller already
    # suppresses their re-dispatch structurally.
    st_chain = chain_completion(st.controller, task=ctx.task, scenario=ctx.scenario)
    st.deterministic_state["statistical_chain"] = {
        "links": st_chain["links"], "missing": st_chain["missing"],
        "refused": st_chain["refused"], "complete": st_chain["complete"],
    }
    missing = list(missing)
    for tool in st_chain["missing"]:
        missing.append(
            f"statistical chain: {tool} never attempted — walk the steady "
            "track in order (forecast → scenario → hypothesis → decay → "
            "discipline audit) and call each missing link before finalizing"
        )
    if passed:
        st.final_validation = {"passed": True, "missing": []}
        st.finalize_now = True
        if st.controller.observation is not None and st.controller.observation.sub_loop is not None:
            st.controller, _ = context._must_govern(
                st.controller, GovernanceEventKind.COMPLETE_SUBLOOP
            )
        st.controller = st.controller.record_loop_visit(
            "validation", st.passes_per_loop.get("validation", 0),
            ("gate",) if st.validation_retries_used == 0
            else ("gate", "recovery"),
            completed=True,
        )
        ctx.controller = st.controller
        ctx.reasoned = st.to_reasoned()
        return None
    if st.validation_retries_used >= VALIDATION_RETRY_PASSES:
        return await run_validation_terminal(engine, ctx, st, list(missing))
    st.repairs_sent += 1
    st.validation_retries_used += 1
    st.controller, _ = context._must_govern(
        st.controller, GovernanceEventKind.OPEN_SUBLOOP, SubLoop.RECOVERY
    )
    scenario_steer = st.controller.scenario_steer()
    repair_header = build_loop_state_block(
        controller=st.controller,
        loop="validation", sub_loop="recovery",
        intent=TaskIntent.VALIDATE_FINAL.value,
        passes_spent=st.passes_per_loop.get("validation", 0),
        pass_budget=LOOP_PASS_BUDGET["validation"],
        dispatches_left=MAX_DISPATCHES_PER_PASS,
        traversal=st.controller.loop_coverage(),
        gate=st.deterministic_state.get("gate"),
        congruence=st.controller.congruence(),
        comprehension=st.deterministic_state.get("comprehension"),
    )
    repair_prompt = (
        f"{repair_header}\n\n"
        f"{TOOL_WINDOWS['validation']}\n\n"
        f"{st.task_reminder if ctx.task else ''}"
        f"{st.scenario_reminder if ctx.scenario else ''}"
        f"{scenario_steer}"
        "FINAL REJECTED — staged inference incomplete. Missing:\n"
        + "\n".join(f"- {item}" for item in missing)
        + f"\n\nACCUMULATED TOOL RESULTS:\n{json.dumps(st.accumulated_tool_results, default=str)[:40_000]}\n\n"
        f"PHASE COVERAGE: {json.dumps({p: sorted(s) for p, s in st.controller.phase_coverage.items()})}\n"
        f"{st.phase_guidance[narration_mod.next_uncovered_phase(st.controller.phase_coverage)]}\n"
        f"{TURN_CONTRACT_LINE}\n"
        "Return the next turn now: declare \"phase\", include the missing tool_calls, "
        "and finalize (tool_calls=[]) only when every missing item is addressed."
    )
    try:
        raw_repair = await engine._call_llm(repair_prompt)
    except Exception as exc:
        log.exception("repair turn failed; ending validation")
        st.failure_kind = "narration_failed"
        st.failure_detail = f"{type(exc).__name__}: {exc}"
        return await run_runtime_failure(engine, ctx, st)
    st.llm_calls += 1
    st.passes_per_loop["validation"] = st.passes_per_loop.get("validation", 0) + 1
    parsed_repair = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_repair))
    if parsed_repair is None:
        log.warning("repair parse failed, ending validation")
        st.failure_kind = "parse_failed"
        st.failure_detail = "no JSON object in repair narration"
        return await run_runtime_failure(engine, ctx, st)
    st.parsed_current = parsed_repair
    st.controller = _mark_declared(st.controller, parsed_repair)
    st.dispatched_in_pass = 0
    await _dispatch_turn(engine, ctx, st)
    # A repair that only repeats a deterministically refused/suppressed tool
    # already contains the candidate final.  Do not spend an unbounded extra
    # narration turn merely to report that no work executed.
    if getattr(st, "_round_had_execution", False):
        if await _followup_turn(engine, ctx, st) is None:
            if st.failure_kind:
                return await run_runtime_failure(engine, ctx, st)
            log.warning("repair follow-up unavailable; judging repair turn as it stands")
    return await run_validation(engine, ctx, st)


async def run_runtime_failure(
    engine: Any, ctx: Any, st: CycleRuntimeState,
) -> tuple:
    """Route a transport/parse failure through the canonical FSM terminal."""
    kind = st.failure_kind or "infra_failed"
    event_kind = {
        "narration_failed": GovernanceEventKind.NARRATION_FAILED,
        "parse_failed": GovernanceEventKind.PARSE_FAILED,
        "budget_exhausted": GovernanceEventKind.BUDGET_EXHAUSTED,
    }.get(kind, GovernanceEventKind.INFRA_FAILED)
    st.controller = st.controller.advance(GovernanceEvent(event_kind))
    terminal = st.controller.terminal.value if st.controller.terminal else kind
    st.deterministic_state["terminal"] = terminal
    st.deterministic_state["failure"] = {
        "kind": kind, "detail": st.failure_detail,
    }
    st.deterministic_state["loop_traversal"] = st.controller.loop_coverage()
    st.deterministic_state["governance_trace"] = st.controller.transition_trace()
    return await engine._degraded_artifact(
        st.deterministic_state, st.capability_log,
        f"{kind}: {st.failure_detail or 'runtime failure'}",
    ), {
        "llm_calls": st.llm_calls,
        "terminal": terminal,
        "failure": st.deterministic_state["failure"],
        "final_validation": st.final_validation,
        "repairs": st.repairs_sent,
        "tool_round": bool(st.tool_results),
        "tool_rounds": st.tool_rounds_used,
        "phase_coverage": {
            phase: sorted(tools)
            for phase, tools in st.controller.phase_coverage.items()
        },
    }


async def run_validation_terminal(
    engine: Any, ctx: Any, st: CycleRuntimeState, missing: list[str],
) -> tuple:
    """Shared terminal path: structured issue → FSM → degraded artifact."""
    validation_issue = {
        "loop": "validation",
        "missing": list(missing),
        "congruence": st.controller.congruence(),
        "loop_traversal": st.controller.loop_coverage(),
        "passes_per_loop": dict(st.passes_per_loop),
    }
    _covered = set(st.controller.loop_coverage())
    if st.passes_per_loop.get("evidence", 0) > 0 and "evidence" not in _covered:
        st.controller = st.controller.record_loop_visit(
            "evidence", st.passes_per_loop["evidence"],
            ("sourcing", "acquisition", "verification"),
            completed=False,
        )
    if st.passes_per_loop.get("reasoning", 0) > 0 and "reasoning" not in _covered:
        st.controller = st.controller.record_loop_visit(
            "reasoning", st.passes_per_loop["reasoning"],
            ("hypothesis", "analysis", "synthesis"),
            completed=False,
        )
    st.controller = st.controller.record_loop_visit(
        "validation", st.passes_per_loop.get("validation", 0),
        ("gate", "recovery"), completed=False,
    )
    st.controller = st.controller.advance(
        GovernanceEvent(GovernanceEventKind.VALIDATION_FAILED)
    )
    terminal = (
        st.controller.terminal.value if st.controller.terminal
        else "validation_failed"
    )
    ctx.controller = st.controller
    st.deterministic_state["terminal"] = terminal
    st.deterministic_state["validation_issue"] = validation_issue
    st.deterministic_state["loop_traversal"] = st.controller.loop_coverage()
    st.deterministic_state["congruence"] = st.controller.congruence()
    return await engine._degraded_artifact(
        st.deterministic_state, st.capability_log,
        f"validation_failed: {'; '.join(missing)[:500]}",
    ), {
        "llm_calls": st.llm_calls,
        "terminal": terminal,
        "validation_issue": validation_issue,
        "final_validation": {"passed": False, "missing": list(missing)},
        "repairs": st.repairs_sent,
        "tool_round": bool(st.tool_results),
        "tool_rounds": st.tool_rounds_used,
        "phase_coverage": {
            phase: sorted(tools)
            for phase, tools in st.controller.phase_coverage.items()
        },
    }
