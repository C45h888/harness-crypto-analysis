"""``nooa market`` — the harness surface exposed inside the ``nooa`` CLI.

This is the "nooa runtime harness CLI": it reads the live venue-scoped
runtime projections (microstructure + derivatives + substrate) and the
durable canonical-output ledger (Redis primary, Postgres durable ledger)
and runs the surviving agent classes directly from a terminal ``nooa``
invocation. The envelope dataclass read path was retired 2026-08-30: the
market read commands go through ``runtime.read_paths`` (raw Redis payload
+ bounded projections), same discipline as the OO agent's ``market.read``
tool.

Registration: ``nooa-cli`` v0.0.6 auto-discovers command modules from its own
``nooa_cli/commands/*.py`` directory (modules exporting ``command``; see
``nooa_cli.commands.__init__``). This repo's ``market_service/commands/
nooa_cli_install.py`` drops a thin shim module there that re-exports the
``command`` group below under the name ``market``, so:

    nooa market read SOLUSDT                      # headline snapshot
    nooa market read SOLUSDT --mode inventory
    nooa market read SOLUSDT --mode full          # raw payload deep-dive
    nooa market read --run-id <UUID>
    nooa market memory recall --session-id <UUID>

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
from market_service.nooa_harness.memory import (
    MemoryNode, session_id_for, task_segment_id,
)
from market_service.runtime.contracts import ValidMemoryKinds
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore
from market_service.runtime.scope import (
    CANONICAL_VENUE, DEFAULT_SYMBOL, VENUES, Scope, normalize_symbol,
    normalize_venue,
)


def _settings() -> Settings:
    return Settings.from_env()


# --------------------------------------------------------------------------
# Identity defaults — derived, NEVER literal.
#
# This CLI used to hardcode 11 symbol/venue defaults (7 agentic commands on
# BTCUSDT/spot, 4 market commands on SOLUSDT) while the runtime captured
# SOLUSDT on futures. Asking for a symbol nothing captured cost a full
# 9-turn cycle before failing. Every default now resolves from the one
# identity authority (runtime/scope.py) via Settings, so the CLI cannot
# disagree with what the runtime actually trades.
#
# Click calls a callable default at parse time, so these read env lazily
# rather than freezing identity at import.
# --------------------------------------------------------------------------
def _scope() -> Scope:
    return Scope.from_settings(_settings())


def _scope_or_default() -> Scope:
    """The resolved scope, or the canonical identity if Settings cannot load.

    Click evaluates a callable default at PARSE time, before the command
    body runs. Requiring a resolvable Settings (i.e. DATABASE_URL) just to
    parse ``--help`` would make the whole CLI unusable on a host without the
    ledger — so an unloadable Settings degrades to the canonical identity.
    The command body still fails loudly if it genuinely needs the DB.
    """
    try:
        return _scope()
    except Exception:  # noqa: BLE001 — a default must never break parsing
        return Scope(DEFAULT_SYMBOL, CANONICAL_VENUE)


def _default_symbol() -> str:
    return _scope_or_default().symbol


def _default_venue() -> str:
    return _scope_or_default().venue


def _resolve_args(symbol: str | None, venue: str | None) -> Scope:
    """Bind an explicit (symbol, venue) or fall back to the resolved scope."""
    base = _scope_or_default()
    return Scope(
        symbol=normalize_symbol(symbol) if symbol else base.symbol,
        venue=normalize_venue(venue) if venue else base.venue,
    )


def _emit(result: Any) -> None:
    click_echo(json.dumps(result, indent=2, default=str))


async def _load_run_record(
    symbol: str, venue: str, run_id: str | None = None,
    redis_only: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    """Read ONE canonical run record (Postgres-first, Redis projection fallback).

    Returns ``(source, record)``. Canonical rows only — the pre-canonical
    artifact ledger is retired; rows that predate it are no longer readable.
    The durable ledger is contacted only when DATABASE_URL is set.
    """
    settings = _settings()
    source = None
    record: dict[str, Any] | None = None
    if not redis_only:
        postgres = PostgresRuntimeStore(settings.database_url)
        try:
            await postgres.connect()
            record = await postgres.read_canonical_output(
                symbol, run_id=run_id, venue=venue,
            )
            if record is not None:
                source = "postgres"
        finally:
            await postgres.close()
    if record is None:
        redis = RedisRuntimeStore(
            settings.redis_url, settings.redis_key_prefix,
            settings.redis_stream_maxlen,
        )
        try:
            record = await redis.read_latest_canonical_output(
                symbol.upper(), venue,
            )
            source = "redis"
        finally:
            await redis.close()
    return source, record


# --------------------------------------------------------------------------
# market group
# --------------------------------------------------------------------------
@click.group(name="market", help="Crypto market harness: collated data classes, analyst, memory.")
def command() -> None:
    """Commands mounted into the ``nooa`` tree by nooa_cli_install."""


@command.command("scope")
def scope_cmd() -> None:
    """Show the identity this runtime will analyse (symbol + venue).

    The operator-visible answer to "what will my run actually trade?" —
    resolved live (Redis control key > env), the same authority the
    poller, the wake guard, and the inference runner read, so a mismatch
    between the CLI and the runtime is impossible to hide.
    """
    from market_service.runtime.scope import resolve_live_scope

    async def _op() -> dict:
        settings = _settings()
        redis = RedisRuntimeStore(
            settings.redis_url, settings.redis_key_prefix,
            settings.redis_stream_maxlen,
        )
        try:
            return (await resolve_live_scope(settings, redis)).to_dict()
        finally:
            await redis.close()

    _emit(asyncio.run(_op()))


@command.command("read")
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--run-id", default=None,
              help="run id is reported, never used as a read key (the "
                   "collated run plane has no writer)")
@click.option(
    "--mode", default="snapshot",
    type=click.Choice(["snapshot", "inventory", "surfaces", "full"]),
    help="output shape (same as the agent's market.read tool)",
)
def read_cmd(symbol: str, venue: str, run_id: str | None, mode: str) -> None:
    """Read LIVE venue-scoped Redis projections for one identity.

    Shares ONE implementation with the agent's ``market.read`` tool. It used
    to read the collated run plane, which has no writer (``publish_run`` and
    ``insert_run`` have no production callers) — so the operator and the
    agent could both be shown frozen prices with no age attached. Now both
    read the live microstructure/derivative/substrate projections and every
    number carries ``age_ms``.
    """
    from market_service.nooa_harness.inference.tooling.tools_market import (
        dispatch_market_read,
    )

    resolved = _resolve_args(symbol, venue)

    async def _op() -> dict:
        settings = _settings()
        redis = RedisRuntimeStore(
            settings.redis_url, settings.redis_key_prefix,
            settings.redis_stream_maxlen,
        )
        try:
            payload, _log = await dispatch_market_read(
                redis, resolved.symbol, mode=mode, venue=resolved.venue,
                run_id=run_id,
            )
            return payload if payload is not None else {
                "status": "denied", "symbol": resolved.symbol,
                "venue": resolved.venue, "mode": mode,
            }
        finally:
            await redis.close()

    _emit(asyncio.run(_op()))


@command.command("substrate-read")
@click.argument("symbol", default=_default_symbol)
@click.option("--substrate", default=None,
              help="single substrate name (default: all → snapshot)")
@click.option("--mode", default="compact",
              type=click.Choice(["compact", "full"]),
              help="output shape (same discipline as the agent's substrate.read tool)")
def substrate_read_cmd(symbol: str, substrate: str | None, mode: str) -> None:
    """Read worker state — via the canonical interaction plane."""
    from market_service.interaction_plane import parsing as _parsing
    from market_service.interaction_plane import prompts as _prompts
    from market_service.interaction_plane import reads as _reads

    async def _op() -> dict:
        payload, source = await _reads.read_warm(
            symbol.upper(),
            substrates=[substrate] if substrate else None, mode=mode)
        return _prompts.attach_header(
            _parsing.wrap_result(
                "substrate.read", payload, segment="warm", mode=mode),
            segment="warm", mode=mode, source=source)

    _emit(asyncio.run(_op()))


@command.group("substrate", help="Substrate worker plane: read state, or "
                                 "request a fire-tick from the calculation plane.")
def substrate_group() -> None:
    """Worker invocation tools (tool-first; replaces run_cycle)."""


@substrate_group.command("read")
@click.argument("symbol", default=_default_symbol)
@click.option("--substrate", default=None,
              help="single substrate name (default: all → snapshot)")
@click.option("--mode", default="compact",
              type=click.Choice(["compact", "full"]))
def substrate_group_read(symbol: str, substrate: str | None, mode: str) -> None:
    """Read worker state (same seam as ``market substrate-read``)."""
    substrate_read_cmd(symbol, substrate, mode)


@substrate_group.command("invoke")
@click.argument("symbol", default=_default_symbol)
@click.argument("substrates", nargs=-1)
def substrate_group_invoke(symbol: str, substrates: tuple[str, ...]) -> None:
    """Request bounded fire-ticks from the calculation plane.

    The fire runs inside the calculation container — the only process that
    constructs workers — so a CLI invocation cannot overwrite the live
    workers' supervisor heartbeats or steal their stream entries. An
    unreachable plane is reported, never worked around by computing here.
    """
    from market_service.substrate_worker.control_client import (
        CalcPlaneError,
        request_invoke,
    )

    async def _op() -> dict:
        return await request_invoke(symbol.upper(), list(substrates) or None)

    try:
        _emit(asyncio.run(_op()))
    except CalcPlaneError as exc:
        raise click.ClickException(str(exc)) from exc


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


#: Kinds the OPERATOR may WRITE here. ``fact`` is seed-only (deterministic
#: paper truth the agent may read but never writes). ``request``/``briefing``
#: remain in the contract for legacy rows and stay readable through
#: ``--kind``, but are no longer writable: an operator could otherwise seed
#: rows the agent can never reproduce (the old Q5 asymmetry).
_OPERATOR_WRITABLE_KINDS = ("observation", "hypothesis", "note", "fact")


def _memory_session(
    session_id: str | None, *, symbol: str | None, venue: str | None,
) -> tuple[str, Any]:
    """Resolve a memory session: explicit id wins, else derived from identity.

    Derivation is the SAME function the engine uses
    (``nooa_harness.memory.session_id_for``), so the operator never has to
    hand-compute ``uuid5(NS_URL, inference-engine://<symbol>/<venue>)`` and
    can always reach the session the agent actually writes.
    """
    scope = _resolve_args(symbol, venue)
    resolved = session_id or session_id_for(scope.symbol, scope.venue)
    return resolved, scope


@memory_group.command("session")
@click.option("--session-id", default=None,
              help="session UUID (default: derived from --symbol/--venue)")
@click.option("--symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--task", default=None,
              help="the task text the cycle ran (to derive its segment)")
@click.option("--target", default=None, help="scenario target_price")
@click.option("--horizon", default=None,
              type=click.Choice(("15m", "1h", "4h")))
def memory_session(session_id, symbol, venue, task, target, horizon) -> None:
    """Print the agent's exact memory identity (session + task segment).

    This is the operator's reproduction key: pass the same --task/--target/
    --horizon the cycle ran and you get the segment the agent read and wrote,
    so ``memory recall --segment`` shows the agent's own view instead of the
    whole session.
    """
    scope = _resolve_args(symbol, venue)
    scenario = None
    if target is not None or horizon is not None:
        scenario = {"target_price": target, "horizon": horizon}
    _emit({
        "symbol": scope.symbol,
        "venue": scope.venue,
        "session_id": session_id or session_id_for(scope.symbol, scope.venue),
        "segment": task_segment_id(scope.symbol, scope.venue, task, scenario),
        "task": task,
        "scenario": scenario,
    })


@memory_group.command("remember")
@click.option("--session-id", default=None,
              help="session UUID (default: derived from --symbol/--venue)")
@click.option("--symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--kind", required=True,
              type=click.Choice(_OPERATOR_WRITABLE_KINDS),
              help="observation|hypothesis|note (agent kinds); fact is seed-only")
@click.option("--content", required=True)
@click.option("--run-id", default=None)
@click.option("--title", default=None)
@click.option("--importance", type=float, default=5.0)
@click.option("--tag", "tags", multiple=True, default=())
@click.option("--evidence", "evidence_refs", multiple=True, default=())
@click.option("--segment", default=None,
              help="task segment (default: unsegmented/operator row)")
def memory_remember(session_id, symbol, venue, kind, content, run_id, title,
                    importance, tags, evidence_refs, segment) -> None:
    """Write one durable memory into the agent-reachable session."""
    resolved, _scope = _memory_session(session_id, symbol=symbol, venue=venue)

    async def _op(node: MemoryNode) -> dict:
        memory = await node.remember(
            session_id=resolved, kind=kind, content=content,
            run_id=run_id, title=title, importance=importance,
            tags=tuple(tags), evidence_refs=tuple(evidence_refs),
            segment=segment,
        )
        return {"stored": True, "session_id": resolved, "segment": segment,
                "memory": memory.to_dict()}

    _emit(asyncio.run(_with_node(_op)))


@memory_group.command("recall")
@click.option("--session-id", default=None,
              help="session UUID (default: derived from --symbol/--venue)")
@click.option("--symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--segment", default=None,
              help="task segment (see `memory session`); default = whole "
                   "session, which is WIDER than the agent's own view")
@click.option("--kind", default=None, type=click.Choice(ValidMemoryKinds),
              help="filter by kind (legacy kinds stay queryable)")
@click.option("--run-id", default=None)
@click.option("--query", default=None, help="keyword relevance query")
@click.option("--limit", type=int, default=16)
def memory_recall(session_id, symbol, venue, segment, kind, run_id, query,
                  limit) -> None:
    """Recall prior memories (Redis-first read), optionally one task segment."""
    resolved, _scope = _memory_session(session_id, symbol=symbol, venue=venue)

    async def _op(node: MemoryNode) -> dict:
        memories = await node.recall(
            session_id=resolved, kind=kind, run_id=run_id,
            query=query, limit=limit, segment=segment,
        )
        return {
            "session_id": resolved, "segment": segment,
            "count": len(memories),
            "memories": [m.to_dict() for m in memories],
        }

    _emit(asyncio.run(_with_node(_op)))


@memory_group.command("stats")
@click.option("--session-id", default=None,
              help="session UUID (default: derived from --symbol/--venue)")
@click.option("--symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
def memory_stats(session_id, symbol, venue) -> None:
    """Per-kind usage counts for one session (durable ledger authority)."""
    resolved, _scope = _memory_session(session_id, symbol=symbol, venue=venue)

    async def _op(node: MemoryNode) -> dict:
        counts = await node.stats(resolved)
        return {"session_id": resolved, "count_by_kind": counts,
                "total": sum(counts.values())}

    _emit(asyncio.run(_with_node(_op)))


@memory_group.command("forget")
@click.option("--session-id", default=None,
              help="session UUID (default: derived from --symbol/--venue)")
@click.option("--symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--memory-id", required=True)
def memory_forget(session_id, symbol, venue, memory_id) -> None:
    """Tombstone one memory (audit row and Redis projection survive)."""
    resolved, _scope = _memory_session(session_id, symbol=symbol, venue=venue)

    async def _op(node: MemoryNode) -> dict:
        memory = await node.forget(resolved, memory_id)
        return {"forgotten": memory is not None,
                "memory": memory.to_dict() if memory else None}

    _emit(asyncio.run(_with_node(_op)))


# --------------------------------------------------------------------------
# (analyst command REMOVED — the specialist/controller plane was deleted in
# the inference-engine pass; `nooa market inference run` is the engine
# surface now.)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# microstructure — Pass-3 deterministic OFI/AD fit + evidence + interpretation
#
# Authority split:
# fitting lives in market_service.microstructure.fitting (pure, no I/O);
# this group is the ONLY sanctioned caller; NOOA agents read the resulting
# evidence and never recompute any value. The agent never opens Binance,
# reconstructs the book, or fits coefficients in its prompt.
# --------------------------------------------------------------------------

# Bounded request scope — DERIVED from the identity authority, never literal.
#
# These were hardcoded to {"BTCUSDT", "SOLUSDT", "ETHUSDT"} and {"spot"},
# which admitted a symbol nothing captures and FORBADE the venue capture
# actually writes (futures). The symbol set now comes from Settings.symbols
# (the capture authority); the venue set from the frozen vocabulary in
# runtime/scope.py.
_MICRO_ALLOWED_INTERVALS_S = frozenset({10, 15, 30})
_MICRO_ALLOWED_WINDOWS_M = frozenset({15, 30, 60})


def _micro_allowed_symbols() -> frozenset[str]:
    """Symbols this runtime captures (Settings is the sole authority)."""
    return frozenset(_settings().symbols)


def _micro_allowed_venues() -> frozenset[str]:
    return frozenset(VENUES)


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
    allowed_symbols = _micro_allowed_symbols()
    if symbol.upper() not in allowed_symbols:
        raise click.ClickException(
            f"symbol {symbol} outside the bounded microstructure scope "
            f"(allowed: {sorted(allowed_symbols)})"
        )
    allowed_venues = _micro_allowed_venues()
    if venue not in allowed_venues:
        raise click.ClickException(
            f"venue {venue} outside the bounded scope (allowed: {sorted(allowed_venues)})"
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
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue,
              help="venue scope (futures|spot|perps; defaults to the runtime venue)")
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

    import market_service.microstructure as fitting
    from market_service.microstructure.tick import resolve_tick_size

    symbol = symbol.upper()
    _validate_micro_request(symbol, venue, interval_s, window_m)
    try:
        frozen_tick = resolve_tick_size(symbol, venue)
    except KeyError:
        frozen_tick = Decimal("0")
    if tick_size is not None and Decimal(str(tick_size)) != frozen_tick:
        raise click.ClickException(
            f"tick size {tick_size} does not match frozen registry value {frozen_tick}"
        )
    tick = frozen_tick
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
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
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
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
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
            # Two-plane boundary pass: the microstructure reader was absorbed
            # from the retired ``agents`` module into the inference engine.
            from market_service.nooa_harness.engine import (
                MicrostructureInterpretationAgent,
            )

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
# This is the INTERACTION plane: a human (or terminal agent) supplies a
# trade hypothesis via --task and fires ONE task-directed cycle explicitly.
# There is no worker, no loop, no trigger matrix — the autonomous wake plane
# was removed and these CLI surfaces are the only invocation model.
# --------------------------------------------------------------------------


@command.group("inference",
               help="Statistical inference engine: task-directed runs + artifact reads.")
def inference_group() -> None:
    """Task-directed inference runs and artifact reads (the interaction plane)."""


@inference_group.command("run")
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--force", is_flag=True,
              help="synthesize a manual wake (the human trigger IS the wake)")
@click.option("--task", default=None,
              help="interactive-plane directive: free-text trade hypothesis / "
                   "question the cycle must answer (frames H0/H1, persists on "
                   "deterministic_state.task)")
@click.option("--target", default=None,
              help="scenario price target (quote currency): the 'can price hit X?' "
                   "level evaluated at --horizon (persists on deterministic_state.scenario)")
@click.option("--horizon", default="1h", type=click.Choice(["15m", "1h", "4h"]),
              help="scenario horizon for the OFI exceedance distribution (default 1h)")
def inference_run(symbol: str, venue: str, force: bool, task: str | None,
                  target: str | None, horizon: str) -> None:
    """Run ONE task-directed inference cycle now (canonical trigger)."""
    from market_service.nooa_harness.inference_runner import run_inference_once

    scenario = None
    if target is not None:
        try:
            from decimal import Decimal
            target_dec = Decimal(str(target))
        except Exception:
            target_dec = None
        if target_dec is None or target_dec <= 0:
            raise click.ClickException(f"unparseable --target: {target!r}")
        scenario = {"target_price": str(target_dec), "horizon": horizon}

    async def _run() -> dict[str, Any]:
        return await run_inference_once(symbol, venue=venue, force=force,
                                        task=task, scenario=scenario)

    result = asyncio.run(_run())
    if result.get("status") == "refused":
        # Identity refusal (0 LLM calls): report and exit non-zero so a
        # scripted caller cannot mistake a no-op for a cycle.
        _emit(result)
        raise click.exceptions.Exit(2)
    answer = result.get("answer")
    if answer:
        # C2 (plan §4.1): the cycle-close stdout surface IS the answer —
        # the operator sees it the moment the run settles. The machine
        # metadata stays beside it as the run-record read-out.
        click_echo(answer)
        click_echo("")
    _emit(result)


@inference_group.command("answer")
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--run-id", "run_id", default=None,
              help="render the answer of one exact run record")
def inference_answer(symbol: str, venue: str, run_id: str | None) -> None:
    """Return THE answer of a past cycle — the authoritative deliverable.

    The frozen ``answer_text`` exactly as delivered at cycle close (parity
    by identity). For the diagnostic record behind it (raw run JSON),
    use ``inference read``.
    """
    from market_service.nooa_harness.engine.core.answer import answer_from_record

    async def _run() -> dict[str, Any] | None:
        return (await _load_run_record(symbol, venue, run_id=run_id))[1]

    record = asyncio.run(_run())
    if record is None:
        raise click.ClickException(
            f"no run record for {symbol.upper()} {venue}"
            + (f" / {run_id}" if run_id else ""))
    answer_text = record.get("answer_text")
    if not answer_text:
        answer_text = answer_from_record(record)
    if answer_text is None:
        raise click.ClickException(
            "run record carries no authored answer (the cycle degraded or "
            "failed before authoring — `inference read` shows the record)")
    click_echo(answer_text)


@inference_group.command("read")
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--run-id", "run_id", default=None,
              help="read one exact run record")
@click.option("--redis-only", is_flag=True, help="skip Postgres; read the live projection")
def inference_read(symbol: str, venue: str, run_id: str | None,
                   redis_only: bool) -> None:
    """Read the diagnostic record of a past cycle (raw run JSON).

    Postgres-first, Redis projection fallback. For the authoritative
    deliverable (the frozen answer prose), use ``inference answer``.
    """
    async def _run() -> dict[str, Any]:
        source, record = await _load_run_record(
            symbol, venue, run_id=run_id, redis_only=redis_only)
        return {"source": source, "record": record}

    _emit(asyncio.run(_run()))


@inference_group.command("history")
@click.argument("symbol", default=_default_symbol)
@click.option("--venue", default=_default_venue)
@click.option("--limit", type=int, default=20)
def inference_history(symbol: str, venue: str, limit: int) -> None:
    """Read the diagnostic run history (canonical ledger, newest first)."""
    async def _run() -> dict[str, Any]:
        settings = _settings()
        postgres = PostgresRuntimeStore(settings.database_url)
        try:
            await postgres.connect()
            history = await postgres.read_canonical_history(
                symbol, limit=limit, venue=venue)
            return {"symbol": symbol.upper(), "count": len(history),
                    "source": "postgres", "history": history}
        finally:
            await postgres.close()

    _emit(asyncio.run(_run()))


# --------------------------------------------------------------------------
# telemetry — the governed kernel's read-only observation surface
# --------------------------------------------------------------------------
TELEMETRY_PLANES = (
    "ingestion", "capture", "calculation", "analysis", "agentic",
)


@command.group(
    "telemetry",
    help="Governed telemetry kernel: sink health and accepted state history "
         "(read-only; never a control surface).",
)
def telemetry_group() -> None:
    """Operational telemetry reads (PG replay source, Redis projections)."""


@telemetry_group.command("status")
def telemetry_status_cmd() -> None:
    """Sink health per owner + ledger availability."""
    from market_service.commands import telemetry as telemetry_cmd

    _emit(asyncio.run(telemetry_cmd.telemetry_status()))


@telemetry_group.command("read")
@click.option("--plane", required=True,
              type=click.Choice(TELEMETRY_PLANES),
              help="emission plane (ingestion/capture/calculation/analysis/agentic)")
@click.option("--owner", required=True,
              help="declared emitter: poller, capture, cycle_controller, or a worker name")
@click.option("--subject-type", default="symbol",
              help="subject identity class (symbol/session/cycle/runtime)")
@click.option("--subject-id", required=True,
              help="the subject's identity value (e.g. SOLUSDT, artifact id)")
@click.option("--limit", type=int, default=50,
              help="max events (bounded, newest last)")
@click.option("--rebuild", is_flag=True, default=False,
              help="rebuild the Redis projection from the durable PG ledger")
def telemetry_read_cmd(
    plane: str, owner: str, subject_type: str, subject_id: str,
    limit: int, rebuild: bool,
) -> None:
    """One subject's accepted event history + latest projection."""
    from market_service.commands import telemetry as telemetry_cmd

    _emit(asyncio.run(telemetry_cmd.telemetry_read(
        plane=plane, owner=owner, subject_type=subject_type,
        subject_id=subject_id, limit=limit, rebuild=rebuild,
    )))


__all__ = ["command"]