"""Loop states — the in-depth hierarchical workflow the agentic loop follows.

LAYER 1 of the two-layer agentic surface. This module defines the LOOP
VOCABULARY — the hierarchical workflow the agent walks. It does NOT define
transition legality and it does NOT own semantics (classification / credit /
exit). Those belong to the governance membrane (Layer 2, deferred) and the
``CycleController``, respectively.

The hierarchy is four levels deep, so each loop state carries an IN-DEPTH
workflow nested loop:

  AgenticStage          the workflow position (WAKE .. OUTPUT)
    └── NestedLoop      the mode of work (COMPREHENSION .. OUTPUT)
          └── SubLoop   an in-depth workflow stage inside the loop
                └── LoopStep   a fine state inside the sub-loop

The SUB-LOOP is the primary agentic loop the agent follows: each
``NestedLoop`` is a multi-stage workflow (2-4 ``SubLoop`` stages), and each
sub-loop is an ordered step sequence with its own exit condition. Some
sub-loops ITERATE until their exit condition is met — those are the
reason/act/observe cores (marked ``PRIMARY_SUBLOOP``).

Tasks are INTENT-based (``TaskIntent``), never P-numbers — the intent names
what the agent is trying to establish, so the agentic surface carries
authority rather than obeying a phase ordinal. ``INTENT_SUBLOOP`` binds each
intent to the sub-loop that serves it, giving the future membrane
in-depth visibility.

The FINAL nested loop is ``OUTPUT``: it composes the output, grounds it in
an UNDERSTANDING OF THE AGENTIC SURFACE ARCHITECTURE (the ``ARCHITECTURE``
sub-loop), places it, and disposes memory. Placing output without
understanding the surface it is placed into is a drift vector; the
architecture sub-loop is structural, not advisory.

Nested loops are IMPLEMENTED here as vocabulary + workflow shape. The
governance membrane (Layer 2) and the runtime iteration are separate passes;
see ``docs/NESTED_LOOP_SPEC.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Level 1 — the workflow stages (top-level traversal)
# ---------------------------------------------------------------------------


class AgenticStage(str, Enum):
    """The agentic loop's top-level workflow stages, in traversal order.

        WAKE -> GATHER -> REASON -> CHECK -> FINALIZE

    Each stage HOSTS exactly one ``NestedLoop`` (see ``STAGE_LOOP``). A stage
    is the agent's position in the workflow; the nested loop inside it
    carries the in-depth work.
    """

    WAKE = "wake"          # prompt/wake received; understand the ask
    GATHER = "gather"      # acquire data: reads + calc/microprice tools
    REASON = "reason"      # reason prompt data against tool data
    CHECK = "check"        # validate the final; recover when rejected
    FINALIZE = "finalize"  # place the output; dispose memory


# ---------------------------------------------------------------------------
# Level 2 — the nested loops (one per stage)
# ---------------------------------------------------------------------------


class NestedLoop(str, Enum):
    """The in-depth workflow loop each stage hosts.

    Named for the MODE of work; the subject is the ``TaskIntent``. The final
    loop is ``OUTPUT`` — placing the output requires understanding the
    agentic surface it is placed into.
    """

    COMPREHENSION = "comprehension"  # WAKE: understand the prompt
    EVIDENCE = "evidence"            # GATHER: acquire + verify data
    REASONING = "reasoning"          # REASON: form + test + synthesize
    VALIDATION = "validation"        # CHECK: gate the final; recover
    OUTPUT = "output"                # FINALIZE: compose, understand, place


# ---------------------------------------------------------------------------
# Level 3 — the sub-loops (the in-depth workflow inside each loop)
# ---------------------------------------------------------------------------


class SubLoop(str, Enum):
    """In-depth workflow stages inside a ``NestedLoop``.

    Each nested loop is a sequence of 2-4 sub-loops; each sub-loop is an
    ordered ``LoopStep`` sequence and may iterate to its exit condition.
    This is the primary agentic loop the agent follows.
    """

    # --- COMPREHENSION (WAKE) ---
    INTAKE = "intake"                     # receive + normalize the input
    INTERPRETATION = "interpretation"     # classify intent, extract constraints
    FRAMING = "framing"                   # decompose task, set acceptance

    # --- EVIDENCE (GATHER) ---
    SOURCING = "sourcing"                 # plan reads, select tools
    ACQUISITION = "acquisition"           # read + calculate + observe
    VERIFICATION = "verification"         # assess coverage, re-plan

    # --- REASONING (REASON) ---
    HYPOTHESIS = "hypothesis"             # frame H0/H1, state prior
    ANALYSIS = "analysis"                 # test + compare against evidence
    SYNTHESIS = "synthesis"               # interpret + reconcile

    # --- VALIDATION (CHECK) ---
    GATE = "gate"                         # validate, classify findings
    RECOVERY = "recovery"                 # diagnose, steer, revalidate

    # --- OUTPUT (FINALIZE) ---
    COMPOSITION = "composition"           # assemble + ground the output
    ARCHITECTURE = "architecture"         # understand the surface, locate output
    PLACEMENT = "placement"               # persist + confirm the output
    MEMORY = "memory"                     # propose + resolve + settle


# ---------------------------------------------------------------------------
# Level 4 — the fine-grained loop steps
# ---------------------------------------------------------------------------


class LoopStep(str, Enum):
    """The fine-grained steps inside each sub-loop.

    These are the states the AGENT traverses within a sub-loop. The
    governance membrane deliberately does NOT observe at this granularity —
    it watches loop + sub-loop boundaries. ``SUBLOOP_SPECS`` maps each
    sub-loop to its ordered steps.
    """

    # --- INTAKE ---
    RECEIVE = "receive"                  # input arrives
    DECODE = "decode"                    # decode the transport (text/JSON)
    NORMALIZE = "normalize"              # normalize fields, defaults, units

    # --- INTERPRETATION ---
    CLASSIFY_INTENT = "classify_intent"          # what is being asked?
    EXTRACT_CONSTRAINTS = "extract_constraints"  # horizon, scenario, limits
    IDENTIFY_QUESTIONS = "identify_questions"    # what must be answered

    # --- FRAMING ---
    DECOMPOSE = "decompose"                      # break the task into parts
    SELECT_EVIDENCE_NEEDS = "select_evidence_needs"  # what evidence is required
    SET_ACCEPTANCE = "set_acceptance"            # acceptance bar for the final

    # --- SOURCING ---
    PLAN_READS = "plan_reads"            # choose the reads
    SELECT_TOOLS = "select_tools"        # choose calc/microprice tools

    # --- ACQUISITION ---
    READ = "read"                        # dispatch read tools
    CALCULATE = "calculate"              # dispatch deterministic calc tools
    OBSERVE = "observe"                  # capture results into the ledger

    # --- VERIFICATION ---
    ASSESS = "assess"                    # coverage vs plan
    REPLAN = "replan"                    # close gaps -> back to sourcing

    # --- HYPOTHESIS ---
    FRAME = "frame"                      # frame H0/H1 from prompt + data
    STATE_PRIOR = "state_prior"          # state the prior / expected direction

    # --- ANALYSIS ---
    TEST = "test"                        # test the hypothesis against evidence
    COMPARE = "compare"                  # compare sources, agreements, conflicts

    # --- SYNTHESIS ---
    INTERPRET = "interpret"              # interpret what the evidence means
    RECONCILE = "reconcile"              # reconcile contradictions explicitly

    # --- GATE ---
    VALIDATE = "validate"                # run the final gate
    CLASSIFY_FINDINGS = "classify_findings"  # classify pass / missing items

    # --- RECOVERY ---
    DIAGNOSE = "diagnose"                # diagnose why the final was rejected
    STEER = "steer"                      # synthesize the repair steer
    REVALIDATE = "revalidate"            # re-run the gate after repair

    # --- COMPOSITION ---
    ASSEMBLE = "assemble"                # assemble the output artifact
    GROUND = "ground"                    # ground every claim in evidence

    # --- ARCHITECTURE ---
    UNDERSTAND_SURFACE = "understand_surface"  # understand the agentic surface
    LOCATE_OUTPUT = "locate_output"            # locate where output belongs

    # --- PLACEMENT ---
    PERSIST = "persist"                  # write the artifact
    CONFIRM = "confirm"                  # confirm placement landed

    # --- MEMORY ---
    PROPOSE = "propose"                  # collect memory proposals
    RESOLVE = "resolve"                  # dispose each proposal
    SETTLE = "settle"                    # cycle closed


# ---------------------------------------------------------------------------
# Level 5 — intent-based tasks
# ---------------------------------------------------------------------------


class TaskIntent(str, Enum):
    """What the agent is trying to establish — intent, not a phase ordinal.

    Replaces the P1->P6 phase ladder as the unit of work. The intent names
    the goal ("infer order flow") so the agentic surface carries authority:
    the model reasons about WHAT it is establishing, and the membrane gates
    on that intent's loop + sub-loop, not on a numeric phase.
    """

    # comprehension
    UNDERSTAND_TASK = "understand_task"        # comprehend task/scenario

    # evidence
    INFER_ORDER_FLOW = "infer_order_flow"      # order-flow (OFI) evidence
    INFER_DEPTH = "infer_depth"                # depth / liquidity evidence

    # reasoning
    CORRELATE_EVIDENCE = "correlate_evidence"  # reconcile substrate vs market
    EXPLAIN_FINDINGS = "explain_findings"      # synthesize meaning
    DERIVE_HYPOTHESIS = "derive_hypothesis"    # paper-grounded derivation
    SYNTHESIZE_OUTPUT = "synthesize_output"    # produce the primary output

    # validation
    VALIDATE_FINAL = "validate_final"          # run the final gate

    # output (the final state — place the output into the surface)
    UNDERSTAND_SURFACE = "understand_surface"  # understand the agentic surface
    PLACE_OUTPUT = "place_output"              # place the output


# ---------------------------------------------------------------------------
# Level 6 — terminal endpoints
# ---------------------------------------------------------------------------


class LoopTerminal(str, Enum):
    """The endpoints of a cycle — success and first-class failures.

    A terminal is not a loop state; it is where the cycle stops. Failures
    are separated by REMEDIATION, not by where they surfaced:

      - ``GATE_REFUSED``      — deterministic gate: insufficient data, zero LLM.
      - ``NARRATION_FAILED``  — the LLM transport itself failed.
      - ``PARSE_FAILED``      — the LLM returned unparseable output.
      - ``VALIDATION_FAILED`` — the final could not be repaired to passing.
      - ``BUDGET_EXHAUSTED``  — turns/rounds spent before the loop resolved.
      - ``INFRA_FAILED``      — store / db / dispatch infrastructure error.
    """

    SETTLED = "settled"                # success: artifact persisted
    GATE_REFUSED = "gate_refused"      # insufficient data, zero LLM tokens
    NARRATION_FAILED = "narration_failed"  # LLM call/transport failed
    PARSE_FAILED = "parse_failed"      # LLM output was unparseable
    VALIDATION_FAILED = "validation_failed"  # repaired-out, never passed
    BUDGET_EXHAUSTED = "budget_exhausted"    # turns/rounds spent
    INFRA_FAILED = "infra_failed"      # store/db/dispatch exception


SUCCESS_TERMINALS: frozenset[LoopTerminal] = frozenset({LoopTerminal.SETTLED})
FAILURE_TERMINALS: frozenset[LoopTerminal] = frozenset(
    t for t in LoopTerminal if t not in SUCCESS_TERMINALS
)


# ---------------------------------------------------------------------------
# Sub-loop specification — the in-depth workflow shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubLoopSpec:
    """An in-depth workflow nested loop inside a ``NestedLoop``.

    ``steps``        — the ordered fine states inside the sub-loop.
    ``iterates``     — whether the sub-loop repeats until ``exit_condition``.
    ``purpose``      — what the sub-loop is for (one line).
    ``exit_condition`` — the condition that ends an iterating sub-loop.
    """

    steps: tuple[LoopStep, ...]
    iterates: bool
    purpose: str
    exit_condition: str = ""


# ---------------------------------------------------------------------------
# The observation the membrane will read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopObservation:
    """The membrane-visible position: nested loop, task intent, sub-loop.

    This is the granularity Layer 2 observes. The fine ``LoopStep``
    sequence stays inside the loop; the membrane watches loop + sub-loop
    boundaries and task context so it can authorize entry/exit and task
    advance without enumerating internal steps (the state explosion the
    split exists to avoid).

    ``sub_loop`` is ``None`` only while the cycle is between sub-loops
    (e.g. the moment a loop is entered but no sub-loop has opened).
    """

    nested_loop: NestedLoop
    task: TaskIntent
    sub_loop: SubLoop | None = None


# ---------------------------------------------------------------------------
# Mappings — the single source of truth for the workflow shape
# ---------------------------------------------------------------------------


STAGE_ORDER: tuple[AgenticStage, ...] = (
    AgenticStage.WAKE,
    AgenticStage.GATHER,
    AgenticStage.REASON,
    AgenticStage.CHECK,
    AgenticStage.FINALIZE,
)

# Each stage hosts exactly one nested loop.
STAGE_LOOP: dict[AgenticStage, NestedLoop] = {
    AgenticStage.WAKE: NestedLoop.COMPREHENSION,
    AgenticStage.GATHER: NestedLoop.EVIDENCE,
    AgenticStage.REASON: NestedLoop.REASONING,
    AgenticStage.CHECK: NestedLoop.VALIDATION,
    AgenticStage.FINALIZE: NestedLoop.OUTPUT,
}

# The reverse: the stage a loop belongs to.
LOOP_STAGE: dict[NestedLoop, AgenticStage] = {
    loop: stage for stage, loop in STAGE_LOOP.items()
}

# Each nested loop is an in-depth workflow of sub-loops, in order.
LOOP_SUBLOOPS: dict[NestedLoop, tuple[SubLoop, ...]] = {
    NestedLoop.COMPREHENSION: (
        SubLoop.INTAKE,
        SubLoop.INTERPRETATION,
        SubLoop.FRAMING,
    ),
    NestedLoop.EVIDENCE: (
        SubLoop.SOURCING,
        SubLoop.ACQUISITION,
        SubLoop.VERIFICATION,
    ),
    NestedLoop.REASONING: (
        SubLoop.HYPOTHESIS,
        SubLoop.ANALYSIS,
        SubLoop.SYNTHESIS,
    ),
    NestedLoop.VALIDATION: (
        SubLoop.GATE,
        SubLoop.RECOVERY,
    ),
    NestedLoop.OUTPUT: (
        SubLoop.COMPOSITION,
        SubLoop.ARCHITECTURE,
        SubLoop.PLACEMENT,
        SubLoop.MEMORY,
    ),
}

# The in-depth workflow shape of every sub-loop.
SUBLOOP_SPECS: dict[SubLoop, SubLoopSpec] = {
    # --- COMPREHENSION ---
    SubLoop.INTAKE: SubLoopSpec(
        steps=(LoopStep.RECEIVE, LoopStep.DECODE, LoopStep.NORMALIZE),
        iterates=False,
        purpose="Receive and normalize the raw input into a known shape.",
    ),
    SubLoop.INTERPRETATION: SubLoopSpec(
        steps=(
            LoopStep.CLASSIFY_INTENT,
            LoopStep.EXTRACT_CONSTRAINTS,
            LoopStep.IDENTIFY_QUESTIONS,
        ),
        iterates=True,
        purpose="Determine what is being asked and under what constraints.",
        exit_condition="the intent and its questions are unambiguous",
    ),
    SubLoop.FRAMING: SubLoopSpec(
        steps=(
            LoopStep.DECOMPOSE,
            LoopStep.SELECT_EVIDENCE_NEEDS,
            LoopStep.SET_ACCEPTANCE,
        ),
        iterates=False,
        purpose="Decompose the task and set the acceptance bar.",
    ),
    # --- EVIDENCE ---
    SubLoop.SOURCING: SubLoopSpec(
        steps=(LoopStep.PLAN_READS, LoopStep.SELECT_TOOLS),
        iterates=False,
        purpose="Plan which reads and tools will satisfy the intent.",
    ),
    SubLoop.ACQUISITION: SubLoopSpec(
        steps=(LoopStep.READ, LoopStep.CALCULATE, LoopStep.OBSERVE),
        iterates=True,
        purpose="Execute the reads/calculations and capture their results.",
        exit_condition="every planned read/calculation has been observed",
    ),
    SubLoop.VERIFICATION: SubLoopSpec(
        steps=(LoopStep.ASSESS, LoopStep.REPLAN),
        iterates=True,
        purpose="Assess coverage against the plan and re-plan gaps.",
        exit_condition="evidence coverage satisfies the intent or budget spent",
    ),
    # --- REASONING ---
    SubLoop.HYPOTHESIS: SubLoopSpec(
        steps=(LoopStep.FRAME, LoopStep.STATE_PRIOR),
        iterates=False,
        purpose="Frame the hypothesis (H0/H1) and state the prior.",
    ),
    SubLoop.ANALYSIS: SubLoopSpec(
        steps=(LoopStep.TEST, LoopStep.COMPARE),
        iterates=True,
        purpose="Test the hypothesis against the evidence and compare sources.",
        exit_condition=("the hypothesis is settled or refinement stops adding "
                        "signal, and the statistical chain is complete (every "
                        "required link evaluated or its refusal recorded as a "
                        "finding)"),
    ),
    SubLoop.SYNTHESIS: SubLoopSpec(
        steps=(LoopStep.INTERPRET, LoopStep.RECONCILE),
        iterates=False,
        purpose="Interpret the findings and reconcile contradictions.",
    ),
    # --- VALIDATION ---
    SubLoop.GATE: SubLoopSpec(
        steps=(LoopStep.VALIDATE, LoopStep.CLASSIFY_FINDINGS),
        iterates=False,
        purpose="Run the final gate and classify pass/missing findings.",
    ),
    SubLoop.RECOVERY: SubLoopSpec(
        steps=(LoopStep.DIAGNOSE, LoopStep.STEER, LoopStep.REVALIDATE),
        iterates=True,
        purpose="Diagnose a rejected final, steer a repair, revalidate.",
        exit_condition="the final passes or the repair budget is spent",
    ),
    # --- OUTPUT ---
    SubLoop.COMPOSITION: SubLoopSpec(
        steps=(LoopStep.ASSEMBLE, LoopStep.GROUND),
        iterates=False,
        purpose="Assemble the output and ground every claim in evidence.",
    ),
    SubLoop.ARCHITECTURE: SubLoopSpec(
        steps=(LoopStep.UNDERSTAND_SURFACE, LoopStep.LOCATE_OUTPUT),
        iterates=False,
        purpose=(
            "Understand the agentic surface plane architecture and locate "
            "where this output belongs inside it."
        ),
    ),
    SubLoop.PLACEMENT: SubLoopSpec(
        steps=(LoopStep.PERSIST, LoopStep.CONFIRM),
        iterates=False,
        purpose="Place the output into the surface and confirm it landed.",
    ),
    SubLoop.MEMORY: SubLoopSpec(
        steps=(LoopStep.PROPOSE, LoopStep.RESOLVE, LoopStep.SETTLE),
        iterates=True,
        purpose="Dispose memory proposals and close the cycle.",
        exit_condition="every proposal is resolved and the cycle is settled",
    ),
}

# The steady-track statistical chain bound to the loop vocabulary.
# Each statistical link executes in exactly one (sub-loop, step): the agent
# walks Run A → Run B → Run Multivariate inside forecast's TEST execution,
# COMPAREs in ANALYSIS/COMPARE, validates in GATE, and composes the common
# output in COMPOSITION. Hypothesis framing happens in HYPOTHESIS/FRAME (see
# INTENT_SUBLOOP[DERIVE_HYPOTHESIS]); execution stays in ANALYSIS/TEST.
# Keys are tool names (plain strings — this layer owns vocabulary, not
# imports); the authoritative completion check lives in engine/core/chain.py.
CHAIN_SUBLOOP: dict[str, tuple[SubLoop, LoopStep]] = {
    "calc.forward.forecast": (SubLoop.ANALYSIS, LoopStep.TEST),
    "calc.forward.scenario": (SubLoop.ANALYSIS, LoopStep.TEST),
    "calc.hypothesis.test": (SubLoop.ANALYSIS, LoopStep.TEST),
    "calc.decay.report": (SubLoop.ANALYSIS, LoopStep.COMPARE),
    "calc.discipline.audit": (SubLoop.GATE, LoopStep.VALIDATE),
    "output.compose": (SubLoop.COMPOSITION, LoopStep.ASSEMBLE),
}


# Derived: the ordered steps of each sub-loop (flat accessor).
SUBLOOP_STEPS: dict[SubLoop, tuple[LoopStep, ...]] = {
    sub_loop: spec.steps for sub_loop, spec in SUBLOOP_SPECS.items()
}

# Derived: the FLATTENED step sequence of each nested loop, in sub-loop
# order. The canonical shape is LOOP_SUBLOOPS + SUBLOOP_SPECS; this is the
# convenience projection for callers that only need "the steps".
LOOP_STEPS: dict[NestedLoop, tuple[LoopStep, ...]] = {
    loop: tuple(
        step for sub_loop in sub_loops for step in SUBLOOP_SPECS[sub_loop].steps
    )
    for loop, sub_loops in LOOP_SUBLOOPS.items()
}

# The agentic core of each nested loop — the iterating sub-loop the agent
# actually runs as its reason/act/observe cycle. For OUTPUT the core is
# PLACEMENT: the final act is placing the output.
PRIMARY_SUBLOOP: dict[NestedLoop, SubLoop] = {
    NestedLoop.COMPREHENSION: SubLoop.INTERPRETATION,
    NestedLoop.EVIDENCE: SubLoop.ACQUISITION,
    NestedLoop.REASONING: SubLoop.ANALYSIS,
    NestedLoop.VALIDATION: SubLoop.RECOVERY,
    NestedLoop.OUTPUT: SubLoop.PLACEMENT,
}

# The nested loop each task intent is served by.
INTENT_LOOP: dict[TaskIntent, NestedLoop] = {
    TaskIntent.UNDERSTAND_TASK: NestedLoop.COMPREHENSION,
    TaskIntent.INFER_ORDER_FLOW: NestedLoop.EVIDENCE,
    TaskIntent.INFER_DEPTH: NestedLoop.EVIDENCE,
    TaskIntent.CORRELATE_EVIDENCE: NestedLoop.REASONING,
    TaskIntent.EXPLAIN_FINDINGS: NestedLoop.REASONING,
    TaskIntent.DERIVE_HYPOTHESIS: NestedLoop.REASONING,
    TaskIntent.SYNTHESIZE_OUTPUT: NestedLoop.REASONING,
    TaskIntent.VALIDATE_FINAL: NestedLoop.VALIDATION,
    TaskIntent.UNDERSTAND_SURFACE: NestedLoop.OUTPUT,
    TaskIntent.PLACE_OUTPUT: NestedLoop.OUTPUT,
}

# The sub-loop that serves each intent (in-depth binding for the membrane).
INTENT_SUBLOOP: dict[TaskIntent, SubLoop] = {
    TaskIntent.UNDERSTAND_TASK: SubLoop.INTERPRETATION,
    TaskIntent.INFER_ORDER_FLOW: SubLoop.ACQUISITION,
    TaskIntent.INFER_DEPTH: SubLoop.ACQUISITION,
    TaskIntent.CORRELATE_EVIDENCE: SubLoop.SYNTHESIS,
    TaskIntent.EXPLAIN_FINDINGS: SubLoop.SYNTHESIS,
    TaskIntent.DERIVE_HYPOTHESIS: SubLoop.ANALYSIS,
    TaskIntent.SYNTHESIZE_OUTPUT: SubLoop.SYNTHESIS,
    TaskIntent.VALIDATE_FINAL: SubLoop.GATE,
    TaskIntent.UNDERSTAND_SURFACE: SubLoop.ARCHITECTURE,
    TaskIntent.PLACE_OUTPUT: SubLoop.PLACEMENT,
}

# The intents served by each loop (reverse of INTENT_LOOP).
LOOP_INTENTS: dict[NestedLoop, tuple[TaskIntent, ...]] = {
    loop: tuple(intent for intent, l in INTENT_LOOP.items() if l is loop)
    for loop in NestedLoop
}


# ---------------------------------------------------------------------------
# Queries (pure reads)
# ---------------------------------------------------------------------------


def loop_for_stage(stage: AgenticStage) -> NestedLoop:
    """The nested loop hosted by a stage."""
    return STAGE_LOOP[stage]


def stage_for_loop(loop: NestedLoop) -> AgenticStage:
    """The stage that hosts a nested loop."""
    return LOOP_STAGE[loop]


def sub_loops_for(loop: NestedLoop) -> tuple[SubLoop, ...]:
    """The ordered sub-loops (in-depth workflow) of a nested loop."""
    return LOOP_SUBLOOPS[loop]


def steps_for_sub_loop(sub_loop: SubLoop) -> tuple[LoopStep, ...]:
    """The ordered steps inside a sub-loop."""
    return SUBLOOP_STEPS[sub_loop]


def spec_for(sub_loop: SubLoop) -> SubLoopSpec:
    """The in-depth spec (steps/purpose/iteration) of a sub-loop."""
    return SUBLOOP_SPECS[sub_loop]


def steps_for(loop: NestedLoop) -> tuple[LoopStep, ...]:
    """The flattened ordered steps of a nested loop (across its sub-loops)."""
    return LOOP_STEPS[loop]


def intents_for(loop: NestedLoop) -> tuple[TaskIntent, ...]:
    """The task intents served by a nested loop."""
    return LOOP_INTENTS[loop]


def loop_for_intent(intent: TaskIntent) -> NestedLoop:
    """The nested loop that serves a task intent."""
    return INTENT_LOOP[intent]


def sub_loop_for_intent(intent: TaskIntent) -> SubLoop:
    """The sub-loop that serves a task intent."""
    return INTENT_SUBLOOP[intent]


def primary_sub_loop(loop: NestedLoop) -> SubLoop:
    """The iterating agentic core sub-loop of a nested loop."""
    return PRIMARY_SUBLOOP[loop]


def iterating_sub_loops(loop: NestedLoop) -> tuple[SubLoop, ...]:
    """The sub-loops of a nested loop that iterate to an exit condition."""
    return tuple(
        sl for sl in LOOP_SUBLOOPS[loop] if SUBLOOP_SPECS[sl].iterates
    )


def is_terminal(terminal: LoopTerminal) -> bool:
    """Every ``LoopTerminal`` is an endpoint; kept for call-site clarity."""
    return isinstance(terminal, LoopTerminal)


def is_success(terminal: LoopTerminal) -> bool:
    """True only for ``SETTLED``."""
    return terminal in SUCCESS_TERMINALS


# ---------------------------------------------------------------------------
# Integrity — fail fast if the workflow shape drifts
# ---------------------------------------------------------------------------


def self_check() -> None:
    """Enforce the Layer-1 invariants. Raises when the workflow is broken.

    Invariants:
      - ``STAGE_ORDER`` lists every stage exactly once.
      - ``STAGE_LOOP`` covers every stage with a valid loop, injectively.
      - ``LOOP_SUBLOOPS`` covers every loop; each loop has >= 2 sub-loops
        and no duplicate sub-loop within the loop.
      - ``SUBLOOP_SPECS`` covers exactly the sub-loops used by loops; each
        spec has >= 2 steps with no duplicates within the spec.
      - every ``LoopStep`` appears in at least one sub-loop (reachable).
      - ``PRIMARY_SUBLOOP`` maps every loop to one of its own sub-loops.
      - ``INTENT_LOOP`` covers every intent with a valid loop.
      - ``INTENT_SUBLOOP`` covers every intent and its sub-loop belongs to
        the intent's loop.
      - every loop serves at least one intent.
      - success and failure terminals partition ``LoopTerminal``.
    """
    if set(STAGE_ORDER) != set(AgenticStage) or len(STAGE_ORDER) != len(
        AgenticStage
    ):
        raise ValueError("STAGE_ORDER must list every stage exactly once")

    if set(STAGE_LOOP) != set(AgenticStage):
        raise ValueError("STAGE_LOOP must cover every stage")
    if len(set(STAGE_LOOP.values())) != len(STAGE_LOOP):
        raise ValueError("STAGE_LOOP must be injective (one loop per stage)")

    if set(LOOP_SUBLOOPS) != set(NestedLoop):
        raise ValueError("LOOP_SUBLOOPS must cover every nested loop")
    for loop, sub_loops in LOOP_SUBLOOPS.items():
        if len(sub_loops) < 2:
            raise ValueError(f"loop {loop.value!r} must have >= 2 sub-loops")
        if len(set(sub_loops)) != len(sub_loops):
            raise ValueError(f"loop {loop.value!r} has duplicate sub-loops")

    used_sub_loops = {sl for subs in LOOP_SUBLOOPS.values() for sl in subs}
    if set(SUBLOOP_SPECS) != used_sub_loops:
        raise ValueError(
            "SUBLOOP_SPECS keys must match the sub-loops used by LOOP_SUBLOOPS"
        )
    for sub_loop, spec in SUBLOOP_SPECS.items():
        if len(spec.steps) < 2:
            raise ValueError(f"sub-loop {sub_loop.value!r} must have >= 2 steps")
        if len(set(spec.steps)) != len(spec.steps):
            raise ValueError(f"sub-loop {sub_loop.value!r} has duplicate steps")

    reachable_steps = {
        step for spec in SUBLOOP_SPECS.values() for step in spec.steps
    }
    if reachable_steps != set(LoopStep):
        missing = sorted(s.value for s in set(LoopStep) - reachable_steps)
        raise ValueError(f"unreachable loop steps: {missing}")

    if set(PRIMARY_SUBLOOP) != set(NestedLoop):
        raise ValueError("PRIMARY_SUBLOOP must cover every nested loop")
    for loop, sub_loop in PRIMARY_SUBLOOP.items():
        if sub_loop not in LOOP_SUBLOOPS[loop]:
            raise ValueError(
                f"PRIMARY_SUBLOOP[{loop.value!r}]={sub_loop.value!r} is not "
                f"a sub-loop of that loop"
            )

    if set(INTENT_LOOP) != set(TaskIntent):
        raise ValueError("INTENT_LOOP must cover every task intent")
    if any(loop not in NestedLoop for loop in INTENT_LOOP.values()):
        raise ValueError("INTENT_LOOP values must be valid nested loops")

    if set(INTENT_SUBLOOP) != set(TaskIntent):
        raise ValueError("INTENT_SUBLOOP must cover every task intent")
    for intent, sub_loop in INTENT_SUBLOOP.items():
        if sub_loop not in LOOP_SUBLOOPS[INTENT_LOOP[intent]]:
            raise ValueError(
                f"INTENT_SUBLOOP[{intent.value!r}]={sub_loop.value!r} is not "
                f"a sub-loop of {INTENT_LOOP[intent].value!r}"
            )

    if any(not intents_for(loop) for loop in NestedLoop):
        raise ValueError("every nested loop must serve at least one intent")

    if SUCCESS_TERMINALS | FAILURE_TERMINALS != frozenset(LoopTerminal):
        raise ValueError("terminals must partition LoopTerminal")
    if SUCCESS_TERMINALS & FAILURE_TERMINALS:
        raise ValueError("success and failure terminals must be disjoint")

    for tool, (sub_loop, step) in CHAIN_SUBLOOP.items():
        if sub_loop not in SUBLOOP_SPECS:
            raise ValueError(
                f"CHAIN_SUBLOOP[{tool!r}] sub-loop {sub_loop.value!r} has no spec"
            )
        if step not in SUBLOOP_SPECS[sub_loop].steps:
            raise ValueError(
                f"CHAIN_SUBLOOP[{tool!r}] step {step.value!r} is not a step "
                f"of sub-loop {sub_loop.value!r}"
            )


self_check()


__all__ = [
    # enums
    "AgenticStage",
    "NestedLoop",
    "SubLoop",
    "LoopStep",
    "TaskIntent",
    "LoopTerminal",
    # specs
    "SubLoopSpec",
    # observation
    "LoopObservation",
    # terminal sets
    "SUCCESS_TERMINALS",
    "FAILURE_TERMINALS",
    # mappings
    "STAGE_ORDER",
    "CHAIN_SUBLOOP",
    "STAGE_LOOP",
    "LOOP_STAGE",
    "LOOP_SUBLOOPS",
    "SUBLOOP_SPECS",
    "SUBLOOP_STEPS",
    "LOOP_STEPS",
    "PRIMARY_SUBLOOP",
    "INTENT_LOOP",
    "INTENT_SUBLOOP",
    "LOOP_INTENTS",
    # queries
    "loop_for_stage",
    "stage_for_loop",
    "sub_loops_for",
    "steps_for_sub_loop",
    "spec_for",
    "steps_for",
    "intents_for",
    "loop_for_intent",
    "sub_loop_for_intent",
    "primary_sub_loop",
    "iterating_sub_loops",
    "is_terminal",
    "is_success",
    # integrity
    "self_check",
]