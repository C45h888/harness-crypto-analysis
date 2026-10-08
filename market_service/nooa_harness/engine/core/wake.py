"""WAKE stage — canonical owner of WAKE / COMPREHENSION.

One NestedLoop = one file. This module owns the full WAKE loop family:

  run_wake           INTAKE (RECEIVE → DECODE → NORMALIZE): envelope
                     normalization + governed controller construction.
  run_comprehension  INTERPRETATION → FRAMING: deterministic task
                     classification, evidence-need framing, acceptance setup.

Per-pass mechanics live next to their owning modules:

  CycleRuntimeState, _must_govern, _CycleContext
        → engine.core.context
  _dispatch_turn, _followup_turn (not used by WAKE — 0 dispatches)
        → InferenceEngine (engine.core.driver)
  seed_evidence_plan, default_comprehension, prompt composers
        → engine.kb

FSM alignment (strict order, no close-reset between siblings):

  initial(CONTEXT / UNDERSTAND_TASK / None)
    → [fetch owns OPEN INTAKE in run_agent_fetch] → OPEN INTERPRETATION
    → OPEN FRAMING (cursor stays open for SOURCING)
    → single CONTEXT visit at VERIFICATION close (all six sub-loops).

Budgets: LOOP_PASS_BUDGET["comprehension"] == 1, 0 dispatches, ≤1 bounded
LLM call (Task-Directive Phase-B assessment only when _assessment_due).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from market_service.runtime.contracts import WakeEnvelope

from . import context
from .context import CycleRuntimeState, _CycleContext
from ..schemas import coerce_turn, extract_json_object
from ..kb import PHASE_GUIDANCE as _PHASE_GUIDANCE
from ..controller import CycleController
from ..fsm import GOVERNANCE_MEMBRANE, GovernanceEvent, GovernanceEventKind
from ..gates import evaluate_plan_gate
from ..kb import (
    build_task_workflow,
    default_comprehension,
    seed_evidence_plan,
)
from ..loop_states import NestedLoop, SubLoop

log = logging.getLogger(__name__)

__all__ = [
    "PromptVariable",
    "build_manual_wake",
    "run_wake",
    "run_comprehension",
    "is_intake_complete",
    "is_interpretation_complete",
    "is_framing_complete",
]


from dataclasses import dataclass, field


@dataclass(frozen=True)
class PromptVariable:
    """Canonical prompt variable — the ONLY normalized CLI→Wake injection.

    Constructed once in ``run_wake`` from the raw CLI ``task``/``scenario``;
    every downstream loop reads these bounded fields instead of re-slicing
    the raw string. Truncation policy lives here and nowhere else:
    preview_200 (capability/attribution), reminder_500 (follow-up turns),
    block_4000 (narrate#1 prompt block). ``is_empty`` drives the token
    guard: empty → 0 WAKE LLM calls, assessment never due.
    """

    raw: str | None = None
    scenario: dict[str, Any] | None = None
    preview_200: str | None = None
    reminder_500: str = ""
    block_4000: str = ""
    is_empty: bool = True

    @classmethod
    def from_cli(
        cls, task: str | None, scenario: dict[str, Any] | None,
    ) -> "PromptVariable":
        stripped = task.strip() if isinstance(task, str) else ""
        empty = not stripped
        return cls(
            raw=task,
            scenario=scenario,
            preview_200=task[:200] if task else None,
            reminder_500=(
                f"TASK reminder (answer this): {task[:500]}\n"
                if task else ""
            ),
            block_4000=(
                f"TASK (interactive-plane directive — frame H0/H1 to ANSWER this; "
                f"cite it in hypothesis.evidence_refs as 'task'):\n{task[:4_000]}\n\n"
                if task else ""
            ),
            is_empty=empty,
        )


async def build_manual_wake(
    engine: Any, task: str | None = None,
) -> tuple[WakeEnvelope, dict[str, Any]]:
    """Synthesize a MANUAL wake (the CLI ``--force`` trigger — the only trigger).

    The human trigger IS the invocation — no predicate evaluation, no
    loop, no worker. A directly-injected envelope (caller-constructed)
    bypasses this entirely and goes straight to the runner. Canonical
    ownership lives here (INTAKE plane); ``InferenceEngine`` forwards.

    ``task`` is the interactive-plane directive (trade hypothesis prompt
    from ``harness.py --task``). It is carried on the manual predicates
    (truncated preview) so the firing is attributable, and threaded
    separately into the cycle for full prompt steering.
    """
    snapshot = await engine.collect_snapshot()
    predicates: dict[str, Any] = {"manual": {}}
    if task:
        predicates["manual"] = {"task_preview": task[:200]}
    envelope = WakeEnvelope.create(
        symbol=engine.symbol, venue=engine.venue, trigger_source="manual",
        predicates_fired=predicates,
        counter_snapshot={
            "event_stream_len": snapshot["event_stream_len"],
            "capture_state": snapshot["capture_state"],
            "last_artifact_events_total": snapshot["last_artifact_events_total"],
        },
        high_water={
            "events_total": snapshot["last_artifact_events_total"],
            "completed_at_ms": snapshot["last_artifact_completed_at_ms"],
        },
    )
    return envelope, {"decision": "fire", "forced": True,
                      "source": "manual",
                      "consumed_wake_ids": [envelope.wake_id]}


def _task_segment_id(prompt: "PromptVariable") -> str | None:
    """Deterministic task-scoped memory segment id (state redistribution).

    The normalized prompt hash is the segment key: re-invoking the same task
    recalls the same segment, and different tasks cannot pollute each other's
    plan/handoff carry. Empty prompts are unsegmented (None) — legacy shape.
    """
    import hashlib

    basis = (prompt.raw or "").strip()
    if not basis:
        return None
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"task:{digest}"


def run_wake(
    engine: Any, wake: Any, wake_meta: dict[str, Any],
    task: str | None, scenario: dict[str, Any] | None,
) -> _CycleContext:
    """Initialize the envelope and governed controller (WAKE seam).

    INTAKE steps RECEIVE → DECODE → NORMALIZE: the raw wake envelope is
    received, decoded into trigger/predicate fields, and normalized into the
    cycle capability log with bounded task preview. Canonical ownership lives
    here; the runner calls this function directly (driver boundary).
    """
    # Canonical injection: the raw CLI prompt normalizes ONCE here into the
    # PromptVariable; every downstream consumer reads its bounded fields.
    prompt = PromptVariable.from_cli(task, scenario)
    generated_at = context._utc_now_iso()
    capability_log: list[dict[str, Any]] = [{
        "capability": "engine.wake",
        "scope": {"symbol": engine.symbol, "venue": engine.venue},
        "result": "ok",
        "detail": {
            "trigger_source": wake.trigger_source,
            "predicates_fired": wake.predicates_fired,
            "decision": wake_meta.get("decision"),
            "consumed_wake_ids": wake_meta.get("consumed_wake_ids", []),
            "task": prompt.preview_200,
            "scenario": scenario,
        },
    }]

    # --- CYCLE CONTROLLER (constructed at wake, immutable from here) ---
    # The controller is the loop's sole semantic authority. It RECEIVES
    # the envelope: cycle context (task, scenario, wake identity) enters
    # its ledger, it hands the envelope to the agent via narrate#1, and
    # every outcome the agent produces is classified by IT alone. Every
    # transition returns a NEW immutable controller; ``controller`` is
    # rebound in place at each step.
    #
    # GOVERNANCE: the controller SITS UNDER the governing membrane. It is
    # built with GOVERNANCE_MEMBRANE and the initial observation
    # (COMPREHENSION / UNDERSTAND_TASK) — the membrane's legality governs
    # every subsequent move.
    controller = (
        CycleController(scenario)
        .with_membrane(GOVERNANCE_MEMBRANE)
        .with_observation(GOVERNANCE_MEMBRANE.initial())
    )

    return _CycleContext(
        wake=wake, task=task, scenario=scenario, generated_at=generated_at,
        controller=controller, capability_log=capability_log,
        prompt=prompt,
        task_id=_task_segment_id(prompt),
    )


# ---------------------------------------------------------------------------
# Completion predicates — executable (code, not prose) per-sub-loop exits.
# Pure reads over ctx/st; Phase 2 hardens call sites to fail closed on False.
# ---------------------------------------------------------------------------


def is_intake_complete(ctx: _CycleContext) -> bool:
    """INTAKE complete iff the envelope normalized into the capability log."""
    if not ctx.capability_log:
        return False
    first = ctx.capability_log[0]
    return (
        first.get("capability") == "engine.wake"
        and isinstance(first.get("detail"), dict)
    )


def is_interpretation_complete(st: CycleRuntimeState) -> bool:
    """INTERPRETATION complete iff directive + assessment disposition bound."""
    directive = st.task_directive or {}
    if not directive.get("kind"):
        return False
    # Fully-bound directive OR recorded assessment outcome both satisfy;
    # assessment_note covers unparseable/unavailable/reject paths.
    return True


def is_framing_complete(st: CycleRuntimeState) -> bool:
    """FRAMING complete iff seed plan + workflow + receipt persisted."""
    ds = st.deterministic_state or {}
    return bool(
        st.seed_plan is not None
        and st.task_workflow is not None
        and ds.get("seeded_plan") is not None
        and ds.get("task_workflow") is not None
        and ds.get("comprehension") is not None
        and ds.get("understanding_receipt") is not None
    )


async def run_comprehension(
    engine: Any, ctx: Any,
) -> tuple[CycleRuntimeState | None, tuple | None]:
    """COMPREHENSION loop (1 pass, no tools). Returns (st, None) or (None, finished)."""
    gathered = ctx.gathered
    if gathered is None:
        # WAKE ordering: comprehension runs after gather's deterministic
        # evidence so the agent's first proposal sees gate reads. The FSM
        # observation is still COMPREHENSION — gather's bootstrap reads are
        # authorized as wake-bootstrap, not as EVIDENCE traversal.
        raise RuntimeError("reason/check requires gathered evidence")
    wake, task, scenario = ctx.wake, ctx.task, ctx.scenario
    # Canonical read: bounded prompt fields come off the injected variable;
    # getattr fallback preserves direct-ctx construction in older tests.
    prompt = getattr(ctx, "prompt", None)
    if prompt is None:
        prompt = PromptVariable.from_cli(task, scenario)
    st = CycleRuntimeState(
        controller=ctx.controller,
        capability_log=ctx.capability_log,
        deterministic_state=gathered.deterministic_state,
        accumulated_tool_results=gathered.accumulated_tool_results,
        tool_results=gathered.tool_results,
        parsed_current={},
    )
    # Seed the carrier from the fetch receipt: the fetch turn's LLM cost,
    # tier history, and unexecuted names belong to this cycle's budgets.
    fetched = getattr(ctx, "fetched", None)
    if fetched is not None:
        st.llm_calls = fetched.llm_calls
        st.llm_tiers = list(fetched.llm_tiers)
        st.unexecuted = list(fetched.unexecuted)
        st.passes_per_loop["fetch"] = 1

    # --- CONTEXT: memory node pruned from the track (transport retained on
    # the driver for later use) — no recall read feeds the prompts; H0/H1
    # grounding comes from the track's own evidence.

    wake_block = json.dumps(
        {"trigger_source": wake.trigger_source,
         "predicates": wake.predicates_fired},
        default=str,
    )
    task_block = prompt.block_4000
    scenario_block = (
        f"SCENARIO (price-target question — evaluate with the "
        f"calc.scenario.evaluate tool at the given horizon; cite its "
        f"→ … paths, never compute the requirement yourself):\n"
        f"{json.dumps(scenario)}\n\n"
        if scenario else ""
    )
    st.task_reminder = prompt.reminder_500
    st.task_block = task_block  # type: ignore[attr-defined]
    st.scenario_block = scenario_block  # type: ignore[attr-defined]
    st.wake_block = wake_block  # type: ignore[attr-defined]
    # STATE REDISTRIBUTION: task-scoped recall. The plan/handoff carry for
    # this task segment is recalled into the prompt block so the agent
    # reasons over its own prior conclusions, segment-isolated from other
    # tasks. Empty when no prior carry exists.
    _memories, st.memory_block = await engine._recall_memory(
        segment=ctx.task_id,
    )
    st.prior_note = fetched.fetch_note if fetched is not None else ""  # type: ignore[attr-defined]
    st.scenario_reminder = (
        f"SCENARIO reminder (evaluate with calc.scenario.evaluate at the "
        f"given horizon; frame H0/H1 as not-reachable/reachable): "
        f"{json.dumps(scenario)}\n"
        if scenario else ""
    )
    st.phase_guidance = dict(_PHASE_GUIDANCE)
    if scenario:
        st.phase_guidance["P5"] = (
            st.phase_guidance["P5"]
            + f" SCENARIO GIVEN ({json.dumps(scenario)}): call "
            "calc.scenario.evaluate with that target_price/horizon IN ADDITION "
            "to price.delta — the scenario verdict is the primary output. "
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
    )
    st.user_prompt_1 = user_prompt_1  # type: ignore[attr-defined]

    # --- COMPREHENSION PASS (1 LLM call, no tools) ---
    # --- TASK DIRECTIVE (Phase A reparse + Phase B assessment) ---
    # UNDERSTANDING (sole author): single entry over the canonical prompt
    # variable — Phase-A parse + plan bind + due-gate. Gather ran the same
    # entry provisionally for pre-acquisition; the values frozen here are
    # the final authority overwriting that provisional write. The LLM then
    # gets ONE bounded assessment turn: it PROPOSES within frozen
    # vocabularies; the engine disposes — validated proposals bind via
    # dispose_assessment, rejects are recorded, the track is never amended
    # by the agent directly (controller doctrine).
    from ..task_directive import (
        assessment_prompt, build_plan, dispose_assessment, understand,
    )
    directive, plan, _assessment_due = understand(prompt)
    assessment_note: str | None = None
    raw_proposal: dict[str, Any] | None = None
    if _assessment_due:
        try:
            raw_assessment = await engine._call_llm(
                assessment_prompt(task, directive))
            st.llm_calls += 1
            context.record_tier(st)
            parsed_assessment = coerce_turn(
                extract_json_object(raw_assessment))
            if parsed_assessment is None:
                assessment_note = "assessment_unparseable; deterministic parse stands"
            else:
                raw_proposal = (
                    dict(parsed_assessment)
                    if isinstance(parsed_assessment, dict) else None
                )
                directive, rejects = dispose_assessment(directive, parsed_assessment)
                plan = build_plan(directive)  # rebind after disposal binds
                if rejects:
                    assessment_note = (
                        "assessment rejects: "
                        + "; ".join(f"{r['field']}: {r['reason']}" for r in rejects))
        except Exception as exc:
            assessment_note = f"assessment_unavailable: {type(exc).__name__}"
    st.task_directive = directive.to_dict()
    st.task_plan = plan
    st.deterministic_state["task_directive"] = st.task_directive
    st.deterministic_state["task_plan"] = plan
    st.deterministic_state["assessment_note"] = assessment_note

    st.seed_plan = seed_evidence_plan(task, scenario)
    st.deterministic_state["seeded_plan"] = st.seed_plan
    st.task_workflow = build_task_workflow(task, scenario,
                                            kind_override=plan["kind"])
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
    # INTAKE opened in run_agent_fetch (the fetch turn runs there); the
    # cursor advances to INTERPRETATION here, FRAMING after that.
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
    # Framing is deterministic in the current runtime. Do NOT complete the
    # sub-loop here: CONTEXT is now a single loop whose next sub-loop is
    # SOURCING (plan-bound acquisition), and completing would reset the
    # cursor to None so the next OPEN_SUBLOOP would re-open INTAKE instead.
    # The cursor stays on FRAMING until sourcing opens.
    # Understanding receipt — frozen at FRAMING close, read-only thereafter.
    # Loop 2 consumes this instead of re-parsing: what the agent was given
    # (preview), what was understood (directive/plan), the answer shape
    # (workflow kind), the disposal outcome, and the agent's raw proposal.
    st.deterministic_state["understanding_receipt"] = {
        "prompt_preview": prompt.preview_200,
        "directive": st.task_directive,
        "plan_kind": plan["kind"],
        "workflow_kind": st.task_workflow["kind"],
        "assessment": assessment_note or ("silent" if not _assessment_due else "ratified"),
        "raw_proposal": raw_proposal,
        "seed_plan": st.seed_plan,
        "comprehension": st.comprehension,
        "loop_visit": ("intake", "interpretation", "framing"),
    }
    # GATE_PLAN (post-framing): the deterministic plan-adequacy guard.
    # Forward-only — a refused plan routes to the canonical gate terminal;
    # the verdict is the frozen data contract, never inline judgement.
    plan_gate = evaluate_plan_gate(st.task_plan)
    st.deterministic_state["plan_gate"] = plan_gate.to_dict()
    if plan_gate.refused:
        st.controller = st.controller.advance(
            GovernanceEvent(GovernanceEventKind.GATE_REFUSED)
        )
        terminal = (
            st.controller.terminal.value if st.controller.terminal
            else "gate_refused"
        )
        st.deterministic_state["terminal"] = terminal
        ctx.controller = st.controller
        return None, (
            await engine._degraded_artifact(
                st.deterministic_state, st.capability_log,
                "plan_gate_refused: " + "; ".join(plan_gate.reasons),
            ),
            {"llm_calls": st.llm_calls, "terminal": terminal,
             "plan_gate": plan_gate.to_dict()},
        )
    # The CONTEXT loop visit is recorded once at its true close (end of
    # verification in run_evidence), covering all six sub-loops.
    return st, None
