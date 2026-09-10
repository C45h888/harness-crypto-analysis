"""NOOA harness package — the statistical inference engine runtime.

Two-plane layout (semantic-debt boundary pass, 2026-09-04):

INTERPRETATION PLANE — deterministic math + canonical persistence, no LLM:

- ``calculations.composition``  — shared deterministic core: GROUP_MAP,
                                   section deps, read_raw_window,
                                   run_calculations/run_analysis, adapters.
                                   ONE source of truth for calc semantics.
- ``pipeline_interpretation``    — the outer-CLI plane: envelope assembly,
                                   the ONE Binance derivative fetch, wall/keystone
                                   payload builders. Computation cycles were
                                   removed: workers are invoked as tools.
- ``pipeline``                   — thin re-export shim (compat path)

INFERENCE PLANE — the OO agent, LLM narration over injected state:

- ``engine.InferenceEngine``     — the OO inference agent (receive in-memory
                                   wake → compute → narrate → persist → remember);
                                   now also owns the absorbed microstructure
                                   reader (``MicrostructureInterpretationAgent``,
                                   ``bounded_envelope_view``, ``response_text``)
- ``inference``                  — mechanics: hard gate, capability registry,
                                   tool base, wake plane. Tool dispatchers use
                                   ONLY the engine's injected store/memory/
                                   settings — never env, never a second pool
- ``pipeline_inference``         — REMOVED with the tool-first migration.
                                   The agent invokes substrate workers via
                                   ``substrate_worker.tools`` (dispatch
                                   ``substrate.*``); it never imports the
                                   interpretation plane.
- ``inference_runner``           — one-shot cycle (envelope / manual / no-wake)
                                   + event-driven engine loop
- ``wake_worker``                — the event-driven wake worker (blocking reads,
                                   deterministic trigger matrix, in-memory
                                   WakeEnvelope → engine dispatch)
- ``memory.MemoryNode``          — the engine's episodic memory + the shared
                                   ``paper_kb_session_id()`` contract
- ``backends.ModelBackendConfig``— narration LLM client config

Boundary rule: the inference plane imports ``composition`` /
``substrate_worker.tools`` only; the interpretation plane never imports
the inference plane. The retired ``agents`` module is gone — everything
the LLM narrates lives in ``engine``.

CLI surfaces (``nooa market inference run/read/history/watch/wake``, outer
``harness --inference [--inference-force]``) are the sanctioned triggers.
"""

from __future__ import annotations

__all__ = [
    "InferenceEngine",
    "MemoryNode",
]
