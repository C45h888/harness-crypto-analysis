"""
Memory node CLI — administer the harness analyst memory over the canonical
runtime stores.

The memory node persists the analyst's own curated knowledge (observations,
hypotheses, requests, briefings, facts/notes) tied to a session UUID and an
optional canonical run_id. Writes go Postgres-first (durable ledger), then
publish to the Redis ``marketflow:agent:<SESSION_ID>:memory`` stream; reads
use Redis first with a Postgres fallback (Redis is the primary read plan).

Usage:
    .venv/bin/python -m market_service.commands.memory remember \
        --session-id <UUID> --kind observation --content "..." \
        [--run-id <UUID>] [--title ...] [--importance 5] [--tag sol] ...
    .venv/bin/python -m market_service.commands.memory recall \
        --session-id <UUID> [--kind observation] [--run-id <UUID>] \
        [--query "funding"] [--limit 16]
    .venv/bin/python -m market_service.commands.memory stats --session-id <UUID>
    .venv/bin/python -m market_service.commands.memory forget \
        --session-id <UUID> --memory-id <UUID>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from market_service.config import Settings
from market_service.nooa_harness.memory import MemoryNode


def _validate_session_id(session_id: str) -> str:
    value = session_id.strip()
    if not value:
        raise ValueError("--session-id is required (UUID)")
    return value


async def _cmd_remember(args: argparse.Namespace) -> dict:
    settings = Settings.from_env()
    node = MemoryNode.from_settings(settings)
    try:
        memory = await node.remember(
            session_id=_validate_session_id(args.session_id),
            kind=args.kind,
            content=args.content,
            run_id=args.run_id,
            title=args.title,
            importance=args.importance,
            tags=tuple(args.tags or ()),
            evidence_refs=tuple(args.evidence or ()),
        )
        return {
            "stored": True,
            "memory": memory.to_dict(),
            "redis_stream": f"{settings.redis_key_prefix}:agent:{memory.session_id}:memory",
        }
    finally:
        await node.postgres.close()
        await node.redis.close()


async def _cmd_recall(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings.from_env()
    node = MemoryNode.from_settings(settings)
    try:
        memories = await node.recall(
            session_id=_validate_session_id(args.session_id),
            kind=args.kind,
            run_id=args.run_id,
            query=args.query,
            limit=args.limit,
        )
        return {
            "session_id": _validate_session_id(args.session_id),
            "kind": args.kind,
            "run_id": args.run_id,
            "query": args.query,
            "limit": args.limit,
            "count": len(memories),
            "memories": [m.to_dict() for m in memories],
        }
    finally:
        await node.postgres.close()
        await node.redis.close()


async def _cmd_stats(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings.from_env()
    node = MemoryNode.from_settings(settings)
    try:
        counts = await node.stats(_validate_session_id(args.session_id))
        total = sum(counts.values())
        return {
            "session_id": _validate_session_id(args.session_id),
            "count_by_kind": counts,
            "total": total,
        }
    finally:
        await node.postgres.close()
        await node.redis.close()


async def _cmd_forget(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings.from_env()
    node = MemoryNode.from_settings(settings)
    try:
        memory = await node.forget(
            _validate_session_id(args.session_id), args.memory_id,
        )
        return {
            "forgotten": memory is not None,
            "memory": memory.to_dict() if memory else None,
        }
    finally:
        await node.postgres.close()
        await node.redis.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Memory node CLI for the NOOA analyst harness",
    )
    sub = p.add_subparsers(dest="action", required=True)

    r = sub.add_parser("remember", help="write one durable analyst memory")
    r.add_argument("--session-id", required=True, help="analyst session UUID")
    r.add_argument("--kind", required=True,
                   choices=("observation", "hypothesis", "request", "briefing", "fact", "note"))
    r.add_argument("--content", required=True, help="memory content")
    r.add_argument("--run-id", default=None, help="canonical run UUID")
    r.add_argument("--title", default=None, help="optional short title")
    r.add_argument("--importance", type=float, default=5.0, help="0..10")
    r.add_argument("--tags", nargs="*", default=None, help="retrieval keywords")
    r.add_argument("--evidence", nargs="*", default=None,
                        help="envelope path evidence refs")

    c = sub.add_parser("recall", help="recall the session's prior analyst memories")
    c.add_argument("--session-id", required=True)
    c.add_argument("--kind", default=None, choices=("observation", "hypothesis", "request", "briefing", "fact", "note"))
    c.add_argument("--run-id", default=None)
    c.add_argument("--query", default=None, help="keyword relevance query")
    c.add_argument("--limit", type=int, default=16)

    s = sub.add_parser("stats", help="per-kind usage counts for a session")
    s.add_argument("--session-id", required=True)

    f = sub.add_parser("forget", help="tombstone one memory (audit row survives)")
    f.add_argument("--session-id", required=True)
    f.add_argument("--memory-id", required=True)

    args = p.parse_args(argv)
    try:
        if args.action == "remember":
            result = asyncio.run(_cmd_remember(args))
        elif args.action == "recall":
            result = asyncio.run(_cmd_recall(args))
        elif args.action == "stats":
            result = asyncio.run(_cmd_stats(args))
        else:
            result = asyncio.run(_cmd_forget(args))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())