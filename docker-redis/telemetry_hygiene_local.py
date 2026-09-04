"""Periodic dead-weight cleanup inside the redis container.

Self-contained (no import from market_service/). Connects to redis
over the local unix socket, walks /data directly with os.stat /
Path.unlink. No socket mount, no shell-out, no subprocess, no
network calls.

Cycle behaviour:
  1. Read INFO persistence from redis.
  2. If aof_rewrite_in_progress or rdb_bgsave_in_progress is set,
     skip the cycle (do not touch /data).
  3. Enumerate /data. For each file matching temp-rewriteaof-*.aof
     or temp-*.rdb, with size > 0 and mtime older than
     TELEMETRY_HYGIENE_MIN_AGE_S (default 3600s), queue for removal.
  4. If DRY_RUN is set, just report. Otherwise unlink each candidate.
  5. Emit one structured log line per cycle to stderr.

Doctrine alignment:
  - no legacy coupling (this file is self-contained in the redis image)
  - no socket mount anywhere
  - no exchange credentials
  - postgres wall_snapshot is unaffected
  - doctrine §Safety rules: no FLUSHALL/FLUSHDB/DEBUG; only unlinks
    stale temp files; safety margin of MIN_AGE_S before touching
    anything
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

try:
    import redis  # provided by redis PyPI package installed in the image
except ImportError:
    print("redis package missing", file=sys.stderr)
    sys.exit(1)

DATA = Path("/data")
INTERVAL_S = int(os.environ.get("TELEMETRY_HYGIENE_INTERVAL_S", "300"))
MIN_AGE_S = int(os.environ.get("TELEMETRY_HYGIENE_MIN_AGE_S", "3600"))
REDIS_URL = os.environ.get("REDIS_HYGIENE_URL", "unix:///tmp/redis.sock")
DRY_RUN = os.environ.get("TELEMETRY_HYGIENE_DRY_RUN", "false").lower() == "true"

log = logging.getLogger("telemetry_hygiene")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
    stream=sys.stderr,
)

_stop = False


def _sig(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def _candidates() -> list[Path]:
    now = time.time()
    out: list[Path] = []
    if not DATA.exists():
        return out
    for p in DATA.iterdir():
        n = p.name
        is_temp_aof = n.startswith("temp-rewriteaof-") and n.endswith(".aof")
        is_temp_rdb = n.startswith("temp-") and n.endswith(".rdb")
        if not (is_temp_aof or is_temp_rdb):
            continue
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if st.st_size <= 0:
            continue
        if (now - st.st_mtime) < MIN_AGE_S:
            continue
        out.append(p)
    return out


def cycle() -> dict:
    report = {
        "cycle_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files_removed": 0,
        "bytes_removed": 0,
        "live_keys": 0,
        "rewrite_in_progress": False,
        "bgsave_in_progress": False,
        "dry_run": DRY_RUN,
        "candidates": [],
        "errors": [],
    }
    try:
        info_p = _client.info("persistence")
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"step": "info persistence", "error": str(e)})
        log.warning("telemetry_hygiene.cycle_error %s", e)
        return report
    report["rewrite_in_progress"] = bool(info_p.get("aof_rewrite_in_progress", 0))
    report["bgsave_in_progress"] = bool(info_p.get("rdb_bgsave_in_progress", 0))
    if report["rewrite_in_progress"] or report["bgsave_in_progress"]:
        log.info(
            "telemetry_hygiene.cycle_skip busy rewrite=%s bgsave=%s",
            report["rewrite_in_progress"], report["bgsave_in_progress"],
        )
        return report

    try:
        report["live_keys"] = int(_client.dbsize())
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"step": "dbsize", "error": str(e)})

    cands = _candidates()
    report["candidates"] = [
        {"name": str(p.name), "size": p.stat().st_size} for p in cands
    ]
    if not cands or DRY_RUN:
        return report

    removed_bytes = 0
    for p in cands:
        try:
            sz = p.stat().st_size
            p.unlink()
            removed_bytes += sz
            report["files_removed"] += 1
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            report["errors"].append({"step": "unlink", "path": str(p), "error": str(e)})
    report["bytes_removed"] = removed_bytes
    return report


def main() -> int:
    log.info(
        "telemetry_hygiene.started interval_s=%d min_age_s=%d url=%s dry_run=%s",
        INTERVAL_S, MIN_AGE_S, REDIS_URL, DRY_RUN,
    )
    while not _stop:
        try:
            report = cycle()
            log.info("telemetry_hygiene.cycle %s", json.dumps(report))
        except Exception as e:  # noqa: BLE001
            log.warning("telemetry_hygiene.cycle_error %s", e)
        # Sleep with interruptible granularity so SIGTERM exits cleanly.
        for _ in range(INTERVAL_S):
            if _stop:
                break
            time.sleep(1)
    log.info("telemetry_hygiene.stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())