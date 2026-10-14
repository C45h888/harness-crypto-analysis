"""Loop states — the in-depth hierarchical workflow the agentic loop follows.

LAYER 1 of the two-layer agentic surface. This module defines the LOOP
VOCABULARY — the hierarchical workflow the agent walks. It does NOT define
transition legality and it does NOT own semantics (classification / credit /
exit). Those belong to the governance membrane (Layer 2, deferred) and the
``CycleController``, respectively.

The hierarchy is six levels deep, so each loop state carries an IN-DEPTH
workflow nested loop:

  AgenticStage          the workflow position (CONTEXT .. FINALIZE)
    └── NestedLoop      the mode of work (CONTEXT .. OUTPUT)
          └── SubLoop   an in-depth workflow stage inside the loop
                └── LoopStep   a fine state inside the sub-loop

plus intent-based tasks (``TaskIntent``) naming what the agent establishes
and ``LoopTerminal`` endpoints where a cycle stops.

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

Positions vs sub-loops (REASONING): the statistical hard track
(``chain.py`` ``POSITION_ORDER``: assemble → interpret → hypothesize)
executes INSIDE the ANALYSIS sub-loop — HYPOTHESIS is the entry frame and
SYNTHESIS the freeze + close. Steers speak position language; governance
speaks sub-loop language; this module is where the two meet.

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
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Level 1 — the workflow stages (top-level traversal)
# ---------------------------------------------------------------------------


class AgenticStage(str, Enum):
    """The agentic loop's top-level workflow stages, in traversal order.

        CONTEXT -> REASON -> CHECK -> FINALIZE

    ``CONTEXT`` is the merged wake+gather stage: understand the ask, plan,
    acquire the plan's data, and verify coverage before reasoning. Each stage
    HOSTS exactly one ``NestedLoop`` (see ``STAGE_LOOP``).
    """

    CONTEXT = "context"    # wake+gather merged: understand, plan, acquire, verify
    REASON = "reason"      # reason prompt data against tool data
    CHECK = "check"        # validate the final; recover when rejected
    FINALIZE = "finalize"  # place the output; dispose memory


# ---------------------------------------------------------------------------
# Level 2 — the nested loops (one per stage)
# ---------------------------------------------------------------------------


class NestedLoop(str, Enum):
    """The in-depth workflow loop each stage hosts.

    Named for the MODE of work; the subject is the ``TaskIntent``. The first
    loop is ``CONTEXT`` — the merged comprehension+evidence loop: intake,
    interpretation, framing, sourcing, acquisition, verification. The final
    loop is ``OUTPUT`` — placing the output requires understanding the
    agentic surface it is placed into.
    """

    CONTEXT = "context"        # merged: understand, plan, source, acquire, verify
    REASONING = "reasoning"    # REASON: form + test + synthesize
    VALIDATION = "validation"  # CHECK: gate the final; recover
    OUTPUT = "output"          # FINALIZE: compose, understand, place


# ---------------------------------------------------------------------------
# Level 3 — the sub-loops (the in-depth workflow inside each loop)
# ---------------------------------------------------------------------------


class SubLoop(str, Enum):
    """In-depth workflow stages inside a ``NestedLoop``.

    Each nested loop is a sequence of 2-6 sub-loops; each sub-loop is an
    ordered ``LoopStep`` sequence and may iterate to its exit condition.
    This is the primary agentic loop the agent follows.
    """

    # --- CONTEXT (merged comprehension + evidence) ---
    INTAKE = "intake"                     # receive + normalize the input
    INTERPRETATION = "interpretation"     # classify intent, extract constraints
    FRAMING = "framing"                   # decompose task, set acceptance
    SOURCING = "sourcing"                 # plan reads, select tools
    ACQUISITION = "acquisition"           # read + calculate + observe
    VERIFICATION = "verification"         # assess coverage, store handoff

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
    GATE_DATA = "gate_data"              # zero-LLM data-sufficiency gate (terminal)

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
    GATE_PLAN = "gate_plan"              # deterministic plan-adequacy gate (terminal)

    # --- ACQUISITION ---
    READ = "read"                        # dispatch read tools
    CALCULATE = "calculate"              # dispatch deterministic calc tools
    OBSERVE = "observe"                  # capture results into the ledger

    # --- VERIFICATION ---
    ASSESS = "assess"                    # coverage vs plan
    STORE_HANDOFF = "store_handoff"      # write context package to memory segment

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
# Loop receipts — the CANONICAL, engine-authored record of a loop close
# ---------------------------------------------------------------------------
#
# A receipt is an engine-authored, frozen record of ONE loop/sub-loop close:
# *what ran, what it covered, how it closed, with what provenance*. Receipts are
# canonical and carry no model prose; the agent's own memory node is a separate
# surface the agent calls and writes.
#
# Emission mechanics live in ``engine/receipts.py``; this module owns only the
# VOCABULARY (matching the loop_states/fsm authority split: vocabulary here,
# legality in fsm, semantics in controller).


class ReceiptDisposition(str, Enum):
    """How a loop/sub-loop actually closed — the honest disposition set.

    ``NOT_APPLICABLE`` and ``SKIPPED`` exist so an inapplicable or unentered
    step is disposed *explicitly* rather than fabricated (NESTED_LOOP_SPEC
    §8.4: no invented work, no no-op event parade).
    """

    COMPLETED = "completed"          # the sub-loop's work ran and satisfied its exit
    NOT_APPLICABLE = "not_applicable"  # the sub-loop does not apply to this cycle
    REFUSED = "refused"              # a deterministic verdict refused the work
    DENIED = "denied"                # the membrane denied the transition
    SKIPPED = "skipped"              # not entered (budget/early exit) — recorded, not claimed


@dataclass(frozen=True)
class LoopReceipt:
    """One engine-authored, JSON-serializable record of a loop/sub-loop close.

    It answers *what happened* (structural facts + provenance). Receipts are
    frozen so a cycle's traversal is replayable and auditable from the
    artifact.

    ``payload``       — bounded, JSON-safe STRUCTURAL facts only (never narration).
    ``evidence_refs`` — provenance ancestors (wake_id / artifact_id / tool paths).
    """

    loop: NestedLoop
    sub_loop: SubLoop
    disposition: ReceiptDisposition = ReceiptDisposition.COMPLETED
    steps: tuple[LoopStep, ...] = ()
    payload: Mapping[str, Any] | None = None
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (stable key order for fixtures/artifacts)."""
        return {
            "loop": self.loop.value,
            "sub_loop": self.sub_loop.value,
            "disposition": self.disposition.value,
            "steps": [step.value for step in self.steps],
            "payload": dict(self.payload) if self.payload else {},
            "evidence_refs": list(self.evidence_refs),
        }


# ---------------------------------------------------------------------------
# Mappings — the single source of truth for the workflow shape
# ---------------------------------------------------------------------------


STAGE_ORDER: tuple[AgenticStage, ...] = (
    AgenticStage.CONTEXT,
    AgenticStage.REASON,
    AgenticStage.CHECK,
    AgenticStage.FINALIZE,
)

# Each stage hosts exactly one nested loop.
STAGE_LOOP: dict[AgenticStage, NestedLoop] = {
    AgenticStage.CONTEXT: NestedLoop.CONTEXT,
    AgenticStage.REASON: NestedLoop.REASONING,
    AgenticStage.CHECK: NestedLoop.VALIDATION,
    AgenticStage.FINALIZE: NestedLoop.OUTPUT,
}

# The reverse: the stage a loop belongs to.
LOOP_STAGE: dict[NestedLoop, AgenticStage] = {
    loop: stage for stage, loop in STAGE_LOOP.items()
}

# ---------------------------------------------------------------------------
# Phase S-1 (docs/STATE_CHARTER_SPEC.md) — the runtime loop-tag vocabulary
# and its ONE crosswalk to the FSM loops.
#
# The runtime's ``loop_tag`` is a plain string carried on
# ``CycleRuntimeState`` (context.py); LoopTag declares its LEGAL members,
# and LOOP_TAG_CROSSWALK is the ONLY place a loop tag resolves to an FSM
# (NestedLoop, SubLoop) observation. driver.py consumes this crosswalk — the
# per-map silent defaults that used to authorize unknown tags as REASONING
# are gone: an unknown tag is a governance error, never an invented
# authorization. The charter test pins that every producer literal, budget
# key, and passes_per_loop key resolves to this namespace.
# ---------------------------------------------------------------------------
class LoopTag:
    """Frozen runtime loop-tag vocabulary. Members are plain strings (the
    runtime's loop_tag grain); every producer (reasoning.py) and consumer
    (config budgets, context.passes_per_loop, driver crosswalk) resolves
    to this namespace."""

    COMPREHENSION = "comprehension"
    EVIDENCE = "evidence"
    REASONING = "reasoning"
    VALIDATION = "validation"
    OUTPUT = "output"


LOOP_TAG_CROSSWALK: dict[str, tuple[NestedLoop, "SubLoop | None"]] = {
    # Comprehension is INTERPRETATION/FRAMING work inside CONTEXT — its
    # declarative home is FRAMING (the receipt close), never ACQUISITION.
    # Phase R7 removed the fetch tag with the fetch semantic.
    LoopTag.COMPREHENSION: (NestedLoop.CONTEXT, SubLoop.FRAMING),
    # Live tags (assigned by reasoning.py) — byte-identical to the previous
    # driver map. VALIDATION tag homes to RECOVERY (the iterating core,
    # matching PRIMARY_SUBLOOP + the CONTEXT→ACQUISITION / REASONING→ANALYSIS
    # / OUTPUT→PLACEMENT pattern). GATE is first-opened but resolved via
    # the dispatch registry homes — the tag names the core, not the first
    # sub-loop. Repair dispatches authorize via this fallback; first-pass
    # GATE authorizes via registry homes.
    LoopTag.EVIDENCE: (NestedLoop.CONTEXT, SubLoop.ACQUISITION),
    LoopTag.REASONING: (NestedLoop.REASONING, SubLoop.ANALYSIS),
    LoopTag.VALIDATION: (NestedLoop.VALIDATION, SubLoop.RECOVERY),
    LoopTag.OUTPUT: (NestedLoop.OUTPUT, SubLoop.PLACEMENT),
}

# The dispatch registry's LEGACY home names (comprehension/evidence) resolve
# to the merged CONTEXT loop — declared here so the reconciliation is data
# in the charter owner, not an inline driver map.
REGISTRY_HOME_LOOPS: dict[str, NestedLoop] = {
    "comprehension": NestedLoop.CONTEXT,
    "evidence": NestedLoop.CONTEXT,
}

# Each nested loop is an in-depth workflow of sub-loops, in order.
LOOP_SUBLOOPS: dict[NestedLoop, tuple[SubLoop, ...]] = {
    NestedLoop.CONTEXT: (
        SubLoop.INTAKE,
        SubLoop.INTERPRETATION,
        SubLoop.FRAMING,
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
    # --- CONTEXT (merged comprehension + evidence) ---
    SubLoop.INTAKE: SubLoopSpec(
        steps=(LoopStep.RECEIVE, LoopStep.DECODE, LoopStep.NORMALIZE,
               LoopStep.GATE_DATA),
        iterates=False,
        purpose=(
            "Receive and normalize the raw input into a known shape. "
            "Executed by run_wake (deterministic INTAKE, no tools). "
            "Phase R7: fetch semantic removed — comprehension is the "
            "first agentic step; GATE_DATA is retired (see gates.py)."
        ),
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
            LoopStep.GATE_PLAN,
        ),
        iterates=False,
        purpose=(
            "Decompose the task, set the acceptance bar, and gate the "
            "disposed plan (GATE_PLAN executes post-framing in "
            "run_comprehension, forward-only — a refused plan ends the cycle)."
        ),
    ),
    # --- CONTEXT (merged comprehension + evidence), continued ---
    SubLoop.SOURCING: SubLoopSpec(
        steps=(LoopStep.PLAN_READS, LoopStep.SELECT_TOOLS),
        iterates=False,
        purpose=(
            "Plan which reads and tools will satisfy the intent. "
            "The planned acquisition itself runs in "
            "run_plan_bound_acquisition under the frozen receipt."
        ),
    ),
    SubLoop.ACQUISITION: SubLoopSpec(
        steps=(LoopStep.READ, LoopStep.CALCULATE, LoopStep.OBSERVE),
        iterates=True,
        purpose=(
            "Execute the reads/calculations and capture their results. "
            "Runs in run_evidence (narrate#1 + budgeted follow-ups)."
        ),
        exit_condition="tool_calls empty (candidate stands) or budget spent",
    ),
    SubLoop.VERIFICATION: SubLoopSpec(
        steps=(LoopStep.ASSESS, LoopStep.STORE_HANDOFF),
        iterates=False,
        purpose=(
            "Assess coverage against the plan and store the handoff package. "
            "Gaps ride forward as findings — there is no re-plan edge "
            "(gate 2 is forward-only). Single close at end of CONTEXT."
        ),
    ),
    # --- REASONING (canonical staged doctrine: 2a → 2b → hypothesize) ---
    # HYPOTHESIS frames the prior from the partition + directive seed — no
    # tools. ANALYSIS is the staged execution core (2a base-interpret =
    # CALCULATE+OBSERVE exiting via join.n_pairs; 2b refined-archaic = TEST
    # against fit/distribution/forecast + COMPARE across decay/events).
    # Position vocabulary (assemble/interpret/hypothesize) maps to LoopSteps
    # CALCULATE/OBSERVE → TEST → COMPARE; the membrane watches sub-loops,
    # positions never move it. SYNTHESIS reconciles (INTERPRET+RECONCILE).
    # Old debt removed: H0 is framed as prior here AND formed as testable
    # H0 in hypothesize — the spec no longer claims formation lives only
    # in ANALYSIS without a framing step.
    SubLoop.HYPOTHESIS: SubLoopSpec(
        steps=(LoopStep.FRAME, LoopStep.STATE_PRIOR),
        iterates=False,
        purpose=(
            "Frame the prior from the task-segment partition "
            "(understanding+plan+handoff) + the directive H0/H1 seed. "
            "No tools. The testable H0 is formed later at the hypothesize "
            "position inside ANALYSIS — this frame is the prior it tests against."
        ),
    ),
    SubLoop.ANALYSIS: SubLoopSpec(
        steps=(LoopStep.TEST, LoopStep.COMPARE),
        iterates=True,
        purpose=(
            "Staged execution core: 2a BASE-INTERPRET (calculate+observe: "
            "ofi/ad/join, exits via join.n_pairs>0 with base_summary frozen "
            "+ base observation stored) → 2b REFINED-ARCHAIC (test: "
            "forecast primary truth + fit/distribution/delta, gated on DATA; "
            "compare: decay/events/scenario) → hypothesize (H0/H1 in line "
            "with the distribution, proving/disproving the prompt). "
            "One position per pass, out-of-position + premature denied."
        ),
        exit_condition="the hypothesis is settled or refinement stops adding signal",
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
        iterates=False,
        purpose="Dispose memory proposals and close the cycle (single pass).",
    ),
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
    NestedLoop.CONTEXT: SubLoop.ACQUISITION,
    NestedLoop.REASONING: SubLoop.ANALYSIS,
    NestedLoop.VALIDATION: SubLoop.RECOVERY,
    NestedLoop.OUTPUT: SubLoop.PLACEMENT,
}

# The nested loop each task intent is served by.
INTENT_LOOP: dict[TaskIntent, NestedLoop] = {
    TaskIntent.UNDERSTAND_TASK: NestedLoop.CONTEXT,
    TaskIntent.INFER_ORDER_FLOW: NestedLoop.CONTEXT,
    TaskIntent.INFER_DEPTH: NestedLoop.CONTEXT,
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
    TaskIntent.CORRELATE_EVIDENCE: SubLoop.ANALYSIS,
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
    # receipt (canonical half of the receipt/note partition)
    "LoopReceipt",
    "ReceiptDisposition",
    # terminal sets
    "SUCCESS_TERMINALS",
    "FAILURE_TERMINALS",
    # mappings
    "STAGE_ORDER",
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