"""Build and persist one canonical live market-run envelope."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from market_service.analysis.market import analyze
from market_service.config import Settings
from market_service.runtime.contracts import MarketRunEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore


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
            source_metadata={"runtime": "market_service"},
        )


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
        }
    finally:
        await postgres.close()
        await redis.close()


async def run(symbol: str, trades: int, depth: int, window: int) -> dict[str, Any]:
    settings = Settings.from_env()
    envelope = await build_envelope(symbol, trades, depth, window)
    persistence = await persist_envelope(envelope, settings)
    return {"envelope": envelope.to_dict(), "persistence": persistence}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist one canonical live market-run envelope")
    parser.add_argument("symbol", nargs="?", default="SOLUSDT")
    parser.add_argument("--trades", type=int, default=500)
    parser.add_argument("--depth", type=int, default=50)
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = asyncio.run(run(args.symbol, args.trades, args.depth, args.window))
    print(json.dumps(result, indent=2, default=str) if args.json else result["envelope"]["run_id"])
    return 0 if result["envelope"]["status"] != "invalid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
