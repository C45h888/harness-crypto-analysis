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

from pathlib import Path
from typing import Any

from .config import (
    AGENTIC_MAX_TOOL_ROUNDS,
    AGENTIC_PER_ROUND_CALL_CAP,
    _CALC_KB_PATHS,
    _MEMORY_PROTOCOL,
    _PAPER_KB,
    _TOOL_MANIFEST,
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
3. Run the agentic loop: you have up to {max_rounds} tool rounds. Use them. Cite every numeric claim with exact paths.

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
TOOL MANIFEST (commandable, deterministic — USE IT):
{tool_manifest}
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
        tool_manifest=(
            "TOOL MANIFEST:\n" + load_kb(_TOOL_MANIFEST)
            if _TOOL_MANIFEST.exists() else ""
        ),
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
    rounds = AGENTIC_MAX_TOOL_ROUNDS
    return (
        f"STAGED INFERENCE — 6 phases (P1→P6), one JSON object per turn, up to {rounds} tool rounds. Declare your phase every turn.\n"
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
        '  "tool_calls": [{"name": "<ONE registry tool name>", "args": {"symbol": "<this cycle\'s symbol>", "venue": "<this cycle\'s venue>", "interval_seconds": 10, "window_minutes": 30, …}}],\n'
        '  KEY RULE: the tool-name key is EXACTLY "name" — never "tool", "tool_name", or any other key. Entries under any other key are dropped unread.\n'
        '  "memory_proposals": [{"kind": "observation|hypothesis", "content": "…", "importance": 5.0, "tags": ["…"]}] or null\n'
        "}\n"
        "REGISTRY TOOL NAMES (use EXACTLY — any other name is denied):\n"
        "  P1 OFI/tape: micro.capture_status, micro.events, micro.ofi_intervals, micro.replay, calc.ofi.intervals;\n"
        "  P2 AD/fits: micro.fit_beta, micro.evidence, calc.depth.average, calc.observation.build, calc.fit.price_impact, calc.fit.depth_scaling;\n"
        "  P3 correlate (substrate.read PRIMARY, market.read context): substrate.invoke + substrate.tape, substrate.density, substrate.delta, substrate.ladders, substrate.anchors, substrate.tiers, substrate.volume_profile, substrate.technicals, substrate.migration, substrate.oi, substrate.signals, substrate.large_print, then substrate.read; market.read, market.derivatives, market.keystone_history, market.wall_history;\n"
        "  P5 paper/derived: memory.recall_paper, calc.price.delta (alias calc.derived_diagnostic), calc.scenario.evaluate (price-target scenarios only);\n"
        "  P6 output: no tools — synthesis only.\n"
        "STAGED WORKFLOW (coverage is measured from tools you EXECUTE, not phases you declare):\n"
        "  P1 OFI inference → P2 AD inference (split — never merged) → P3 warm-plane correlation "
        "(TWO-BEAT: invoke ≥1 substrate.* worker listed BEFORE substrate.read in the same tool_calls array; "
        "substrate.read is the verdict, market.read is context; judge freshness via age_ms; "
        "dormant/empty results are findings, never re-invoke) → "
        "P4 explanation (the why-now synthesis) → P5 derivation (memory.recall_paper FIRST, then "
        "calc.price.delta with an OFI value for the NUMERIC ΔP + band) → "
        "P6 OUTPUT GENERATION: the primary inference output from this run's reasoning, tool_calls=[].\n"
        "WINDOW FREEDOM: pre-gather spine is interval 10s / window 30m, but you may pass interval_seconds "
        "(10/15/30) and window_minutes (15/30/60) in any calc/fit/group args to recompute at other cadences.\n"
        f"Rules: tool_calls max {AGENTIC_PER_ROUND_CALL_CAP} per round, max {rounds} rounds. "
        "Empty tool_calls advances the phase ONLY when earlier phases are covered; a FINAL turn "
        "(phase P6, tool_calls=[] or omitted) is REJECTED for repair unless P1+P2+P3+P5 all have executed tools, "
        "a P6 synthesis turn was declared, hypothesis.H0 is set, summary ≥200 chars, confidence low|medium|high, "
        "every evidence entry carries a non-empty interpretation, "
        "and evidence cites ≥2 distinct roots incl. a calc.price.delta → … ΔP path plus ≥1 more fresh tool result. "
        "memory_proposals max 3, kind fact forbidden."
    )


__all__ = [
    "SYSTEM_PROMPT_TEMPLATE",
    "load_kb",
    "load_paper_kb",
    "build_system_prompt",
    "build_output_format",
]
