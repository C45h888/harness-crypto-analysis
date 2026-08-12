"""Build and persist one canonical live market-run envelope.

Two entrypoints are supported:

1. **Default (legacy unified path)** - calls ``analyze()`` end-to-end and
   mints a fresh ``run_id``. This path is preserved unchanged per the
   containerization contract's migration rule #4.

2. **Domain-aware path** (``--from-domain-state``) - reads the three latest
   domain envelopes from Redis (data-access, calculations, analysis),
   verifies they all share the orchestrator's ``run_id`` (provided via
   ``--run-id``), and builds the ``MarketRunEnvelope`` from their outputs.
   This is what the orchestrator uses so the final envelope's ``run_id``
   matches the cycle that just ran.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from market_service.analysis.market import analyze
from market_service.config import Settings
from market_service.runtime.contracts import MarketRunEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _domain_outputs(core: dict[str, Any]) -> dict[str, Any]:
    """Expose canonical outputs by domain without re-running legacy code."""
    return {
        "data_access": {
            "data_source": core.get("data_source"),
            "evidence": core.get("evidence", {}),
            "errors": core.get("errors", []),
        },
        "calculations": {
            "spot_flow": (core.get("spot") or {}).get("flow"),
            "futures_flow": (core.get("futures") or {}).get("flow"),
            "spot_bucketed_cvd": (core.get("spot") or {}).get("bucketed_cvd"),
            "futures_bucketed_cvd": (core.get("futures") or {}).get("bucketed_cvd"),
            "cvd_correlation": core.get("correlation"),
            "signal_inputs": core.get("signal_inputs"),
        },
        "analysis": {
            "signals": core.get("signals", []),
            "open_interest": core.get("open_interest_analysis"),
            "liquidation_pressure": core.get("liquidation_pressure"),
            "macro": core.get("macro_analysis"),
            "cryptoquant": core.get("cryptoquant"),
        },
    }


# ---------------------------------------------------------------------------
# Default (legacy) path - calls analyze()
# ---------------------------------------------------------------------------

async def build_envelope(symbol: str, trades: int, depth: int, window: int) -> MarketRunEnvelope:
    started_ms = int(time.time() * 1000)
    try:
        core = await analyze(symbol.upper(), trade_limit=trades, depth_limit=depth, bucket_window_s=window)
        completed_ms = int(time.time() * 1000)
        errors = list(core.get("errors") or [])
        status = core.get("status", "degraded")
        return MarketRunEnvelope.create(
            symbol=symbol,
            generated_at=_iso(started_ms),
            completed_at=_iso(completed_ms),
            status=status,
            data_source=core.get("data_source", "canonical_market_runtime"),
            coverage=core.get("coverage", {}),
            canonical_state=core,
            domain_outputs=_domain_outputs(core),
            errors=errors,
            source_metadata={
                "runtime": "market_service",
                "path": "unified_analyze",
                "requested": {"trades": trades, "depth": depth, "window": window},
                "latency_ms": round((completed_ms - started_ms), 1),
            },
        )
    except Exception as exc:
        completed_ms = int(time.time() * 1000)
        return MarketRunEnvelope.create(
            symbol=symbol,
            generated_at=_iso(started_ms),
            completed_at=_iso(completed_ms),
            status="invalid",
            data_source="canonical_market_runtime",
            coverage={}, canonical_state={}, domain_outputs={},
            errors=[{"stage": "build_envelope", "error": f"{type(exc).__name__}: {exc}"}],
            source_metadata={"runtime": "market_service", "path": "unified_analyze"},
        )


# ---------------------------------------------------------------------------
# Domain-aware path - reads the three latest domain envelopes from Redis
# and uses the orchestrator's run_id verbatim.
# ---------------------------------------------------------------------------

class RunIdMismatch(Exception):
    """Raised when a domain envelope's run_id does not match the requested run_id."""


class MissingDomainState(Exception):
    """Raised when a required domain envelope is not available."""


async def build_envelope_from_domain_states(
    symbol: str,
    run_id: str,
    settings: Settings,
) -> MarketRunEnvelope:
    """Build a ``MarketRunEnvelope`` from the latest domain states.

    Reads ``<prefix>:runtime-run:<run_id>:domain:<source>`` for each of
    data-access / calculations / analysis. Verifies each envelope's ``run_id``
    matches the requested ``run_id`` so the resulting envelope is provably
    derived from THIS cycle's outputs (the same ``run_id`` the orchestrator
    has been tracking).

    On missing or mismatched state, the function still returns a
    ``MarketRunEnvelope`` (with ``status="invalid"`` and structured
    ``canonical_state["domain_status"]`` markers) rather than raising. This
    matches the containerization contract acceptance criterion #11: "A source
    failure produces degraded or invalid, never fabricated healthy data."
    """
    started_ms = int(time.time() * 1000)
    sources = ("data-access", "calculations", "analysis")
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    envelopes: dict[str, Any] = {}
    domain_status: dict[str, str] = {}
    domain_errors: list[dict[str, Any]] = []

    try:
        for source in sources:
            env = await redis.read_run_domain_state(run_id, source)
            if env is None:
                domain_status[source] = "missing"
                domain_errors.append({
                    "domain": source,
                    "error": f"missing domain envelope for run_id={run_id}",
                    "details": {"run_id": run_id, "source": source},
                })
                continue
            if env.run_id != run_id:
                domain_status[source] = "mismatch"
                domain_errors.append({
                    "domain": source,
                    "error": f"run_id mismatch: expected {run_id}, got {env.run_id}",
                    "details": {"expected": run_id, "got": env.run_id},
                })
                continue
            domain_status[source] = env.status
            for err in env.errors:
                domain_errors.append({"domain": env.source, **err})
            for err in (env.data.get("errors") or []):
                domain_errors.append({"domain": env.source, **err})
            envelopes[source] = env
    finally:
        await redis.close()

    if "missing" in domain_status.values() or "mismatch" in domain_status.values():
        status = "invalid"
    else:
        statuses = [env.status for env in envelopes.values()]
        if "invalid" in statuses:
            status = "invalid"
        elif "degraded" in statuses or domain_errors:
            status = "degraded"
        else:
            status = "healthy"

    completed_ms = int(time.time() * 1000)
    coverage = {
        "flow_window_seconds": (envelopes.get("calculations").data.get("coverage_seconds") if envelopes.get("calculations") else None),
        "domain_observed_at": {s: envelopes[s].observed_at for s in sources if s in envelopes},
        "domain_status": domain_status,
    }
    canonical_state: dict[str, Any] = {"domain_status": dict(domain_status)}
    for source in sources:
        if source in envelopes:
            canonical_state[source] = dict(envelopes[source].data)
            canonical_state[source]["run_id"] = envelopes[source].run_id

    return MarketRunEnvelope(
        run_id=run_id,
        symbol=symbol.upper(),
        generated_at=_iso(started_ms),
        completed_at=_iso(completed_ms),
        status=status,
        data_source="domain_pipeline",
        coverage=coverage,
        canonical_state=canonical_state,
        domain_outputs=canonical_state,
        errors=tuple(domain_errors),
        source_metadata={
            "runtime": "market_service",
            "path": "domain_aware_collate",
            "latency_ms": round((completed_ms - started_ms), 1),
            "domain_observed_at": {s: envelopes[s].observed_at for s in sources if s in envelopes},
            "domain_run_ids": {s: envelopes[s].run_id for s in sources if s in envelopes},
        },
    )


# ---------------------------------------------------------------------------
# Persistence - PostgreSQL first, then Redis with the identical payload.
# ---------------------------------------------------------------------------

async def persist_envelope(envelope: MarketRunEnvelope, settings: Settings) -> dict[str, Any]:
    """Persist PostgreSQL first, then publish the same bytes to Redis."""
    postgres = PostgresRuntimeStore(settings.database_url)
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    try:
        await postgres.connect()
        inserted = await postgres.insert_run(envelope)
        redis_stream_id = await redis.publish_run(envelope)
        return {
            "run_id": envelope.run_id,
            "postgres_inserted": inserted,
            "redis_stream_id": redis_stream_id,
            "redis_key": redis.collated_latest_key(envelope.symbol),
            "schema_version": envelope.schema_version,
            "status": envelope.status,
        }
    finally:
        await postgres.close()
        await redis.close()


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------

async def run(symbol: str, trades: int, depth: int, window: int) -> dict[str, Any]:
    settings = Settings.from_env()
    envelope = await build_envelope(symbol, trades, depth, window)
    persistence = await persist_envelope(envelope, settings)
    return {"envelope": envelope.to_dict(), "persistence": persistence}


async def run_from_domain_state(symbol: str, *, run_id: str) -> dict[str, Any]:
    settings = Settings.from_env()
    envelope = await build_envelope_from_domain_states(symbol, run_id, settings)
    persistence = await persist_envelope(envelope, settings)
    return {"envelope": envelope.to_dict(), "persistence": persistence}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist one canonical live market-run envelope")
    parser.add_argument("symbol", nargs="?", default="SOLUSDT")
    parser.add_argument("--trades", type=int, default=500)
    parser.add_argument("--depth", type=int, default=50)
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--from-domain-state",
        action="store_true",
        help="Build the envelope from the latest data-access / calculations / analysis "
             "envelopes in Redis instead of re-running analyze() end-to-end.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Required when --from-domain-state is set. The orchestrator's run_id; "
             "the collator verifies all three domain envelopes share this run_id.",
    )
    args = parser.parse_args(argv)

    if args.from_domain_state:
        if not args.run_id:
            print(json.dumps({"error": "--from-domain-state requires --run-id"}))
            return 2
        result = asyncio.run(run_from_domain_state(args.symbol, run_id=args.run_id))
    else:
        result = asyncio.run(run(args.symbol, args.trades, args.depth, args.window))

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        env = result["envelope"]
        print(env["run_id"])
    return 0 if result["envelope"]["status"] != "invalid" else 1


if __name__ == "__main__":
    raise SystemExit(main())