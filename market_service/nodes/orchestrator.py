"""Pipeline orchestrator.

The orchestrator is the only timer-driven service. Each ``POLL_SECONDS`` it
runs a fresh pipeline for every configured symbol:

  1. ``RefreshCommand(domain="data-access", run_id=R, symbol=S)``  -> ``stream:commands``
  2. wait for ``data_access_complete`` event with matching ``run_id``
  3. ``RefreshCommand(domain="calculations", run_id=R, ...)`` -> ``stream:commands``
  4. wait for ``calculations_complete`` event
  5. ``RefreshCommand(domain="analysis", run_id=R, ...)`` -> ``stream:commands``
  6. wait for ``analysis_complete`` event
  7. invoke ``python -m market_service.commands.collate <SYM> --json`` so the
     ``MarketRunEnvelope`` is written to PostgreSQL and published to Redis.

It does not contain any domain logic; it just sequences the existing services.

Run::

    python -m market_service.nodes.orchestrator
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from market_service.config import Settings
from market_service.runtime.contracts import RefreshCommand, RuntimeRunState
from market_service.runtime.redis_store import RedisRuntimeStore

from ._base import install_signal_handlers, setup_logging

log = logging.getLogger(__name__)

DOMAIN_ORDER = ("data-access", "calculations", "analysis")
DEFAULT_STEP_TIMEOUT_S = 60.0


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def send_command(
    redis: RedisRuntimeStore,
    *,
    domain: str,
    symbol: str,
    run_id: str,
    parameters: dict[str, Any] | None = None,
    requested_by: str = "orchestrator",
) -> str:
    command = RefreshCommand(
        domain=domain,
        symbol=symbol,
        requested_by=requested_by,
        command_id=run_id,
        parameters={"run_id": run_id, **(parameters or {})},
    )
    return await redis.request_refresh(command)


async def run_collator_subprocess(symbol: str, run_id: str) -> dict[str, Any]:
    """Invoke the collator in domain-aware mode and verify the envelope's run_id matches this cycle.

    The collator subprocess is given ``--from-domain-state --run-id <run_id>``
    so it reads the three latest domain envelopes from Redis, verifies they all
    carry our ``run_id``, and persists a single ``MarketRunEnvelope`` whose
    ``run_id`` is the SAME id we have been tracking. If the collator persists
    a different ``run_id`` (e.g. fell back to the unified ``analyze()`` path
    for some reason), we surface the mismatch as a structured failure rather
    than silently accepting a cycle that is not actually traceable to the
    domain outputs.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "market_service.commands.collate",
        symbol, "--json", "--from-domain-state", "--run-id", run_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error(
            "collator failed for %s run_id=%s (rc=%s): %s",
            symbol, run_id, proc.returncode, stderr.decode("utf-8", "replace"),
        )
        return {"ok": False, "stderr": stderr.decode("utf-8", "replace"), "returncode": proc.returncode}
    try:
        parsed = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        log.error("collator returned non-JSON for %s: %s", symbol, exc)
        return {"ok": False, "stderr": "non-json output", "stdout": stdout.decode("utf-8", "replace")[:1024]}
    persisted_run_id = (parsed.get("envelope") or {}).get("run_id")
    if persisted_run_id != run_id:
        log.error(
            "collator persisted run_id=%s but orchestrator expected run_id=%s",
            persisted_run_id, run_id,
        )
        return {
            "ok": False,
            "stderr": "run_id mismatch between orchestrator and collated envelope",
            "expected": run_id,
            "got": persisted_run_id,
            "result": parsed,
        }
    return {"ok": True, "result": parsed}


async def run_cycle(redis: RedisRuntimeStore, symbol: str, settings: Settings) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    started = _utc_iso()
    domains = {domain: "pending" for domain in DOMAIN_ORDER}
    await redis.write_runtime_run(RuntimeRunState(
        run_id=run_id, symbol=symbol, phase="INITIALIZING", started_at=started,
        updated_at=started, domains=domains,
    ))
    log.info("orchestrator cycle start symbol=%s run_id=%s", symbol, run_id)

    for domain in DOMAIN_ORDER:
        phase = {"data-access": "COLLECTING", "calculations": "CALCULATING", "analysis": "ANALYZING"}[domain]
        await redis.write_runtime_run(RuntimeRunState(
            run_id=run_id, symbol=symbol, phase=phase, started_at=started,
            updated_at=_utc_iso(), domains=domains,
        ))
        await send_command(
            redis,
            domain=domain,
            symbol=symbol,
            run_id=run_id,
            parameters={"depth_levels": settings.depth_levels, "flow_window_seconds": settings.flow_window_seconds},
        )
        result = await redis.wait_for_result(
            run_id=run_id,
            source=domain,
            timeout_s=DEFAULT_STEP_TIMEOUT_S,
        )
        if result is None:
            log.error("orchestrator timeout waiting for %s run_id=%s", domain, run_id)
            domains[domain] = "timeout"
            await redis.write_runtime_run(RuntimeRunState(
                run_id=run_id, symbol=symbol, phase="FAILED", started_at=started,
                updated_at=_utc_iso(), domains=domains, publication="not_attempted",
                errors=({"stage": domain, "error": "timeout"},),
            ))
            return {"ok": False, "stage": domain, "run_id": run_id, "started": started}
        domains[domain] = str(result.payload.get("status", "invalid"))

    await redis.write_runtime_run(RuntimeRunState(
        run_id=run_id, symbol=symbol, phase="COLLATING", started_at=started,
        updated_at=_utc_iso(), domains=domains,
    ))
    collate = await run_collator_subprocess(symbol, run_id)
    final_phase = "PUBLISHED" if collate.get("ok") else "FAILED"
    publication = "published" if collate.get("ok") else "failed"
    await redis.write_runtime_run(RuntimeRunState(
        run_id=run_id, symbol=symbol, phase=final_phase, started_at=started,
        updated_at=_utc_iso(), domains=domains, publication=publication,
        errors=tuple() if collate.get("ok") else ({"stage": "collation", "error": str(collate.get("stderr", "failed"))},),
    ))
    log.info("orchestrator cycle complete run_id=%s collator_ok=%s", run_id, collate.get("ok"))
    return {
        "ok": collate.get("ok", False),
        "run_id": run_id,
        "symbol": symbol,
        "started": started,
        "completed": _utc_iso(),
        "collator": collate,
    }


async def main() -> int:
    setup_logging()
    settings = Settings.from_env()
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix, settings.redis_stream_maxlen)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    poll_seconds = settings.poll_seconds
    log.info("orchestrator starting (symbols=%s poll=%ss)", settings.symbols, poll_seconds)
    try:
        if not await redis.ping():
            log.error("redis ping failed; aborting orchestrator")
            return 1
        while not stop.is_set():
            cycle_started = time.monotonic()
            for symbol in settings.symbols:
                if stop.is_set():
                    break
                try:
                    await run_cycle(redis, symbol, settings)
                except Exception:
                    log.exception("orchestrator cycle failed for %s", symbol)
            elapsed = time.monotonic() - cycle_started
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, poll_seconds - elapsed))
            except asyncio.TimeoutError:
                pass
    finally:
        await redis.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
