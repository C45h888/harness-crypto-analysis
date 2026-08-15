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
    AnalystBriefing,
    MarketRunEnvelope,
)
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

from .backends import ModelBackendConfig
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


async def run_analyst_loop(
    symbol: str,
    *,
    interval_s: float = 60.0,
    timeout_s: float = 120.0,
    cycles: int = 0,
    run_id: str | None = None,
    use_latest: bool = False,
    session_id: str | None = None,
) -> None:
    """Continuously read canonical envelopes and analyze each one.

    Mode resolution (in order):

    1. ``run_id`` set → read that exact envelope once.
    2. ``use_latest`` set, or no selector set → poll the latest persisted envelope.
    3. no mode can trigger a canonical refresh; refresh remains a separate
       explicit harness command.

    ``cycles=0`` means run until interrupted.
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
    completed = 0
    last_processed_run_id: str | None = None
    while cycles == 0 or completed < cycles:
        envelope: MarketRunEnvelope | None = None
        cycle_meta: dict[str, Any] = {"mode": "read", "scope": "latest"}

        if run_id is not None:
            cycle_meta = {"mode": "read", "run_id": run_id}
            envelope = await _read_envelope(settings, symbol, run_id)
        else:
            cycle_meta = {"mode": "read", "scope": "latest", "symbol": symbol.upper()}
            envelope = await _read_envelope(settings, symbol, None)

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
            suite = build_suite(symbol, backend.build_llm())
            envelope_dict = envelope.to_dict()
            analysis = await suite.analyze(
                envelope_dict,
                session_id=resolved_session_id,
                model_provider=backend.provider,
                model_name=backend.model,
            )
            last_processed_run_id = envelope.run_id
            briefing = AnalystBriefing.from_mapping(analysis["briefing"])
            persistence: dict[str, Any] = {}
            try:
                persistence = await _persist_briefing(settings, briefing)
            except Exception as exc:
                log.exception("briefing persistence failed for run_id=%s", briefing.run_id)
                persistence = {"error": f"{type(exc).__name__}: {exc}"}
            result = {
                "session_id": resolved_session_id,
                "symbol": symbol.upper(),
                "cycle": cycle_meta,
                "run_id": briefing.run_id,
                "briefing": analysis["briefing"],
                "parse_errors": analysis["parse_errors"],
                "persistence": persistence,
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


__all__ = ["run_analyst_loop", "read_briefing", "_read_envelope", "_persist_briefing"]
