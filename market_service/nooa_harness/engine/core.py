"""InferenceEngine — the cycle: wake → gather → gate → narrate → persist.

Semantic authority: THE RUNTIME LOOP. This module orchestrates the
deterministic compute + LLM narration + persistence of one inference
cycle. It DELEGATES to sibling modules for every non-orchestration concern:

  - ``kb``     — system prompt + output format templates
  - ``llm``    — LLM transport + content extraction
  - ``narration`` — turn parsing, validator, scenario verdict, phase guidance
  - ``schemas`` — NarrationParseError
  - ``config`` — budgets, session template, KB paths, env
  - ``inference.*`` — tool dispatch + capability registry + gate

The ``InferenceEngine`` class is the only class in this module. Its
``run_cycle`` body is the canonical cycle: GATHER → HARD GATE → MEMORY
→ NARRATE#1 → STAGED TOOL LOOP → VERDICT → PERSIST → MEMORY PROPOSALS.

The memory-proposal resolution (LLM proposes, engine disposes) lives
here because it threads through the cycle's persistence step — it is
part of the cycle, not a separable concern.

Methods that are now thin wrappers over sibling modules (kept as
methods so the existing call surface is preserved):

  - ``_system_prompt()``   → ``kb.build_system_prompt``
  - ``_output_format()``   → ``kb.build_output_format`` (static)
  - ``_call_llm(prompt)``  → ``llm.call_narration_llm`` with the
                             engine's injected llm client + system prompt
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from market_service.nooa_harness.inference import (
    execute_tool,
    resolve_inference_status,
)
from market_service.runtime.contracts import InferenceArtifact, WakeEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from . import narration as narration_mod
from .config import (
    AGENTIC_MAX_LLM_TURNS,
    AGENTIC_MAX_TOOL_ROUNDS,
    AGENTIC_PER_ROUND_CALL_CAP,
    MEMORY_CONTEXT_BUDGET,
    MEMORY_RECALL_LIMIT,
    MAX_MEMORY_PROPOSALS,
    _PRIOR_HEADLINE_MAX_CHARS,
)
from .kb import SystemPromptCache, TURN_CONTRACT_LINE, build_output_format
from .llm import call_narration_llm
from .schemas import NarrationParseError

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
        session_id: str | None = None,
        settings: Any | None = None,
    ) -> None:
        self.store = store
        self.postgres = postgres
        self.memory = memory
        self.llm = llm
        self.symbol = symbol.upper()
        self.venue = venue
        self.session_id = session_id or _stable_session_id(self.symbol, self.venue)
        # Cycle-scoped system prompt: rendered lazily on first narration
        # call, then reused for every turn in every cycle of this engine.
        # The KB payload is constant for (symbol, venue, budgets), which
        # never change on a constructed engine — so the cache lives for the
        # engine's lifetime, not per run_cycle call.
        self._system_prompt_cache = SystemPromptCache(
            self.symbol, self.venue, AGENTIC_MAX_TOOL_ROUNDS,
        )
        # Operator settings (one read at construction, threaded to tools).
        # Tools NEVER re-read the environment (two-plane boundary pass).
        self.settings = settings

    # ------------------------------------------------------------------
    # Invocation adapters (Redis counter collection for the manual wake)
    # ------------------------------------------------------------------

    async def collect_snapshot(self) -> dict[str, Any]:
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
        return {
            "event_stream_len": stream_len,
            "capture_state": (status or {}).get("state"),
            "last_artifact_events_total": prior_state.get("events_total"),
            "last_artifact_capture_state": prior_state.get("capture_state"),
            "last_artifact_completed_at_ms": prior_state.get("completed_at_ms"),
        }

    async def acquire_manual_wake(self, task: str | None = None) -> tuple[WakeEnvelope, dict[str, Any]]:
        """Synthesize a MANUAL wake (the CLI ``--force`` trigger — the only trigger).

        The human trigger IS the invocation — no predicate evaluation, no
        loop, no worker. A directly-injected envelope (caller-constructed)
        bypasses this entirely and goes straight to ``run_cycle``.

        ``task`` is the interactive-plane directive (trade hypothesis prompt
        from ``harness.py --task``). It is carried on the manual predicates
        (truncated preview) so the firing is attributable, and threaded
        separately into ``run_cycle`` for full prompt steering.
        """
        snapshot = await self.collect_snapshot()
        predicates: dict[str, Any] = {"manual": {}}
        if task:
            predicates["manual"] = {"task_preview": task[:200]}
        envelope = WakeEnvelope.create(
            symbol=self.symbol, venue=self.venue, trigger_source="manual",
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
    # Narration (delegates to sibling modules)
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        """Build the per-cycle system prompt (delegates to ``kb``)."""
        return build_system_prompt(
            self.symbol, self.venue, AGENTIC_MAX_TOOL_ROUNDS,
        )

    @staticmethod
    def _output_format() -> str:
        """The JSON turn contract + registry tool names + staged workflow.

        Re-sent on every follow-up turn so the model always has the
        registry keys + the staged workflow in view. Delegates to ``kb``.
        """
        return build_output_format()

    async def _call_llm(self, user_prompt: str) -> str:
        """One LLM call over the cycle-scoped system prompt.

        The system prompt is rendered ONCE per cycle (``SystemPromptCache``)
        and reused for every narration turn — the KB payload is
        cycle-constant, so per-turn re-reads were pure waste (up to 12 KB
        disk reads + formatting per cycle, and the same ~34 KB payload
        re-sent as message content regardless).
        """
        return await call_narration_llm(
            self.llm, user_prompt,
            system_prompt=self._system_prompt_cache.get(),
        )

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
        task: str | None = None,
        scenario: dict[str, Any] | None = None,
    ) -> tuple[InferenceArtifact, dict[str, Any]]:
        """One full inference cycle. Returns (artifact, cycle_meta).

        ``task`` is the interactive-plane directive: a free-text trade
        hypothesis / question from the harness caller (``harness.py --task``
        or ``nooa market inference run --task``). It steers narration — the
        TASK block opens user_prompt_1 and is repeated on follow-up/repair
        turns so every phase answers it — and is persisted on
        ``deterministic_state["task"]`` plus the wake capability detail.
        ``None`` preserves the legacy autonomous behaviour (agent frames its
        own generic H0/H1, as seen in pre-task artifacts).
        ``scenario`` is ``{"target_price": str, "horizon": "15m|1h|4h"}``
        (CLI ``--target/--horizon``): the 'can price hit X?' level, persisted
        on ``deterministic_state["scenario"]`` and echoed in the prompt so
        Phase 3 semantics can evaluate it via ``calc.scenario.evaluate``.
        """
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
                "task": task[:200] if task else None,
                "scenario": scenario,
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
            "task": task,
            "scenario": scenario,
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
            return artifact, {"task": task[:200] if task else None,
                              "scenario": scenario,
                              "llm_calls": 0, "gate": gate_status,
                              "reasons": list(gate_reasons)}

        # --- CONTEXT: memory + prior diff ---
        _memories, memory_block = await self._recall_memory()
        prior_note = await self._prior_change_note()

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
        user_prompt_1 = (
            f"{task_block}"
            f"{scenario_block}"
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
        parsed_1 = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_1))
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
        task_reminder = (
            f"TASK reminder (answer this): {task[:500]}\n"
            if task else ""
        )
        scenario_reminder = (
            f"SCENARIO reminder (evaluate with calc.scenario.evaluate at the "
            f"given horizon; frame H0/H1 as not-reachable/reachable): "
            f"{json.dumps(scenario)}\n"
            if scenario else ""
        )
        phase_guidance = dict(narration_mod.PHASE_GUIDANCE)
        if scenario:
            phase_guidance["P5"] = (
                phase_guidance["P5"]
                + f" SCENARIO GIVEN ({json.dumps(scenario)}): call "
                "calc.scenario.evaluate with that target_price/horizon IN ADDITION "
                "to recall_paper + price.delta — the scenario verdict is the primary output. "
                "The final scenario block must echo fit_status + n_windows_usable + r2 "
                "and its rationale (≥80 chars) must name them beside the verdict."
            )
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
                next_phase = narration_mod.next_uncovered_phase(phase_coverage)
                # Follow-up prompt economy: the prior round's results arrive
                # in ACCUMULATED only (they are the delta the model has not
                # seen as fresh data) — no separate ROUND block re-sending
                # the same payload twice — plus the one-line turn-contract
                # reminder instead of re-rendering the full output format.
                user_prompt_next = (
                    f"{task_reminder}"
                    f"{scenario_reminder}"
                    f"TOOL RESULTS (round {tool_rounds_used}/{AGENTIC_MAX_TOOL_ROUNDS} dispatched; cite paths):\n"
                    f"{json.dumps(round_results, default=str)[:40_000]}\n\n"
                    f"PRIOR ROUNDS (earlier results, for citation):\n"
                    f"{json.dumps({k: v for k, v in accumulated_tool_results.items() if k not in round_results}, default=str)[:40_000]}\n\n"
                    "PHASE COVERAGE (families with ≥1 ok tool): "
                    f"{json.dumps({p: sorted(s) for p, s in phase_coverage.items()})}\n"
                    f"{phase_guidance[next_phase]}\n"
                    f"{TURN_CONTRACT_LINE}\n"
                    f"Tool rounds remaining: {AGENTIC_MAX_TOOL_ROUNDS - tool_rounds_used}. "
                    "Declare \"phase\" every turn; call the next phase's tools, or advance with tool_calls=[]."
                )
                try:
                    raw_next = await self._call_llm(user_prompt_next)
                except Exception:
                    log.exception("staged narration round failed; falling back")
                    break
                llm_calls += 1
                parsed_next = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_next))
                if parsed_next is None:
                    log.warning("staged round parse failed, keeping prior")
                    break
                parsed_current = parsed_next
                _mark_declared(parsed_current)
                continue
            # No (more) tool calls this turn → validate the final.
            passed, missing = narration_mod.validate_final_turn(
                parsed_current, phase_coverage, scenario=scenario)
            if passed:
                final_validation = {"passed": True, "missing": []}
                finalize_now = True
            else:
                repairs_sent += 1
                scenario_steer = ""
                if scenario and "calc.scenario.evaluate" not in accumulated_tool_results:
                    scenario_steer = (
                        "SCENARIO UNEVALUATED: call calc.scenario.evaluate with the "
                        f"SCENARIO target_price/horizon ({json.dumps(scenario)}) "
                        "before finalizing — the final is rejected without its "
                        "→ … evidence root.\n"
                    )
                repair_prompt = (
                    f"{task_reminder if task else ''}"
                    f"{scenario_reminder if scenario else ''}"
                    f"{scenario_steer}"
                    "FINAL REJECTED — staged inference incomplete. Missing:\n"
                    + "\n".join(f"- {item}" for item in missing)
                    + f"\n\nACCUMULATED TOOL RESULTS:\n{json.dumps(accumulated_tool_results, default=str)[:40_000]}\n\n"
                    f"PHASE COVERAGE: {json.dumps({p: sorted(s) for p, s in phase_coverage.items()})}\n"
                    f"{phase_guidance[narration_mod.next_uncovered_phase(phase_coverage)]}\n"
                    f"{TURN_CONTRACT_LINE}\n"
                    "Return the next turn now: declare \"phase\", include the missing tool_calls, "
                    "and finalize (tool_calls=[]) only when every missing item is addressed."
                )
                try:
                    raw_repair = await self._call_llm(repair_prompt)
                except Exception:
                    log.exception("repair turn failed; falling back")
                    break
                llm_calls += 1
                parsed_repair = narration_mod.coerce_turn(narration_mod.extract_json_object(raw_repair))
                if parsed_repair is None:
                    log.warning("repair parse failed, keeping prior")
                    break
                parsed_current = parsed_repair
                _mark_declared(parsed_current)
        parsed_final = parsed_current
        # Budget-exhaustion transparency (live-proven): a final turn may carry
        # tool_calls that never dispatched because the LLM/round budget was
        # spent. Name them on cycle_meta so the artifact is self-documenting
        # about work the agent requested but never received.
        unexecuted: list[str] = []
        if not finalize_now:
            pending = parsed_final.get("tool_calls")
            if isinstance(pending, list):
                for call in pending:
                    if isinstance(call, dict):
                        raw_name = str(call.get("name") or call.get("tool") or "").strip()
                        if raw_name:
                            unexecuted.append(raw_name)
            if unexecuted:
                log.warning("cycle finalized with undispatched tool calls: %s", unexecuted)
        # Honest record when the loop exited via break (exception/parse fail
        # or LLM budget spent): validate whatever we finalize with.
        if not finalize_now:
            passed, missing = narration_mod.validate_final_turn(
                parsed_final, phase_coverage, scenario=scenario)
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
        # Scenario block rides the interpretation JSON (no migration): the agent's
        # verdict/probability/rationale over the deterministic requirement.
        if scenario is not None and isinstance(parsed_final.get("scenario"), dict):
            interpretation["scenario"] = parsed_final["scenario"]
        # Pass C: hypothesis formed via memory.recall_paper + calc.* tools,
        # final DeltaP is derived diagnostic heteroskedastic ν·OFI.
        # Scenario cycles ALWAYS take their verdict from the deterministic
        # tool payload — even when the agent formed no H0/H1 (live-proven:
        # budget exhaustion can strand tool calls in an undispatched final
        # turn, and the verdict must not depend on narration surviving).
        scenario_verdict: tuple[str, str] | None = None
        if scenario is not None:
            scenario_verdict = narration_mod.scenario_verdict(
                tool_results.get("calc.scenario.evaluate"), capability_log,
                gate_status, list(gate_reasons),
            )
        hypothesis = parsed_final.get("hypothesis")
        if not isinstance(hypothesis, dict) and hypothesis is not None:
            hypothesis = {"raw": hypothesis}
        beta = (evidence or {}).get("price_impact_fit", {}).get("beta") if isinstance(evidence, dict) else None
        betastr = str(beta)[:12] if beta is not None else "unknown"
        if hypothesis is None:
            hypothesis = {"H0": f"β ≈ {betastr} ticks/OFI per OFI calculation, AD separately validated", "paper_refs": ["Cont 1011.6402 OFI_k, AD_i, derived ΔP diagnostic"], "evidence_refs": ["calc.ofi.intervals","calc.depth.average","memory.recall_paper"]}
            if scenario_verdict is not None:
                hypothesis_verdict, verdict_reason = (
                    scenario_verdict[0],
                    scenario_verdict[1] + " ; agent formed no H0/H1 this cycle",
                )
            else:
                hypothesis_verdict = "inconclusive"
                verdict_reason = "Agent did not explicitly form H0/H1 via memory.recall_paper; calculations split but hypothesis implicit"
        elif scenario_verdict is not None:
            # Scenario cycles: reachability verdict computed deterministically
            # from the accumulated scenario tool payload — never from LLM text.
            hypothesis_verdict, verdict_reason = scenario_verdict
            if isinstance(hypothesis, dict) and hypothesis.get("H0") \
                    and hypothesis.get("H1"):
                verdict_reason += " ; H0/H1 reachability pair via calc.scenario.evaluate"
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
            "task": task[:200] if task else None,
            "scenario": scenario,
            "unexecuted_tool_calls": unexecuted,
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


__all__ = ["InferenceEngine"]
