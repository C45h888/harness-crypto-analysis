"""MemoryNode — the harness memory node over the agent-namespace stores.

The memory node is the analyst's own long-term memory: what the model
concluded, hypothesized, requested, and briefed across canonical runs and
sessions. It sits inside the analyst loop so each new cycle can recall the
prior session's conclusions before reasoning again (cross-run coherence),
and it persists every cycle's outputs as typed ``AgentMemory`` artifacts.

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
tests) never pulls in ``nooa``/litellm. The optional in-agent
``nooa-memory`` ``MemorySkill`` attachment is a separate, import-guarded
seam (``memory_skill.py``) so the fast test path stays clean.
"""

from __future__ import annotations

import logging
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
    ) -> AgentMemory:
        """Create and persist one agent memory. Returns the stored memory."""
        memory = AgentMemory(
            session_id=str(session_id),
            kind=kind,
            content=content,
            run_id=str(run_id) if run_id else None,
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
        if not memories:
            memories = await self.postgres.read_recent_memories(
                session_id, kind=kind, limit=limit * 2,
            )
            if run_id is not None:
                memories = [m for m in memories if m.run_id == run_id]
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


__all__ = ["MemoryNode", "new_memory_id"]