"""NOOA-free tests for the memory node and its contract surface.

Exercises the ``AgentMemory`` contract, the ``MemoryNode`` write/read
ordering (Postgres ledger before Redis live projection on write; Redis
primary read with Postgres fallback), keyword recall, tombstone exclusion,
and the deterministic cycle-output remembering helper.
"""

from __future__ import annotations

import json
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock

from market_service.nooa_harness.memory import MemoryNode, _keywords
from market_service.runtime.contracts import (
    AGENT_MEMORY_SCHEMA_VERSION,
    AgentMemory,
    ValidMemoryKinds,
)
from market_service.runtime.redis_store import RedisRuntimeStore

SESSION_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())


def _memory(**overrides) -> AgentMemory:
    base = dict(
        session_id=SESSION_ID,
        kind="observation",
        content="spot buy share rose above 62%",
        run_id=RUN_ID,
        title="buy-share test",
        importance=6.0,
        tags=("sol", "flow"),
        evidence_refs=("canonical_state.analysis.analysis.demand.decomposition",),
    )
    base.update(overrides)
    return AgentMemory(**base)


def _fake_stores():
    postgres = MagicMock()
    postgres.connect = AsyncMock()
    postgres.close = AsyncMock()
    redis = MagicMock()
    redis.close = AsyncMock()
    redis.publish_agent_artifact = AsyncMock(return_value="1-1")
    return redis, postgres


class AgentMemoryContractTests(unittest.TestCase):
    def test_round_trip_preserves_all_fields(self):
        m = _memory()
        m.validate()
        m2 = AgentMemory.from_mapping(m.to_dict())
        self.assertEqual(m, m2)
        self.assertEqual(m2.schema_version, AGENT_MEMORY_SCHEMA_VERSION)

    def test_from_mapping_defaults(self):
        m = AgentMemory.from_mapping({"session_id": SESSION_ID, "kind": "fact", "content": "x"})
        self.assertTrue(m.memory_id)
        self.assertEqual(m.importance, 5.0)
        self.assertFalse(m.forgotten)
        self.assertIsNone(m.run_id)

    def test_validation_rejects_bad_kind(self):
        with self.assertRaises(ValueError):
            _memory(kind="not-a-kind").validate()

    def test_validation_rejects_empty_content(self):
        with self.assertRaises(ValueError):
            _memory(content="  ").validate()

    def test_validation_rejects_importance_out_of_range(self):
        with self.assertRaises(ValueError):
            _memory(importance=11.0).validate()


class MemoryNodeRecallOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis, self.postgres = _fake_stores()

    async def test_redis_is_primary_read_plan(self):
        recalled = [_memory(importance=7.0)]
        self.redis.read_recent_memories = AsyncMock(return_value=recalled)
        node = MemoryNode(self.redis, self.postgres)
        out = await node.recall(SESSION_ID, limit=8)
        self.redis.read_recent_memories.assert_awaited_once()
        self.postgres.read_recent_memories.assert_not_called()
        self.assertEqual([m.memory_id for m in out], [m.memory_id for m in recalled])

    async def test_postgres_fallback_when_redis_empty(self):
        self.redis.read_recent_memories = AsyncMock(return_value=[])
        pg_row = _memory()
        self.postgres.read_recent_memories = AsyncMock(return_value=[pg_row])
        node = MemoryNode(self.redis, self.postgres)
        out = await node.recall(SESSION_ID, kind="observation")
        self.postgres.read_recent_memories.assert_awaited_once_with(
            SESSION_ID, kind="observation", limit=32,
        )
        self.assertEqual([m.memory_id for m in out], [pg_row.memory_id])

    async def test_kind_and_run_id_filters_on_redis_read(self):
        matching = _memory()
        self.redis.read_recent_memories = AsyncMock(
            return_value=[matching, _memory(kind="hypothesis", run_id=str(uuid.uuid4()))],
        )
        out = await MemoryNode(self.redis, self.postgres).recall(
            SESSION_ID, kind="observation", run_id=RUN_ID,
        )
        self.assertEqual([m.memory_id for m in out], [matching.memory_id])

    async def test_keyword_query_ranks_relevance(self):
        memorized = [
            _memory(memory_id="aaa", content="funding rate turned negative", importance=1.0),
            _memory(memory_id="bbb", content="order book walls moved deeper", importance=1.0),
        ]
        self.redis.read_recent_memories = AsyncMock(return_value=memorized)
        out = await MemoryNode(self.redis, self.postgres).recall(
            SESSION_ID, query="funding", limit=1,
        )
        self.assertEqual([m.memory_id for m in out], ["aaa"])

    async def test_forget_tombstones_and_recall_skips(self):
        self.redis.read_recent_memories = AsyncMock(return_value=[])
        self.postgres.forget_memory = AsyncMock(return_value=True)
        tombstoned = _memory(forgotten=True)
        self.postgres.read_memory = AsyncMock(return_value=tombstoned)
        node = MemoryNode(self.redis, self.postgres)
        result = await node.forget(SESSION_ID, tombstoned.memory_id)
        self.postgres.forget_memory.assert_awaited_once_with(tombstoned.memory_id)
        self.assertTrue(result.forgotten)


class MemoryNodeWriteOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis, self.postgres = _fake_stores()

    async def test_postgres_first_then_redis_publish(self):
        self.postgres.insert_agent_memory = AsyncMock(return_value=True)
        node = MemoryNode(self.redis, self.postgres)
        stored = await node.remember(
            SESSION_ID, "fact", "durable fact",
            run_id=RUN_ID, title="t", importance=7.0, tags=("tag",),
        )
        self.postgres.insert_agent_memory.assert_awaited_once()
        self.redis.publish_agent_artifact.assert_awaited_once_with(
            SESSION_ID, "memory", stored.to_json(), run_id=RUN_ID,
        )

    async def test_strict_redis_is_skipped_when_ledger_fails(self):
        self.postgres.insert_agent_memory = AsyncMock(return_value=False)
        with self.assertRaises(RuntimeError):
            await MemoryNode(self.redis, self.postgres).remember(
                SESSION_ID, "fact", "x",
            )
        self.redis.publish_agent_artifact.assert_not_called()


class RedisMemoryDedupeTests(unittest.IsolatedAsyncioTestCase):
    """read_recent_memories must collapse versions and drop tombstones."""

    def setUp(self):
        self.redis, self.postgres = _fake_stores()

    async def test_tombstone_entry_excludes_original_from_live_read(self):
        memory_id = str(uuid.uuid4())
        original = _memory(memory_id=memory_id, created_at="2026-01-01T00:00:00+00:00")
        tombstoned = _memory(
            memory_id=memory_id,
            forgotten=True,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-02T00:00:00+00:00",
        )

        class _FakeRedis:
            async def xrevrange(self, key, count=None):
                # newest first: tombstone append is newest
                return [
                    ("2-1", {"payload": tombstoned.to_json()}),
                    ("1-1", {"payload": original.to_json()}),
                ]

        store = RedisRuntimeStore.__new__(RedisRuntimeStore)
        store.redis = _FakeRedis()
        store.prefix = "marketflow"
        out = await store.read_recent_memories(SESSION_ID)
        self.assertEqual(out, [])

    async def test_dedupe_keeps_newest_version(self):
        memory_id = str(uuid.uuid4())
        older = _memory(memory_id=memory_id, importance=3.0, created_at="2026-01-01T00:00:00+00:00")
        newer = _memory(
            memory_id=memory_id,
            importance=9.0,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-02T00:00:00+00:00",
        )

        class _FakeRedis:
            async def xrevrange(self, key, count=None):
                return [
                    ("2-1", {"payload": newer.to_json()}),
                    ("1-1", {"payload": older.to_json()}),
                ]

        store = RedisRuntimeStore.__new__(RedisRuntimeStore)
        store.redis = _FakeRedis()
        store.prefix = "marketflow"
        out = await store.read_recent_memories(SESSION_ID)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].importance, 9.0)

    async def test_forget_publishes_tombstone_to_redis(self):
        self.redis.read_recent_memories = AsyncMock(return_value=[])
        self.postgres.forget_memory = AsyncMock(return_value=True)
        tombstoned = _memory(forgotten=True)
        self.postgres.read_memory = AsyncMock(return_value=tombstoned)
        self.redis.publish_agent_artifact = AsyncMock(return_value="9-1")
        node = MemoryNode(self.redis, self.postgres)
        await node.forget(SESSION_ID, tombstoned.memory_id)
        self.redis.publish_agent_artifact.assert_awaited_once_with(
            SESSION_ID, "memory", tombstoned.to_json(), run_id=RUN_ID,
        )


class MemoryNodeRenderTests(unittest.TestCase):
    def test_context_block_labels_memories(self):
        block = MemoryNode.render_context_block([_memory(title="buy ratio")])
        self.assertIn("## Recalled session memory", block)
        self.assertIn("[observation#", block)
        self.assertIn("buy ratio", block)
        self.assertIn(RUN_ID, block)

    def test_empty_recall_renders_empty_block(self):
        self.assertEqual(MemoryNode.render_context_block([]), "")


if __name__ == "__main__":
    unittest.main()