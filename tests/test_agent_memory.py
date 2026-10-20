"""Agent memory surface — B1 tools + ``AgentMemorySurface`` (in memory.py).

Pins the partition:

  - ``AgentMemorySurface`` is the AGENT's memory: kind hygiene, segment pinning,
    bounded content, and explicit empty results when no store is configured.
    There is NO engine disposition — the agent's write lands as-is.
  - The ``memory.*`` tools are registered, phase-less (a memory write is never
    coverage credit), homed legally, and dispatch through ``execute_tool``
    carrying the cycle's task segment.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from market_service.nooa_harness.engine.fsm import GOVERNANCE_MEMBRANE
from market_service.nooa_harness.memory import AgentMemorySurface
from market_service.nooa_harness.inference import (
    TOOL_NAMES,
    TOOL_PHASE,
    execute_tool,
)
from market_service.nooa_harness.inference.tooling.registry import tool_homes


class _FakeMemory:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    async def remember(self, session_id, kind, content, **kwargs):
        self.rows.append((session_id, kind, content, kwargs))
        return SimpleNamespace(memory_id=f"m{len(self.rows)}")

    async def recall(self, session_id, **kwargs):
        return [SimpleNamespace(
            memory_id="m1", kind="observation", content="beta flipped",
            importance=5.0, tags=("beta",), created_at="t")]

    async def forget(self, session_id, memory_id):
        return object()

    async def stats(self, session_id):
        return {"observation": 1}


class AgentMemoryTests(unittest.TestCase):
    def test_write_pins_the_segment(self):
        memory = _FakeMemory()
        skill = AgentMemorySurface(memory=memory, session_id="sess", segment="task:abc")
        out = asyncio.run(skill.remember("observation", "beta flipped", tags=["beta"]))
        self.assertTrue(out["stored"])
        session_id, kind, _content, kwargs = memory.rows[0]
        self.assertEqual((session_id, kind), ("sess", "observation"))
        # The segment is the SKILL's — a caller can never redirect the write.
        self.assertEqual(kwargs["segment"], "task:abc")

    def test_reserved_kinds_are_refused(self):
        skill = AgentMemorySurface(memory=_FakeMemory(), session_id="s", segment="task:abc")
        for kind in ("fact", "understanding", "plan", "handoff"):
            out = asyncio.run(skill.remember(kind, "x"))
            self.assertFalse(out["stored"], kind)

    def test_content_is_bounded_not_dropped(self):
        memory = _FakeMemory()
        skill = AgentMemorySurface(memory=memory, session_id="s")
        asyncio.run(skill.remember("note", "x" * 10_000))
        self.assertEqual(len(memory.rows[0][2]), 4000)

    def test_no_store_is_explicit_never_fabricated(self):
        skill = AgentMemorySurface(memory=None, session_id="s")
        self.assertFalse(asyncio.run(skill.remember("note", "x"))["stored"])
        self.assertEqual(asyncio.run(skill.recall()), [])
        self.assertFalse(asyncio.run(skill.stats())["configured"])

    def test_render_is_labelled_advisory(self):
        skill = AgentMemorySurface(memory=_FakeMemory(), session_id="s", segment="t")
        block = skill.render(asyncio.run(skill.recall()))
        self.assertIn("never evidence", block)
        self.assertIn("[observation#", block)


class MemoryToolTests(unittest.TestCase):
    def test_registered_and_phase_less(self):
        for tool in ("memory.remember", "memory.recall",
                     "memory.forget", "memory.stats"):
            self.assertIn(tool, TOOL_NAMES)
            # A memory write is never coverage credit.
            self.assertIsNone(TOOL_PHASE.get(tool))

    def test_homes_are_legal_and_broad(self):
        self.assertEqual(GOVERNANCE_MEMBRANE.audit_tool_homes(), {})
        # Usable "in flight" — declared across the agent's working loops.
        self.assertGreaterEqual(len(tool_homes("memory.remember")), 6)

    def test_dispatch_round_trip(self):
        memory = _FakeMemory()
        scope = {"symbol": "BTCUSDT", "venue": "spot"}
        wrote, log = asyncio.run(execute_tool(
            None, "memory.remember",
            {**scope, "kind": "observation", "content": "beta flipped"},
            memory=memory, session_id="sess", segment="task:abc"))
        self.assertTrue(wrote["stored"])
        self.assertEqual(wrote["segment"], "task:abc")
        self.assertEqual(log["capability"], "memory.remember")
        rows, _ = asyncio.run(execute_tool(
            None, "memory.recall", {**scope, "query": "beta"},
            memory=memory, session_id="sess", segment="task:abc"))
        self.assertEqual(len(rows), 1)

    def test_dispatch_refuses_reserved_kind(self):
        memory = _FakeMemory()
        out, _ = asyncio.run(execute_tool(
            None, "memory.remember",
            {"symbol": "BTCUSDT", "venue": "spot",
             "kind": "fact", "content": "x"},
            memory=memory, session_id="sess", segment="task:abc"))
        self.assertFalse(out["stored"])
        self.assertEqual(memory.rows, [])

    def test_dispatch_denies_out_of_scope(self):
        out, log = asyncio.run(execute_tool(
            None, "memory.remember",
            {"symbol": "DOGEUSDT", "venue": "spot",
             "kind": "note", "content": "x"},
            memory=_FakeMemory(), session_id="sess", segment="task:abc"))
        self.assertFalse(out["stored"])
        self.assertEqual(log["result"], "denied")


if __name__ == "__main__":
    unittest.main()
