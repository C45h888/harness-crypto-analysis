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

Every handler reads/writes through the canonical runtime stores (Redis
primary read, Postgres durable ledger) — no second pipeline.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click
from click import echo as click_echo

from market_service.config import Settings
from market_service.nooa_harness.memory import MemoryNode
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore


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
# (analyst command REMOVED — the specialist/controller plane was deleted in
# the inference-engine pass; `nooa market inference run/watch` is the engine
# surface now.)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# microstructure — Pass-3 deterministic OFI/AD fit + evidence + interpretation
#
# Authority split (docs/nooa-kb/nooa-micro-structure-archieture.md):
# fitting lives in market_service.microstructure.fitting (pure, no I/O);
# this group is the ONLY sanctioned caller; NOOA agents read the resulting
# evidence and never recompute any value. The agent never opens Binance,
# reconstructs the book, or fits coefficients in its prompt.
# --------------------------------------------------------------------------

# Bounded request scope — frozen for the initial soft-test phase.
_MICRO_ALLOWED_SYMBOLS = frozenset({"BTCUSDT"})
_MICRO_ALLOWED_VENUES = frozenset({"spot"})
_MICRO_ALLOWED_INTERVALS_S = frozenset({10, 15, 30})
_MICRO_ALLOWED_WINDOWS_M = frozenset({15, 30, 60})
_MICRO_DEFAULT_TICK_SIZES = {"BTCUSDT": "0.01"}


def _parse_duration_ms(text: str) -> int:
    """Parse '10s' / '30m' / '1h' into milliseconds (bounded validator)."""
    text = text.strip().lower()
    if text.endswith("ms"):
        value, unit = int(text[:-2]), "ms"
    elif text.endswith("s"):
        value, unit = int(text[:-1]), "s"
    elif text.endswith("m"):
        value, unit = int(text[:-1]), "m"
    elif text.endswith("h"):
        value, unit = int(text[:-1]), "h"
    else:
        raise click.ClickException(f"cannot parse duration: {text!r}")
    if value <= 0:
        raise click.ClickException("duration must be positive")
    return value * {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000}[unit]


def _validate_micro_request(symbol: str, venue: str, interval_s: int, window_m: int) -> None:
    """Bounded-request gate: reject anything outside the frozen scope."""
    if symbol.upper() not in _MICRO_ALLOWED_SYMBOLS:
        raise click.ClickException(
            f"symbol {symbol} outside the bounded microstructure scope "
            f"(allowed: {sorted(_MICRO_ALLOWED_SYMBOLS)})"
        )
    if venue not in _MICRO_ALLOWED_VENUES:
        raise click.ClickException(
            f"venue {venue} outside the bounded scope (allowed: {sorted(_MICRO_ALLOWED_VENUES)})"
        )
    if interval_s not in _MICRO_ALLOWED_INTERVALS_S:
        raise click.ClickException(
            f"interval {interval_s}s outside the bounded scope "
            f"(allowed: {sorted(_MICRO_ALLOWED_INTERVALS_S)})"
        )
    if window_m not in _MICRO_ALLOWED_WINDOWS_M:
        raise click.ClickException(
            f"fit window {window_m}m outside the bounded scope "
            f"(allowed: {sorted(_MICRO_ALLOWED_WINDOWS_M)})"
        )


@command.group("microstructure",
               help="Pass-3 deterministic OFI/AD fitting, immutable evidence, read-only interpretation.")
def microstructure_group() -> None:
    """Deterministic microstructure inference over the isolated capture ledger."""


@microstructure_group.command("fit")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot", help="venue scope (spot only in Pass 3)")
@click.option("--interval", "interval_s", type=int, default=10,
              help="OFI interval seconds (10|15|30)")
@click.option("--fit-window", "window_m", type=int, default=30,
              help="estimation block minutes (15|30|60)")
@click.option("--tick-size", default=None,
              help="instrument tick size; defaults to the frozen per-symbol registry")
def micro_fit(symbol: str, venue: str, interval_s: int, window_m: int,
              tick_size: str | None) -> None:
    """Replay the capture ledger, run the deterministic β/c/λ fitter, persist evidence.

    Flow: bounded request validation → capture-quality gate → read event
    stream → deterministic replay → assemble MicrostructureEvidence →
    persist (Postgres-first, Redis projection) → emit JSON.
    """
    from decimal import Decimal

    from market_service.microstructure import fitting

    symbol = symbol.upper()
    _validate_micro_request(symbol, venue, interval_s, window_m)
    tick = Decimal(tick_size or _MICRO_DEFAULT_TICK_SIZES.get(symbol, "0"))
    if tick <= 0:
        raise click.ClickException(
            f"no tick size resolved for {symbol}; pass --tick-size explicitly"
        )

    async def _run() -> dict[str, Any]:
        settings = _settings()
        redis = RedisRuntimeStore(
            settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
        )
        postgres = PostgresRuntimeStore(settings.database_url)
        try:
            # Capture-quality gate: refuse to fit over an unestablished capture.
            status = await redis.read_microstructure_status(venue, symbol)
            if status is None:
                raise click.ClickException(
                    "no microstructure capture status found; start the "
                    "microstructure-capture container first"
                )
            if status.get("state") not in ("running", "gap", "reconnecting", "connected"):
                raise click.ClickException(
                    f"capture state {status.get('state')!r} is not established; "
                    "fitting is refused until capture quality is proven"
                )

            payloads = await redis.read_microstructure_events(venue, symbol)
            events, dropped = fitting.replay_events_from_payloads(payloads)
            if not events:
                raise click.ClickException(
                    "event stream is empty; capture must produce best-quote events first"
                )

            # Window anchor: the last captured event (reproducible from the
            # same stream contents, unlike wall-clock anchoring).
            window_ms = window_m * 60_000
            end_ts = events[-1].current.exchange_ts_ms
            start_ts = end_ts - window_ms
            windowed = [e for e in events if e.current.exchange_ts_ms >= start_ts]
            if len(windowed) < 2:
                raise click.ClickException(
                    f"only {len(windowed)} events inside the {window_m}m window; "
                    "capture longer before fitting"
                )

            intervals = fitting.replay_intervals(windowed, interval_ms=interval_s * 1_000)
            # Drop the final flushed partial interval: it may still be open on
            # the live tape and would make the replay non-reproducible.
            if intervals and intervals[-1].end_ts_ms > end_ts:
                intervals = intervals[:-1]

            fit_config = {
                "symbol": symbol, "venue": venue, "tick_size": str(tick),
                "interval_seconds": interval_s, "window_minutes": window_m,
            }
            evidence = fitting.assemble_evidence(
                intervals,
                symbol=symbol, venue=venue, tick_size=tick,
                interval_seconds=interval_s,
                evidence_id=f"ev-{fitting.input_hash(intervals, fit_config)[:16]}",
                generated_at_ms=end_ts,
                events=windowed,
                coverage={
                    "events_total": len(events),
                    "events_in_window": len(windowed),
                    "events_dropped_on_decode": dropped,
                    "intervals_closed": len(intervals),
                    "capture_state": status.get("state"),
                    "capture_sequence_gaps": status.get("sequence_gaps"),
                    "capture_reconnects": status.get("reconnects"),
                },
            )
            evidence_dict = evidence.to_dict()

            # Postgres-first durable ledger, then the Redis projection.
            await postgres.connect()
            persisted_pg = await postgres.insert_microstructure_evidence(evidence_dict)
            await redis.publish_microstructure_evidence(venue, symbol, evidence_dict)

            return {
                "persisted": {"postgres": persisted_pg, "redis": True},
                "evidence": evidence_dict,
            }
        finally:
            await redis.close()
            await postgres.close()

    _emit(asyncio.run(_run()))


@microstructure_group.command("evidence")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot")
@click.option("--evidence-id", default=None, help="read one exact evidence object")
@click.option("--latest", "use_latest", is_flag=True, default=True,
              help="read the latest evidence (default)")
def micro_evidence(symbol: str, venue: str, evidence_id: str | None, use_latest: bool) -> None:
    """Read persisted MicrostructureEvidence (Postgres-first, Redis fallback)."""
    # --latest is the default read mode; an explicit --evidence-id wins.
    _ = use_latest

    async def _run() -> dict[str, Any]:
        settings = _settings()
        redis = RedisRuntimeStore(
            settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
        )
        postgres = PostgresRuntimeStore(settings.database_url)
        try:
            await postgres.connect()
            evidence = await postgres.read_microstructure_evidence(
                symbol.upper(), venue, evidence_id,
            )
            if evidence is None and evidence_id is None:
                evidence = await redis.read_microstructure_evidence(venue, symbol.upper())
            return {"evidence": evidence}
        finally:
            await redis.close()
            await postgres.close()

    _emit(asyncio.run(_run()))


@microstructure_group.command("interpret")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot")
@click.option("--evidence-id", default=None, help="interpret one exact evidence object")
def micro_interpret(symbol: str, venue: str, evidence_id: str | None) -> None:
    """Hand one MicrostructureEvidence to the NOOA interpretation agent.

    The agent reads BOTH fitted models plus diagnostics and explains them;
    it never recomputes OFI, β, c, or λ. Output is one SpecialistReport JSON.
    """
    import uuid as _uuid

    from market_service.nooa_harness.backends import ModelBackendConfig
    from market_service.runtime.contracts import SpecialistReport, SpecialistReportParseError

    async def _run() -> dict[str, Any]:
        settings = _settings()
        redis = RedisRuntimeStore(
            settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen,
        )
        postgres = PostgresRuntimeStore(settings.database_url)
        try:
            await postgres.connect()
            evidence = await postgres.read_microstructure_evidence(
                symbol.upper(), venue, evidence_id,
            )
            if evidence is None and evidence_id is None:
                evidence = await redis.read_microstructure_evidence(venue, symbol.upper())
            if evidence is None:
                raise click.ClickException(
                    "no MicrostructureEvidence persisted; run "
                    "'nooa market microstructure fit' first"
                )

            backend = ModelBackendConfig.from_env()
            from market_service.nooa_harness.agents import MicrostructureInterpretationAgent

            agent = MicrostructureInterpretationAgent(symbol, llm=backend.build_llm())
            run_id = str(evidence.get("evidence_id") or _uuid.uuid4())
            raw = await agent.assess(evidence)
            try:
                report = SpecialistReport.from_llm_text("microstructure", run_id, raw)
                return {"evidence_id": evidence.get("evidence_id"),
                        "report": report.to_dict()}
            except SpecialistReportParseError as exc:
                return {"evidence_id": evidence.get("evidence_id"),
                        "parse_error": str(exc), "raw_preview": exc.raw_preview}
        finally:
            await redis.close()
            await postgres.close()

    _emit(asyncio.run(_run()))


# --------------------------------------------------------------------------
# inference — the statistical inference engine's user-facing surfaces
#
# This is the OUTER-trigger plane: a human (or terminal agent) fires the
# engine explicitly. Bounded by the same capability registry as every other
# dispatch (BTCUSDT/spot frozen). Autonomous operation runs through the
# event-driven wake worker (`market_service.nooa_harness.wake_worker` or
# `nooa market inference wake/watch`), never a lazy compute loop.
# --------------------------------------------------------------------------


@command.group("inference",
               help="Statistical inference engine: trigger, read artifacts, wake/watch.")
def inference_group() -> None:
    """Wake, read, watch, and fire the statistical inference engine."""


@inference_group.command("run")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot")
@click.option("--force", is_flag=True,
              help="synthesize a manual wake (the human trigger IS the wake)")
def inference_run(symbol: str, venue: str, force: bool) -> None:
    """Run ONE inference cycle now (event-driven envelope or manual force)."""
    from market_service.nooa_harness.inference_runner import run_inference_once

    async def _run() -> dict[str, Any]:
        return await run_inference_once(symbol, venue=venue, force=force)

    _emit(asyncio.run(_run()))


@inference_group.command("read")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot")
@click.option("--artifact-id", default=None, help="read one exact artifact (durable ledger)")
@click.option("--redis-only", is_flag=True, help="skip Postgres; read the live projection")
def inference_read(symbol: str, venue: str, artifact_id: str | None,
                   redis_only: bool) -> None:
    """Read inference artifacts (Postgres-first durable ledger, Redis fallback)."""
    async def _run() -> dict[str, Any]:
        settings = _settings()
        artifact = None
        source = None
        if not redis_only:
            postgres = PostgresRuntimeStore(settings.database_url)
            try:
                await postgres.connect()
                artifact = await postgres.read_inference_artifact(
                    symbol, artifact_id=artifact_id,
                )
                source = "postgres"
            finally:
                await postgres.close()
        if artifact is None:
            redis = RedisRuntimeStore(
                settings.redis_url, settings.redis_key_prefix,
                settings.redis_stream_maxlen,
            )
            try:
                artifact = await redis.read_latest_inference_artifact(
                    symbol.upper(), venue,
                )
                source = "redis"
            finally:
                await redis.close()
        return {"source": source, "artifact": artifact}

    _emit(asyncio.run(_run()))


@inference_group.command("history")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot")
@click.option("--limit", type=int, default=20)
def inference_history(symbol: str, venue: str, limit: int) -> None:
    """Read the inference cycle history (durable ledger, newest first)."""
    async def _run() -> dict[str, Any]:
        settings = _settings()
        postgres = PostgresRuntimeStore(settings.database_url)
        try:
            await postgres.connect()
            history = await postgres.read_inference_history(symbol, limit=limit)
            return {"symbol": symbol.upper(), "count": len(history),
                    "history": history}
        finally:
            await postgres.close()

    _emit(asyncio.run(_run()))


@inference_group.command("watch")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot")
@click.option("--interval", "interval_s", type=float, default=30.0,
              help="kept for CLI compatibility; the loop is now event-driven (fire-by-data)")
@click.option("--cycles", type=int, default=0,
              help="number of wake cycles (0 = forever; -1 = one bounded pass)")
def inference_watch(symbol: str, venue: str, interval_s: float, cycles: int) -> None:
    """Event-driven engine loop: wake worker with the engine as dispatcher.

    Drives the WakeSupervisor (blocking reads + deterministic trigger
    matrix) and runs the engine cycle on every firing wake — includes the
    hard gate, narration (0 tokens on insufficient), Postgres-first
    persistence, memory proposals. Never a lazy compute loop.
    """
    from market_service.nooa_harness.inference_runner import run_inference_loop

    async def _run() -> None:
        await run_inference_loop(
            symbol, venue=venue, interval_s=interval_s,
            forever=(cycles != -1),
        )

    asyncio.run(_run())


@inference_group.command("wake")
@click.argument("symbol", default="BTCUSDT")
@click.option("--venue", default="spot")
@click.option("--once", is_flag=True,
              help="run a single bounded tick (smoke test) instead of the loop")
@click.option("--read-block-ms", type=int, default=None,
              help="blocking read wait per iteration")
def inference_wake(symbol: str, venue: str, once: bool, read_block_ms: int | None) -> None:
    """Event-driven wake worker: fires the engine on deterministic conditions.

    Replaces the lazy drain loop with the blocker-only, clock-free wake
    plane: XREADGROUP BLOCK on the microstructure event stream + status
    transition stream, deterministic predicate evaluation, in-memory
    WakeEnvelope, async engine dispatch. ``--once`` runs one bounded tick.
    """
    from market_service.nooa_harness.wake_worker import (
        WakeSupervisorConfig,
        _build_supervisor,
    )

    config = WakeSupervisorConfig.from_env(symbol)
    config.venue = venue
    if read_block_ms is not None:
        config.read_block_ms = read_block_ms

    async def _run() -> None:
        supervisor = await _build_supervisor(config)
        if once:
            await supervisor.start()
            try:
                await supervisor._tick()
                await supervisor._read_once(block_ms=10)
                await supervisor._read_status_once(block_ms=10)
            finally:
                await supervisor.stop()
            _emit({"mode": "once", "symbol": symbol.upper(),
                   "venue": venue, "fired": supervisor.fired_count})
        else:
            fired = await supervisor.run_forever()
            _emit({"mode": "loop", "symbol": symbol.upper(),
                   "venue": venue, "fired": fired})

    asyncio.run(_run())


__all__ = ["command"]