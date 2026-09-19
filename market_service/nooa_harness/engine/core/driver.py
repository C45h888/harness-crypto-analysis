"""InferenceEngine — loop-walk runtime shell."""

from __future__ import annotations

import json
import logging
from typing import Any

from market_service.runtime.contracts import InferenceArtifact, WakeEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from . import context
from .context import _CycleContext, _stable_session_id
from ..controller import CycleController
from ..fsm import GOVERNANCE_MEMBRANE, GovernanceEvent, GovernanceEventKind
from . import gather, output, reasoning
from ..kb import SystemPromptCache, build_output_format, build_system_prompt
from ..llm import call_narration_llm
from ..config import (
    AGENTIC_MAX_TOOL_ROUNDS,
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
        """Synthesize a MANUAL wake (the CLI ``--force`` trigger — the only trigger).

        The human trigger IS the invocation — no predicate evaluation, no
        loop, no worker. A directly-injected envelope (caller-constructed)
        bypasses this entirely and goes straight to ``narrate_cycle``.

        ``task`` is the interactive-plane directive (trade hypothesis prompt
        from ``harness.py --task``). It is carried on the manual predicates
        (truncated preview) so the firing is attributable, and threaded
        separately into ``narrate_cycle`` for full prompt steering.
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

    async def narrate_cycle(
        self, wake: WakeEnvelope, wake_meta: dict[str, Any],
        task: str | None = None,
        scenario: dict[str, Any] | None = None,
    ) -> tuple[InferenceArtifact, dict[str, Any]]:
        """One narration cycle — READ-PLANE, envelope-driven.

        The controller receives the envelope (task, scenario, wake identity)
        and hands it to the agent via the comprehension pass. The agent then reads as it
        pleases through the tool base — calc.*/substrate.*/market.*/memory.*
        read tools — to complete its task; the controller classifies every
        outcome and the loop persists the artifact. NO deterministic spine is
        pre-computed here; only the two pre-gate reads serving the zero-token
        gate contract precede narration.

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
        ctx = self.run_wake(wake, wake_meta, task, scenario)
        try:
            completed = await gather.run_gather(self, ctx)
            if completed is not None:
                return completed
            st, completed = await reasoning.run_comprehension(self, ctx)
            if completed is not None:
                return completed
            assert st is not None
            completed = await reasoning.run_evidence(self, ctx, st)
            if completed is not None:
                return completed
            completed = await reasoning.run_reasoning(self, ctx, st)
            if completed is not None:
                return completed
            completed = await reasoning.run_validation(self, ctx, st)
            if completed is not None:
                return completed
            return await output.run_output(self, ctx)
        except context.GovernanceDenied as exc:
            # A governance denial is an infrastructure/contract failure, not
            # permission to continue under an invented observation.  Preserve
            # the denial beside the deterministic state and terminate through
            # the canonical FSM failure route.  The exception carries the
            # immutable controller successor so the denial trace survives.
            if exc.controller is not None:
                ctx.controller = exc.controller
            ctx.controller = ctx.controller.advance(
                GovernanceEvent(GovernanceEventKind.INFRA_FAILED)
            )
            state = (
                ctx.gathered.deterministic_state
                if ctx.gathered is not None
                else {"task": task, "scenario": scenario}
            )
            terminal = ctx.controller.terminal.value if ctx.controller.terminal else "infra_failed"
            state["terminal"] = terminal
            state["governance_denial"] = str(exc)
            state["governance_trace"] = ctx.controller.transition_trace()
            artifact = await self._degraded_artifact(
                state, ctx.capability_log, f"governance_denied: {exc}",
            )
            return artifact, {
                "task": task[:200] if task else None,
                "scenario": scenario,
                "terminal": terminal,
                "governance_denial": str(exc),
            }

    def run_wake(
        self, wake: WakeEnvelope, wake_meta: dict[str, Any],
        task: str | None, scenario: dict[str, Any] | None,
    ) -> _CycleContext:
        """Initialize the envelope and governed controller (WAKE seam)."""
        generated_at = context._utc_now_iso()
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
        )

    async def _degraded_artifact(
        self,
        deterministic_state: dict[str, Any],
        capability_log: list[dict[str, Any]],
        error: str,
    ) -> InferenceArtifact:
        """Narration-failure artifact: deterministic state preserved, interpretation NULL."""
        artifact = InferenceArtifact.create(
            symbol=self.symbol, venue=self.venue,
            generated_at=context._utc_now_iso(), completed_at=context._utc_now_iso(),
            status="provisional", window_minutes=30, interval_seconds=10,
            deterministic_state=deterministic_state,
            capability_log=capability_log,
            input_hash=str(
                (deterministic_state.get("forecast_result") or {}).get("input_hash")
                or (deterministic_state.get("microstructure_evidence") or {}).get("input_hash")
                or "narration-failed"
            ),
            model_version=str(
                (deterministic_state.get("forecast_result") or {}).get("model_version")
                or "inference-engine-v1"
            ),
            interpretation=None,
            session_id=self.session_id,
            errors=[{"source": "narration", "error": error}],
        )
        await self._persist(artifact)
        return artifact

    async def _persist(self, artifact: InferenceArtifact) -> dict[str, Any]:
        """Write the pending artifact to the durable authority first.

        Postgres is the canonical durable ledger.  Redis is published only
        after the primary write succeeds; a projection failure therefore
        cannot create a live artifact that has no durable source.
        """
        persistence: dict[str, Any] = {
            "postgres": self.postgres is None,
            "redis": False,
            "projection_error": None,
        }
        if self.postgres is not None:
            try:
                persistence["postgres"] = bool(
                    await self.postgres.insert_inference_artifact(artifact)
                )
            except Exception as exc:
                log.exception("postgres artifact insert failed")
                persistence["postgres_error"] = f"{type(exc).__name__}: {exc}"
        if not persistence["postgres"]:
            persistence["redis_skipped"] = True
            return persistence
        try:
            await self.store.publish_inference_artifact(artifact)
            persistence["redis"] = True
        except Exception as exc:
            log.exception("redis artifact publish failed")
            persistence["projection_error"] = f"{type(exc).__name__}: {exc}"
        return persistence

    async def _finalize_persisted(
        self, artifact: InferenceArtifact,
    ) -> dict[str, Any]:
        """Finalize the pending durable row and refresh its Redis projection.

        The production Postgres adapter exposes an update seam.  The small
        fallback is retained for lightweight injected test doubles that only
        implement the historical insert contract; production never uses it.
        """
        result: dict[str, Any] = {"postgres": False, "redis": False}
        if self.postgres is None:
            result["postgres"] = True
        else:
            updater = getattr(self.postgres, "update_inference_artifact", None)
            if updater is None:
                # Compatibility for injected legacy fakes; the real adapter
                # is required to implement the update method.
                result["postgres"] = True
            else:
                try:
                    result["postgres"] = bool(await updater(artifact))
                except Exception as exc:
                    log.exception("postgres artifact finalization failed")
                    result["postgres_error"] = f"{type(exc).__name__}: {exc}"
        if not result["postgres"]:
            return result
        try:
            await self.store.publish_inference_artifact(artifact)
            result["redis"] = True
        except Exception as exc:
            log.exception("redis finalized projection failed")
            result["redis_error"] = f"{type(exc).__name__}: {exc}"
        return result

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
