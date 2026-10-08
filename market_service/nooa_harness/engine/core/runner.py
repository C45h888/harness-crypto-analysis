"""Outer loop — the inference runtime's cycle orchestrator.

The runner is the OUTER loop; the nested loops are derived within it via
``OUTER_SEQUENCE``. It owns the stage order and nothing else:

  - stage order — DERIVED from ``OUTER_SEQUENCE`` (Phase S-1,
    docs/STATE_CHARTER_SPEC.md): the receipts enumerate the real chain
    (wake → agent_fetch → data_gate → comprehension → plan-bound
    acquisition → evidence → reasoning → validation → output) and
    narrate_cycle iterates the table through _STAGE_RUNNERS;
  - the orchestration membrane: ``GovernanceDenied`` → canonical FSM
    failure route (sits between the governance membrane and the stages);
  - durable writes (persist / finalize / degraded artifact) over the
    engine's stores.

It owns no transport (``_call_llm`` / dispatch rounds live on the engine
handle), no loop bodies (those live in ``wake`` / ``gather`` /
``reasoning`` / ``output``), and no legality (``engine.fsm`` /
``engine.controller``). ``InferenceEngine`` (``core.driver``) is a handle
bag + transport + pure forwards; every ``engine.narrate_cycle`` /
``engine._persist`` call forwards here and owns no logic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from market_service.runtime.contracts import InferenceArtifact

from . import context
from . import gather, output, reasoning
from . import wake as wake_mod
from ..fsm import GovernanceEvent, GovernanceEventKind
from ..loop_states import NestedLoop

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase S-1 (docs/STATE_CHARTER_SPEC.md) — the outer cycle is DERIVED from
# the table. OUTER_SEQUENCE is no longer dead documentation: narrate_cycle
# iterates it, and each entry's runner resolves through _STAGE_RUNNERS.
# Stage spies still patch core.wake / core.gather / the reasoning+output
# owners — every stage wrapper calls through the module attribute at call
# time, so the seams survive.
#
# Stage receipts are the honest grain: the FSM's CONTEXT loop covers the
# four pre-reasoning stages (wake is pre-FSM; the merged CONTEXT loop hosts
# gate reads → comprehension → plan-bound acquisition → evidence).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageReceipt:
    """One stage of the outer cycle: its name (the _STAGE_RUNNERS key),
    the NestedLoop it narrates (None for the pre-FSM wake), and its role."""
    name: str
    loop: NestedLoop | None
    note: str = ""


OUTER_SEQUENCE: tuple[StageReceipt, ...] = (
    StageReceipt("wake", None, "bind the cycle context (pre-FSM)"),
    StageReceipt("agent_fetch", NestedLoop.CONTEXT,
                 "agent-commanded tape-quality reads"),
    StageReceipt("data_gate", NestedLoop.CONTEXT,
                 "DATA verdict on the agent's pull"),
    StageReceipt("comprehension", NestedLoop.CONTEXT,
                 "understanding pass + receipt gate + placement"),
    StageReceipt("plan_bound_acquisition", NestedLoop.CONTEXT,
                 "acquisition under the frozen receipt"),
    StageReceipt("evidence", NestedLoop.CONTEXT,
                 "evidence narration"),
    StageReceipt("reasoning", NestedLoop.REASONING,
                 "assemble → interpret → hypothesize"),
    StageReceipt("validation", NestedLoop.VALIDATION,
                 "gate + one bounded retry"),
    StageReceipt("output", NestedLoop.OUTPUT,
                 "finalize + settle + place"),
)


# Stage runners — uniform signature (engine, ctx, st, wake, wake_meta,
# task, scenario) → (st, ctx, completed). Each wrapper calls through its
# module attribute AT CALL TIME so test spies on core.wake / core.gather /
# the reasoning+output owners keep working.
async def _stage_wake(engine, ctx, st, wake, wake_meta, task, scenario):
    return st, wake_mod.run_wake(engine, wake, wake_meta, task, scenario), None


async def _stage_agent_fetch(engine, ctx, st, wake, wake_meta, task, scenario):
    completed = await gather.run_agent_fetch(engine, ctx)
    if completed is not None:
        return st, ctx, completed
    if ctx.fetched is None:
        raise context.GovernanceDenied(
            "fetch closed without fetched reads")
    return st, ctx, None


async def _stage_data_gate(engine, ctx, st, wake, wake_meta, task, scenario):
    completed = await gather.run_data_gate(engine, ctx)
    if completed is not None:
        return st, ctx, completed
    if ctx.gathered is None:
        raise context.GovernanceDenied(
            "gate closed without gathered evidence")
    return st, ctx, None

async def _stage_comprehension(engine, ctx, st, wake, wake_meta, task, scenario):
    st, completed = await wake_mod.run_comprehension(engine, ctx)
    if completed is not None:
        return st, ctx, completed
    if st is None:
        raise context.GovernanceDenied(
            "comprehension closed without a runtime state")
    # Receipt gate: the runner refuses to advance on a missing
    # understanding — a loop never starts on an un-understood prompt.
    if not wake_mod.is_framing_complete(st):
        raise context.GovernanceDenied(
            "comprehension closed without understanding receipt")
    await _place_understanding(engine, ctx, st)
    return st, ctx, None


async def _stage_plan_bound_acquisition(engine, ctx, st, wake, wake_meta, task, scenario):
    await gather.run_plan_bound_acquisition(engine, ctx, st)
    return st, ctx, None


async def _stage_evidence(engine, ctx, st, wake, wake_meta, task, scenario):
    completed = await reasoning.run_evidence(engine, ctx, st)
    return st, ctx, completed


async def _stage_reasoning(engine, ctx, st, wake, wake_meta, task, scenario):
    completed = await reasoning.run_reasoning(engine, ctx, st)
    return st, ctx, completed


async def _stage_validation(engine, ctx, st, wake, wake_meta, task, scenario):
    completed = await reasoning.run_validation(engine, ctx, st)
    return st, ctx, completed


async def _stage_output(engine, ctx, st, wake, wake_meta, task, scenario):
    artifact = await output.run_output(engine, ctx)
    return st, ctx, artifact


_STAGE_RUNNERS: dict[str, Callable[..., Awaitable[tuple[Any, Any, Any]]]] = {
    "wake": _stage_wake,
    "agent_fetch": _stage_agent_fetch,
    "data_gate": _stage_data_gate,
    "comprehension": _stage_comprehension,
    "plan_bound_acquisition": _stage_plan_bound_acquisition,
    "evidence": _stage_evidence,
    "reasoning": _stage_reasoning,
    "validation": _stage_validation,
    "output": _stage_output,
}

__all__ = [
    "OUTER_SEQUENCE",
    "StageReceipt",
    "narrate_cycle",
    "persist_artifact",
    "finalize_persisted",
    "degraded_artifact",
]


async def narrate_cycle(
    engine: Any, wake: Any, wake_meta: dict[str, Any],
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
    # Phase S-1: the stage order IS OUTER_SEQUENCE — the loop below is the
    # only traversal (previously a hardcoded await chain that could silently
    # drift from the table). Stage wrappers call through the module
    # attributes so the core.wake/core.gather spy seams survive. The
    # GovernanceDenied membrane below is unchanged.
    ctx: Any = None
    st: Any = None
    completed: Any = None
    try:
        for receipt in OUTER_SEQUENCE:
            st, ctx, completed = await _STAGE_RUNNERS[receipt.name](
                engine, ctx, st, wake, wake_meta, task, scenario,
            )
            if completed is not None:
                return completed
        return completed
    except context.GovernanceDenied as exc:
        # --- ORCHESTRATION MEMBRANE (governance → runner) ---
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
        artifact = await degraded_artifact(
            engine, state, ctx.capability_log, f"governance_denied: {exc}",
        )
        return artifact, {
            "task": task[:200] if task else None,
            "scenario": scenario,
            "terminal": terminal,
            "governance_denial": str(exc),
        }


async def _place_understanding(engine: Any, ctx: Any, st: Any) -> None:
    """Place the understanding receipt on the reason memory node.

    Runs once at the comprehension→evidence handoff (loop close): the
    receipt is stored under kind ``"understanding"`` with the wake
    envelope as provenance ancestor (the artifact does not exist yet, so
    the wake_id — not a run/artifact id — is the evidence ref). Future
    workers retrieve by ``(session_id, kind=\"understanding\")``
    filtered on tags. Memory-less runs skip silently (transport pattern).
    """
    if engine.memory is None:
        return
    receipt = (st.deterministic_state or {}).get("understanding_receipt") or {}
    segment = getattr(ctx, "task_id", None)
    try:
        await engine.memory.remember(
            engine.session_id, "understanding",
            json.dumps(receipt, default=str),
            segment=segment,
            importance=2.0,
            tags=("wake", "comprehension", engine.symbol.lower()),
            evidence_refs=(ctx.wake.wake_id,),
        )
        # Plan carry (state redistribution): the disposed plan is written as
        # its own memory kind so the next turn can gather against it.
        plan = getattr(st, "task_plan", None)
        if plan is not None:
            await engine.memory.remember(
                engine.session_id, "plan",
                json.dumps(plan, default=str),
                segment=segment,
                importance=3.0,
                tags=("wake", "plan", engine.symbol.lower()),
                evidence_refs=(ctx.wake.wake_id,),
            )
    except Exception:
        log.exception("understanding placement failed")


async def degraded_artifact(
    engine: Any,
    deterministic_state: dict[str, Any],
    capability_log: list[dict[str, Any]],
    error: str,
) -> InferenceArtifact:
    """Narration-failure artifact: deterministic state preserved, interpretation NULL."""
    artifact = InferenceArtifact.create(
        symbol=engine.symbol, venue=engine.venue,
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
        session_id=engine.session_id,
        errors=[{"source": "narration", "error": error}],
    )
    await persist_artifact(engine, artifact)
    return artifact


async def persist_artifact(
    engine: Any, artifact: InferenceArtifact,
) -> dict[str, Any]:
    """Write the pending artifact to the durable authority first.

    Postgres is the canonical durable ledger.  Redis is published only
    after the primary write succeeds; a projection failure therefore
    cannot create a live artifact that has no durable source.
    """
    persistence: dict[str, Any] = {
        "postgres": engine.postgres is None,
        "redis": False,
        "projection_error": None,
    }
    if engine.postgres is not None:
        try:
            persistence["postgres"] = bool(
                await engine.postgres.insert_inference_artifact(artifact)
            )
        except Exception as exc:
            log.exception("postgres artifact insert failed")
            persistence["postgres_error"] = f"{type(exc).__name__}: {exc}"
    if not persistence["postgres"]:
        persistence["redis_skipped"] = True
        return persistence
    try:
        await engine.store.publish_inference_artifact(artifact)
        persistence["redis"] = True
    except Exception as exc:
        log.exception("redis artifact publish failed")
        persistence["projection_error"] = f"{type(exc).__name__}: {exc}"
    return persistence


async def finalize_persisted(
    engine: Any, artifact: InferenceArtifact,
) -> dict[str, Any]:
    """Finalize the pending durable row and refresh its Redis projection.

    The production Postgres adapter exposes an update seam.  The small
    fallback is retained for lightweight injected test doubles that only
    implement the historical insert contract; production never uses it.
    """
    result: dict[str, Any] = {"postgres": False, "redis": False}
    if engine.postgres is None:
        result["postgres"] = True
    else:
        updater = getattr(engine.postgres, "update_inference_artifact", None)
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
        await engine.store.publish_inference_artifact(artifact)
        result["redis"] = True
    except Exception as exc:
        log.exception("redis finalized projection failed")
        result["redis_error"] = f"{type(exc).__name__}: {exc}"
    return result
