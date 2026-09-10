"""Inference-engine runner — the CLI invocation seam for one-shot cycles.

The runner is the engine's only sanctioned host seam:

- ``run_inference_once``: run ONE task-directed cycle. ``force=True``
  synthesizes a manual invocation (the CLI trigger); a caller-constructed
  ``WakeEnvelope`` may be injected directly; otherwise returns ``no_wake``
  without fabricating a cycle. ``task`` carries the interactive-plane
  directive (trade hypothesis / question) into prompt steering + artifact
  persistence.

There is no loop, no worker, no trigger matrix: the autonomous wake plane
was removed, and the CLI surfaces (``harness --inference`` /
``nooa market inference run``) are the only trigger.

Store lifecycle mirrors the analyst runner: Redis-only reads never construct
Postgres; the durable ledger is contacted only when DATABASE_URL is set.
The OpenAI SDK client is built lazily inside run_inference_once (the single
openai import site), keeping module import openai-free.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from market_service.config import Settings
from market_service.nooa_harness.engine import InferenceEngine
from market_service.runtime.contracts import WakeEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


def _build_llm() -> Any | None:
    """Lazily build the narration LLM client (the single nooa/litellm import site)."""
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
    """Assemble the engine with its stores, memory, and narration LLM.

    ONE Settings object (already resolved by the caller) is constructed
    here and injected into the engine — tools never re-read the environment
    (two-plane boundary pass).
    """
    store = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    postgres = PostgresRuntimeStore(settings.database_url) if settings.database_url else None
    if postgres is not None:
        await postgres.connect()
    memory = _build_memory(settings)
    llm = _build_llm()
    # Operator-pinned session id (NOOA_SESSION_ID) wins; otherwise the engine
    # derives a stable UUID per (symbol, venue) so memory stays coherent.
    session_id = os.getenv("NOOA_SESSION_ID") or None
    return InferenceEngine(
        store, postgres, memory, llm,
        symbol=symbol, venue=venue,
        session_id=session_id,
        settings=settings,
    )


async def run_inference_once(
    symbol: str, *, venue: str = "spot", force: bool = False,
    envelope: WakeEnvelope | None = None,
    task: str | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One engine cycle: invoke → run → return artifact + meta.

    ``envelope`` — a caller-constructed ``WakeEnvelope`` dispatched straight
    to ``run_cycle`` (no stream, no worker).
    ``force`` — synthesize a manual invocation (CLI ``--force`` trigger).
    ``task`` — interactive-plane directive (``harness.py --task``): the trade
    hypothesis / question the cycle must answer. Threaded into
    ``acquire_manual_wake`` (predicate preview) and ``run_cycle`` (full
    prompt steering + ``deterministic_state["task"]`` persistence).
    ``scenario`` — ``{"target_price": str, "horizon": "15m|1h|4h"}``
    (CLI ``--target/--horizon``): the 'can price hit X?' level evaluated
    via ``calc.scenario.evaluate``; persists on
    ``deterministic_state["scenario"]``.
    Neither envelope nor force → ``{"status": "no_wake"}`` — nothing
    computed, nothing persisted, never a fabricated cycle.
    """
    settings = Settings.from_env()
    engine = await _build_engine(settings, symbol=symbol.upper(), venue=venue)
    try:
        if envelope is not None:
            wake, wake_meta = envelope, {
                "decision": "fire", "source": "direct",
                "consumed_wake_ids": [envelope.wake_id],
            }
        elif force:
            wake, wake_meta = await engine.acquire_manual_wake(task=task)
        else:
            return {
                "status": "no_wake",
                "symbol": symbol.upper(),
                "venue": venue,
                "decision": {"source": "none", "reason": "no envelope and not forced"},
            }
        artifact, cycle_meta = await engine.run_cycle(
            wake, wake_meta, task=task, scenario=scenario)
        return {
            "status": artifact.status,
            "symbol": artifact.symbol,
            "venue": artifact.venue,
            "task": task[:200] if task else None,
            "scenario": scenario,
            "artifact_id": artifact.artifact_id,
            "artifact": artifact.to_dict(),
            "cycle": cycle_meta,
        }
    finally:
        await engine.close()


__all__ = ["run_inference_once"]
