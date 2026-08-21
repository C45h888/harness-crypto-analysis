"""One-shot NOOA analyst — pipeline + LLM, subordinate runtime module.

The poller continuously feeds raw evidence into Redis. This module is the
subordinate runtime authority on top of the canonical pipeline: it reads the
raw window, runs the pipeline (calc + analysis + collate), invokes the NOOA
specialist agents, and persists the briefing.

> ***Note (repurposing):*** the NOOA specialist/controller agent classes loaded
> through this module are being repurposed into deterministic
> calculation/analysis objects that PULL from the poller-fed Redis stream.
> They are stream-fed, never directly called by ``harness.py``, and never
> open a live Binance session for core market data.

Runtime authority:

  harness.py                  outer CLI       clean market-data contract only
       └─► --nooa ─► nooa_cli.py / nooa_cli_ext.py   inner CLI (sole direct
                                                      caller of THIS module)

This module is never imported by ``market_service/commands/harness.py``.
The outer harness routes every analyst / briefing / memory / agent
operation through ``--nooa`` so the inner NOOA CLI remains the single
direct caller of this subordinate runtime.

CLI shape (mounted by ``nooa_cli_ext.market analyst …``, reached from
harness.py via ``--nooa market analyst …``):

  SOLUSDT --cycles 1              run pipeline + NOOA agents once
  --run-id <UUID>                 analyze a specific existing envelope (skip pipeline)
  --latest                        read the latest persisted envelope (skip pipeline)
  --window 15m|1h|4h             raw evidence lookback window (default: 15m)
  --session-id <UUID>             optional stable UUID (default: env NOOA_SESSION_ID or generated)
  --with-memory                   recall prior session memory and remember this cycle
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
    """Read one exact envelope from Redis (Postgres fallback)."""
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
    """Postgres-first durable write, then Redis agent-stream publish."""
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
    """Auto-remember one cycle's advisory outputs as typed AgentMemory."""
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


async def run_analyze_once(
    symbol: str,
    *,
    window_minutes: int = 15,
    run_id: str | None = None,
    use_latest: bool = False,
    session_id: str | None = None,
    with_memory: bool = False,
    deriv_ttl_s: int = 300,
    include_cross_asset: bool = False,
    include_derivatives: bool = True,
    force_refresh_derivatives: bool = False,
) -> dict[str, Any]:
    """Run the pipeline + NOOA agents once and return the result.

    Mode resolution:
    1. ``run_id`` set → read that exact envelope, analyze it.
    2. ``use_latest`` set → read the latest persisted envelope, analyze it.
    3. Neither set → run the pipeline (raw window → calc → analysis → collate),
       then analyze the produced envelope.

    Returns a dict with the full cycle result suitable for JSON output.
    """
    settings = Settings.from_env()
    backend = ModelBackendConfig.from_env()
    resolved_session_id = _resolve_session_id(session_id)

    memory: MemoryNode | None = None
    if with_memory:
        memory = MemoryNode.from_settings(settings)

    try:
        # --- Step 1: get the envelope ---
        envelope: MarketRunEnvelope | None = None
        cycle_meta: dict[str, Any]

        if run_id is not None:
            cycle_meta = {"mode": "read", "run_id": run_id}
            envelope = await _read_envelope(settings, symbol, run_id)
        elif use_latest:
            cycle_meta = {"mode": "read", "scope": "latest", "symbol": symbol.upper()}
            envelope = await _read_envelope(settings, symbol, None)
        else:
            cycle_meta = {"mode": "pipeline", "symbol": symbol.upper(), "window_minutes": window_minutes}
            envelope = await pipeline_run_cycle(
                settings, symbol, window_minutes,
                deriv_ttl_s=deriv_ttl_s,
                include_cross_asset=include_cross_asset,
                include_derivatives=include_derivatives,
                force_refresh_derivatives=force_refresh_derivatives,
            )

        if envelope is None:
            return {
                "session_id": resolved_session_id,
                "symbol": symbol.upper(),
                "cycle": cycle_meta,
                "status": "no_envelope",
            }

        # --- Step 2: recall prior memory ---
        memory_meta: dict[str, Any] = {"enabled": memory is not None}
        prior_memories: list[AgentMemory] = []
        if memory is not None:
            try:
                prior_memories = await memory.recall(resolved_session_id, limit=8)
                memory_meta["recalled"] = [
                    {"kind": m.kind, "memory_id": m.memory_id}
                    for m in prior_memories
                ]
            except Exception as exc:
                log.exception("memory recall failed for session=%s", resolved_session_id)
                memory_meta["recall_error"] = f"{type(exc).__name__}: {exc}"

        # --- Step 3: run NOOA agents ---
        suite = build_suite(symbol, backend.build_llm())
        envelope_dict = envelope.to_dict()
        analysis = await suite.analyze(
            envelope_dict,
            session_id=resolved_session_id,
            model_provider=backend.provider,
            model_name=backend.model,
            prior_memories=prior_memories or None,
            specialist_timeout_s=float(os.getenv("NOOA_SPECIALIST_TIMEOUT_S", "120")),
            controller_timeout_s=float(os.getenv("NOOA_CONTROLLER_TIMEOUT_S", "180")),
        )
        briefing = AnalystBriefing.from_mapping(analysis["briefing"])

        # --- Step 4: persist briefing ---
        persistence: dict[str, Any] = {}
        try:
            persistence = await _persist_briefing(settings, briefing)
        except Exception as exc:
            log.exception("briefing persistence failed for run_id=%s", briefing.run_id)
            persistence = {"error": f"{type(exc).__name__}: {exc}"}

        # --- Step 5: remember this cycle ---
        if memory is not None:
            try:
                memory_meta["remembered"] = await _remember_cycle_outputs(
                    memory, resolved_session_id, envelope, briefing,
                )
            except Exception as exc:
                log.exception("memory remember failed for run_id=%s", briefing.run_id)
                memory_meta["remember_error"] = f"{type(exc).__name__}: {exc}"

        return {
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
    finally:
        if memory is not None:
            await memory.postgres.close()
            await memory.redis.close()


__all__ = [
    "run_analyze_once",
    "read_briefing",
    "_read_envelope",
    "_persist_briefing",
    "_remember_cycle_outputs",
]