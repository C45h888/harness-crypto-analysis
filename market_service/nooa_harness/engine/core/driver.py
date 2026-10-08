"""InferenceEngine — handle bag + transport (owns no order, no stages).

The outer loop lives in ``core.runner`` (stage order, denial membrane,
durable writes). Loop bodies live in ``core.wake`` / ``gather`` /
``reasoning`` / ``output``. Legality lives in ``engine.fsm`` /
``engine.controller``. This class holds engine state (symbol, venue,
session, stores, memory, LLM client), transport primitives (``_call_llm``,
dispatch rounds, snapshot reads), lifecycle (``close``), and pure forwards
to the canonical owners. Enforced boundary: this module imports no loop
body module — only ``runner`` (outer loop) and ``wake`` (INTAKE forwards).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from market_service.runtime.contracts import InferenceArtifact, WakeEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from . import context
from .context import (
    CycleRuntimeState,
    _CycleContext,
    _stable_session_id,
    freeze_chain,
)
from ..controller import CycleController
from ..fsm import GOVERNANCE_MEMBRANE, GovernanceEvent, GovernanceEventKind
from . import runner
from . import wake as wake_mod
from .. import kb
from ..kb import (
    SystemPromptCache,
    build_output_format,
    build_system_prompt,
)
from ..schemas import coerce_turn, extract_json_object
from ..controller import next_uncovered_phase
from ..loop_states import (
    LOOP_TAG_CROSSWALK, NestedLoop, REGISTRY_HOME_LOOPS, SubLoop, TaskIntent,
)
from .chain import (
    FORWARD_SCENARIO_TOOL,
    POSITION_ORDER,
    POSITION_TOOLS,
    TOOL_POSITION,
    chain_completion,
    chain_steer,
    halt_steer,
    position_status,
    position_steer,
)
from ..principles import evaluate_principles, render_findings


def _derived_hypothesis_id(
    ctx: Any, st: CycleRuntimeState,
) -> tuple[str, str | None, str | None]:
    """Deterministic H0 pre-registration identity for ``calc.hypothesis.test``.

    The directive's ``hypothesis_seed`` (or, failing that, the task text) is
    the pre-registration record; the id is a stable digest of it — never
    random, never LLM-invented. Returns ``(hypothesis_id, h0, h1)``.
    """
    import hashlib

    seed = (st.task_directive or {}).get("hypothesis_seed") or {}
    h0 = str(seed.get("H0") or "").strip() or None
    h1 = str(seed.get("H1") or "").strip() or None
    basis = h0 or (ctx.task or "").strip() or "autonomous-H0"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
    return f"hyp-{digest}", h0, h1
from ..llm import call_narration_llm
from market_service.nooa_harness.inference import (
    _normalize_tool_name,
    capability_log_entry,
    tool_homes,
)
from ..config import (
    AGENTIC_MAX_LLM_TURNS,
    AGENTIC_MAX_TOOL_ROUNDS,
    LOOP_PASS_BUDGET,
    MAX_DISPATCHES_PER_PASS,
    MEMORY_CONTEXT_BUDGET,
    MEMORY_RECALL_LIMIT,
    MAX_MEMORY_PROPOSALS,
    _PRIOR_HEADLINE_MAX_CHARS,
)

log = logging.getLogger(__name__)


class InferenceEngine:
    """The statistical inference agent: wake → gather → comprehend → evidence → reasoning → validation → output.

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
        # engine's lifetime, not per narrate_cycle call.
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
        """Synthesize a MANUAL wake (the CLI ``--force`` trigger).

        Pure forward — canonical ownership lives in
        ``core.wake.build_manual_wake`` (INTAKE plane). This handle owns
        no logic.
        """
        return await wake_mod.build_manual_wake(self, task)

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

    async def _recall_memory(
        self, *, segment: str | None = None,
    ) -> tuple[list[Any], str]:
        """Recall the engine's own prior conclusions, rendered as a block.

        ``segment`` scopes recall to one task segment (state redistribution);
        ``None`` keeps legacy unsegmented recall.
        """
        if self.memory is None:
            return [], ""
        try:
            query = f"{self.symbol} beta fit regime capture"
            memories = await self.memory.recall(
                self.session_id, query=query, segment=segment,
                limit=MEMORY_RECALL_LIMIT,
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
    # Pass-loop mechanics
    # ------------------------------------------------------------------
    # These three methods are the bounded per-pass primitives every loop
    # body in ``reasoning.py`` re-uses. They live on the engine because
    # they consume engine state (``self.symbol``, ``self.venue``,
    # ``self.store``, etc.) and call ``self._call_llm`` — the engine *is*
    # the cycle transport, so the per-pass primitives belong here.

    async def _dispatch_turn(self, ctx: Any, st: CycleRuntimeState) -> None:
        """Dispatch ``st.parsed_current``'s tool_calls under the pass ceiling.

        Operates against the same per-round cap the agent's contract sees
        (MAX_DISPATCHES_PER_PASS); overflow is named on ``unexecuted``,
        never silently dropped. Each candidate is routed through the FSM
        (work authorization + redundancy suppression) and recorded back
        on the controller as a successor.
        """
        st._round_had_execution = False  # type: ignore[attr-defined]
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
            args.setdefault("symbol", self.symbol)
            args.setdefault("venue", self.venue)
            canonical = _normalize_tool_name(raw_name) or raw_name
            if canonical == "calc.hypothesis.test":
                # Pre-registration is ENGINE-SUPPLIED (deterministic): the
                # directive's hypothesis_seed (or the task text) IS the
                # pre-registration, hashed into a stable id. Without this the
                # tool's hypothesis_id precondition was unsatisfiable and H0
                # could never be tested. An explicit id still wins.
                _hid, _h0, _h1 = _derived_hypothesis_id(ctx, st)
                args.setdefault("hypothesis_id", _hid)
                if _h0:
                    args.setdefault("h0", _h0)
                if _h1:
                    args.setdefault("h1", _h1)
            # Hard-track positions (reasoning only): a tool belonging to a
            # LATER position than the active one is denied as an
            # out-of-position finding — logged only, never dispatched, never
            # classified (no controller outcome: no work was requested of
            # the deterministic plane, so there is nothing to classify and
            # the tool stays NOT_CALLED until its position is active).
            # Named on unexecuted: it is genuinely pending, not dropped.
            if (st.loop_tag == "reasoning" and st.chain_halt is None
                    and st.reason_position < len(POSITION_ORDER)):
                active_position = POSITION_ORDER[st.reason_position]
                wanted_position = TOOL_POSITION.get(canonical)
                if (wanted_position is not None
                        and POSITION_ORDER.index(wanted_position)
                        > POSITION_ORDER.index(active_position)):
                    oop_log = capability_log_entry(
                        f"tool.out_of_position:{canonical}",
                        {"symbol": self.symbol, "venue": self.venue},
                        "denied",
                        detail={"reason": "later_track_position",
                                "active_position": active_position,
                                "tool_position": wanted_position},
                    )
                    st.dispatched_in_pass += 1
                    st.capability_log.append(oop_log)
                    if raw_name and raw_name not in st.unexecuted:
                        st.unexecuted.append(raw_name)
                    round_results[canonical] = None
                    continue
            # Phase S-1 (docs/STATE_CHARTER_SPEC.md): ONE crosswalk in the
            # loop-identity owner (loop_states.LOOP_TAG_CROSSWALK) resolves
            # the runtime loop tag to its FSM (NestedLoop, SubLoop)
            # observation. The per-map silent default that authorized an
            # UNKNOWN tag as REASONING is REMOVED — an unknown loop tag is a
            # governance error (the orchestration membrane routes it), never
            # an invented authorization.
            try:
                expected_loop, expected_sub_loop = LOOP_TAG_CROSSWALK[st.loop_tag]
            except KeyError:
                raise context.GovernanceDenied(
                    f"unknown loop_tag {st.loop_tag!r}: no FSM crosswalk — "
                    "refusing to authorize work under an invented observation"
                ) from None
            # The dispatch registry owns the allowed work homes; the FSM owns
            # whether the current observation is one of them.  A model cannot
            # move a tool into a convenient phase by naming it differently.
            # Registry home names (comprehension/evidence) resolve to the
            # merged CONTEXT loop via the charter's REGISTRY_HOME_LOOPS table.
            authorization = None
            for home_loop, home_sub_loop in tool_homes(canonical):
                home_nested_loop = REGISTRY_HOME_LOOPS.get(home_loop)
                if home_nested_loop is None:
                    try:
                        home_nested_loop = NestedLoop(home_loop)
                    except ValueError:
                        continue
                try:
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
                    {"symbol": self.symbol, "venue": self.venue},
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
            # Controller authority: redundant re-dispatch of a tool with
            # IDENTICAL args that already refused this cycle is suppressed
            # structurally (logged, never executed). Reformulated args are
            # new work and flow through (budgets + predicates still gate).
            if st.controller.is_redundant(canonical, args):
                log.warning(
                    "pass loop: suppressing redundant dispatch of "
                    "%s (refused earlier this cycle)", canonical,
                )
                _supp_log = capability_log_entry(
                    f"tool.suppressed:{canonical}",
                    {"symbol": self.symbol, "venue": self.venue},
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
                    self.store, raw_name, args, postgres=self.postgres,
                    memory=self.memory, settings=self.settings,
                )
            except Exception as exc:  # one bad tool never kills the cycle
                log.exception("pass loop: tool %s raised", raw_name)
                result, tool_log = None, capability_log_entry(
                    f"tool.error:{canonical}",
                    {"symbol": self.symbol, "venue": self.venue},
                    "error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            st.dispatched_in_pass += 1
            st.capability_log.append(tool_log)
            # Hard-track hits: an executed in-position tool call counts
            # toward its position's ≥1-dispatch floor (denials and errors
            # never count — only real executions do).
            if (st.loop_tag == "reasoning" and tool_log.get("result") == "ok"
                    and st.reason_position < len(POSITION_ORDER)):
                active_position = POSITION_ORDER[st.reason_position]
                active_tools = set(POSITION_TOOLS.get(active_position, ()))
                if active_position == "hypothesize":
                    active_tools = active_tools | {FORWARD_SCENARIO_TOOL}
                if canonical in active_tools:
                    st.position_hits[active_position] = \
                        st.position_hits.get(active_position, 0) + 1
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

    async def _followup_turn(self, ctx: Any, st: CycleRuntimeState) -> dict[str, Any] | None:
        """One follow-up LLM turn for ``st.loop_tag``. Returns parsed or None.

        On failure the carrier is annotated with ``failure_kind`` /
        ``failure_detail`` and ``None`` is returned; callers route through
        ``context.run_runtime_failure``. On parse failure the carrier is
        similarly annotated.
        """
        coverage = st.controller.phase_coverage
        next_phase = next_uncovered_phase(coverage)
        # P1–P6 remain coverage/provenance labels only.  They do not retask the
        # FSM observation; the prompt below derives its actual intent from the
        # controller and uses the next phase only for evidence guidance.
        chain = (st.task_workflow or {}).get("chain") or []
        round_results = getattr(st, "_round_results", {})
        chain_block = ""
        if st.loop_tag == "reasoning":
            freeze_chain(st, ctx.task, ctx.scenario)
            st_chain = st.deterministic_state["statistical_chain"]
            st_render = chain_completion(
                st.controller, task=ctx.task, scenario=ctx.scenario,
            )
            st_chain["links"] = st_render["links"]
            st_chain["missing"] = st_render["missing"]
            st_chain["refused"] = st_render["refused"]
            st_chain["complete"] = st_render["complete"]
            chain_block = f"\n{st_render['render']}\n"
            steer = chain_steer(st_render)
            if steer:
                chain_block += f"{steer}\n"
            # Hard-track position: the agent's current third of the track
            # (core-computed from outcomes, never agent claims).
            # Halted track: render the reformulation steer instead of the
            # position block (positions are frozen; only the halted tool
            # with new args, or closing, moves the cycle).
            if st.chain_halt is not None:
                chain_block += f"\n{halt_steer(st.chain_halt)}\n"
            elif st.reason_position < len(POSITION_ORDER):
                active_position = POSITION_ORDER[st.reason_position]
                pstat = position_status(
                    st.controller.outcomes, active_position,
                    task=ctx.task, scenario=ctx.scenario)
                chain_block += (
                    f"\nREASONING POSITION {st.reason_position + 1}/3: "
                    f"{active_position}\n{position_steer(pstat)}\n"
                )
        # Principles (advisory guard, never denials): the frozen plan + the
        # just-dispatched turn render as steer findings on every follow-up.
        _pfindings = evaluate_principles(
            controller=st.controller, plan=st.task_plan,
            turn=st.parsed_current, reason_position=st.reason_position,
        )
        if _pfindings:
            chain_block = f"{chain_block}\n{render_findings(_pfindings)}"
        # Compose the follow-up prompt via the prompt-ecosystem composer so
        # follow-up layout stays in lock-step with the narrate-1 and repair
        # composers.
        user_prompt_next = kb.compose_followup_prompt(
            controller=st.controller,
            loop=("context" if st.loop_tag == "evidence" else st.loop_tag),
            sub_loop=("acquisition" if st.loop_tag == "evidence"
                      else "analysis" if st.loop_tag == "reasoning"
                      else "recovery"),
            passes_spent=st.passes_per_loop.get(st.loop_tag, 0),
            pass_budget=LOOP_PASS_BUDGET.get(st.loop_tag, 0),
            dispatches_left=MAX_DISPATCHES_PER_PASS - st.dispatched_in_pass,
            chain=chain,
            accumulated=st.accumulated_tool_results,
            round_results=round_results,
            task_reminder=st.task_reminder,
            scenario_reminder=st.scenario_reminder,
            phase_guidance=st.phase_guidance,
            next_phase=next_phase,
            chain_block=chain_block,
        )
        try:
            raw_next = await self._call_llm(user_prompt_next)
            context.record_tier(st)
        except Exception as exc:
            log.exception("pass narration round failed; ending loop early")
            st.failure_kind = "narration_failed"
            st.failure_detail = f"{type(exc).__name__}: {exc}"
            return None
        st.llm_calls += 1
        st.passes_per_loop[st.loop_tag] = st.passes_per_loop.get(st.loop_tag, 0) + 1
        parsed_next = coerce_turn(extract_json_object(raw_next))
        if parsed_next is None:
            log.warning("pass round parse failed, ending loop")
            st.failure_kind = "parse_failed"
            st.failure_detail = "no JSON object in follow-up narration"
            return None
        st.parsed_current = parsed_next
        st.turn_log.append(parsed_next)
        st.controller = context._mark_declared(st.controller, parsed_next)
        return parsed_next

    def _name_pending(self, st: CycleRuntimeState) -> None:
        """Name undispatched calls on a loop that ends with work pending."""
        pending = st.parsed_current.get("tool_calls")
        if isinstance(pending, list):
            for call in pending:
                if isinstance(call, dict):
                    raw_name = str(call.get("name") or call.get("tool") or "").strip()
                    if raw_name and raw_name not in st.unexecuted:
                        st.unexecuted.append(raw_name)

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

    async def narrate_cycle(
        self, wake: WakeEnvelope, wake_meta: dict[str, Any],
        task: str | None = None,
        scenario: dict[str, Any] | None = None,
    ) -> tuple[InferenceArtifact, dict[str, Any]]:
        """One narration cycle — READ-PLANE, envelope-driven.

        Pure forward — the outer loop lives in ``core.runner.narrate_cycle``.
        This handle owns no order, no stages, no denial handling. See the
        runner for the ``task``/``scenario`` contract.
        """
        return await runner.narrate_cycle(self, wake, wake_meta, task, scenario)

    async def _degraded_artifact(
        self,
        deterministic_state: dict[str, Any],
        capability_log: list[dict[str, Any]],
        error: str,
    ) -> InferenceArtifact:
        """Pure forward — implementation lives in ``core.runner``."""
        return await runner.degraded_artifact(
            self, deterministic_state, capability_log, error)

    async def _persist(self, artifact: InferenceArtifact) -> dict[str, Any]:
        """Pure forward — implementation lives in ``core.runner``."""
        return await runner.persist_artifact(self, artifact)

    async def _finalize_persisted(
        self, artifact: InferenceArtifact,
    ) -> dict[str, Any]:
        """Pure forward — implementation lives in ``core.runner``."""
        return await runner.finalize_persisted(self, artifact)

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
