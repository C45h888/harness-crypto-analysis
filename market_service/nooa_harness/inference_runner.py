"""Inference-engine runner — one-shot cycle and the long-running loop.

The runner is the engine's only sanctioned host seam:

- ``run_inference_once``: run ONE cycle. An event-driven ``WakeEnvelope``
  may be injected (the wake worker's in-memory assertion — no drain);
  ``force=True`` synthesizes a manual wake; otherwise returns ``no_wake``
  without fabricating a cycle.
- ``run_inference_loop``: EVENT-DRIVEN long-running mode — drives the
  ``WakeSupervisor`` (blocking reads, deterministic trigger matrix) with
  the engine as its dispatcher. The lazy ``interval_s`` poller is retired:
  a fire happens when the DATA crosses a predicate, never on a clock.

Store lifecycle mirrors the analyst runner: Redis-only reads never construct
Postgres; the durable ledger is contacted only when DATABASE_URL is set.
The LLM client is built lazily inside run_inference_once (the single nooa
import site), keeping module import litellm-free.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from market_service.config import Settings
from market_service.nooa_harness.engine import InferenceEngine
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


def _build_llm() -> Any | None:
    """Lazily build the narration LLM client (the only nooa import site)."""
    try:
        from market_service.nooa_harness.backends import ModelBackendConfig

        backend = ModelBackendConfig.from_env()
        return backend.build_llm()
    except Exception as exc:  # noqa: BLE001 — missing LLM config degrades to gate-limited cycles
        log.warning("narration LLM unavailable (%s); cycles will be gate-limited", exc)
        return None


def _build_memory(settings: Settings):
    """Build the engine's episodic MemoryNode (Postgres required)."""
    if not settings.database_url:
        return None
    try:
        from market_service.nooa_harness.memory import MemoryNode

        return MemoryNode.from_settings(settings)
    except Exception:
        log.exception("memory node unavailable; engine runs without memory")
        return None


async def _build_engine(
    settings: Settings, *, symbol: str, venue: str = "spot",
) -> InferenceEngine:
    """Assemble the engine with its stores, memory, and narration LLM."""
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    postgres = PostgresRuntimeStore(settings.database_url) if settings.database_url else None
    if postgres is not None:
        await postgres.connect()
    memory = _build_memory(settings)
    llm = _build_llm()
    from market_service.nooa_harness.inference import (
        DEFAULT_CYCLE_COOLDOWN_S,
        WakeConfig,
    )

    cooldown = int(os.getenv("INFERENCE_COOLDOWN_S", str(DEFAULT_CYCLE_COOLDOWN_S)))
    return InferenceEngine(
        store, postgres, memory, llm,
        symbol=symbol, venue=venue,
        config=WakeConfig(cooldown_seconds=cooldown),
    )


async def run_inference_once(
    symbol: str, *, venue: str = "spot", force: bool = False,
    envelope: "WakeEnvelope | None" = None,
) -> dict[str, Any]:
    """One engine cycle: wake → run → return artifact + meta.

    ``envelope`` — an event-driven in-memory ``WakeEnvelope`` from the wake
    worker (dispatched straight to ``run_cycle``; no stream drain).
    ``force`` — synthesize a manual wake (outer-CLI --force).
    Neither → ``{"status": "no_wake"}`` — nothing computed, nothing
    persisted, never a fabricated cycle.
    """
    settings = Settings.from_env()
    engine = await _build_engine(settings, symbol=symbol.upper(), venue=venue)
    try:
        if envelope is not None:
            wake, wake_meta = envelope, {
                "decision": "fire", "source": "event_driven",
                "consumed_wake_ids": [envelope.wake_id],
            }
        elif force:
            wake, wake_meta = await engine.acquire_manual_wake()
        else:
            return {
                "status": "no_wake",
                "symbol": symbol.upper(),
                "venue": venue,
                "decision": {"source": "none", "reason": "no envelope and not forced"},
            }
        artifact, cycle_meta = await engine.run_cycle(wake, wake_meta)
        return {
            "status": artifact.status,
            "symbol": artifact.symbol,
            "venue": artifact.venue,
            "artifact_id": artifact.artifact_id,
            "artifact": artifact.to_dict(),
            "cycle": cycle_meta,
        }
    finally:
        await engine.close()


async def run_inference_loop(
    symbol: str, *, venue: str = "spot", interval_s: float = 30.0, forever: bool = True,
) -> None:
    """Event-driven engine loop: wake worker with the engine as dispatcher.

    Drives ``wake_worker.WakeSupervisor`` — XREADGROUP BLOCK on the
    microstructure event + status-transition streams, deterministic trigger
    matrix, in-memory WakeEnvelope, then ``engine.run_cycle`` as an async
    task. ``interval_s`` is retained for CLI compatibility but the loop is
    now fire-by-data: a wake fires the instant a predicate crosses, never
    on a polling clock. ``forever=False`` runs one bounded drain pass
    (used by tests).
    """
    from market_service.nooa_harness.wake_worker import (
        WakeSupervisorConfig,
        _engine_dispatcher_factory,
    )

    settings = Settings.from_env()
    config = WakeSupervisorConfig.from_env(symbol)
    config.venue = venue
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    dispatch, engine_close = _engine_dispatcher_factory(store, settings)
    supervisor = WakeSupervisor(
        store,
        symbol=config.symbol, venue=config.venue,
        read_block_ms=config.read_block_ms, tick_reset_ms=config.tick_reset_ms,
        supervisor_ms=config.supervisor_ms,
        status_stream_maxlen=config.status_stream_maxlen,
        dispatcher=dispatch,
        on_stop=engine_close,
    )
    try:
        if forever:
            await supervisor.run_forever()
        else:
            await supervisor.start()
            try:
                await supervisor._tick()
                event_rows = await supervisor._read_once(block_ms=100)
                status_rows = await supervisor._read_status_once(block_ms=100)
                if event_rows or status_rows:
                    await supervisor._handle_delta(status_rows, event_rows, supervisor._now())
                for task in list(supervisor._tasks):
                    await task
            finally:
                await supervisor.stop()
    finally:
        await store.close()


__all__ = ["run_inference_loop", "run_inference_once"]
