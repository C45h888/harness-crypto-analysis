"""``nooa market`` — the harness surface exposed inside the ``nooa`` CLI.

This is the "nooa runtime harness CLI": it lets you speak to the collated
data class objects (``MarketRunEnvelope``, ``AnalystBriefing``,
``AgentMemory``) and run the defined agentic classes
(``ControllerAgent`` + specialists in ``market_service/nooa_harness/agents.py``)
directly from a terminal ``nooa`` invocation.

Registration: ``nooa-cli`` v0.0.6 auto-discovers command modules from its own
``nooa_cli/commands/*.py`` directory (modules exporting ``command``; see
``nooa_cli.commands.__init__``). This repo's ``market_service/commands/
nooa_cli_install.py`` drops a thin shim module there that re-exports the
``command`` group below under the name ``market``, so:

    nooa market envelope SOLUSDT --latest
    nooa market envelope SOLUSDT --run-id <UUID>
    nooa market briefing --session-id <UUID> --run-id <UUID>
    nooa market memory recall --session-id <UUID>
    nooa market analyst SOLUSDT --cycles 1 --with-memory
    nooa market refresh SOLUSDT --all

Every handler reads/writes through the canonical runtime stores (Redis
primary read, Postgres durable ledger) — no second pipeline.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click

from market_service.config import Settings
from market_service.nooa_harness.memory import MemoryNode
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore
from click import echo as click_echo


def _settings() -> Settings:
    return Settings.from_env()


async def _read_envelope(
    settings: Settings, symbol: str, run_id: str | None,
):
    """Redis-first, Postgres-fallback read of a collated MarketRunEnvelope."""
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    postgres = PostgresRuntimeStore(settings.database_url)
    try:
        await postgres.connect()
        if run_id:
            env = await redis.read_run(run_id)
            if env is None:
                env = await postgres.read_run(run_id)
        else:
            env = await redis.read_latest_run(symbol)
            if env is None:
                env = await postgres.latest_run(symbol)
        return env
    finally:
        await redis.close()
        await postgres.close()


async def _read_briefing(
    settings: Settings, session_id: str, run_id: str,
) -> Any | None:
    postgres = PostgresRuntimeStore(settings.database_url)
    redis = RedisRuntimeStore(
        settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
    )
    try:
        await postgres.connect()
        briefing = await postgres.read_analyst_briefing(session_id, run_id)
        if briefing is not None:
            return briefing
        for candidate in await redis.read_recent_briefings(session_id):
            if candidate.run_id == run_id:
                return candidate
        return None
    finally:
        await redis.close()
        await postgres.close()


def _emit(result: Any) -> None:
    click_echo(json.dumps(result, indent=2, default=str))


# --------------------------------------------------------------------------
# market group
# --------------------------------------------------------------------------
@click.group(name="market", help="Crypto market harness: collated data classes, analyst, memory.")
def command() -> None:
    """Commands mounted into the ``nooa`` tree by nooa_cli_install."""


@command.command("envelope")
@click.argument("symbol", default="SOLUSDT")
@click.option("--run-id", default=None, help="read one exact collated envelope by run id")
@click.option("--latest", "use_latest", is_flag=True, help="read the latest collated envelope (default)")
def envelope_cmd(symbol: str, run_id: str | None, use_latest: bool) -> None:
    """Read a collated MarketRunEnvelope data class object."""
    if run_id and use_latest:
        raise click.ClickException("--run-id and --latest are mutually exclusive")
    env = asyncio.run(_read_envelope(_settings(), symbol, run_id))
    if env is None:
        _emit({"envelope": None})
        return
    _emit(env.to_dict())


@command.command("briefing")
@click.option("--session-id", required=True, help="analyst session UUID")
@click.option("--run-id", required=True, help="canonical run UUID")
def briefing_cmd(session_id: str, run_id: str) -> None:
    """Read one persisted AnalystBriefing data class object."""
    briefing = asyncio.run(_read_briefing(_settings(), session_id, run_id))
    if briefing is None:
        _emit({"briefing": None})
        return
    _emit(briefing.to_dict())


# --------------------------------------------------------------------------
# memory — MemoryNode over the runtime stores
# --------------------------------------------------------------------------
@command.group("memory", help="MemoryNode: the analyst's durable long-term memory.")
def memory_group() -> None:
    """Agent memory (AgentMemory data class) over the runtime stores."""


async def _close_node(node: MemoryNode) -> None:
    await node.postgres.close()
    await node.redis.close()


async def _with_node(op) -> Any:
    """Open a MemoryNode, run ``op(node)``, and close on ONE event loop.

    The runtime stores own connections bound to the loop they were created
    on, so open/operate/close must share a single ``asyncio.run``; a second
    run in ``finally`` would attempt to close a loop that is already dead.
    """
    node = MemoryNode.from_settings(_settings())
    try:
        return await op(node)
    finally:
        await _close_node(node)


@memory_group.command("remember")
@click.option("--session-id", required=True)
@click.option("--kind", required=True,
              type=click.Choice(("observation", "hypothesis", "request",
                                 "briefing", "fact", "note")))
@click.option("--content", required=True)
@click.option("--run-id", default=None)
@click.option("--title", default=None)
@click.option("--importance", type=float, default=5.0)
@click.option("--tag", "tags", multiple=True, default=())
@click.option("--evidence", "evidence_refs", multiple=True, default=())
def memory_remember(session_id, kind, content, run_id, title, importance,
                    tags, evidence_refs) -> None:
    """Write one durable analyst memory."""
    async def _op(node: MemoryNode) -> dict:
        memory = await node.remember(
            session_id=session_id, kind=kind, content=content,
            run_id=run_id, title=title, importance=importance,
            tags=tuple(tags), evidence_refs=tuple(evidence_refs),
        )
        return {"stored": True, "memory": memory.to_dict()}

    _emit(asyncio.run(_with_node(_op)))


@memory_group.command("recall")
@click.option("--session-id", required=True)
@click.option("--kind", default=None,
              type=click.Choice(("observation", "hypothesis", "request",
                                 "briefing", "fact", "note")))
@click.option("--run-id", default=None)
@click.option("--query", default=None, help="keyword relevance query")
@click.option("--limit", type=int, default=16)
def memory_recall(session_id, kind, run_id, query, limit) -> None:
    """Recall the session's prior analyst memories (Redis-first read)."""
    async def _op(node: MemoryNode) -> dict:
        memories = await node.recall(
            session_id=session_id, kind=kind, run_id=run_id,
            query=query, limit=limit,
        )
        return {
            "session_id": session_id, "count": len(memories),
            "memories": [m.to_dict() for m in memories],
        }

    _emit(asyncio.run(_with_node(_op)))


@memory_group.command("stats")
@click.option("--session-id", required=True)
def memory_stats(session_id) -> None:
    """Per-kind usage counts for one session (durable ledger authority)."""
    async def _op(node: MemoryNode) -> dict:
        counts = await node.stats(session_id)
        return {"session_id": session_id, "count_by_kind": counts,
                "total": sum(counts.values())}

    _emit(asyncio.run(_with_node(_op)))


@memory_group.command("forget")
@click.option("--session-id", required=True)
@click.option("--memory-id", required=True)
def memory_forget(session_id, memory_id) -> None:
    """Tombstone one memory (audit row and Redis projection survive)."""
    async def _op(node: MemoryNode) -> dict:
        memory = await node.forget(session_id, memory_id)
        return {"forgotten": memory is not None,
                "memory": memory.to_dict() if memory else None}

    _emit(asyncio.run(_with_node(_op)))


# --------------------------------------------------------------------------
# analyst — runs the agentic classes from market_service/nooa_harness/agents.py
# --------------------------------------------------------------------------
@command.command("analyst")
@click.argument("symbol", default="SOLUSDT")
@click.option("--run-id", default=None, help="analyze one exact envelope")
@click.option("--latest", "use_latest", is_flag=True, help="analyze the latest envelope")
@click.option("--cycles", type=int, default=1, help="number of cycles (0 = forever)")
@click.option("--interval", type=float, default=60.0, help="seconds between cycles")
@click.option("--with-memory", is_flag=True, help="recall prior memory + remember outputs")
@click.option("--session-id", default=None, help="stable analyst session UUID")
def analyst_cmd(symbol, run_id, use_latest, cycles, interval,
                with_memory, session_id) -> None:
    """Run the NOOA analyst suite (ControllerAgent + 4 specialists)."""
    from market_service.nooa_harness.runner import run_analyst_loop

    asyncio.run(run_analyst_loop(
        symbol,
        interval_s=interval,
        cycles=cycles,
        run_id=run_id,
        use_latest=use_latest,
        session_id=session_id,
        with_memory=with_memory,
    ))


# --------------------------------------------------------------------------
# refresh — request a bounded canonical cycle (typed HarnessRunRequest)
# --------------------------------------------------------------------------
@command.command("refresh")
@click.argument("symbol", default="SOLUSDT")
@click.option("--scope", default="all",
              type=click.Choice(("all", "order_book", "trades", "funding",
                                 "open_interest", "tickers")))
@click.option("--domain", default=None,
              type=click.Choice(("data-access", "calculations", "analysis")),
              help="trigger one domain instead of a full cycle")
@click.option("--timeout", type=float, default=120.0)
def refresh_cmd(symbol, scope, domain, timeout) -> None:
    """Ask the orchestrator for one bounded canonical refresh run."""
    from market_service.commands.harness import trigger_domain, trigger_full_cycle

    if domain is not None:
        result = asyncio.run(trigger_domain(symbol, domain, scope, timeout))
    else:
        result = asyncio.run(trigger_full_cycle(symbol, timeout, scope))
    _emit(result)


__all__ = ["command"]