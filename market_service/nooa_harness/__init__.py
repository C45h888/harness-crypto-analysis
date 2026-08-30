"""NOOA harness package — the statistical inference engine runtime.

The old interpretation plane (specialist/controller agents, AnalystSuite,
run_analyze_once) was REMOVED in the inference-engine pass. The engine is
the primary interpretation plane:

- ``engine.InferenceEngine``     — the OO inference agent (receive in-memory
                                   wake → compute → narrate → persist → remember)
- ``inference``                  — mechanics: hard gate, capability registry,
                                   tool base, wake plane
- ``inference_runner``           — one-shot cycle (envelope / manual / no-wake)
                                   + event-driven engine loop
- ``wake_worker``                — the event-driven wake worker (blocking reads,
                                   deterministic trigger matrix, in-memory
                                   WakeEnvelope → engine dispatch)
- ``pipeline``                   — the deterministic calculation pipeline
                                   (unchanged; the engine commands it as tools)
- ``memory.MemoryNode``          — the engine's episodic memory
- ``backends.ModelBackendConfig``— narration LLM client config
- ``agents.MarketAnalyst``       — legacy base kept only for
                                   ``MicrostructureInterpretationAgent`` until
                                   its removal in a follow-up cleanup

CLI surfaces (``nooa market inference run/read/history/watch/wake``, outer
``harness --inference [--inference-force]``) are the sanctioned triggers.
"""

from __future__ import annotations

__all__ = [
    "InferenceEngine",
    "MemoryNode",
]
