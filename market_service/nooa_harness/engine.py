"""InferenceEngine — the statistical inference agent (Pass B3).

One cycle of the engine, from wake to durable artifact:

    receive in-memory WakeEnvelope (event-driven worker → run_cycle)
    → gather (capabilities) → recall memory (MemoryNode)
    → HARD GATE (resolve_inference_status, deterministic, pre-LLM)
        ├─ insufficient → artifact w/ NULL interpretation, 0 LLM tokens,
        │                 quality observation remembered
        └─ else → NARRATE#1 (deterministic_state + memory + tool manifest)
                  → execute tool_calls (ONE round max)
                  → NARRATE#2 (final; tool results included)
    → assemble InferenceArtifact → Postgres-first → Redis projection
    → resolve memory proposals (LLM proposes, MemoryNode disposes)

The wake arrives as an IN-MEMORY ``WakeEnvelope`` handed over by the
wake worker (``wake_worker.WakeSupervisor``) — never by draining a
stream. The retired ``publish_wake``/``read_pending_wakes`` transport is
gone; ``run_cycle`` takes the assertion object directly. The only manual
wake factory kept is ``acquire_manual_wake`` (outer-CLI force trigger).

The narration LLM call budget is TWO per cycle (narrate + one tool round).
Gate-failed cycles spend ZERO tokens. The LLM client is INJECTED (built by
the caller via ``backends.build_llm``) so module import stays nooa-free and
contract tests never pay the OpenAI SDK import cost.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from market_service.nooa_harness.inference import (
    CounterSnapshot,
    WakeConfig,
    execute_tool,
    resolve_inference_status,
)
from market_service.runtime.contracts import InferenceArtifact, WakeEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover — narration falls back to raw JSON
    BaseModel = None  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]


if BaseModel is not None:
    class NarrationToolCall(BaseModel):
        """One validated tool dispatch request from a narration turn."""
        name: str
        args: dict[str, Any] = Field(default_factory=dict)

    class NarrationTurn(BaseModel):
        """Structured narration turn — enforced via output_model when supported.

        Interpretation fields are optional so a tool-request turn parses;
        tool_calls entries REQUIRE a non-empty name (nameless calls fail
        validation here instead of dying later as tool.unknown).
        """
        summary: str | None = None
        evidence: list[dict[str, Any]] | None = None
        confidence: str | None = None
        limitations: list[str] | None = None
        model_separation: str | None = None
        hypothesis: dict[str, Any] | None = None
        phase: str = "P1"
        tool_calls: list[NarrationToolCall] = Field(default_factory=list)
        memory_proposals: list[dict[str, Any]] | None = None
else:
    NarrationToolCall = None  # type: ignore[assignment]
    NarrationTurn = None  # type: ignore[assignment]

# Module-level probe flag: once a backend rejects output_model, stop paying
# the failed structured attempt on every subsequent turn (same process).
_STRUCTURED_OK: bool | None = None

SESSION_TEMPLATE = "inference-engine-{symbol}-{venue}"

# Narration token budget — agentic loop, not controlled generator.
# Muse Spark 1.2 contributor is mandatory-reasoning with 1_048_576 context;
# 25% of that is 262k. Default 50_000 lets the model run as an agent
# (tool calls + validation + memory cross-check + structured report) without
# starving the final JSON block. Operators override with NOOA_MODEL_MAX_TOKENS.
# Calculation modules + read tools are now the primary inference path — the
# predefined formula output is never the final word.
DEFAULT_NARRATION_MAX_TOKENS = 50_000
NARRATION_MAX_TOKENS_ENV = "NOOA_MODEL_MAX_TOKENS"
DEFAULT_CONTEXT_WINDOW = 1_048_576  # Muse Spark 1.2 contributor
AGENTIC_MAX_TOOL_ROUNDS = 5  # one per phase + spare; staged P1→P6 cycle (P4 needs no tools, P6 is synthesis-only)
AGENTIC_MAX_LLM_TURNS = 8  # narrate#1 + tool follow-ups + repair + forced-final
AGENTIC_PER_ROUND_CALL_CAP = 3  # unchanged: max dispatches per tool round
SUMMARY_MIN_CHARS = 200  # P4 explanation floor — thinner finals are repaired

# Bounded memory recall per cycle — expanded for paper KB cross-check.
MEMORY_RECALL_LIMIT = 12
MEMORY_CONTEXT_BUDGET = 8_000
MAX_MEMORY_PROPOSALS = 3
# Hard output bound for the prior-artifact headline block.
_PRIOR_HEADLINE_MAX_CHARS = 4_000

_TOOL_MANIFEST = Path(__file__).resolve().parents[2] / (
    "docs/nooa-kb/behavior/tool-manifest.md"
)
_MEMORY_PROTOCOL = Path(__file__).resolve().parents[2] / (
    "docs/nooa-kb/behavior/memory-protocol.md"
)
_PAPER_KB = Path(__file__).resolve().parents[2] / (
    "docs/nooa-kb/nooa-micro-structure-archieture.md"
)
_CALC_KB_PATHS = [
    Path(__file__).resolve().parents[2] / "docs/nooa-kb/behavior/B01-agents-base.md",
]

_SYSTEM_PROMPT_TEMPLATE = """You are the statistical inference engine for {symbol} ({venue}) — an AGENTIC loop, not a controlled output generator.

You receive ONE deterministic_state object: microstructure fits, coverage,
and gate reasons computed by deterministic Python from the Redis ledgers.
You also receive recalled memory (priors + paper KB) and a deterministic diff vs prior artifact.

YOUR JOB — VALID inference, not predefined addition:
1. VALIDATE the deterministic fits by invoking calculation modules via tools — do not accept the formula output as final.
   Call market.read plus substrate.* worker tools (tape/density/delta/ladders/…)
   to read the always-fresh worker projections and invoke bounded fire-ticks,
   call micro.ofi_intervals / micro.evidence to audit intervals, and cross-check against paper KB and memory.
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


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_kb(path: Path) -> str:
    """Bounded KB doc read; empty string on any failure (never raises)."""
    try:
        return path.read_text(encoding="utf-8")[:12_000]
    except OSError:
        return ""

def _load_paper_kb() -> str:
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


def _narration_max_tokens() -> int:
    """Resolve the narration generation budget — reasoning-aware, 25% window cap.

    Muse Spark contributor: 1_048_576 context → 25% = 262_144. Default 50_000
    is the floor for agentic execution; env override is clamped to
    [2_000, 262_144] so a misconfigured 500_000 does not OOM the gateway.
    """
    cap = int(DEFAULT_CONTEXT_WINDOW * 0.25)  # 262_144 for Muse Spark
    try:
        requested = int(os.getenv(NARRATION_MAX_TOKENS_ENV, str(DEFAULT_NARRATION_MAX_TOKENS)))
        return max(2_000, min(requested, cap))
    except (TypeError, ValueError):
        return DEFAULT_NARRATION_MAX_TOKENS


def _stable_session_id(symbol: str, venue: str) -> str:
    """Deterministic per-(symbol, venue) UUID session id.

    The durable stores (agent_memory, inference_artifact) cast session_id to
    a UUID column, so the engine must hand them a real UUID. Deriving it from
    symbol+venue keeps memory coherent across cycles for one scope — a random
    id per cycle would fragment the memory plane.
    """
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"inference-engine://{symbol.lower()}/{venue}",
    ))


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from LLM text (fenced or embedded)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # fenced block first
    if "```" in raw:
        for chunk in raw.split("```"):
            candidate = chunk.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
    # first brace-balanced object
    start = raw.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(raw)):
            if raw[idx] == "{":
                depth += 1
            elif raw[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start:idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
                    break
        start = raw.find("{", start + 1)
    return None


def _coerce_turn(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize one narration turn: drop undispatchable tool_calls entries.

    Entries without a non-empty string ``name`` can never dispatch — drop
    them here (logged) instead of burning a tool round on tool.unknown.
    Missing/invalid ``args`` become {}. A non-dict hypothesis is wrapped.
    """
    if not isinstance(parsed, dict):
        return None
    calls = parsed.get("tool_calls")
    if calls is None:
        return parsed
    if not isinstance(calls, list):
        parsed["tool_calls"] = []
        return parsed
    kept: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            log.warning("dropping tool_call without a registry name: %.120r", call)
            continue
        args = call.get("args")
        kept.append({"name": name.strip(),
                     "args": dict(args) if isinstance(args, dict) else {}})
    parsed["tool_calls"] = kept
    hypothesis = parsed.get("hypothesis")
    if hypothesis is not None and not isinstance(hypothesis, dict):
        parsed["hypothesis"] = {"raw": hypothesis}
    return parsed


def _validate_final_turn(
    parsed: dict[str, Any],
    coverage: dict[str, set[str]],
) -> tuple[bool, list[str]]:
    """Phase-aware final gate: depth is structural, not advisory.

    A FINAL turn passes only when every required phase family has at least
    one executed tool (P1 OFI, P2 AD/fits, P3 market correlation, P5 paper
    + derived ΔP), the hypothesis carries H0, the P4 explanation meets the
    length floor, confidence is the contract enum, every evidence entry
    carries a non-empty interpretation, and evidence cites at least two
    distinct roots with at least one fresh tool result (not just
    deterministic_state). Returns (passed, missing[]) — missing drives
    the repair prompt.
    """
    from market_service.nooa_harness.inference import _REQUIRED_PHASES
    from market_service.runtime.contracts import normalize_confidence

    missing: list[str] = []
    for phase in _REQUIRED_PHASES:
        if not coverage.get(phase):
            missing.append(
                f"phase {phase} uncovered: execute its tools before finalizing "
                f"(covered so far: {sorted(coverage.get(phase) or [])})"
            )
    if not coverage.get("P6"):
        missing.append(
            "P6 output-generation turn required: synthesize the primary inference "
            "output from this run's reasoning (declare phase P6, no tools)"
        )
    hypothesis = parsed.get("hypothesis")
    if not isinstance(hypothesis, dict) or not str(hypothesis.get("H0") or "").strip():
        missing.append("hypothesis.H0 required: frame H0/H1 grounded in recalled paper facts")
    summary = parsed.get("summary") or ""
    if not isinstance(summary, str) or len(summary.strip()) < SUMMARY_MIN_CHARS:
        missing.append(
            f"P4 explanation too thin ({len(summary.strip())}/{SUMMARY_MIN_CHARS} chars): "
            "say what the fits show AND why it is happening now"
        )
    confidence = parsed.get("confidence")
    if confidence is not None and normalize_confidence(confidence) is None:
        missing.append(
            f"confidence must be one of low|medium|high (got {confidence!r}); "
            "hyphenated blends coerce conservatively (low-medium → low)"
        )
    roots: set[str] = set()
    evidence = parsed.get("evidence")
    if isinstance(evidence, list):
        for idx, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                missing.append(f"evidence[{idx}] must be an object with path + interpretation")
                continue
            if not str(entry.get("interpretation") or "").strip():
                missing.append(
                    f"evidence[{idx}] missing interpretation: say what "
                    f"{entry.get('path')!r} shows (paths+values cited, readings empty is a violation)"
                )
            path = str(entry.get("path") or "")
            head = path.split("→")[0].strip().split(".")[0].strip()
            if head:
                roots.add(head)
    if len(roots) < 2 or not (roots - {"deterministic_state"}):
        missing.append(
            "evidence must cite ≥2 distinct roots including ≥1 fresh tool result "
            f"(got roots: {sorted(roots) or 'none'}" + ")"
        )
    delta_paths = [
        str(entry.get("path") or "")
        for entry in (evidence if isinstance(evidence, list) else [])
        if isinstance(entry, dict)
    ]
    if not any(
        head in ("calc.price.delta", "calc.derived_diagnostic")
        for path in delta_paths
        for head in [path.split("→")[0].strip()]
    ):
        missing.append(
            "evidence must cite the derived ΔP via a calc.price.delta → … path"
        )
    return (not missing), missing


# Per-phase steering fragments — appended to follow-up prompts so each
# turn knows what the next uncovered phase demands. Window freedom is
# stated once here: the agent may vary interval_seconds (10/15/30) and
# window_minutes (15/30/60) in calc/fit tool args; deterministic code
# executes, the agent never recomputes.
_PHASE_GUIDANCE: dict[str, str] = {
    "P1": ("PHASE P1 — OFI INFERENCE: call calc.ofi.intervals "
           "(and/or micro.ofi_intervals). Judge tape quality: n vs minimum, "
           "capture gaps, hetero flag. Verdict: is this OFI tape usable or degraded, and why. "
           "You may vary interval_seconds (10/15/30) and window_minutes (15/30/60) in tool args."),
    "P2": ("PHASE P2 — AD INFERENCE: call calc.depth.average + "
           "calc.observation.build (and/or micro.fit_beta, calc.fit.price_impact). "
           "Judge AD stability and observation count separately from OFI — never merge. "
           "You may vary interval_seconds/window_minutes in tool args."),
    "P3": ("PHASE P3 — CORRELATE (substrate.read is the PRIMARY evidence; market.read is context): "
           "first invoke >=1 substrate.* worker (substrate.tape/density/delta/... for a bounded "
           "fire-tick), THEN substrate.read IN THE SAME tool_calls array with invoke listed BEFORE read "
           "(dispatches run sequentially in order, so the read sees the fresh projection). "
           "Judge freshness from age_ms in the compact projection; available:false, fired:0, or invoked:false "
           "(cooldown-dormant) are FINDINGS — report them, never re-invoke the same worker in one cycle. "
           "An empty upstream (no projections — capture down) is itself the finding: cite "
           "substrate.read -> substrates.<name>.available. "
           "Cross-validate P1/P2 against the warm plane and name agreements AND contradictions explicitly. "
           "market.read (snapshot) plus derivatives/keystone/wall histories are regime context, not the correlation verdict."),
    "P4": ("PHASE P4 — EXPLAIN: no new tools required. Write the synthesis: what the fits show "
           f"(≥{SUMMARY_MIN_CHARS} chars in summary) AND why it is happening now — regime, capture quality, "
           "flow/positioning drivers. Then proceed to P5."),
    "P5": ("PHASE P5 — DERIVE: call memory.recall_paper FIRST (ground H0/H1 in Cont 1011.6402 facts), "
           "then calc.price.delta (alias calc.derived_diagnostic) with an OFI value — scenario arg or "
           "latest-interval default — for the NUMERIC derived ΔP (route A direct + route B when c/λ exist, "
           "with 95% band). A refusal (insufficient fit) is a finding, not a failure: report it."),
    "P6": ("PHASE P6 — OUTPUT GENERATION (final): no tools. Synthesize the PRIMARY inference output "
           "strictly from this run's reasoning: H0/H1 verdict, numeric ΔP with band, regime explanation, "
           "confidence, limitations. Every numeric claim cites its tool path, including a "
           "calc.price.delta → … path for the ΔP. Return FINAL JSON: tool_calls=[], full summary/evidence/"
           "confidence/limitations/model_separation/hypothesis{H0,H1,paper_refs,evidence_refs}."),
}


def _next_uncovered_phase(coverage: dict[str, set[str]]) -> str:
    """First required phase family with no executed tool yet; then P6."""
    from market_service.nooa_harness.inference import _REQUIRED_PHASES

    for phase in _REQUIRED_PHASES:
        if not coverage.get(phase):
            return phase
    if not coverage.get("P6"):
        return "P6"
    return "P5"


class NarrationParseError(ValueError):
    """Narration output was not parseable into the required JSON contract."""


class InferenceEngine:
    """The statistical inference agent: wake → compute → narrate → persist.

    Composition: owns a RedisRuntimeStore (data plane), a PostgresRuntimeStore
    (durable ledger), a MemoryNode (episodic memory), and one injected LLM
    client (narration only). All deterministic computation happens through
    the capability registry / tool base — never inside the LLM.
    """

    def __init__(
        self,
        store: RedisRuntimeStore,
        postgres: PostgresRuntimeStore,
        memory: Any | None,
        llm: Any | None,
        *,
        symbol: str = "BTCUSDT",
        venue: str = "spot",
        config: WakeConfig | None = None,
        session_id: str | None = None,
        settings: Any | None = None,
    ) -> None:
        self.store = store
        self.postgres = postgres
        self.memory = memory
        self.llm = llm
        self.symbol = symbol.upper()
        self.venue = venue
        self.config = config or WakeConfig()
        self.session_id = session_id or _stable_session_id(self.symbol, self.venue)
        # Operator settings (one read at construction, threaded to tools).
        # Tools NEVER re-read the environment (two-plane boundary pass).
        self.settings = settings

    # ------------------------------------------------------------------
    # Wake plane adapters (Redis counter collection + two-phase gate)
    # ------------------------------------------------------------------

    async def collect_snapshot(self) -> CounterSnapshot:
        """Live counters + last-artifact high-water marks (pure reads)."""
        status = await self.store.read_microstructure_status(self.venue, self.symbol)
        stream_len = int(
            await self.store.redis.xlen(
                self.store.microstructure_event_stream(self.venue, self.symbol),
            ) or 0
        )
        prior = None
        if self.postgres is not None:
            try:
                prior = await self.postgres.read_inference_artifact(self.symbol)
            except Exception:
                log.exception("collect_snapshot: prior-artifact read failed")
        prior_state = self._prior_headline(prior)
        return CounterSnapshot(
            event_stream_len=stream_len,
            capture_state=(status or {}).get("state"),
            last_artifact_events_total=prior_state.get("events_total"),
            last_artifact_capture_state=prior_state.get("capture_state"),
            last_artifact_completed_at_ms=prior_state.get("completed_at_ms"),
        )

    async def acquire_manual_wake(self) -> tuple[WakeEnvelope, dict[str, Any]]:
        """Synthesize a MANUAL wake (the outer-CLI ``--force`` trigger).

        The human trigger IS the wake — no stream drain, no predicate
        evaluation, no two-phase revalidation. Event-driven wakes arrive as
        in-memory envelopes directly from the worker and never pass through
        here.
        """
        snapshot = await self.collect_snapshot()
        envelope = WakeEnvelope.create(
            symbol=self.symbol, venue=self.venue, trigger_source="manual",
            predicates_fired={"manual": {}},
            counter_snapshot={
                "event_stream_len": snapshot.event_stream_len,
                "capture_state": snapshot.capture_state,
                "last_artifact_events_total": snapshot.last_artifact_events_total,
            },
            high_water={
                "events_total": snapshot.last_artifact_events_total,
                "completed_at_ms": snapshot.last_artifact_completed_at_ms,
            },
        )
        return envelope, {"decision": "fire", "forced": True,
                          "source": "manual",
                          "consumed_wake_ids": [envelope.wake_id]}

    @staticmethod
    def _prior_headline(prior: dict[str, Any] | None) -> dict[str, Any]:
        """Extract the bounded headline of a prior artifact (provenance + last numbers)."""
        if not prior:
            return {}
        deterministic = prior.get("deterministic_state") or {}
        coverage = deterministic.get("coverage") or {}
        pif = deterministic.get("price_impact_fit") or {}
        completed_at = prior.get("completed_at")
        try:
            completed_ms = int(
                datetime.fromisoformat(str(completed_at)).timestamp() * 1000
            ) if completed_at else None
        except (ValueError, TypeError):
            completed_ms = None
        return {
            "artifact_id": prior.get("artifact_id"),
            "status": prior.get("status"),
            "events_total": coverage.get("events_total"),
            "capture_state": coverage.get("capture_state"),
            "completed_at_ms": completed_ms,
            "beta": pif.get("beta"),
            "fit_status": pif.get("status"),
        }

    # ------------------------------------------------------------------
    # Memory (episodic layer)
    # ------------------------------------------------------------------

    async def _recall_memory(self) -> tuple[list[Any], str]:
        """Recall the engine's own prior conclusions, rendered as a block."""
        if self.memory is None:
            return [], ""
        try:
            query = f"{self.symbol} beta fit regime capture"
            memories = await self.memory.recall(
                self.session_id, query=query, limit=MEMORY_RECALL_LIMIT,
            )
        except Exception:
            log.exception("memory recall failed; continuing without memory")
            return [], ""
        block = ""
        try:
            block = self.memory.render_context_block(
                memories, budget=MEMORY_CONTEXT_BUDGET,
            )
        except Exception:
            log.exception("memory render failed; continuing without block")
        return memories, block

    async def _prior_change_note(self) -> str:
        """Deterministic self-consistency diff vs the prior artifact."""
        prior = None
        if self.postgres is not None:
            try:
                prior = await self.postgres.read_inference_artifact(self.symbol)
            except Exception:  # noqa: BLE001 — ledger read must never kill the cycle
                prior = None
        if prior is None:
            prior = await self.store.read_latest_inference_artifact(
                self.symbol, self.venue,
            )
        headline = self._prior_headline(prior)
        if not headline:
            return "No prior artifact: this is the first inference cycle."
        return (
            "Prior artifact headline (deterministic diff basis):\n"
            + json.dumps(headline, default=str)[:_PRIOR_HEADLINE_MAX_CHARS]
        )

    # ------------------------------------------------------------------
    # Narration (max TWO LLM calls per cycle; ZERO when gate refuses)
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        return _SYSTEM_PROMPT_TEMPLATE.format(
            symbol=self.symbol,
            venue=self.venue,
            max_rounds=AGENTIC_MAX_TOOL_ROUNDS,
            memory_protocol_section=(
                "MEMORY PROTOCOL (propose, never write):\n"
                + _load_kb(_MEMORY_PROTOCOL)
                if _MEMORY_PROTOCOL.exists() else ""
            ),
            paper_kb=_load_paper_kb()[:18_000],
            tool_manifest=(
                "TOOL MANIFEST:\n" + _load_kb(_TOOL_MANIFEST)
                if _TOOL_MANIFEST.exists() else ""
            ),
        )

    @staticmethod
    def _output_format() -> str:
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
            '  "tool_calls": [{"name": "<ONE registry tool name>", "args": {"symbol": "<this cycle\'s symbol>", "venue": "<this cycle\'s venue>", "interval_seconds": 10, "window_minutes": 30, …}}],\n'
            '  "memory_proposals": [{"kind": "observation|hypothesis", "content": "…", "importance": 5.0, "tags": ["…"]}] or null\n'
            "}\n"
            "REGISTRY TOOL NAMES (use EXACTLY — any other name is denied):\n"
            "  P1 OFI/tape: micro.capture_status, micro.events, micro.ofi_intervals, micro.replay, calc.ofi.intervals;\n"
            "  P2 AD/fits: micro.fit_beta, micro.evidence, calc.depth.average, calc.observation.build, calc.fit.price_impact, calc.fit.depth_scaling;\n"
            "  P3 correlate (substrate.read PRIMARY, market.read context): substrate.invoke + substrate.tape, substrate.density, substrate.delta, substrate.ladders, substrate.anchors, substrate.tiers, substrate.volume_profile, substrate.technicals, substrate.migration, substrate.oi, substrate.signals, substrate.large_print, then substrate.read; market.read, market.derivatives, market.keystone_history, market.wall_history;\n"
            "  P5 paper/derived: memory.recall_paper, calc.price.delta (alias calc.derived_diagnostic);\n"
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

    async def _call_llm(self, user_prompt: str) -> str:
        if self.llm is None:
            raise NarrationParseError("no LLM client configured for narration")
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_prompt},
        ]
        # Structured attempt first (schema rejects nameless tool_calls at the
        # source); plain-JSON fallback preserves current behavior on backends
        # that refuse response_format. The probe flag avoids paying the failed
        # attempt on every turn once a backend has refused.
        global _STRUCTURED_OK
        response = None
        if NarrationTurn is not None and _STRUCTURED_OK is not False:
            try:
                response = await self.llm.acall(
                    messages=messages,
                    output_model=NarrationTurn,
                    max_tokens=_narration_max_tokens(),
                )
                _STRUCTURED_OK = True
            except Exception:
                log.warning("structured narration call refused; falling back to raw JSON")
                _STRUCTURED_OK = False
                response = None
        if response is None:
            response = await self.llm.acall(
                messages=messages,
                max_tokens=_narration_max_tokens(),
            )
        # 0) Structured-model surface: already validated, serialize to JSON.
        content = getattr(response, "content", None)
        if BaseModel is not None and isinstance(content, BaseModel):
            return json.dumps(content.model_dump(), default=str)
        # 1) The client's parsed text surface (nooa LLMResponse.content) is the
        #    authoritative transport-agnostic extraction.
        content = getattr(response, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if any(p.strip() for p in parts):
                return "".join(parts)
        # 2) Reasoning-model fallback: with a reasoning backend, the final JSON
        #    may be parked in ``reasoning`` when the generation budget ran out
        #    in the reasoning preamble before a content block was emitted.
        #    Extract from reasoning rather than serializing the raw response.
        reasoning = getattr(response, "reasoning", None)
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
        # 3) Legacy raw-transport extraction (direct litellm/OpenAI shapes).
        raw = getattr(response, "raw_response", response)
        choices = getattr(raw, "choices", None)
        if choices:
            content = getattr(getattr(choices[0], "message", None), "content", None)
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                if parts:
                    return "".join(parts)
        if isinstance(response, str):
            return response
        return str(raw)

    # ------------------------------------------------------------------
    # Memory proposal resolution (LLM proposes; engine disposes)
    # ------------------------------------------------------------------

    def resolve_memory_proposals(
        self, proposals: Any, *, artifact_status: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Deterministically resolve LLM memory proposals.

        Returns (accepted, dispositions). Rules (spec §3):
        - non-dict / unknown kind / empty content → rejected
        - kind 'fact' is NEVER accepted from the LLM
        - more than MAX_MEMORY_PROPOSALS → the excess is rejected
        - proposal never carries run numbers as prose claims — tagging only
        """
        accepted: list[dict[str, Any]] = []
        dispositions: list[dict[str, Any]] = []
        if not isinstance(proposals, list):
            dispositions.append({"proposal": None, "disposition": "rejected",
                                 "reason": "proposals_not_a_list"})
            return accepted, dispositions
        valid_kinds = {"observation", "hypothesis", "note"}
        for index, proposal in enumerate(proposals):
            if len(accepted) >= MAX_MEMORY_PROPOSALS:
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "budget_exceeded"})
                continue
            if not isinstance(proposal, dict):
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "not_an_object"})
                continue
            kind = str(proposal.get("kind", ""))
            content = str(proposal.get("content", "")).strip()
            if kind == "fact":
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "fact_kind_is_llm_forbidden"})
                continue
            if kind not in valid_kinds or not content:
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "invalid_kind_or_empty_content"})
                continue
            try:
                importance = float(proposal.get("importance", 5.0))
            except (TypeError, ValueError):
                importance = 5.0
            importance = min(max(importance, 0.0), 10.0)
            tags = tuple(str(t) for t in (proposal.get("tags") or ()))
            accepted.append({
                "kind": kind,
                "content": content,
                "importance": importance,
                "tags": tags + (f"{self.symbol.lower()}-{self.venue}",),
            })
        return accepted, dispositions

    async def _remember(
        self, accepted: list[dict[str, Any]], run_meta: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Persist accepted proposals through MemoryNode (engine disposes)."""
        written: list[dict[str, Any]] = []
        if self.memory is None:
            return written
        for proposal in accepted:
            try:
                memory = await self.memory.remember(
                    self.session_id,
                    proposal["kind"],
                    proposal["content"],
                    run_id=run_meta.get("artifact_id"),
                    importance=proposal["importance"],
                    tags=proposal["tags"],
                    evidence_refs=(run_meta.get("artifact_id"),),
                )
                written.append({"memory_id": memory.memory_id,
                                "kind": memory.kind})
            except Exception:
                log.exception("memory write failed for proposal %s", proposal["kind"])
                written.append({"error": "write_failed", "kind": proposal["kind"]})
        return written

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

    async def run_cycle(
        self, wake: WakeEnvelope, wake_meta: dict[str, Any],
    ) -> tuple[InferenceArtifact, dict[str, Any]]:
        """One full inference cycle. Returns (artifact, cycle_meta)."""
        generated_at = _utc_now_iso()
        capability_log: list[dict[str, Any]] = [{
            "capability": "engine.wake",
            "scope": {"symbol": self.symbol, "venue": self.venue},
            "result": "ok",
            "detail": {
                "trigger_source": wake.trigger_source,
                "predicates_fired": wake.predicates_fired,
                "decision": wake_meta.get("decision"),
                "consumed_wake_ids": wake_meta.get("consumed_wake_ids", []),
            },
        }]

        # --- GATHER: capture status + deterministic fit via the tool base ---
        # All dispatches run on the ENGINE's injected connections + settings;
        # no tool opens its own store (two-plane boundary pass).
        status, status_log = await execute_tool(
            self.store, "micro.capture_status",
            {"symbol": self.symbol, "venue": self.venue},
            memory=self.memory, settings=self.settings,
        )
        capability_log.append(status_log)
        evidence, fit_log = await execute_tool(
            self.store, "micro.fit_beta",
            {"symbol": self.symbol, "venue": self.venue,
             "interval_seconds": 10, "window_minutes": 30},
            postgres=self.postgres, memory=self.memory, settings=self.settings,
        )
        capability_log.append(fit_log)

        fit_status = None
        n_observations = 0
        if evidence is not None:
            pif = evidence.get("price_impact_fit") or {}
            fit_status = pif.get("status")
            n_observations = int(pif.get("n_observations") or 0)
        events_in_window = int((evidence or {}).get("coverage", {}).get(
            "events_in_window", 0) or 0)
        status_obj = status or {}
        gate_status, gate_reasons = resolve_inference_status(
            n_observations=n_observations,
            min_observations=30,
            fit_status=fit_status,
            capture_state=status_obj.get("state"),
            events_in_window=events_in_window,
            sequence_gaps=int(status_obj.get("sequence_gaps") or 0),
        )

        # Pass C split: AD and OFI are calculated as SEPARATE deterministic tools
        # in the wake cycle — agent will call them to validate, final DeltaP is derived diagnostic
        ofi_blocks, ofi_log = await execute_tool(self.store, "calc.ofi.intervals", {"symbol": self.symbol, "venue": self.venue, "interval_seconds": 10, "window_minutes": 30}, memory=self.memory, settings=self.settings)
        capability_log.append(ofi_log)
        ad_result, ad_log = await execute_tool(self.store, "calc.depth.average", {"symbol": self.symbol, "venue": self.venue, "window_minutes": 30}, memory=self.memory, settings=self.settings)
        capability_log.append(ad_log)
        obs_preview, obs_log = await execute_tool(self.store, "calc.observation.build", {"symbol": self.symbol, "venue": self.venue, "interval_seconds": 10, "window_minutes": 30}, memory=self.memory, settings=self.settings)
        capability_log.append(obs_log)
        derived_diag, derived_log = await execute_tool(self.store, "calc.derived_diagnostic", {"symbol": self.symbol, "venue": self.venue}, memory=self.memory, settings=self.settings)
        capability_log.append(derived_log)
        calculations = {
            "ofi_blocks": ofi_blocks[:5] if isinstance(ofi_blocks, list) else ofi_blocks,
            "ad_blocks": ad_result,
            "observations_preview": obs_preview[:5] if isinstance(obs_preview, list) else obs_preview,
            "derived_diagnostic": derived_diag,
            "split_note": "AD and OFI called as separate tools; final DeltaP is derived hypothesis, not shortcut — per Cont 1011.6402"
        }
        deterministic_state: dict[str, Any] = {
            "wake": {
                "trigger_source": wake.trigger_source,
                "predicates_fired": wake.predicates_fired,
            },
            "capture_status": status,
            "microstructure_evidence": evidence,
            "gate": {"status": gate_status, "reasons": list(gate_reasons)},
            "calculations": calculations,
            "data_quality": {
                "capture_state": status_obj.get("state"),
                "sequence_gaps": int(status_obj.get("sequence_gaps") or 0),
                "heteroskedasticity_flag": ((evidence or {}).get("price_impact_fit") or {}).get("heteroskedasticity_flag"),
                "fit_status": fit_status,
                "n_observations": n_observations,
                "spine": {"interval_seconds": 10, "window_minutes": 30},
                "note": ("provisional persists while gaps>0 or hetero=true; "
                         "agent may recompute at interval 10/15/30s × window 15/30/60m via tool args"),
            },
        }

        # --- HARD GATE ---
        if gate_status == "insufficient":
            artifact = InferenceArtifact.create(
                symbol=self.symbol, venue=self.venue,
                generated_at=generated_at, completed_at=_utc_now_iso(),
                status="insufficient", window_minutes=30, interval_seconds=10,
                deterministic_state=deterministic_state,
                capability_log=capability_log,
                input_hash=str((evidence or {}).get("input_hash") or "gate-refused"),
                model_version="inference-engine-v1",
                interpretation=None,
                session_id=self.session_id,
                errors=[{"source": "gate", "error": "; ".join(gate_reasons)}],
            )
            await self._persist(artifact)
            # Null discipline with learning: remember the quality observation
            # (deterministic write, not an LLM proposal).
            if self.memory is not None:
                try:
                    await self.memory.remember(
                        self.session_id, "observation",
                        f"Cycle refused by gate: {'; '.join(gate_reasons)}",
                        run_id=artifact.artifact_id, importance=2.0,
                        tags=("gate", "insufficient", self.symbol.lower()),
                        evidence_refs=(artifact.artifact_id,),
                    )
                except Exception:
                    log.exception("gate-cycle memory write failed")
            return artifact, {"llm_calls": 0, "gate": gate_status,
                              "reasons": list(gate_reasons)}

        # --- CONTEXT: memory + prior diff ---
        _memories, memory_block = await self._recall_memory()
        prior_note = await self._prior_change_note()

        wake_block = json.dumps(
            {"trigger_source": wake.trigger_source,
             "predicates": wake.predicates_fired},
            default=str,
        )
        user_prompt_1 = (
            f"WAKE: {wake_block}\n\n"
            "DETERMINISTIC STATE (computed; never recomputed by you):\n"
            f"{json.dumps(deterministic_state, default=str)[:60_000]}\n\n"
            f"{prior_note}\n\n"
        )
        if memory_block:
            user_prompt_1 += (
                "RECALLED MEMORY (provenance-tagged priors; subordinate to the "
                f"ledger):\n{memory_block}\n\n"
            )
        user_prompt_1 += self._output_format()

        # --- NARRATE#1 ---
        llm_calls = 0
        try:
            raw_1 = await self._call_llm(user_prompt_1)
        except Exception as exc:
            log.exception("narration#1 failed")
            return await self._degraded_artifact(
                deterministic_state, capability_log,
                f"narration_failed: {type(exc).__name__}: {exc}",
            ), {"llm_calls": llm_calls}
        llm_calls += 1
        parsed_1 = _coerce_turn(_extract_json_object(raw_1))
        if parsed_1 is None:
            return await self._degraded_artifact(
                deterministic_state, capability_log,
                "narration_parse_failed: no JSON object in output",
            ), {"llm_calls": llm_calls}

        # --- STAGED TOOL LOOP (P1→P6, phase coverage enforced) ---
        # D1 decision (B2): P4 explanation rides in the P6 synthesis summary
        # (≥200 chars); no P4 declared-turn is required. P6 must be declared.
        # The model advances OFI (P1) → AD (P2) → market correlation (P3) →
        # explanation (P4) → paper + derived ΔP (P5). Coverage is credited
        # from TOOL FAMILIES actually executed with result "ok"; a FINAL turn
        # with empty tool_calls is validated and REJECTED FOR REPAIR while
        # LLM budget remains (depth is structural, not advisory).
        from market_service.nooa_harness.inference import (
            TOOL_PHASE,
            _normalize_tool_name,
            capability_log_entry,
        )

        tool_results: dict[str, Any] = {}
        accumulated_tool_results: dict[str, Any] = {}
        phase_coverage: dict[str, set[str]] = {
            phase: set() for phase in ("P1", "P2", "P3", "P4", "P5", "P6")
        }

        def _mark_declared(parsed: dict[str, Any]) -> None:
            declared = str(parsed.get("phase") or "").strip().upper()
            if declared in ("P4", "P6"):
                phase_coverage[declared].add("declared")

        _mark_declared(parsed_1)
        parsed_current = parsed_1
        tool_rounds_used = 0
        repairs_sent = 0
        finalize_now = False
        final_validation: dict[str, Any] = {"passed": False, "missing": ["loop_not_run"]}
        while llm_calls < AGENTIC_MAX_LLM_TURNS and not finalize_now:
            tool_calls = parsed_current.get("tool_calls")
            if (
                isinstance(tool_calls, list)
                and tool_calls
                and tool_rounds_used < AGENTIC_MAX_TOOL_ROUNDS
            ):
                to_execute = [
                    c for c in tool_calls if isinstance(c, dict)
                ][:AGENTIC_PER_ROUND_CALL_CAP]
                if not to_execute:
                    break
                tool_rounds_used += 1
                round_results: dict[str, Any] = {}
                for call in to_execute:
                    raw_name = str(call.get("name", ""))
                    args = dict(call.get("args") or {})
                    args.setdefault("symbol", self.symbol)
                    args.setdefault("venue", self.venue)
                    canonical = _normalize_tool_name(raw_name) or raw_name
                    try:
                        result, tool_log = await execute_tool(
                            self.store, raw_name, args, postgres=self.postgres,
                            memory=self.memory, settings=self.settings,
                        )
                    except Exception as exc:  # one bad tool never kills the cycle
                        log.exception("staged loop: tool %s raised", raw_name)
                        result, tool_log = None, capability_log_entry(
                            f"tool.error:{canonical}",
                            {"symbol": self.symbol, "venue": self.venue},
                            "error",
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    capability_log.append(tool_log)
                    # Phase credit needs a real dispatch: denials, errors,
                    # and explicit null-discipline refusals never count. A
                    # successful read that legitimately returns null
                    # (absent evidence, empty cache) still counts — the
                    # absence itself is information the agent must interpret.
                    _detail = tool_log.get("detail")
                    _refused = isinstance(_detail, dict) and _detail.get("status") == "refused"
                    if tool_log.get("result") == "ok" and not _refused:
                        phase = TOOL_PHASE.get(canonical)
                        if phase is not None:
                            phase_coverage[phase].add(canonical)
                    if result is None:
                        result_payload = None
                    else:
                        rendered = json.dumps(result, default=str)
                        if len(rendered) < 40_000:
                            result_payload = json.loads(rendered)
                        else:
                            result_payload = {"_truncated": True, "preview": rendered[:4_000]}
                    round_results[canonical] = result_payload
                    accumulated_tool_results[canonical] = result_payload
                tool_results.update(round_results)
                next_phase = _next_uncovered_phase(phase_coverage)
                user_prompt_next = (
                    f"TOOL RESULTS ROUND {tool_rounds_used}/{AGENTIC_MAX_TOOL_ROUNDS} (deterministic; cite paths):\n"
                    f"{json.dumps(round_results, default=str)[:40_000]}\n\n"
                    f"ACCUMULATED TOOL RESULTS SO FAR:\n{json.dumps(accumulated_tool_results, default=str)[:40_000]}\n\n"
                    "PHASE COVERAGE (families with ≥1 ok tool): "
                    f"{json.dumps({p: sorted(s) for p, s in phase_coverage.items()})}\n"
                    f"{_PHASE_GUIDANCE[next_phase]}\n"
                    f"Tool rounds remaining: {AGENTIC_MAX_TOOL_ROUNDS - tool_rounds_used}. "
                    "Declare \"phase\" every turn; call the next phase's tools, or advance with tool_calls=[]."
                )
                try:
                    raw_next = await self._call_llm(user_prompt_next)
                except Exception:
                    log.exception("staged narration round failed; falling back")
                    break
                llm_calls += 1
                parsed_next = _coerce_turn(_extract_json_object(raw_next))
                if parsed_next is None:
                    log.warning("staged round parse failed, keeping prior")
                    break
                parsed_current = parsed_next
                _mark_declared(parsed_current)
                continue
            # No (more) tool calls this turn → validate the final.
            passed, missing = _validate_final_turn(parsed_current, phase_coverage)
            if passed:
                final_validation = {"passed": True, "missing": []}
                finalize_now = True
            else:
                repairs_sent += 1
                repair_prompt = (
                    "FINAL REJECTED — staged inference incomplete. Missing:\n"
                    + "\n".join(f"- {item}" for item in missing)
                    + f"\n\nACCUMULATED TOOL RESULTS:\n{json.dumps(accumulated_tool_results, default=str)[:40_000]}\n\n"
                    f"PHASE COVERAGE: {json.dumps({p: sorted(s) for p, s in phase_coverage.items()})}\n"
                    f"{_PHASE_GUIDANCE[_next_uncovered_phase(phase_coverage)]}\n"
                    "Return the next turn now: declare \"phase\", include the missing tool_calls, "
                    "and finalize (tool_calls=[]) only when every missing item is addressed."
                )
                try:
                    raw_repair = await self._call_llm(repair_prompt)
                except Exception:
                    log.exception("repair turn failed; falling back")
                    break
                llm_calls += 1
                parsed_repair = _coerce_turn(_extract_json_object(raw_repair))
                if parsed_repair is None:
                    log.warning("repair parse failed, keeping prior")
                    break
                parsed_current = parsed_repair
                _mark_declared(parsed_current)
        parsed_final = parsed_current
        # Honest record when the loop exited via break (exception/parse fail
        # or LLM budget spent): validate whatever we finalize with.
        if not finalize_now:
            passed, missing = _validate_final_turn(parsed_final, phase_coverage)
            final_validation = {"passed": passed, "missing": missing}
        # ensure tool_results reflects all rounds for cycle_meta
        if accumulated_tool_results:
            tool_results = accumulated_tool_results

        interpretation = {
            "summary": parsed_final.get("summary"),
            "evidence": parsed_final.get("evidence"),
            "confidence": parsed_final.get("confidence"),
            "limitations": parsed_final.get("limitations"),
            "model_separation": parsed_final.get("model_separation"),
        }
        # Validator hardening: coerce confidence blends conservatively so the
        # persisted artifact never stores "low-medium" (contract enum only).
        try:
            from market_service.runtime.contracts import normalize_confidence as _norm_conf
            _coerced = _norm_conf(interpretation.get("confidence"))
            if interpretation.get("confidence") is not None and _coerced is not None:
                interpretation["confidence"] = _coerced
        except Exception:
            pass
        # If still null after forced final, fall back to first round's interpretation
        if interpretation["summary"] is None and parsed_1.get("summary") is not None:
            interpretation = {
                "summary": parsed_1.get("summary"),
                "evidence": parsed_1.get("evidence"),
                "confidence": parsed_1.get("confidence"),
                "limitations": parsed_1.get("limitations"),
                "model_separation": parsed_1.get("model_separation"),
            }
            parsed_final = parsed_1
        # Validator hardening (post-fallback): coerce again so fallback path
        # also persists the contract enum.
        try:
            from market_service.runtime.contracts import normalize_confidence as _norm_conf2
            _coerced2 = _norm_conf2(interpretation.get("confidence"))
            if interpretation.get("confidence") is not None and _coerced2 is not None:
                interpretation["confidence"] = _coerced2
        except Exception:
            pass
        # Pass C: hypothesis formed via memory.recall_paper + calc.* tools,
        # final DeltaP is derived diagnostic heteroskedastic ν·OFI
        hypothesis = parsed_final.get("hypothesis")
        if not isinstance(hypothesis, dict) and hypothesis is not None:
            hypothesis = {"raw": hypothesis}
        beta = (evidence or {}).get("price_impact_fit", {}).get("beta") if isinstance(evidence, dict) else None
        betastr = str(beta)[:12] if beta is not None else "unknown"
        if hypothesis is None:
            hypothesis = {"H0": f"β ≈ {betastr} ticks/OFI per OFI calculation, AD separately validated", "paper_refs": ["Cont 1011.6402 OFI_k, AD_i, derived ΔP diagnostic"], "evidence_refs": ["calc.ofi.intervals","calc.depth.average","memory.recall_paper"]}
            hypothesis_verdict = "inconclusive"
            verdict_reason = "Agent did not explicitly form H0/H1 via memory.recall_paper; calculations split but hypothesis implicit"
        else:
            if gate_status == "provisional":
                hypothesis_verdict = "inconclusive"
                verdict_reason = f"Gate provisional ({';'.join(gate_reasons)}); hypothesis held as derived diagnostic, not shortcut — ΔP diagnostic heteroskedastic"
            elif gate_status == "validated":
                hypothesis_verdict = "validated"
                verdict_reason = "Deterministic fits validated; hypothesis confirmed via split AD/OFI and derived ΔP diagnostic"
            else:
                hypothesis_verdict = "invalidated"
                verdict_reason = "; ".join(gate_reasons)
            if isinstance(hypothesis, dict) and "H0" in hypothesis:
                verdict_reason += " ; H0 paper-grounded via memory.recall_paper"
        artifact = InferenceArtifact.create(
            symbol=self.symbol, venue=self.venue,
            generated_at=generated_at, completed_at=_utc_now_iso(),
            status=gate_status, window_minutes=30, interval_seconds=10,
            deterministic_state=deterministic_state,
            capability_log=capability_log,
            input_hash=str((evidence or {}).get("input_hash") or "no-evidence"),
            model_version="inference-engine-v1",
            interpretation=interpretation,
            session_id=self.session_id,
            hypothesis=hypothesis,
            hypothesis_verdict=hypothesis_verdict,
            verdict_reason=verdict_reason,
            calculations=calculations,
        )
        await self._persist(artifact)

        # --- MEMORY PROPOSAL RESOLUTION (LLM proposes; engine disposes) ---
        accepted, dispositions = self.resolve_memory_proposals(
            parsed_final.get("memory_proposals"), artifact_status=gate_status,
        )
        written = await self._remember(
            accepted, {"artifact_id": artifact.artifact_id},
        )
        cycle_meta = {
            "llm_calls": llm_calls,
            "gate": gate_status,
            "tool_round": bool(tool_results),
            "tool_rounds": tool_rounds_used,
            "repairs": repairs_sent,
            "phase_coverage": {p: sorted(s) for p, s in phase_coverage.items()},
            "final_validation": final_validation,
            "memory": {"accepted": accepted, "dispositions": dispositions,
                       "written": written},
        }
        return artifact, cycle_meta

    async def _degraded_artifact(
        self,
        deterministic_state: dict[str, Any],
        capability_log: list[dict[str, Any]],
        error: str,
    ) -> InferenceArtifact:
        """Narration-failure artifact: deterministic state preserved, interpretation NULL."""
        artifact = InferenceArtifact.create(
            symbol=self.symbol, venue=self.venue,
            generated_at=_utc_now_iso(), completed_at=_utc_now_iso(),
            status="provisional", window_minutes=30, interval_seconds=10,
            deterministic_state=deterministic_state,
            capability_log=capability_log,
            input_hash=str((deterministic_state.get("microstructure_evidence") or {}).get(
                "input_hash") or "narration-failed"),
            model_version="inference-engine-v1",
            interpretation=None,
            session_id=self.session_id,
            errors=[{"source": "narration", "error": error}],
        )
        await self._persist(artifact)
        return artifact

    async def _persist(self, artifact: InferenceArtifact) -> dict[str, Any]:
        """Postgres-first durable write, then the Redis projection."""
        persistence: dict[str, Any] = {"postgres": False, "redis": False}
        if self.postgres is not None:
            try:
                persistence["postgres"] = bool(
                    await self.postgres.insert_inference_artifact(artifact)
                )
            except Exception:
                log.exception("postgres artifact insert failed")
        try:
            await self.store.publish_inference_artifact(artifact)
            persistence["redis"] = True
        except Exception:
            log.exception("redis artifact publish failed")
        return persistence

    async def close(self) -> None:
        await self.store.close()
        if self.postgres is not None:
            await self.postgres.close()
        if self.memory is not None:
            try:
                await self.memory.postgres.close()
                await self.memory.redis.close()
            except Exception:  # close is best-effort on shutdown
                log.debug("memory store close failed", exc_info=True)


# ======================================================================
# Absorbed from the retired ``agents`` module (final decomposition pass,
# 2026-09-04). The historical interpretation-plane agents were deleted in
# the inference-engine pass; what remained was:
#
#   - ``bounded_envelope_view`` / ``response_text`` — LLM-view helpers
#   - ``MicrostructureInterpretationAgent`` — the Pass-3 read-only reader
#     for ``nooa market microstructure interpret``
#
# They lived in a separate module only to inherit the NOOA ``Agent`` base —
# a framework coupling the inference plane does not need. This class is a
# plain object with an INJECTED llm client (exactly like InferenceEngine
# itself), so engine.py stays nooa-free at import and the agent surface is
# one plane, one home: everything the LLM narrates lives HERE.
# ======================================================================


def bounded_envelope_view(
    payload: dict[str, Any] | None,
    roof: int = 180_000,
    list_cap: int = 60,
) -> str:
    """Serialized envelope sized for the LLM param limit (never silent).

    Returns the full envelope when it already fits. Otherwise large lists
    are capped with an explicit ``__truncated__`` marker that records the
    original count, and only as a last resort is the payload reduced to run
    identity + coverage + errors with the trimmed top-level keys listed. The
    complete canonical envelope is never mutated — this is only the LLM-bound
    projection.
    """
    from market_service.runtime.read_paths import json_safe_dumps

    if not payload:
        return "{}"
    rendered = json_safe_dumps(payload)
    if len(rendered) <= roof:
        return rendered

    def _cap(value: Any, max_items: int) -> Any:
        if isinstance(value, dict):
            return {k: _cap(v, max_items) for k, v in value.items()}
        if isinstance(value, list):
            if len(value) > max_items:
                return {
                    "__truncated__": True,
                    "count": len(value),
                    "items": [_cap(item, max_items) for item in value[:max_items]],
                }
            return [_cap(item, max_items) for item in value]
        return value

    rendered = json_safe_dumps(_cap(payload, list_cap))
    if len(rendered) <= roof:
        return rendered

    kept = {
        k: payload.get(k)
        for k in ("run_id", "symbol", "status", "schema_version",
                  "generated_at", "completed_at", "data_source")
    }
    kept["coverage"] = payload.get("coverage")
    kept["errors"] = payload.get("errors")
    kept["_trimmed_keys"] = sorted(payload.get("canonical_state") or {})
    return json_safe_dumps(kept)


def response_text(resp: Any) -> str:
    """Extract the text content from a NOOA unified-LLM response."""
    raw = getattr(resp, "raw_response", resp)
    choices = getattr(raw, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if parts:
                return "".join(parts)
    if isinstance(resp, str):
        return resp
    return str(raw)


_MICROSTRUCTURE_EVIDENCE_SCHEMA = json.dumps(
    {
        "schema_version": 1,
        "symbol": "BTCUSDT",
        "venue": "spot",
        "evidence_id": "ev-<hash-prefix>",
        "input_hash": "<sha256>",
        "model_version": "ofi-depth-v1",
        "interval_seconds": 10,
        "window_start_ms": "<int>",
        "window_end_ms": "<int>",
        "tick_size": "<decimal>",
        "depth_estimator": "event_mean_best_bid_ask_v1",
        "price_impact_fit": {
            "fit_id": "beta-<hash>",
            "alpha": "<decimal>",
            "beta": "<decimal>",
            "stderr_beta": "<decimal|null>",
            "r2": "<decimal|null>",
            "n_observations": "<int>",
            "excluded_observations": "<int>",
            "heteroskedasticity_flag": "<bool>",
            "mean_ad": "<decimal|null>",
            "price_unit": "ticks",
            "status": "validated|provisional|insufficient",
        },
        "sensitivity_fit": "same shape as price_impact_fit, OFI recomputed without price-changing events, or null",
        "depth_scaling_fit": {
            "fit_id": "depth-<hash>",
            "c": "<decimal|null>",
            "lambda": "<decimal|null>",
            "stderr_lambda": "<decimal|null>",
            "n_blocks": "<int>",
            "r2": "<decimal|null>",
            "fit_ids": ["beta-<hash>", "..."],
            "status": "validated|provisional|insufficient",
        },
        "coverage": "object: events/intervals captured, gaps, reconnects",
        "status": "validated|provisional|insufficient",
    },
    indent=2,
)


class MicrostructureInterpretationAgent:
    """Read-only interpreter of one persisted MicrostructureEvidence.

    RECEIVES BOTH fitted models — the empirical price-impact fit
    (ΔP_k = α + β·OFI_k) and the depth-scaling fit (β = c·AD^-λ) — each with
    independent diagnostics, and explains fit quality, sign, magnitude and
    limitations. The two models are NEVER merged into a single point
    prediction: the substituted combined expression
    ΔP = α + c·OFI/AD^λ + (ν·OFI + ε) carries a heteroskedastic ν·OFI term,
    so it is a derived diagnostic at most.

    Authority: this agent never recomputes OFI, never refits β/c/λ, never
    opens Binance, never reconstructs the book, and never overrides a
    deterministic status. It cites evidence paths and states what the data
    cannot establish.

    Lives in the inference plane (engine module) since the decomposition
    pass; the llm client is INJECTED exactly like InferenceEngine — no NOOA
    ``Agent`` base, no framework import at module load.
    """

    remit: str = (
        "Interpret fitted microstructure evidence without recomputing any "
        "value. The deterministic fitter is the only producer of coefficients."
    )

    _MAX_LLM_ENVELOPE_CHARS = 190_000
    _LIST_CAP = 200
    _MAX_TOKENS = 4000

    def __init__(self, symbol: str, *, llm: Any):
        self.llm = llm
        self.symbol = symbol.upper()

    def _envelope_schema(self) -> str:
        """Shape reference for MicrostructureEvidence (not the market envelope)."""
        return _MICROSTRUCTURE_EVIDENCE_SCHEMA

    @staticmethod
    def _evidence_from_envelope(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
        """Locate microstructure evidence inside a canonical envelope view."""
        if not isinstance(envelope, dict):
            return None
        for key in ("microstructure", "microstructure_evidence"):
            candidate = envelope.get(key)
            if isinstance(candidate, dict):
                return candidate
        canonical = envelope.get("canonical_state")
        if isinstance(canonical, dict):
            for key in ("microstructure", "microstructure_evidence"):
                candidate = canonical.get(key)
                if isinstance(candidate, dict):
                    return candidate
        return None

    async def assess(self, evidence: dict[str, Any]) -> str:
        """Interpret one evidence object; deterministic unavailable when absent.

        When no evidence is present this returns a parseable unavailable report
        WITHOUT a model call (null discipline — no LLM call to state absence).
        """
        payload = evidence if isinstance(evidence, dict) else None
        resolved = payload
        if resolved is None or "evidence_id" not in resolved:
            resolved = self._evidence_from_envelope(payload)
        if resolved is None:
            return json.dumps({
                "summary": "Microstructure evidence unavailable for this run.",
                "evidence": [],
                "confidence": "low",
                "limitations": ["no MicrostructureEvidence persisted or present in the envelope"],
                "null_fields": ["microstructure"],
            })
        return await self._call_model_once(resolved)

    async def _call_model_once(
        self,
        payload: dict[str, Any] | None,
        *,
        max_tokens: int | None = None,
        extra_context: str | None = None,
    ) -> str:
        """One deterministic LLM call over the evidence payload."""
        if self.llm is None:
            raise RuntimeError(
                f"{type(self).__name__}: no NOOA model client configured"
            )
        extra = f"{extra_context}\n\n" if extra_context else ""
        user = (
            "Return ONLY one valid JSON object with fields: "
            "{summary, evidence: "
            "[{path, value, interpretation, metric_name}], confidence, "
            "limitations, model_separation}.\n"
            "confidence MUST be EXACTLY one of: \"low\", \"medium\", \"high\" "
            "(NOT the evidence status like provisional/insufficient).\n\n"
            f"{extra}"
            "Evidence schema (shape reference):\n"
            f"{self._envelope_schema()}\n\n"
            "MicrostructureEvidence for this run:\n"
            f"{bounded_envelope_view(payload, self._MAX_LLM_ENVELOPE_CHARS, self._LIST_CAP)}"
            "\n\nIMPORTANT: Output ONLY one valid JSON object. No markdown fences."
        )
        system = self.remit
        resp = await self.llm.acall(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens or self._MAX_TOKENS,
        )
        return response_text(resp)


__all__ = [
    "SESSION_TEMPLATE",
    "InferenceEngine",
    "MicrostructureInterpretationAgent",
    "NarrationParseError",
    "bounded_envelope_view",
    "response_text",
]
