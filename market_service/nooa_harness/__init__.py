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

- ``engine.InferenceEngine``     — the OO inference agent (CLI invocation
                                   with task → compute → narrate → persist →
                                   remember);
                                   now also owns the absorbed microstructure
                                   reader (``MicrostructureInterpretationAgent``,
                                   ``bounded_envelope_view``, ``response_text``)
- ``inference``                  — mechanics: hard gate, capability registry,
                                   tool base. Tool dispatchers use
                                   ONLY the engine's injected store/memory/
                                   settings — never env, never a second pool
- ``pipeline_inference``         — REMOVED with the tool-first migration.
                                   The agent invokes substrate workers via
                                   ``substrate_worker.tools`` (dispatch
                                   ``substrate.*``); it never imports the
                                   interpretation plane.
- ``inference_runner``           — task-directed one-shot cycle (manual +
                                   ``task`` / direct envelope / no-wake; the
                                   interaction plane's only seam)
- ``memory.MemoryNode``          — the engine's episodic memory + the shared
                                   ``paper_kb_session_id()`` contract
- ``backends.ModelBackendConfig``— narration LLM client config

Boundary rule: the inference plane imports ``composition`` /
``substrate_worker.tools`` only; the interpretation plane never imports
the inference plane. The retired ``agents`` module is gone — everything
the LLM narrates lives in ``engine``.

CLI surfaces (``nooa market inference run --force --task "..."`` / ``read`` /
``history``, outer ``harness --inference --inference-force --task "..."``)
are the ONLY triggers. There is no worker, no loop, no trigger matrix.
"""

from __future__ import annotations

__all__ = [
    "InferenceEngine",
    "MemoryNode",
]
