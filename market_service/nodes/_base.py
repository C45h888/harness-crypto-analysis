"""Shared bootstrap for stream-driven domain service nodes.

The containerization contract mandates that each domain service subscribe to
``<prefix>:stream:commands`` rather than running an internal poll timer. This
module holds the shared bootstrap: structured logging, signal handling,
envelope construction, run-id propagation, and graceful shutdown. Domain-
specific node files (data_access / calculations / analysis) stay thin
adapters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from market_service.config import Settings
from market_service.runtime.contracts import (
    MARKET_STATE_SCHEMA_VERSION,
    MarketEvent,
    MarketStateEnvelope,
    RefreshCommand,
    StateStatus,
)
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

# Type alias: a handler turns a single RefreshCommand into the data payload
# that should be wrapped in a MarketStateEnvelope and published downstream.
CommandHandler = Callable[[RefreshCommand], Awaitable[dict[str, Any]]]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_envelope(
    *,
    symbol: str,
    source: str,
    data: dict[str, Any],
    status: StateStatus,
    errors: list[dict[str, Any]] | None = None,
    observed_at: str | None = None,
    coverage_seconds: int | None = None,
) -> MarketStateEnvelope:
    """Construct a versioned envelope that satisfies the wire contract.

    ``run_id`` is attached by ``_handle_one`` after the domain handler has
    returned. It remains an explicit top-level transport field while the
    schema version stays compatible with the existing envelope contract.
    """
    now = _utc_iso()
    return MarketStateEnvelope(
        symbol=symbol.upper(),
        source=source,
        observed_at=observed_at or now,
        produced_at=now,
        status=status,
        data=data,
        errors=tuple(errors or ()),
        coverage_seconds=coverage_seconds,
        schema_version=MARKET_STATE_SCHEMA_VERSION,
    )


def make_failure_envelope(
    *,
    symbol: str,
    source: str,
    stage: str,
    exc: BaseException,
    partial_data: dict[str, Any] | None = None,
) -> MarketStateEnvelope:
    """Build an ``invalid`` envelope for a fatal domain error.

    Per the contract: "A failed domain service is observable and does not
    silently produce a healthy envelope." This helper ensures every failure is
    recorded with structured ``errors`` and never returns ``healthy``.
    """
    err = {"stage": stage, "error": f"{type(exc).__name__}: {exc}"}
    return build_envelope(
        symbol=symbol,
        source=source,
        data=partial_data or {},
        status="invalid",
        errors=[err],
    )


async def publish_completion(
    redis: RedisRuntimeStore,
    *,
    source: str,
    run_id: str,
    symbol: str,
    status: StateStatus,
    extras: dict[str, Any] | None = None,
) -> str:
    """Publish a structured ``MarketEvent`` to ``<prefix>:stream:results``.

    Used by domain nodes to signal the orchestrator that a step in the
    pipeline has finished (so it can advance to the next step).
    """
    payload: dict[str, Any] = {
        "source": source,
        "run_id": run_id,
        "symbol": symbol.upper(),
        "status": status,
    }
    if extras:
        payload.update(extras)
    return await redis.publish_result(
        MarketEvent(
            event_type=f"{source}_complete",
            symbol=symbol,
            payload=payload,
            event_id=run_id,
        )
    )


async def run_command_loop(
    *,
    redis: RedisRuntimeStore,
    domain: str,
    handler: CommandHandler,
    settings: Settings,
    stop: asyncio.Event,
    source: str,
) -> None:
    """Generic command-consumption loop shared by every domain node.

    Watches ``<prefix>:stream:commands`` for entries whose ``domain`` field
    matches this node, runs ``handler``, publishes a ``MarketStateEnvelope``
    to the domain stream + latest key, and emits a completion event.
    """
    log.info("[%s] starting command loop (symbols=%s)", source, settings.symbols)
    last_id = "$"  # do not replay history on fresh start
    while not stop.is_set():
        try:
            async for _entry_id, command in redis.consume_commands(
                domain=domain, last_id=last_id, block_ms=2000, count=8
            ):
                if stop.is_set():
                    break
                last_id = _entry_id
                await _handle_one(
                    redis=redis,
                    command=command,
                    handler=handler,
                    settings=settings,
                    source=source,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("[%s] loop iteration failed; continuing", source)
            await asyncio.sleep(1.0)
    log.info("[%s] command loop stopped", source)


async def _handle_one(
    *,
    redis: RedisRuntimeStore,
    command: RefreshCommand,
    handler: CommandHandler,
    settings: Settings,
    source: str,
) -> None:
    """Process one matching command: invoke handler, publish envelope + event.

    The ``run_id`` is derived (in priority order) from:

      1. ``command.command_id``  - the orchestrator's run id
      2. ``command.parameters["run_id"]``  - alternative path
      3. a freshly generated uuid  - standalone test fallback

    The ``run_id`` is stamped into the published envelope's ``data["run_id"]``
    field (additive - no schema_version bump) so the collator can prove the
    final MarketRunEnvelope is derived from THIS cycle's domain states.
    """
    symbol = command.symbol.upper()
    run_id = (
        command.command_id
        or (command.parameters or {}).get("run_id")
        or str(uuid.uuid4())
    )
    log.info(
        "[%s] handling command domain=%s symbol=%s run_id=%s",
        source, command.domain, symbol, run_id,
    )
    try:
        data = await handler(command)
        envelope = build_envelope(
            symbol=symbol,
            source=source,
            data=data,
            status=data.get("status", "healthy"),
            errors=data.get("errors") or [],
            coverage_seconds=data.get("coverage_seconds"),
        )
        # Stamp run_id onto the envelope itself so the Redis store can index
        # it under <prefix>:runtime-run:<run_id>:domain:<source>. This is an
        # additive top-level field; schema_version stays at 1.
        envelope_with_run = MarketStateEnvelope(
            symbol=envelope.symbol,
            source=envelope.source,
            observed_at=envelope.observed_at,
            produced_at=envelope.produced_at,
            status=envelope.status,
            data=envelope.data,
            run_id=run_id,
            errors=envelope.errors,
            coverage_seconds=envelope.coverage_seconds,
            schema_version=envelope.schema_version,
        )
        await redis.publish_domain_state(envelope_with_run)
        await publish_completion(
            redis,
            source=source,
            run_id=run_id,
            symbol=symbol,
            status=envelope.status,
            extras={"envelope": envelope.to_dict()},
        )
    except Exception as exc:
        log.exception("[%s] handler failed for %s", source, symbol)
        # Build a failure envelope that still carries run_id for provenance.
        failure = make_failure_envelope(
            symbol=symbol,
            source=source,
            stage="handler",
            exc=exc,
        )
        failure_with_run = MarketStateEnvelope(
            symbol=failure.symbol,
            source=failure.source,
            observed_at=failure.observed_at,
            produced_at=failure.produced_at,
            status=failure.status,
            data=failure.data,
            run_id=run_id,
            errors=failure.errors,
            coverage_seconds=failure.coverage_seconds,
            schema_version=failure.schema_version,
        )
        # Distinguish contract violations from generic errors so the collator
        # can mark the envelope accordingly without inventing a healthy state.
        from .contracts import ContractViolation
        contract_violation = isinstance(exc, ContractViolation)
        try:
            await redis.publish_domain_state(failure_with_run)
        except Exception:
            log.exception("[%s] failed to publish failure envelope", source)
        await publish_completion(
            redis,
            source=source,
            run_id=run_id,
            symbol=symbol,
            status="invalid",
            extras={
                "error": f"{type(exc).__name__}: {exc}",
                "contract_violation": contract_violation,
            },
        )


def install_signal_handlers(stop: asyncio.Event) -> None:
    def _stop(*_: Any) -> None:
        log.info("signal received; initiating graceful shutdown")
        stop.set()
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _stop)
    except NotImplementedError:
        # Windows / restricted environments: no-op; the orchestrator will
        # rely on the container lifecycle.
        pass


def setup_logging(level_name: str | None = None) -> None:
    import os
    level = (level_name or os.getenv("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def dump(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))
