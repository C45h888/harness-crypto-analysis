"""Knowledge-base loaders + prompt-payload templates.

Semantic authority: WHAT THE MODEL READS. Every KB string and prompt
template lives here — the rest of the engine reads them but does not
compose them. This is the file the prompt-consolidation pass touches.

Three artefacts live here:

  - ``load_kb(path)`` / ``load_paper_kb()`` — bounded doc reads; failure
    is non-fatal (the engine continues without the KB rather than killing
    the cycle).
  - ``SYSTEM_PROMPT_TEMPLATE`` — the role + invariants + KB injection
    points, formatted per cycle.
  - ``build_system_prompt(symbol, venue, max_rounds)`` — formats the
    template with the engine's identity + KB payloads.
  - ``build_output_format()`` — the JSON turn contract + registry tool
    names + staged workflow (~40 lines, re-sent every turn).

The system prompt is ~3 KB; the manifest injection is ~10 KB; the paper
KB is up to ~18 KB. Total payload per turn ≈ 34 KB re-sent on every
follow-up turn. This is the seam the prompt-economy work lives at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import (
    LOOP_PASS_BUDGET,
    MAX_DISPATCHES_PER_PASS,
    _CALC_KB_PATHS,
    _MEMORY_PROTOCOL,
    _PAPER_KB,
    _TOOL_MANIFEST,
)
from .tool_schemas import tool_schema_block, wrap_tool_result
from .loop_states import (
    LOOP_SUBLOOPS,
    STAGE_ORDER,
    SUBLOOP_SPECS,
    loop_for_stage,
)


def load_kb(path: Path) -> str:
    """Bounded KB doc read; empty string on any failure (never raises)."""
    try:
        return path.read_text(encoding="utf-8")[:12_000]
    except OSError:
        return ""


def load_paper_kb() -> str:
    """Bounded paper KB (Cont et al.) for cross-check — failure is non-fatal."""
    try:
        if _PAPER_KB.exists():
            return _PAPER_KB.read_text(encoding="utf-8")[:20_000]
    except OSError:
        pass
    # fallback: behavior docs that encode the paper semantics
    for p in _CALC_KB_PATHS:
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")[:20_000]
        except OSError:
            continue
    return ""


SYSTEM_PROMPT_TEMPLATE = """You are the statistical inference engine for {symbol} ({venue}) — an AGENTIC loop, not a controlled output generator.

You are a loop-walking conditional-inference engine. Your core purpose is to produce statistical evidence — never a trading instruction — by walking five loops in order: understand once, gather thrice, reason twice, submit to one validation, compose once. Closed loops are never revisited; findings (including refusals and nulls) travel forward on the ledger.

You receive ONE deterministic_state object: microstructure fits, coverage,
and gate reasons computed by deterministic Python from the Redis ledgers.
You also receive recalled memory (priors + paper KB) and a deterministic diff vs prior artifact.

YOUR JOB — VALID inference, not predefined addition:
1. VALIDATE the deterministic fits by invoking calculation modules via tools — do not accept the formula output as final.
   Call market.read plus substrate.* worker tools (tape/density/delta/ladders/…)
   to read the always-fresh worker projections and request bounded fire-ticks.
   A substrate.* invoke is a REQUEST to the calculation plane — the process that
   owns the workers — which applies its own cooldown gates and may decline, and
   may be unreachable. A granted call is not a guaranteed fresh compute: always
   confirm what you actually got via substrate.read age_ms.
   Call micro.ofi_intervals / micro.evidence to audit intervals, and cross-check against paper KB and memory.
2. Interpret fitted models — sign, magnitude, r2, stderr, and status of price_impact_fit; depth-scaling (c, lambda) with its own status; what changed vs prior cycle. Be specific, numeric, grounded.
3. Run the agentic loopwalk: comprehension (1 pass, no tools) → evidence (3 passes) → reasoning (2 passes) → validation (1 pass + 1 bounded retry) → output (1 non-agentic pass). Pack each pass densely with the tools it needs. Cite every numeric claim with exact paths.

ABSOLUTE RULES:
1. NEVER recompute any value in your reasoning. If you need data you do not have, COMMAND a tool — results arrive next turn.
2. The two fitted models (beta; c/lambda) are NEVER merged into one prediction. Combined expression carries heteroskedastic nu*OFI term — derived diagnostic at most.
3. Respect deterministic status: 'insufficient' must not be interpreted; 'provisional' must be caveated. You cannot override status.
4. Null means not-provided — never substitute zero.
5. Cite exact deterministic_state or tool-result paths for every numeric claim.
6. Cross-check memory/KB: recalled entries include Cont-Kukanov-Stoikov paper excerpts — reference them when relevant.

MEMORY: recalled entries are provenance-tagged priors, subordinate to fresh ledger data. If you contradict a prior conclusion, say so explicitly.
{memory_protocol_section}
PAPER KB (Cont et al. 1011.6402 excerpts — bounded):
{paper_kb}
{workflow_brief}
TOOL MANIFEST (commandable, deterministic — USE IT):
{tool_manifest}

TOOL OUTPUT SCHEMAS (every tool returns a ToolResult envelope with tool/status/reason/data/null_fields; cite fields from ``data``; ``null_fields`` names what was not computed):
{tool_schemas}
"""


def build_system_prompt(symbol: str, venue: str, max_rounds: int) -> str:
    """Format the system prompt for one cycle — KB reads happen here, once."""
    return SYSTEM_PROMPT_TEMPLATE.format(
        symbol=symbol,
        venue=venue,
        max_rounds=max_rounds,
        memory_protocol_section=(
            "MEMORY PROTOCOL (propose, never write):\n"
            + load_kb(_MEMORY_PROTOCOL)
            if _MEMORY_PROTOCOL.exists() else ""
        ),
        paper_kb=load_paper_kb()[:18_000],
        workflow_brief=build_workflow_brief(),
        tool_manifest=(
            "TOOL MANIFEST:\n" + load_kb(_TOOL_MANIFEST)
            if _TOOL_MANIFEST.exists() else ""
        ),
        tool_schemas=tool_schema_block(),
    )


class SystemPromptCache:
    """Cycle-scoped system prompt: KB disk reads + formatting once, reused.

    ``build_system_prompt`` reads up to three KB files from disk and formats
    a ~34 KB payload. The engine calls it once per LLM turn (up to 12), so a
    cycle re-read the same files and re-paid the same formatting every turn.
    The engine constructs one of these per cycle and passes the rendered
    string to every narration call — the KB payload is cycle-constant anyway
    (symbol, venue, and budgets do not change mid-cycle).
    """

    __slots__ = ("_symbol", "_venue", "_max_rounds", "_rendered")

    def __init__(self, symbol: str, venue: str, max_rounds: int) -> None:
        self._symbol = symbol
        self._venue = venue
        self._max_rounds = max_rounds
        self._rendered: str | None = None

    def get(self) -> str:
        """Rendered system prompt; KB reads + formatting happen on first call."""
        if self._rendered is None:
            self._rendered = build_system_prompt(
                self._symbol, self._venue, self._max_rounds)
        return self._rendered


TURN_CONTRACT_LINE = (
    'Return ONE JSON object per turn with keys {phase, summary, evidence, '
    'confidence, limitations, model_separation, hypothesis, scenario, '
    'tool_calls, memory_proposals} — the tool-name key is EXACTLY "name".'
)


def build_output_format() -> str:
    """The JSON turn contract + registry tool names + staged workflow.

    Re-sent on every follow-up turn so the model always has the
    registry keys + the staged workflow in view. ~40 lines, ~3 KB.
    The one-line compression of this contract for follow-up turns is
    ``TURN_CONTRACT_LINE`` (single source: this module).
    """
    return (
        f"LOOPS, NOT ROUNDS — comprehension (1 pass, no tools) → evidence (3 passes) → reasoning (2 passes) → validation (1 pass + 1 bounded retry) → output (1 non-agentic pass). Phases (P1→P6) name evidence families, loops name when you work. Declare your phase every turn.\n"
        "Return ONLY one JSON object per turn with EXACTLY these keys:\n"
        "{\n"
        '  "phase": "P1|P2|P3|P4|P5|P6 — the phase this turn advances (P6 = final output generation, no tools)",\n'
        '  "summary": "P4 explanation (≥200 chars: what the fits show AND why now), or null while tools are still pending",\n'
        '  "evidence": [{"path": "deterministic_state.… OR <tool-name> → <field>", "value": …, "interpretation": "…", "metric_name": "…"}] or null,\n'
        '  "confidence": "low|medium|high" or null,\n'
        '  "limitations": ["…"] or null,\n'
        '  "model_separation": "one sentence on why beta and c/lambda are read separately" or null,\n'
        '  "hypothesis": {"H0": "…", "H1": "…", "paper_refs": ["Cont 1011.6402 §…"], "evidence_refs": ["calc.ofi.intervals", …]} or null (REQUIRED at final),\n'
        '  "scenario": {"target_price": "…", "horizon": "15m|1h|4h", "direction": "up|down", "required_ofi": "…", "required_ofi_range": […] or null, "exceedance": "…", "exceedance_range": […] or null, "fit_status": "validated|provisional", "n_windows_usable": N, "r2": "…" or null, "probability": "low|medium|high", "verdict": "reachable|not_reachable|unevaluable", "rationale": "… (≥80 chars, name fit_status + usable windows + r2 beside the verdict)"} or null (REQUIRED at final ONLY when a SCENARIO block was given — echo numerics from calc.scenario.evaluate → … paths, never compute),\n'
        '  "forward_scenario": {"horizon_ms": N, "targets": [{"T": "…", "p_ge": "…", "p_lo": "…", "p_hi": "…"}], "invalidations": [{"S": "…", "p_le": "…"}], "fit_id": "…"} or null (OPTIONAL — horizon-native P(T)/P(S) from calc.forward.scenario → … paths; cite when the task bears horizons/targets),\n'
        '  "hypothesis_evidence": {"hypothesis_id": "…", "effect": "…", "se": "…", "ci": ["…", "…"], "p_value": "…", "n": N, "multiplicity_adj": "…"} or null (OPTIONAL — calc.hypothesis.test → … ledger entry, post-fit only; p<0.05 is evidence, never execution),\n'
        '  "tool_calls": [{"name": "<ONE registry tool name>", "args": {"symbol": "<this cycle\'s symbol>", "venue": "<this cycle\'s venue>", "interval_seconds": 10, "window_minutes": 30, …}}],\n'
        '  KEY RULE: the tool-name key is EXACTLY "name" — never "tool", "tool_name", or any other key. Entries under any other key are dropped unread.\n'
        '  "memory_proposals": [{"kind": "observation|hypothesis", "content": "…", "importance": 5.0, "tags": ["…"]}] or null\n'
        "}\n"
        "REGISTRY TOOL NAMES (use EXACTLY — any other name is denied):\n"
        "  P1 OFI/tape: micro.capture_status, micro.events, micro.ofi_intervals, micro.replay, calc.ofi.intervals;\n"
        "  P2 AD/fits: micro.fit_beta, micro.evidence, calc.depth.average, calc.observation.build, calc.fit.price_impact, calc.fit.depth_scaling;\n"
        "  P3 correlate (substrate.read PRIMARY, market.read context): substrate.invoke + substrate.tape, substrate.density, substrate.delta, substrate.ladders, substrate.anchors, substrate.tiers, substrate.volume_profile, substrate.technicals, substrate.migration, substrate.oi, substrate.signals, substrate.large_print, then substrate.read; market.read, market.derivatives, market.keystone_history, market.wall_history;\n"
        "  P5 derived (memory node pruned from the track — transport retained): calc.price.delta (alias calc.derived_diagnostic), calc.scenario.evaluate (price-target scenarios only);\n"
        "  P5 forward stack (Track D horizon-native): calc.forward.forecast (canonical ForecastResult), "
        "the feature/join/fit/distribution tools are diagnostic sub-tools, then calc.forward.scenario, "
        "calc.hypothesis.test (post-fit only, needs hypothesis_id), "
        "calc.decay.report, calc.discipline.audit;\n"
        "  P3 events (Track D typed): calc.events.absorption, calc.events.walls;\n"
        "  P6 output: no tools — synthesis only.\n"
        "STAGED WORKFLOW (coverage is measured from tools you EXECUTE, not phases you declare):\n"
        "  P1 OFI inference → P2 AD inference (split — never merged) → P3 warm-plane correlation "
        "(TWO-BEAT: invoke ≥1 substrate.* worker listed BEFORE substrate.read in the same tool_calls array; "
        "substrate.read is the verdict, market.read is context; judge freshness via age_ms; "
        "dormant/empty results are findings, never re-invoke) → "
        "P4 explanation (the why-now synthesis) → P5 derivation (calc.price.delta "
        "with an OFI value for the NUMERIC ΔP + band; H0/H1 grounding comes from the "
        "track's own evidence, formed in reasoning position 3) → "
        "P6 OUTPUT GENERATION: the primary inference output from this run's reasoning, tool_calls=[].\n"
        "WINDOW FREEDOM: pre-gather spine is interval 10s / window 30m, but you may pass interval_seconds "
        "(10/15/30) and window_minutes (15/30/60) in any calc/fit/group args to recompute at other cadences.\n"
        f"Rules: pack each pass densely (≤{MAX_DISPATCHES_PER_PASS} dispatches per pass guardrail); budgeted passes per cycle: 8. "
        "Empty tool_calls advances the phase ONLY when earlier phases are covered; a FINAL turn "
        "(phase P6, tool_calls=[] or omitted) is REJECTED for repair unless P1+P2+P3+P5 all have executed tools, "
        "a P6 synthesis turn was declared, hypothesis.H0 is set, summary ≥200 chars, confidence low|medium|high, "
        "every evidence entry carries a non-empty interpretation, "
        "and evidence cites ≥2 distinct roots incl. a calc.price.delta → … ΔP path plus ≥1 more fresh tool result. "
        "Every tool result arrives in a ToolResult envelope ``{tool, status, reason, data, null_fields}``. "
        "Cite fields from the ``data`` key using the path ``<tool> → data.<field>``. "
        "``null_fields`` explicitly names what was not computed (never zero).\n"
        "memory_proposals max 3, kind fact forbidden."
    )


def build_workflow_brief() -> str:
    """The agent's walk-through, generated FROM the runtime state module.

    Loop order, sub-loop purposes + exit conditions, and pass budgets are
    read from loop_states/config — never hand-synced prose. If the runtime
    moves, this text follows on the next cycle.
    """
    lines = ["LOOP WALK (generated from the runtime state module — authoritative):"]
    for stage in STAGE_ORDER:
        loop = loop_for_stage(stage)
        budget = LOOP_PASS_BUDGET.get(loop.value, 0)
        subs = "; ".join(
            f"{sl.value} ({SUBLOOP_SPECS[sl].purpose} "
            f"Exit: {SUBLOOP_SPECS[sl].exit_condition or 'once'})"
            for sl in LOOP_SUBLOOPS[loop]
        )
        lines.append(
            f"- {stage.value.upper()} → {loop.value} ({budget} passes): {subs}"
        )
    lines.append(
        "Standing rules: closed loops never revisited; refusals and nulls "
        "travel forward as findings; validation owns one bounded retry, then "
        "the cycle ends at a terminal with findings preserved; output "
        "composes without tools."
    )
    return "\n".join(lines)


# Task-driven required chains (prompts pass refinement — visibility, not
# gate mandates). For a task shape, the engine states the ORDERED tool chain
# the answer requires, and every follow-up shows done vs remaining computed
# from the ledger. The validator is untouched: enforcement here is structural
# visibility (the agent always sees what the shape demands and what is
# missing), never accept/reject rules.
PRICE_TARGET_HINTS = (
    "target", "hit", "reach", "touch", "break above", "break below",
    "all-time", "ath", "support", "resistance", "stop", "take-profit",
)


def classify_task(task: str | None, scenario: dict[str, Any] | None) -> str:
    """Task shape classifier (deterministic keyword + scenario presence)."""
    if scenario is not None:
        return "price_target"
    blob = (task or "").lower()
    if any(hint in blob for hint in PRICE_TARGET_HINTS):
        return "price_target"
    return "general"


TASK_CHAINS: dict[str, list[str]] = {
    # The price-X shape: read → condition on X_t → forward evidence →
    # price delta → target/invalidation odds → prove/disprove → trust scope.
    # Presentation only: order and required-ness are owned by the loop layer
    # (loop_states.CHAIN_SUBLOOP + engine/core/chain.py). If this text ever
    # disagrees with that mapping, the mapping wins.
    "price_target": [
        "market.read",
        "calc.forward.forecast",
        "calc.forward.scenario",
        "calc.hypothesis.test",
        "calc.decay.report",
        "calc.discipline.audit",
    ],
    "general": [
        "calc.forward.forecast",
        "calc.discipline.audit",
    ],
}

CHAIN_MEANING: dict[str, str] = {
    "market.read": "current price + regime context (snapshot first)",
    "calc.forward.forecast": "canonical ForecastResult: schema-checked X_t, forward Y(h), train-only OOS, calibration gate, assumptions, and Route A/B diagnostics",
    "calc.feature.build": "condition on X_t — versioned state vector (diagnostic sub-step)",
    "calc.forward.join": "forward targets Y(h) + exclusion log (diagnostic sub-step)",
    "calc.forward.fit": "per-horizon fit + comparator + OOS (diagnostic sub-step)",
    "calc.forward.distribution": "price delta: E[Y(h)] + PI + gated probability (diagnostic sub-step)",
    "calc.forward.scenario": "P(T)/P(S) curves with bands at fitted horizons",
    "calc.hypothesis.test": "prove/disprove H0 post-fit (needs hypothesis_id)",
    "calc.decay.report": "which horizons to trust (nulls are results)",
    "calc.discipline.audit": "9-lock discipline gate (validation home; attempt required, refusal is a finding)",
}


def build_task_workflow(
    task: str | None, scenario: dict[str, Any] | None,
    *, kind_override: str | None = None,
) -> dict[str, Any]:
    """Ordered required chain for this task shape (deterministic).

    ``kind_override`` is the directive-bound kind from the plan — the plan
    selects required-ness; it never invents links (TASK_CHAINS stays the
    only vocabulary).
    """
    kind = kind_override or classify_task(task, scenario)
    chain = list(TASK_CHAINS[kind])
    steps = [
        f"{i}. {tool} — {CHAIN_MEANING.get(tool, 'cited evidence')}"
        for i, tool in enumerate(chain, 1)
    ]
    handoff = (
        "Handoff rule: cite each link's output path before pulling the next "
        "(fit_id → distribution → scenario → hypothesis_id); a refused link "
        "is recorded as a finding with its reason, the chain continues, and "
        "the final states exactly which links held."
    )
    return {"kind": kind, "chain": chain, "steps": steps, "handoff": handoff}


def chain_status(chain: list[str], done: list[str]) -> str:
    """Done vs remaining rendering from ledger keys (deterministic)."""
    done_set = set(done)
    remaining = [t for t in chain if t not in done_set]
    return (
        f"REQUIRED CHAIN — done: [{', '.join(t for t in chain if t in done_set) or 'none'}]; "
        f"remaining: [{', '.join(remaining) or 'complete'}]."
    )


def build_loop_state_block(
    *,
    controller: Any | None = None,
    loop: str | None = None,
    sub_loop: str | None = None,
    intent: str | None = None,
    passes_spent: int = 0,
    pass_budget: int = 0,
    dispatches_left: int | None = None,
    traversal: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    congruence: dict[str, Any] | None = None,
    comprehension: dict[str, Any] | None = None,
    seeds: list[str] | None = None,
) -> str:
    """Render the prompt position from the canonical controller state.

    ``loop``/``sub_loop``/``intent`` remain optional compatibility inputs for
    callers outside the runtime, but an injected controller always wins.  The
    model must never receive a state that the FSM did not authorize.
    """
    if controller is not None and controller.observation is not None:
        observation = controller.observation
        # The controller is semantic authority.  Callers may provide labels
        # for compatibility, but they may not render a prompt that disagrees
        # with the live FSM observation.
        requested = (
            loop,
            sub_loop,
            intent,
        )
        actual = (
            observation.nested_loop.value,
            observation.sub_loop.value if observation.sub_loop else None,
            observation.task.value,
        )
        if any(value is not None and value != expected
               for value, expected in zip(requested, actual)):
            raise AssertionError(
                f"prompt state {requested!r} disagrees with controller observation {actual!r}"
            )
        loop, sub_loop, intent = actual
    loop = loop or "unknown"
    done = ", ".join(
        f"{name}({info.get('passes_spent', 0)}p"
        f"{'✓' if info.get('completed') else '…'})"
        for name, info in (traversal or {}).items()
        if isinstance(info, dict)
    ) or "none yet"
    parts = [
        f"LOOP STATE — inside {loop}" + (f" / {sub_loop}" if sub_loop else "")
        + (f" [{intent}]" if intent else "")
        + f" (pass {passes_spent + 1} of {pass_budget} for this loop).",
        f"TRAVERSAL SO FAR — {done}.",
    ]
    if dispatches_left is not None:
        parts.append(f"DISPATCH CEILING — {dispatches_left} calls left this pass (guardrail, not target).")
    if comprehension and loop in ("reasoning", "validation", "output"):
        parts.append(
            "YOUR COMPREHENSION (binding — conclusions answer it): "
            f"{json_dumps_short(comprehension)}"
        )
    if seeds and loop == "evidence":
        parts.append(f"SEEDED PLAN (dispose freely): {', '.join(seeds)}.")
    if gate:
        parts.append(f"GATE — {json_dumps_short(gate)}.")
    if congruence and loop in ("reasoning", "validation"):
        parts.append(f"CONGRUENCE — {json_dumps_short(congruence)}.")
    return "\n".join(parts)


def json_dumps_short(value: Any, cap: int = 600) -> str:
    """Bounded JSON rendering for prompt blocks (never raises)."""
    try:
        return json.dumps(value, default=str)[:cap]
    except Exception:
        return str(value)[:cap]


# Per-loop tool windows (turns carry their loop's window; the full packages
# live in the manifest, loaded once in the system prompt). Entries mirror
# the manifest's ≤4-line discipline by reference, not by duplication.
TOOL_WINDOWS: dict[str, str] = {
    "comprehension": "No tools this pass — understand and plan only.",
    "evidence": (
        "YOUR TOOLS THIS LOOP — gather in this order, cite each link: "
        "micro.capture_status + micro.fit_beta (gate reads already done — do not re-pull); "
        "market.read snapshot (current price + regime); "
        "calc.forward.forecast (canonical ForecastResult: X_t, Y(h), train-only OOS, calibration gate, "
        "forecast regime, assumptions, and Route A/B diagnostics). "
        "Gather only: do not conclude, do not test hypotheses. "
        "Refusals are findings to record with reasons, never failures to repair."
    ),
    "reasoning": (
        "YOUR TOOLS THIS LOOP — three forced positions, in order (the loop cannot exit "
        "before all three complete; tools from a later position are denied until it is active): "
        "POSITION 1/3 ASSEMBLE — re-derive the substrates, no interpretation: state your "
        "paper-grounded prior from the recalled facts, then calc.ofi.intervals (Route A OFI first) → "
        "calc.depth.average (Route B AD) → calc.forward.join (multivariate X_t joined to Y(h)). "
        "POSITION 2/3 INTERPRET — fit the equation and read the distribution: calc.forward.fit → "
        "calc.forward.distribution (expected delta, validation_state, probability_status, assumptions, "
        "route disagreement, chain_trace receipt) → calc.decay.report (which horizons survive — quote it "
        "before trusting any horizon); calc.forward.forecast re-reads are allowed for cross-checking. "
        "POSITION 3/3 HYPOTHESIZE — think against the user prompt, form the testable H0 (only now that "
        "the distribution exists) and run calc.hypothesis.test with hypothesis_id + horizon_ms + m_tests "
        "(post-fit only; p<0.05 is evidence, never execution), plus calc.forward.scenario with horizon_ms + "
        "targets/invalidations when horizons are present (P_ge/P_le only when calibration passes; legacy "
        "scenario stays the flow-requirement baseline — report agreement AND disagreement); "
        "calc.events.absorption/walls for E[dP|event] conditioning when liquidity language is present. "
        "Then close with tool_calls=[]. Validation (calc.discipline.audit) and output composition are later "
        "loops, not this one. Refusals are findings to record with reasons, never failures to repair."
    ),
    "validation": (
        "YOUR TOOLS THIS LOOP — calc.discipline.audit (pull it), then stop. "
        "Cite refusals as `tool → refusal` findings with verdict unevaluable. "
        "One repair round exists; a second rejection ends the cycle with "
        "findings preserved — do not thrash."
    ),
    "output": "No tools this pass — compose only. Any tool_calls returned are dropped unread.",
}


__all__ = [
    "SYSTEM_PROMPT_TEMPLATE",
    "TOOL_WINDOWS",
    "TASK_CHAINS",
    "build_loop_state_block",
    "build_task_workflow",
    "build_workflow_brief",
    "chain_status",
    "classify_task",
    "compose_followup_prompt",
    "compose_narrate1_prompt",
    "compose_repair_prompt",
    "default_comprehension",
    "json_dumps_short",
    "load_kb",
    "load_paper_kb",
    "seed_evidence_plan",
    "build_system_prompt",
    "build_output_format",
    # Tool schema exports
    "wrap_tool_result",
]


# --------------------------------------------------------------------------
# Comprehension-block builders (deterministic prompt sections)
# --------------------------------------------------------------------------
# These two functions are pure over (task, scenario). They produce the
# prompt sections the comprehension loop prepares deterministically before
# any LLM call — the seeded plan and the default-comprehension fallback.


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
    # Memory is pruned from the seeded track (its node stays transport for
    # later use): H0/H1 grounding comes from the track's own evidence +
    # position-3 formation, not from seeded paper recall.
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


# --------------------------------------------------------------------------
# Per-pass prompt composers
# --------------------------------------------------------------------------
# Three composers replace the multi-block prompt-assembly that used to be
# inlined in run_evidence (narrate-1), _followup_turn (follow-up), and
# run_validation (repair). Each one renders a string the engine can hand
# to the LLM directly; none of them mutate state. They depend on
# build_loop_state_block / TOOL_WINDOWS / TURN_CONTRACT_LINE / chain_status
# and the LLM block, so they live here, next to those primitives.


def compose_narrate1_prompt(
    *,
    controller: Any,
    loop: str,
    sub_loop: str,
    intent: str | None,
    passes_spent: int,
    pass_budget: int,
    dispatches_left: int,
    seeds: list[str] | None,
    task_block: str,
    scenario_block: str,
    wake_block: str,
    deterministic_state: dict[str, Any],
    prior_note: str,
    memory_block: str,
    output_format: str,
    workflow_block: str,
    traversal: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
) -> str:
    """Compose the loop's first-turn (narrate#1) prompt.

    Two-stage assembly, mirrored from the legacy inline code: first the
    envelope + recall blocks, then the loop-state relay header prepended.
    Pure: returns the assembled prompt string without mutating inputs.
    """
    context_block = (
        f"{task_block}"
        f"{scenario_block}"
        f"WAKE: {wake_block}\n\n"
        "ENVELOPE STATE (gate reads + wake identity — READ-PLANE; pull "
        "everything else yourself via tools; never recompute values, "
        "COMMAND the read tools and cite their paths):\n"
        f"{json.dumps(deterministic_state, default=str)[:60_000]}\n\n"
        f"{prior_note}\n\n"
    )
    if memory_block:
        context_block += (
            "RECALLED MEMORY (provenance-tagged priors; subordinate to the "
            f"ledger):\n{memory_block}\n\n"
        )
    context_block += output_format
    # Wrap the deterministic_state envelope + gate reads in ToolResult
    # envelopes so the LLM sees the same structure from turn 1.
    from .tool_schemas import wrap_tool_result

    # Wake identity + gate reads get wrapped so the model gets the uniform
    # envelope from the very first turn.
    wrapped_det = {}
    for k, v in deterministic_state.items():
        if k in ("gate", "forecast_result") and isinstance(v, dict):
            wrapped_det[k] = k
        elif k in ("forecast_result", "fit_status", "capture_state"):
            wrapped_det[k] = v
        else:
            wrapped_det[k] = v
    # The envelope block stays raw (it's the run identity, not a tool)
    # but we inject a note about the ToolResult envelope.
    context_block = (
        f"{task_block}"
        f"{scenario_block}"
        f"WAKE: {wake_block}\n\n"
        "ENVELOPE STATE (gate reads + wake identity — READ-PLANE; pull "
        "everything else yourself via tools; never recompute values, "
        "COMMAND the read tools and cite their paths from the ``data`` key):\n"
        f"{json.dumps(deterministic_state, default=str)[:60_000]}\n\n"
        f"{prior_note}\n\n"
    )
    if memory_block:
        context_block += (
            "RECALLED MEMORY (provenance-tagged priors; subordinate to the "
            f"ledger):\n{memory_block}\n\n"
        )
    context_block += output_format
    return (
        build_loop_state_block(
            controller=controller,
            loop=loop, sub_loop=sub_loop,
            intent=intent,
            passes_spent=passes_spent, pass_budget=pass_budget,
            dispatches_left=dispatches_left,
            traversal=traversal or controller.loop_coverage(),
            gate=gate or deterministic_state.get("gate"),
            seeds=seeds,
        )
        + "\n\n" + TOOL_WINDOWS.get(loop, "")
        + "\n\n" + workflow_block
        + "\n\n" + context_block
    )


def compose_followup_prompt(
    *,
    controller: Any,
    loop: str,
    sub_loop: str,
    passes_spent: int,
    pass_budget: int,
    dispatches_left: int,
    chain: list[str],
    accumulated: dict[str, Any],
    round_results: dict[str, Any],
    task_reminder: str,
    scenario_reminder: str,
    phase_guidance: dict[str, str],
    next_phase: str,
    chain_block: str = "",
) -> str:
    """Compose the per-pass follow-up prompt.

    Single source of truth for the follow-up layout: loop-state header,
    tool window, chain status, accumulated results, prior passes, phase
    coverage, turn contract. The caller supplies the loop-specific chain
    block (rendered by chain_completion + chain_steer) so the layout
    stays composable across reasoning / validation / recovery.
    """
    loop_header = build_loop_state_block(
        controller=controller,
        loop=loop, sub_loop=sub_loop, intent=None,
        passes_spent=passes_spent, pass_budget=pass_budget,
        dispatches_left=dispatches_left,
        traversal=controller.loop_coverage(),
        gate=None,
        congruence=(controller.congruence() if loop == "reasoning" else None),
        comprehension=None,
    )
    from .tool_schemas import build_phase_summary_block

    # Wrap raw tool results in the ToolResult envelope so the LLM sees
    # uniform tool/status/reason/data/null_fields structure. Results that
    # already have a "tool" key are treated as already wrapped.
    def _maybe_wrap(name: str, value: Any) -> Any:
        if isinstance(value, dict) and "tool" in value:
            return value
        return wrap_tool_result(name, value, status="ok")

    wrapped_round = {
        name: _maybe_wrap(name, value)
        for name, value in round_results.items()
    }
    wrapped_accum = {
        name: _maybe_wrap(name, value)
        for name, value in accumulated.items()
        if name not in round_results
    }
    phase_block = build_phase_summary_block(wrapped_round)

    return (
        f"{loop_header}\n\n"
        f"{TOOL_WINDOWS.get(loop, '')}\n\n"
        f"{chain_status(chain, list(accumulated))}\n"
        f"{chain_block}\n"
        f"{task_reminder}"
        f"{scenario_reminder}"
        f"PASS RESULTS (pass {passes_spent} of "
        f"{pass_budget} for {loop}; cite paths from the ``data`` key):\n"
        f"{json.dumps(wrapped_round, default=str)[:40_000]}\n\n"
        f"PRIOR PASSES (earlier results, for citation):\n"
        f"{json.dumps(wrapped_accum, default=str)[:40_000]}\n\n"
        f"{phase_block}"
        "PHASE COVERAGE (families with ≥1 ok tool): "
        f"{json.dumps({p: sorted(s) for p, s in controller.phase_coverage.items()})}\n"
        f"{phase_guidance[next_phase]}\n"
        f"{TURN_CONTRACT_LINE}\n"
        f"Passes remaining in {loop}: "
        f"{pass_budget - passes_spent}. "
        'Declare "phase" every turn; call this loop\'s tools, or advance with tool_calls=[].'
    )


def compose_repair_prompt(
    *,
    controller: Any,
    loop: str,
    sub_loop: str,
    passes_spent: int,
    pass_budget: int,
    dispatches_left: int,
    missing: list[str],
    accumulated: dict[str, Any],
    task_reminder: str,
    scenario_reminder: str,
    scenario_steer: str,
    phase_guidance: dict[str, str],
    next_phase: str,
) -> str:
    """Compose the validation-loop repair-turn prompt.

    Mirror of compose_followup_prompt, structured for the bounded retry:
    rejection header, accumulated results, missing items, phase guidance.
    """
    from .tool_schemas import build_phase_summary_block, wrap_tool_result

    def _maybe_wrap(name: str, value: Any) -> Any:
        if isinstance(value, dict) and "tool" in value:
            return value
        return wrap_tool_result(name, value, status="ok")

    wrapped_accum = {
        name: _maybe_wrap(name, value)
        for name, value in accumulated.items()
    }
    phase_block = build_phase_summary_block(wrapped_accum)

    repair_header = build_loop_state_block(
        controller=controller,
        loop=loop, sub_loop=sub_loop,
        intent=None,
        passes_spent=passes_spent, pass_budget=pass_budget,
        dispatches_left=dispatches_left,
        traversal=controller.loop_coverage(),
        gate=None,
        congruence=controller.congruence(),
        comprehension=None,
    )
    return (
        f"{repair_header}\n\n"
        f"{TOOL_WINDOWS.get(loop, '')}\n\n"
        f"{task_reminder}"
        f"{scenario_reminder}"
        f"{scenario_steer}"
        "FINAL REJECTED — staged inference incomplete. Missing:\n"
        + "\n".join(f"- {item}" for item in missing)
        + f"\n\nACCUMULATED TOOL RESULTS (ToolResult envelopes):\n{json.dumps(wrapped_accum, default=str)[:40_000]}\n\n"
        f"{phase_block}"
        f"PHASE COVERAGE: {json.dumps({p: sorted(s) for p, s in controller.phase_coverage.items()})}\n"
        f"{phase_guidance[next_phase]}\n"
        f"{TURN_CONTRACT_LINE}\n"
        "Return the next turn now: declare \"phase\", include the missing tool_calls, "
        "and finalize (tool_calls=[]) only when every missing item is addressed."
    )
