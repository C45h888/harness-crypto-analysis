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

import inspect
import json
import logging
from datetime import datetime
from typing import Any, Mapping

from market_service.runtime.contracts import (
    CanonicalOutput,
    WakeEnvelope,
    run_record_of,
)
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
from ..gates import evaluate_budget_gate
from ..cadence import INTERVAL_SECONDS, WINDOW_MINUTES
from ..ledger import ledger_rows, render_ledger


def _cadence_arg_refusals(canonical: str, args: dict[str, Any]) -> list[str]:
    """Validate cadence arguments against the frozen vocabulary (pure).

    Phase G6: the FSM authorizes the TOOL; the cadence ARGUMENTS are
    checked against engine.cadence's frozen sets before anything executes.
    Only keys the tool actually passed are checked (defaults are the
    executor's business, and every default is already in-vocabulary).
    ``horizon_ms`` is horizon-charter territory (validated by the tool
    bodies with the supported-horizons refusal) and is NOT re-checked here.
    """
    refusals: list[str] = []
    if "interval_seconds" in args:
        value = args["interval_seconds"]
        if value not in INTERVAL_SECONDS:
            refusals.append(
                f"interval_seconds {value!r} outside {list(INTERVAL_SECONDS)}"
            )
    if "window_minutes" in args:
        value = args["window_minutes"]
        if value not in WINDOW_MINUTES:
            refusals.append(
                f"window_minutes {value!r} outside {list(WINDOW_MINUTES)}"
            )
    return refusals


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
from ..llm import call_narration_llm, call_narration_llm_streaming, NarrationStalledError, narration_failure_kind, streaming_enabled
from ..scenario_contract import render_status_block
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
        answer_sink: Any | None = None,
    ) -> None:
        self.store = store
        self.postgres = postgres
        self.memory = memory
        self.llm = llm
        self.symbol = symbol.upper()
        self.venue = venue
        self.session_id = session_id or _stable_session_id(self.symbol, self.venue)
        # C2 delivery sink: where THE answer is delivered at PLACEMENT
        # (CLI channel / interaction plane). None = headless; the host
        # surface picks the answer up from the cycle meta.
        self.answer_sink = answer_sink
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
                prior = await self.postgres.read_canonical_output(self.symbol)
            except Exception:
                log.exception("collect_snapshot: prior-record read failed")
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
        """Extract the bounded headline of a prior run (provenance + last numbers)."""
        if not prior:
            return {}
        deterministic = run_record_of(prior).get("deterministic_state") or {}
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
            "run_id": prior.get("run_id") or prior.get("artifact_id"),
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

    async def _recall_ledger(
        self, *, segment: str | None = None,
    ) -> tuple[list[Any], str]:
        """Read the engine's own prior CENTRAL LEDGER as a context block.

        The central ledger is the ONE home for engine memory. Cross-cycle, the
        prior canonical record carries it (run_record.deterministic_state);
        this reads it by key (symbol/venue), never by similarity or scoring.
        Returns ``(rows, rendered block)``.
        """
        prior = None
        if self.postgres is not None:
            try:
                prior = await self.postgres.read_canonical_output(self.symbol)
            except Exception:  # noqa: BLE001 — ledger read must never kill the cycle
                prior = None
        if prior is None:
            try:
                prior = await self.store.read_latest_canonical_output(
                    self.symbol, self.venue,
                )
            except Exception:  # noqa: BLE001
                prior = None
        if prior is None:
            return [], ""
        if isinstance(prior, Mapping):
            ds = run_record_of(prior).get("deterministic_state")
        else:
            run_record = getattr(prior, "run_record", None)
            if isinstance(run_record, Mapping):
                ds = run_record.get("deterministic_state")
            else:
                ds = getattr(prior, "deterministic_state", None)
        if not isinstance(ds, Mapping):
            return [], ""
        rows = ledger_rows(ds, segment=segment)
        if not rows:
            return [], ""
        return rows, render_ledger(
            ds, segment=segment, budget=MEMORY_CONTEXT_BUDGET,
        )

    async def _prior_change_note(self) -> str:
        """Deterministic self-consistency diff vs the prior run record."""
        prior = None
        if self.postgres is not None:
            try:
                prior = await self.postgres.read_canonical_output(self.symbol)
            except Exception:  # noqa: BLE001 — ledger read must never kill the cycle
                prior = None
        if prior is None:
            prior = await self.store.read_latest_canonical_output(
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

        Signal B: when streaming is enabled, the turn goes over SSE with
        cadence supervision (heartbeats + progressive parse); stall
        verdicts surface as ``NarrationStalledError`` carrying cadence
        evidence. Streaming unavailability (``RuntimeError``) falls back
        to the buffered call — supervision degrades, the turn does not.
        """
        if streaming_enabled():
            try:
                return await call_narration_llm_streaming(
                    self.llm, user_prompt,
                    system_prompt=self._system_prompt_cache.get(),
                    engine=self,
                )
            except RuntimeError:
                log.debug("streaming unavailable; buffered narration fallback")
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
        dict_calls = [c for c in tool_calls if isinstance(c, dict)]
        # Malformed tool-call SHAPES are no longer silently dropped. Shapes that
        # carry a NAME are SALVAGED for the deterministic mirror (the TOOL
        # worker), which re-runs them through the authorized dispatch path.
        _dropped = [c for c in tool_calls if not isinstance(c, dict)]
        if _dropped:
            st.capability_log.append(capability_log_entry(
                "tool.parse_failed:shape",
                {"symbol": self.symbol, "venue": self.venue},
                "error",
                detail={"reason": "tool_call_not_an_object", "count": len(_dropped)},
            ))
        _malformed: list[dict[str, Any]] = []
        # Phase G4 (budget governance): the ceiling is adjudicated by the
        # BUDGET gate (gates.py — governance-owned verdict over config-owned
        # numbers), never decided inline. A refused gate is a finding that
        # names what will not run: everything pending moves to unexecuted
        # and the round ends — the loop continues its closing semantics.
        _budget = evaluate_budget_gate(
            tool_rounds_used=st.tool_rounds_used,
            dispatched_in_pass=st.dispatched_in_pass,
            max_tool_rounds=AGENTIC_MAX_TOOL_ROUNDS,
            max_dispatches_per_pass=MAX_DISPATCHES_PER_PASS,
        )
        if _budget.refused:
            for reason in _budget.reasons:
                log.warning("budget gate refused: %s", reason)
            for call in dict_calls:
                raw_name = str(call.get("name") or call.get("tool") or "").strip()
                if raw_name:
                    st.unexecuted.append(raw_name)
            return
        pass_remaining = MAX_DISPATCHES_PER_PASS - st.dispatched_in_pass
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
            try:
                args = dict(call.get("args") or {})
            except (TypeError, ValueError):
                # args was NOT an object (a string/list): the call cannot be
                # dispatched as-is. SALVAGE the intent (the name survives) so the
                # deterministic mirror can re-run it, and record the finding.
                if raw_name.strip():
                    _malformed.append({"name": raw_name, "args": {}})
                st.capability_log.append(capability_log_entry(
                    f"tool.parse_failed:{raw_name or '?'}",
                    {"symbol": self.symbol, "venue": self.venue},
                    "error", detail={"reason": "args_not_an_object"},
                ))
                continue
            args.setdefault("symbol", self.symbol)
            args.setdefault("venue", self.venue)
            canonical = _normalize_tool_name(raw_name) or raw_name
            # Phase G6 (cadence-arg governance): the FSM authorizes the
            # TOOL at its home; the cadence ARGUMENTS are validated here
            # against the frozen cadence vocabulary (engine.cadence) — the
            # single owner of interval/window domains. Out-of-vocabulary
            # values become a tool.denied-cadence finding: never
            # dispatched, never executed, never re-called blind. The
            # finding carries the supported set so the model can reform.
            _cadence_reasons = _cadence_arg_refusals(canonical, args)
            if _cadence_reasons:
                cadence_log = capability_log_entry(
                    f"tool.denied-cadence:{canonical}",
                    {"symbol": self.symbol, "venue": self.venue},
                    "denied",
                    detail={
                        "reason": "cadence_args_out_of_vocabulary",
                        "refusals": _cadence_reasons,
                        "supported": {
                            "interval_seconds": list(INTERVAL_SECONDS),
                            "window_minutes": list(WINDOW_MINUTES),
                        },
                    },
                )
                st.dispatched_in_pass += 1
                st.capability_log.append(cadence_log)
                if raw_name and raw_name not in st.unexecuted:
                    st.unexecuted.append(raw_name)
                round_results[canonical] = None
                continue
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
            # Phase 1 (staged ANALYSIS): DATA-READY preconditions gate the
            # refined stack PRE-DISPATCH (after position: a later-position
            # call stays out_of_position, never premature — position owns
            # the track, data owns readiness within it). A premature refined
            # call becomes a tool.premature finding: named needs, never
            # executed, never burning a tool round, controller NOT_CALLED.
            if st.loop_tag == "reasoning":
                try:
                    from .chain import check_tool_preconditions as _check_pre
                    _pre_ok, _pre_needs = _check_pre(
                        canonical, st.accumulated_tool_results)
                except Exception:
                    _pre_ok, _pre_needs = True, []
                if not _pre_ok:
                    pre_log = capability_log_entry(
                        f"tool.premature:{canonical}",
                        {"symbol": self.symbol, "venue": self.venue},
                        "denied",
                        detail={"reason": "data_not_ready",
                                "needs": list(_pre_needs)},
                    )
                    st.dispatched_in_pass += 1
                    st.capability_log.append(pre_log)
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
            # Controller authority: the directive scenario's target/horizon
            # fill ABSENT scenario args before dispatch (carry_scenario) so
            # the loop cannot pass validation without evaluating the asked
            # target. Explicit args always win; the carry is logged as a
            # finding. Redundancy below sees effective args.
            args, _carry = st.controller.carry_scenario(canonical, args)
            if _carry is not None:
                st.capability_log.append(capability_log_entry(
                    "engine.scenario.carry",
                    {"symbol": self.symbol, "venue": self.venue},
                    "ok",
                    detail=dict(_carry),
                ))
                # Model-visible counterpart: the next prompt renders this
                # state (the capability_log is operator-plane only). Bounded
                # list — one carry row per dispatch, last 8 kept.
                st.scenario_surface.append(dict(_carry))
                del st.scenario_surface[:-8]
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
                    session_id=self.session_id,
                    segment=getattr(ctx, "task_id", None),
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
            # Digest-fallback (failure substrate, bounded once per cycle):
            # a failed 2b forecast call reveals the receipt digest SUMMARY
            # (Y_pred/regime/status only — never the full payload) so the
            # reformulation has numbers to aim at. Proof still requires a
            # successful agent re-call; the digest never satisfies the chain.
            if (st.loop_tag == "reasoning"
                    and canonical == "calc.forward.forecast"
                    and not getattr(st, "digest_revealed", False)):
                _failed = (result is None) or (tool_log.get("result") != "ok")
                try:
                    _det = tool_log.get("detail")
                    _failed = _failed or (isinstance(_det, dict)
                                           and _det.get("status") == "refused")
                except Exception:
                    pass
                if _failed:
                    try:
                        _digest = (st.deterministic_state.get("forecast_result")
                                 if isinstance(st.deterministic_state, dict) else None)
                        if isinstance(_digest, dict):
                            _ft = _digest.get("forward_target") or {}
                            _fb = {
                                "y_pred": _ft.get("y_pred") if isinstance(_ft, dict) else None,
                                "validation_state": _digest.get("validation_state"),
                                "note": ("receipt digest fallback (bounded once): reform "
                                         "calc.forward.forecast with different args; "
                                         "this summary is not proof"),
                            }
                            st.accumulated_tool_results["forecast.digest_fallback"] = _fb
                            st.capability_log.append(capability_log_entry(
                                "engine.forecast.digest_fallback",
                                {"symbol": self.symbol, "venue": self.venue},
                                "ok",
                                detail={"bounded": "once_per_cycle",
                                        "y_pred": _fb["y_pred"]},
                            ))
                    except Exception:
                        pass
                    st.digest_revealed = True
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
        # Deterministic output mirror: a malformed intended call is SALVAGED and
        # re-dispatched through the failure node (the TOOL worker mirrors it via
        # the authorized path), so its result lands in the outcome ledger. The
        # `_mirroring` guard stops the mirrored round from re-salvaging itself.
        if _malformed and not getattr(st, "_mirroring", False):
            st.tool_intent = {"tool_calls": _malformed}  # type: ignore[attr-defined]
            st.failure_kind = "tool_parse_failed"
            st.failure_detail = f"{len(_malformed)} malformed tool call(s)"
            # None ⇒ resolved (mirror ran, results present); else escalation,
            # and the caller's `if st.failure_kind: …terminate` writes it once.
            await context.recover_tool_failure(self, ctx, st)

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
            # The TRACK block above already renders chain status
            # (done/missing/refused) — chain_block carries only the
            # actionable steer, never a second status render.
            chain_block = ""
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
                # Slice B: hypothesize renders the deterministic H0/H1
                # frame (prompt variable × actual data) so the LLM's
                # hypothesis stays in line with the distribution it got.
                if active_position == "hypothesize":
                    try:
                        from .chain import build_hypothesis_frame as _bhf
                        from .chain import hypothesis_steer as _hs
                        _frame = _bhf(
                            ctx.task,
                            st.task_directive
                            if isinstance(getattr(st, "task_directive", None), dict)
                            else (st.task_plan or {}).get("directive"),
                            st.accumulated_tool_results,
                        )
                        chain_block += f"\n{_hs(_frame)}\n"
                    except Exception:
                        pass
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
        # Reasoning first stages refer to the partition writes: the entry
        # block rides the early reasoning passes (position 0 / first two
        # passes) so agents act on what previous sub-loops concluded.
        # Phase 2: ids persist for ALL reasoning passes (full block early
        # only) so 2b still cites 2a/partition conclusions without repaying
        # the full-text budget.
        _entry_block = ""
        if st.loop_tag == "reasoning":
            _entry_block = getattr(st, "reasoning_ledger_block", "") or ""
            if st.passes_per_loop.get("reasoning", 0) > 1:
                _entry_block = ""
            _stage_ids = list(getattr(st, "ledger_stage_ids", 
) or [])
            _entry_ids = list(getattr(st, "reasoning_ledger_ids", 
) or [])
            _all_ids = [i for i in (_entry_ids + _stage_ids) if i]
            if not _entry_block and _all_ids:
                _entry_block = (
                    "Prior stage conclusions (cite these ids; full text "
                    "was shown on early passes): "
                    + ", ".join(_all_ids)
                )
        # Signal B shrink-retry: the composer is a closure over caps so a
        # stalled_thinking turn can be recomposed tighter (halved history)
        # instead of repeating the oversized prompt blindly. Defaults carry
        # the production widths.
        def _compose_followup(round_cap: int = 32_000,
                              history_cap: int = 10_000) -> str:
            return kb.compose_followup_prompt(
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
            scenario_contract_block=render_status_block(
                st.scenario_contract, st.controller),
            phase_guidance=st.phase_guidance,
            next_phase=next_phase,
            chain_block=chain_block,
            reasoning_entry_block=_entry_block,
            round_cap=round_cap,
            history_cap=history_cap,
        )

        user_prompt_next = _compose_followup()
        try:
            raw_next = await self._call_llm(user_prompt_next)
            context.record_tier(st)
        except Exception as exc:
            log.exception("pass narration round failed; dispatching to the failure node")
            # Cause-carrying kinds: Signal B stall verdicts route as their
            # stall kind (classified transient/narration); everything else
            # keeps the legacy narration_failed kind. A stalled_thinking
            # turn retries against a shrunk recomposition (halved caps) —
            # the retry attacks the proven killer (prompt out-thinking the
            # clock) instead of repeating it. All other stalls retry the
            # same prompt; budgets stay substrate-owned.
            stall_kind = narration_failure_kind(exc)
            st.failure_kind = stall_kind
            st.failure_detail = f"{type(exc).__name__}: {exc}"
            retry_prompt = user_prompt_next
            if (isinstance(exc, NarrationStalledError)
                    and exc.verdict == "stalled_thinking"):
                retry_prompt = _compose_followup(16_000, 5_000)
                st.failure_detail += " (retrying shrunk: round 16K/history 5K)"
            # Bounded RETRY remediation: the substrate dispatches the narration
            # worker, which re-attempts the LLM call (the worker owns transport).
            # On escalation the failure kind stays set, so the caller's existing
            # `if st.failure_kind: return run_runtime_failure(...)` writes the
            # terminal exactly once.
            raw_next = await context.recover_narration_call(
                self, ctx, st, retry_prompt,
            )
            if raw_next is None:
                return None
            context.record_tier(st)
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
    # The cycle
    # ------------------------------------------------------------------

    async def narrate_cycle(
        self, wake: WakeEnvelope, wake_meta: dict[str, Any],
        task: str | None = None,
        scenario: dict[str, Any] | None = None,
    ) -> tuple[CanonicalOutput, dict[str, Any]]:
        """One narration cycle — READ-PLANE, envelope-driven.

        Pure forward — the outer loop lives in ``core.runner.narrate_cycle``.
        This handle owns no order, no stages, no denial handling. See the
        runner for the ``task``/``scenario`` contract.
        """
        return await runner.narrate_cycle(self, wake, wake_meta, task, scenario)

    async def _deliver_answer(self, answer_text: str) -> None:
        """Deliver THE answer to the interaction-plane sink (C2 PLACEMENT).

        Delivery is CONFIRMED when this call returns without error — the
        deliverable reaching its surface is what SETTLE gates on. An
        injected ``answer_sink`` transports (CLI channel, interaction
        plane); the headless default lets the host surface pick the answer
        up from the cycle meta.
        """
        sink = self.answer_sink
        if sink is None:
            return
        result = sink(answer_text)
        if inspect.isawaitable(result):
            await result

    async def _degraded_output(
        self,
        deterministic_state: dict[str, Any],
        capability_log: list[dict[str, Any]],
        error: str,
    ) -> CanonicalOutput:
        """Pure forward — implementation lives in ``core.runner``."""
        return await runner.degraded_output(
            self, deterministic_state, capability_log, error)

    async def _degraded_artifact(
        self,
        deterministic_state: dict[str, Any],
        capability_log: list[dict[str, Any]],
        error: str,
    ) -> CanonicalOutput:
        """Deprecated alias of ``_degraded_output`` (legacy artifact name).

        Kept only because a parallel workstream still calls the legacy name
        from ``core.reasoning``; retire it when that lands.
        """
        return await self._degraded_output(
            deterministic_state, capability_log, error)

    async def _place_output(self, output: CanonicalOutput) -> dict[str, Any]:
        """Pure forward — implementation lives in ``core.runner``."""
        return await runner.place_output(self, output)

    async def _finalize_output(
        self, output: CanonicalOutput,
    ) -> dict[str, Any]:
        """Pure forward — implementation lives in ``core.runner``."""
        return await runner.finalize_output(self, output)

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
