"""Long-running analyst loop mounted through the canonical harness CLI.

The default behavior analyzes an EXISTING canonical envelope; refreshing
or triggering a new cycle is an explicit opt-in. This is the seam closure
called out in the project plan: the immutable ``run_id`` is the unit of
analysis, and the analyst layer must never claim a ``run_id`` it did not
read from the canonical runtime.

CLI shape (mounted by ``market_service.commands.harness --analyst-loop``):

  --run-id <UUID>          analyze the exact envelope with that run_id (default read mode)
  --latest                 analyze the latest persisted envelope for the symbol
  --interval <seconds>     polling interval for latest read mode
  --cycles <N>             number of cycles (0 = until interrupted)
  --session-id <UUID>      optional stable UUID (default: env NOOA_SESSION_ID or generated UUID)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from market_service.config import Settings
from market_service.runtime.contracts import (
    AgentMemory,
    AnalystBriefing,
    MarketRunEnvelope,
)
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from .backends import ModelBackendConfig
from .memory import MemoryNode
from .pipeline import run_cycle as pipeline_run_cycle
from .pipeline import WINDOW_MINUTES_MAP
from .suite import build_suite

log = logging.getLogger(__name__)


def _resolve_session_id(explicit: str | None) -> str:
    value = explicit or os.getenv("NOOA_SESSION_ID")
    if value:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("NOOA session IDs must be UUIDs") from exc
    return str(uuid.uuid4())


async def _read_envelope(
    settings: Settings, symbol: str, run_id: str | None,
) -> MarketRunEnvelope | None:
    """Read one exact envelope. ``run_id`` if given, else latest for symbol.

    Postgres is the durable authority for historical envelopes; the
    Redis ``latest:<symbol>:collated`` projection is consulted first
    because it is the cheapest path for the live loop, with a Postgres
    fallback so the analyst layer still works immediately after a Redis
    restart.
    """
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    postgres = PostgresRuntimeStore(settings.database_url)
    try:
        await postgres.connect()
        if run_id:
            env = await redis.read_run(run_id)
            if env is None:
                env = await postgres.read_run(run_id)
            return env
        env = await redis.read_latest_run(symbol)
        if env is None:
            env = await postgres.latest_run(symbol)
        return env
    finally:
        await redis.close()
        await postgres.close()


async def _persist_briefing(
    settings: Settings, briefing: AnalystBriefing,
) -> dict[str, Any]:
    """Postgres-first durable write, then Redis agent-stream publish.

    Order matches the rest of the runtime: durable ledger before
    operational stream. If Postgres fails, the briefing is NOT published
    to Redis — the durable record is the source of truth.
    """
    postgres = PostgresRuntimeStore(settings.database_url)
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        await postgres.connect()
        inserted = await postgres.insert_analyst_briefing(briefing)
        stream_id = await redis.publish_briefing(briefing)
        return {
            "postgres_inserted": inserted,
            "redis_stream_id": stream_id,
            "redis_key": redis.agent_stream(briefing.session_id, "briefings"),
        }
    finally:
        await redis.close()
        await postgres.close()


async def read_briefing(
    settings: Settings, session_id: str, run_id: str,
) -> AnalystBriefing | None:
    """Read one persisted briefing through the harness-owned stores."""
    resolved_session_id = _resolve_session_id(session_id)
    postgres = PostgresRuntimeStore(settings.database_url)
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        await postgres.connect()
        briefing = await postgres.read_analyst_briefing(resolved_session_id, run_id)
        if briefing is not None:
            return briefing
        for candidate in await redis.read_recent_briefings(resolved_session_id):
            if candidate.run_id == run_id:
                return candidate
        return None
    finally:
        await redis.close()
        await postgres.close()


async def _remember_cycle_outputs(
    node: MemoryNode,
    session_id: str,
    envelope: MarketRunEnvelope | None,
    briefing: AnalystBriefing,
) -> dict[str, Any]:
    """Auto-remember one cycle's advisory outputs as typed AgentMemory.

    Deterministic extraction from the validated briefing (no LLM re-read):
    - ``briefing`` — the narrative itself (highest importance);
    - ``observation`` — the consensus direction/confidence;
    - ``hypothesis`` — one per disagreement topic.

    Everything is best-effort and non-fatal: a memory failure is recorded
    on the result dict, never raised into the analyst loop.
    """
    evidence_refs = tuple(
        str(e.path)
        for e in briefing.key_evidence if e.path
    )
    run_id = briefing.run_id
    remembered: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async def _try(memory: AgentMemory, label: str) -> None:
        try:
            stored = await node.remember(
                session_id=str(session_id),
                kind=memory.kind,
                content=memory.content,
                run_id=memory.run_id,
                title=memory.title,
                importance=memory.importance,
                evidence_refs=memory.evidence_refs,
            )
            remembered.append({"kind": memory.kind, "memory_id": stored.memory_id})
        except Exception as exc:
            errors.append({"stage": label, "error": f"{type(exc).__name__}: {exc}"})

    await _try(AgentMemory(
        session_id=str(session_id),
        kind="briefing",
        content=briefing.narrative or "",
        run_id=run_id,
        title=f"briefing:{run_id[:8]}",
        importance=6.0,
        tags=("briefing", briefing.status),
        evidence_refs=evidence_refs,
    ), "memory.briefing")
    await _try(AgentMemory(
        session_id=str(session_id),
        kind="observation",
        content=(
            f"Consensus: {briefing.consensus.direction} "
            f"(confidence {briefing.consensus.confidence})"
        ),
        run_id=run_id,
        title=f"observation:{run_id[:8]}",
        importance=5.0,
        tags=("consensus",),
        evidence_refs=evidence_refs,
    ), "memory.observation")
    for i, d in enumerate(briefing.disagreements):
        topic = d.topic or f"disagreement-{i + 1}"
        resolution = d.resolution or "unresolved"
        await _try(AgentMemory(
            session_id=str(session_id),
            kind="hypothesis",
            content=f"{topic}: {resolution}",
            run_id=run_id,
            title=f"disagreement:{topic[:40]}",
            importance=4.0,
            tags=("disagreement",),
            evidence_refs=evidence_refs,
        ), "memory.hypothesis")

    return {
        "remembered": remembered,
        "errors": errors,
        "count": len(remembered),
        "error_count": len(errors),
    }


async def run_analyst_loop(
    symbol: str,
    *,
    interval_s: float = 60.0,
    timeout_s: float = 120.0,
    cycles: int = 0,
    run_id: str | None = None,
    use_latest: bool = False,
    session_id: str | None = None,
    with_memory: bool = False,
    window_minutes: int = 15,
) -> None:
    """Continuously run the pipeline and analyze each produced envelope.

    Mode resolution (in order):

    1. ``run_id`` set → read that exact envelope once (legacy, no pipeline).
    2. ``use_latest`` set, or no selector set → run the pipeline every cycle
       to produce a fresh envelope, then analyze it.

    ``cycles=0`` means run until interrupted.
    ``window_minutes`` controls how far back the pipeline reads raw evidence.
    """
    if interval_s < 0:
        raise ValueError("interval_s must be non-negative")
    if cycles < 0:
        raise ValueError("cycles must be non-negative")
    if run_id is not None and use_latest:
        raise ValueError("--run-id and --latest are mutually exclusive")
    if run_id is not None and cycles not in (0, 1):
        raise ValueError("--run-id analysis is one-shot; use --cycles 1 or omit it")
    if run_id is not None and cycles == 0:
        cycles = 1
    if run_id is None and not use_latest:
        use_latest = True

    settings = Settings.from_env()
    backend = ModelBackendConfig.from_env()
    resolved_session_id = _resolve_session_id(session_id)

    memory: MemoryNode | None = None
    if with_memory:
        memory = MemoryNode.from_settings(settings)

    completed = 0
    last_processed_run_id: str | None = None
    try:
        await _run_analyst_loop_body(
            settings=settings,
            backend=backend,
            symbol=symbol,
            interval_s=interval_s,
            timeout_s=timeout_s,
            cycles=cycles,
            run_id=run_id,
            use_latest=use_latest,
            resolved_session_id=resolved_session_id,
            memory=memory,
            window_minutes=window_minutes,
        )
    finally:
        if memory is not None:
            await memory.postgres.close()
            await memory.redis.close()


async def _run_analyst_loop_body(
    *,
    settings: Settings,
    backend: ModelBackendConfig,
    symbol: str,
    interval_s: float,
    timeout_s: float,
    cycles: int,
    run_id: str | None,
    use_latest: bool,
    resolved_session_id: str,
    memory: MemoryNode | None,
    window_minutes: int = 15,
) -> None:
    """The actual pipeline/analyze/remember loop (split out so the memory
    node's store lifecycle is owned by ``run_analyst_loop``)."""
    completed = 0
    last_processed_run_id: str | None = None
    while cycles == 0 or completed < cycles:
        envelope: MarketRunEnvelope | None = None
        cycle_meta: dict[str, Any] = {"mode": "pipeline", "window_minutes": window_minutes}

        if run_id is not None:
            cycle_meta = {"mode": "read", "run_id": run_id}
            envelope = await _read_envelope(settings, symbol, run_id)
        else:
            cycle_meta = {"mode": "pipeline", "symbol": symbol.upper(), "window_minutes": window_minutes}
            envelope = await pipeline_run_cycle(settings, symbol, window_minutes)

        if envelope is None:
            result: dict[str, Any] = {
                "session_id": resolved_session_id,
                "symbol": symbol.upper(),
                "cycle": cycle_meta,
                "status": "no_envelope",
            }
        elif run_id is None and envelope.run_id == last_processed_run_id:
            result = {
                "session_id": resolved_session_id,
                "symbol": symbol.upper(),
                "cycle": cycle_meta,
                "run_id": envelope.run_id,
                "status": "unchanged",
            }
        else:
            # Build the suite lazily, only when an envelope is actually going
            # to be analyzed. This keeps the run's read path (no-envelope /
            # unchanged-run) free of the nooa/litellm import cost: on the live
            # loop, retries for an unchanged run_id never build the agent stack.
            #
            # Memory inside the loop: recall the session's prior conclusions
            # (Redis-first live read) and seed them into the suite as a
            # context block; after the briefing is persisted, auto-remember
            # this cycle's outputs back into the ledger. Best-effort only.
            memory_meta: dict[str, Any] = {"enabled": memory is not None}
            prior_memories: list[AgentMemory] = []
            if memory is not None:
                try:
                    prior_memories = await memory.recall(
                        resolved_session_id, limit=8,
                    )
                    memory_meta["recalled"] = [
                        {"kind": m.kind, "memory_id": m.memory_id}
                        for m in prior_memories
                    ]
                except Exception as exc:
                    log.exception("memory recall failed for session=%s", resolved_session_id)
                    memory_meta["recall_error"] = f"{type(exc).__name__}: {exc}"

            suite = build_suite(symbol, backend.build_llm())
            envelope_dict = envelope.to_dict()
            analysis = await suite.analyze(
                envelope_dict,
                session_id=resolved_session_id,
                model_provider=backend.provider,
                model_name=backend.model,
                prior_memories=prior_memories or None,
                specialist_timeout_s=float(
                    os.getenv("NOOA_SPECIALIST_TIMEOUT_S", "120")
                ),
                controller_timeout_s=float(
                    os.getenv("NOOA_CONTROLLER_TIMEOUT_S", "180")
                ),
            )
            last_processed_run_id = envelope.run_id
            briefing = AnalystBriefing.from_mapping(analysis["briefing"])
            persistence: dict[str, Any] = {}
            try:
                persistence = await _persist_briefing(settings, briefing)
            except Exception as exc:
                log.exception("briefing persistence failed for run_id=%s", briefing.run_id)
                persistence = {"error": f"{type(exc).__name__}: {exc}"}
            if memory is not None:
                try:
                    memory_meta["remembered"] = await _remember_cycle_outputs(
                        memory, resolved_session_id, envelope, briefing,
                    )
                except Exception as exc:
                    log.exception("memory remember failed for run_id=%s", briefing.run_id)
                    memory_meta["remember_error"] = f"{type(exc).__name__}: {exc}"
            result = {
                "session_id": resolved_session_id,
                "symbol": symbol.upper(),
                "cycle": cycle_meta,
                "run_id": briefing.run_id,
                "briefing": analysis["briefing"],
                "parse_errors": analysis["parse_errors"],
                "persistence": persistence,
                "memory": memory_meta,
                "model": {
                    "provider": backend.provider,
                    "name": backend.model,
                    "temperature": backend.temperature,
                },
            }
        print(json.dumps(result, default=str), flush=True)
        completed += 1
        if cycles == 0 or completed < cycles:
            await asyncio.sleep(interval_s)


__all__ = [
    "run_analyst_loop",
    "read_briefing",
    "_read_envelope",
    "_persist_briefing",
    "_remember_cycle_outputs",
    "_run_analyst_loop_body",
]
