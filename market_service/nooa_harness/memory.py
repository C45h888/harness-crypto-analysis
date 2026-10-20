"""MemoryNode — the harness memory node over the agent-namespace stores.

The memory node is the AGENT's own long-term memory: what the model
concluded, hypothesized, and noted across canonical runs and sessions. It is
the only agent-writable surface in the runtime — the agent reaches it in
flight through the ``memory.*`` tools, which dispatch through the
``AgentMemorySurface`` policy facade (defined below) onto this node.

Scope and recall discipline:

* **Scope** — ``session_id`` is a deterministic per-``(symbol, venue)`` id
  (``engine/core/context.py::_stable_session_id``), so rows survive across
  cycles for one scope; ``segment`` (``AgentMemory.segment``, the task id)
  partitions them, so one task can never read or write another's rows.
* **Recall is agent-initiated** — nothing here reaches a prompt unless the
  agent calls ``memory.recall``. The single relay that does surface those
  reads is ``engine/core/synthesize.py::memory_context_block``.

THE ENGINE'S MEMORY IS NOT HERE. Engine-authored memory is the central ledger
(``engine/ledger.py``) on ``deterministic_state["ledger"]``: the agent reads it
as context but never writes it. Loop closes are recorded as gate verdicts
(``engine/gates.py``) on the engine ledger.
likewise never projected into this node. No cycle path writes a row here —
``AgentMemory`` rows come only from the agent's own ``memory.*`` calls or the
operator CLI (``commands/memory.py``). Cross-run continuity the engine
provides *without* a tool call is the ledger's prior-run recall
(``engine/core/driver.py::_prior_memory_context``), not this node.

Persistence discipline mirrors the rest of the runtime:

* **Write path** — Postgres is the durable ledger (``agent_memory`` table,
  schema-versioned, immutable-by-id); Redis is the live projection
  (``marketflow:agent:<SESSION_ID>:memory`` stream). The Postgres row
  commits before the stream is published — same order as ``analyst_briefing``.
* **Read path** — Redis is the primary (cheap, live) read; Postgres is the
  fallback authority when Redis is empty or unavailable — same order as
  ``_read_envelope`` in the runner.
* **Tombstones** — ``forgotten=True`` rows survive for audit but are
  excluded from recall everywhere.

This module is NOOA-free: importing it (and running the contract/store
tests) never pulls in ``nooa``/litellm nor the OpenAI SDK. ``AgentMemorySurface``
is the agent-facing policy facade over this node —
every ``memory.*`` dispatch goes through it — and is imported lazily at
tool-dispatch time so the fast test path stays clean.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
import re
import uuid
from typing import Any

from market_service.config import Settings
from market_service.runtime.contracts import AgentMemory
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")

def _keywords(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


class MemoryNode:
    """Owns remember/recall/forget/stats over the agent-namespace stores.

    Construct with explicit stores (test-friendly) or ``from_settings``
    (runtime-friendly). Stores are NOT owned by the node when injected —
    callers close them (mirrors the runner's store lifecycle).
    """

    def __init__(
        self,
        redis: RedisRuntimeStore,
        postgres: PostgresRuntimeStore,
    ) -> None:
        self.redis = redis
        self.postgres = postgres

    @classmethod
    def from_settings(cls, settings: Settings) -> "MemoryNode":
        """Build stores from canonical settings like the runner does."""
        redis = RedisRuntimeStore(
            settings.redis_url,
            settings.redis_key_prefix,
            settings.redis_stream_maxlen,
        )
        postgres = PostgresRuntimeStore(settings.database_url)
        return cls(redis=redis, postgres=postgres)

    # ------------------------------------------------------------------
    # write path — Postgres ledger first, Redis live projection second
    # ------------------------------------------------------------------
    async def remember(
        self,
        session_id: str,
        kind: str,
        content: str,
        *,
        run_id: str | None = None,
        title: str | None = None,
        importance: float = 5.0,
        tags: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        segment: str | None = None,
    ) -> AgentMemory:
        """Create and persist one agent memory. Returns the stored memory."""
        memory = AgentMemory(
            session_id=str(session_id),
            kind=kind,
            content=content,
            run_id=str(run_id) if run_id else None,
            segment=str(segment) if segment else None,
            title=title,
            importance=importance,
            tags=tuple(tags),
            evidence_refs=tuple(evidence_refs),
        )
        memory.validate()
        await self.postgres.connect()
        inserted = await self.postgres.insert_agent_memory(memory)
        if not inserted:
            raise RuntimeError(f"memory insert returned no row for id={memory.memory_id}")
        # Live projection only after the durable row exists.
        stream_id = await self.redis.publish_agent_artifact(
            memory.session_id,
            "memory",
            memory.to_json(),
            run_id=memory.run_id,
        )
        log.debug(
            "memory.remember id=%s kind=%s run_id=%s redis=%s",
            memory.memory_id[:8], memory.kind, memory.run_id, stream_id,
        )
        return memory

    # ------------------------------------------------------------------
    # read path — Redis (live) first, Postgres (durable) fallback
    # ------------------------------------------------------------------
    async def recall(
        self,
        session_id: str,
        *,
        kind: str | None = None,
        run_id: str | None = None,
        segment: str | None = None,
        query: str | None = None,
        limit: int = 16,
        redis_first: bool = True,
    ) -> list[AgentMemory]:
        """Recall the session's non-forgotten memories.

        Primary read plan is Redis (live projection); Postgres is consulted
        when Redis returns nothing usable or has no stream entries. When
        ``query`` is given the recalled set is keyword-scored (recency order
        ties broken by importance) so the analyst sees the most relevant
        prior conclusions first.

        ``segment`` scopes recall to one task segment (state redistribution);
        ``None`` keeps legacy unsegmented recall.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        memories: list[AgentMemory] = []
        if redis_first:
            memories = await self.redis.read_recent_memories(session_id, count=limit * 4)
            if kind is not None:
                memories = [m for m in memories if m.kind == kind]
            if run_id is not None:
                memories = [m for m in memories if m.run_id == run_id]
            if segment is not None:
                memories = [m for m in memories if m.segment == segment]
        if not memories:
            memories = await self.postgres.read_recent_memories(
                session_id, kind=kind, limit=limit * 2,
            )
            if run_id is not None:
                memories = [m for m in memories if m.run_id == run_id]
            if segment is not None:
                memories = [m for m in memories if m.segment == segment]
        if query:
            q = _keywords(query)
            scored = []
            for m in memories:
                tag_tokens: set[str] = set()
                for t in m.tags:
                    tag_tokens |= _keywords(t)
                tokens = _keywords(m.content) | _keywords(m.title or "") | tag_tokens
                score = len(q & tokens) + m.importance / 10.0
                scored.append((score, m))
            scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
            memories = [m for _s, m in scored[:limit]]
        return memories[:limit]

    async def forget(self, session_id: str, memory_id: str) -> AgentMemory | None:
        """Tombstone one memory. Returns the updated memory (or None if absent).

        Postgres marks the durable row ``forgotten`` AND a tombstoned copy is
        appended to the Redis live projection so the primary read plan excludes
        it too (streams are append-only audit; the newest version per
        ``memory_id`` wins in recall).
        """
        await self.postgres.connect()
        ok = await self.postgres.forget_memory(memory_id)
        if not ok:
            return None
        updated = await self.postgres.read_memory(memory_id)
        if updated is not None:
            await self.redis.publish_agent_artifact(
                str(session_id),
                "memory",
                updated.to_json(),
                run_id=updated.run_id,
            )
        return updated

    async def stats(self, session_id: str) -> dict[str, Any]:
        """Per-kind usage counts for one session (durable ledger authority)."""
        return await self.postgres.count_memories(str(session_id))

    # ------------------------------------------------------------------
    # context-block rendering for the analyst loop
    # ------------------------------------------------------------------
    @staticmethod
    def render_context_block(memories: list[AgentMemory], *, budget: int = 3000) -> str:
        """Render recalled memories as a compact prompt block.

        Same shape as the reference ``nooa-memory`` spontaneous-injection
        block (``[kind#id8] title / content``) so the analyst LLM sees its
        own prior conclusions labeled and can reference them by id. The
        block is bounded; nothing here ever mutates canonical state.
        """
        if not memories:
            return ""
        lines = ["## Recalled session memory (prior analyst conclusions)"]
        for m in memories:
            head = (m.title or m.content).replace("\n", " ").strip()
            lines.append(f"- [{m.kind}#{m.memory_id[:8]}] {head}")
            for ref in m.evidence_refs[:3]:
                lines.append(f"    {ref}")
            if m.run_id:
                lines.append(f"    run_id: {m.run_id}")
        text = "\n".join(lines)
        if len(text) > budget:
            text = text[:budget].rstrip() + "\n…"
        return text


def new_memory_id() -> str:
    return str(uuid.uuid4())




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
class AgentMemorySurface:
    """The agent's segmented episodic memory (host-agnostic).

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
    "AgentMemorySurface",
    "RESERVED_KINDS",
]


__all__ = ["AgentMemorySurface", "AGENT_WRITABLE_KINDS",
           "MAX_CONTENT_CHARS", "MemoryNode", "new_memory_id"]