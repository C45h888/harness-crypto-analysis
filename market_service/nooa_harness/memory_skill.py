"""MemorySkill — the AGENT's own episodic memory surface (B1 + B2).

The agent WRITES its observations and READS them back **in flight**, from any
loop, through this skill. It is the agent's memory, not the engine's:

  - **No engine disposition.** A write lands in the agent's own segment. There
    is no proposal queue, no admission gate, no ``resolve_memory_proposals``.
    The agent decides what it remembers.
  - **Structural hygiene only** (not a semantic gate): the kind must be
    agent-writable, the content is bounded, and the segment is pinned to the
    cycle's task segment so the agent can never write into another task's
    partition.
  - **Segment-scoped.** Every read/write is confined to ``(session_id,
    segment)``. Cross-task bleed is structurally impossible.
  - **The engine's own memory does NOT live here.** Engine memory is the
    central ledger (``engine/ledger.py``); loop receipts are canonical state
    (``engine/receipts.py``). Only the agent writes this store.

NOOA shape: the store is the harness ``MemoryNode`` (Postgres ledger + Redis
projection) whose render block mirrors the reference ``nooa-memory``
spontaneous-injection format. Attaching this to a real ``nooa.skill.Skill``
requires a NOOA Agent host — see ``docs/B3_NOOA_HOST_VECTORS.md``.

Memory is CONTEXT, never evidence: nothing here grants phase coverage, chain
credit, or gate authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger(__name__)

# Kinds the AGENT may write. "fact" is reserved (paper-KB truth) and the
# engine carry kinds are the engine's own — now on the central ledger, never
# written here.
AGENT_WRITABLE_KINDS: tuple[str, ...] = ("observation", "hypothesis", "note")
RESERVED_KINDS: tuple[str, ...] = ("fact", "understanding", "plan", "handoff")

# Structural bounds (hygiene, never semantic judgement).
MAX_CONTENT_CHARS = 4_000
DEFAULT_RECALL_LIMIT = 8
MAX_RECALL_LIMIT = 32


@dataclass
class MemorySkill:
    """The agent's segmented episodic memory (host-agnostic, NOOA-shaped).

    Bound to one ``(session_id, segment)`` so every read/write is confined to
    the cycle's task segment. When the store is absent (memory-less
    deployment) every call returns an explicit empty/null result — never a
    fabricated recall and never a second connection pool.
    """

    memory: Any | None
    session_id: str
    segment: str | None = None

    # ------------------------------------------------------------------
    # write — the agent decides; only structural hygiene applies
    # ------------------------------------------------------------------
    async def remember(
        self,
        kind: str,
        content: str,
        *,
        importance: float = 5.0,
        tags: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
        title: str | None = None,
    ) -> dict[str, Any]:
        """Write one observation to the agent's own segment.

        Returns ``{"stored": bool, "kind", "memory_id"|None, "reason"|None}``.
        The segment is ALWAYS this skill's segment — a caller cannot redirect
        the write.
        """
        kind = str(kind or "").strip().lower()
        if kind not in AGENT_WRITABLE_KINDS:
            return {"stored": False, "kind": kind, "memory_id": None,
                    "reason": f"kind {kind!r} is not agent-writable "
                              f"(allowed: {', '.join(AGENT_WRITABLE_KINDS)})"}
        text = str(content or "").strip()
        if not text:
            return {"stored": False, "kind": kind, "memory_id": None,
                    "reason": "empty content"}
        if len(text) > MAX_CONTENT_CHARS:
            text = text[:MAX_CONTENT_CHARS]
        if self.memory is None:
            return {"stored": False, "kind": kind, "memory_id": None,
                    "reason": "memory_node_not_configured"}
        try:
            importance = min(max(float(importance), 0.0), 10.0)
        except (TypeError, ValueError):
            importance = 5.0
        try:
            mem = await self.memory.remember(
                self.session_id,
                kind,
                text,
                title=(str(title)[:200] if title else None),
                importance=importance,
                tags=tuple(str(t) for t in (tags or ())),
                evidence_refs=tuple(str(r) for r in (evidence_refs or ())),
                segment=self.segment,
            )
        except Exception as exc:  # noqa: BLE001 — a memory write must not kill the cycle
            log.exception("agent memory write failed")
            return {"stored": False, "kind": kind, "memory_id": None,
                    "reason": f"{type(exc).__name__}: {exc}"}
        memory_id = getattr(mem, "memory_id", None)
        return {"stored": bool(memory_id), "kind": kind,
                "memory_id": memory_id, "segment": self.segment,
                "reason": None}

    # ------------------------------------------------------------------
    # read — the agent's own segment, deterministic by query/recency
    # ------------------------------------------------------------------
    async def recall(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> list[dict[str, Any]]:
        """Read back the agent's own memories for this segment."""
        if self.memory is None:
            return []
        try:
            bounded = max(1, min(int(limit or DEFAULT_RECALL_LIMIT), MAX_RECALL_LIMIT))
        except (TypeError, ValueError):
            bounded = DEFAULT_RECALL_LIMIT
        wanted = str(kind).strip().lower() if kind else None
        try:
            rows = await self.memory.recall(
                self.session_id,
                kind=wanted,
                segment=self.segment,
                query=(str(query) if query else None),
                limit=bounded,
            )
        except Exception:  # noqa: BLE001
            log.exception("agent memory recall failed")
            return []
        return [
            {
                "id": getattr(m, "memory_id", None),
                "kind": getattr(m, "kind", None),
                "content": str(getattr(m, "content", ""))[:MAX_CONTENT_CHARS],
                "importance": getattr(m, "importance", None),
                "tags": list(getattr(m, "tags", ()) or ()),
                "created_at": getattr(m, "created_at", None),
            }
            for m in rows
        ]

    # ------------------------------------------------------------------
    # housekeeping — the agent's own segment only
    # ------------------------------------------------------------------
    async def forget(self, memory_id: str) -> dict[str, Any]:
        """Tombstone one of the agent's own memories (audit row survives)."""
        if self.memory is None:
            return {"forgotten": False, "reason": "memory_node_not_configured"}
        try:
            row = await self.memory.forget(self.session_id, str(memory_id))
        except Exception as exc:  # noqa: BLE001
            log.exception("agent memory forget failed")
            return {"forgotten": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {"forgotten": row is not None, "memory_id": memory_id}

    async def stats(self) -> dict[str, Any]:
        """Per-kind counts for the agent's session."""
        if self.memory is None:
            return {"configured": False, "counts": {}}
        try:
            counts = await self.memory.stats(self.session_id)
        except Exception:  # noqa: BLE001
            log.exception("agent memory stats failed")
            return {"configured": True, "counts": {}, "error": True}
        return {"configured": True, "counts": dict(counts or {})}

    # ------------------------------------------------------------------
    # rendering — for a prompt block when the host chooses to inject it
    # ------------------------------------------------------------------
    def render(self, rows: Sequence[dict[str, Any]], *, budget: int = 3_000) -> str:
        """Render recalled rows as a compact block (advisory context only)."""
        if not rows:
            return ""
        lines = ["## Your memory (this task segment — context, never evidence)"]
        for row in rows:
            head = str(row.get("content") or "").replace("\n", " ").strip()
            lines.append(
                f"- [{row.get('kind')}#{str(row.get('id') or '')[:8]}] {head}"
            )
        text = "\n".join(lines)
        if len(text) > budget:
            text = text[:budget].rstrip() + "\n…"
        return text


__all__ = [
    "AGENT_WRITABLE_KINDS",
    "DEFAULT_RECALL_LIMIT",
    "MAX_CONTENT_CHARS",
    "MAX_RECALL_LIMIT",
    "MemorySkill",
    "RESERVED_KINDS",
]
