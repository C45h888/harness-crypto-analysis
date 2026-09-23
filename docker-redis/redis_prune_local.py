"""Redis retention worker — time-vector pruning inside the redis substrate.

Self-contained (no import from market_service/). Runs as a third process in
the redis container, sharing /data and the network namespace with
redis-server and the telemetry hygiene cleaner. It is bounded to the redis
substrate and the container: it never crosses into the canonical
market_service package, never touches Postgres, and never crosses a semantic
boundary of the canonical runtime state.

WHAT IT PRUNES (all by ENTRY AGE, keyed on the stream id's epoch-ms prefix
— the vector that `publish_raw_evidence` stores as `ts`, and that every
reader already keys on for its evidence window):

  class="evidence"   stream:raw:*  +  stream:microstructure:*:raw/*
      retention:  RAW_RETENTION_S   (default 4h)
      every entry older than RAW_RETENTION_S is deleted (oldest first), BUT
      the newest RAW_STREAM_ENTRIES (default 90) are always kept regardless
      of age so the live analysis window is never starved, and
      a hard length beam (default 8h of the poll cadence) bounds a burst.

  class="calculation"  stream:substrate:*  stream:domain:*  stream:collated:*
      retention:  CALC_RETENTION_S  (default 24h)
      all calculation state streams older than 1 day are deleted.
      `latest:*` projections are NEVER touched by this worker — for the
      run-cycle surfaces (`latest:*:collated` etc.) the durable ledger is
      Postgres, and the present snapshot must always remain readable.

SAFETY RAILS (doctrine — mirror of telemetry hygiene):
  - only XDEL on stream root entries / XTRIM MINID; NO FLUSHALL/FLUSHDB/DEBUG.
  - skips its cycle while aof_rewrite_in_progress or rdb_bgsave_in_progress
    (never interleaves with redis's own persistence writers).
  - consumer-group readers (XREADGROUP ">") are unaffected by XDEL of old
    root entries — no reader position is ever rewound.
  - DRY_RUN=true logs candidates without deleting (default sample-safe).

Every cycle emits one structured JSON log line.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

try:
    import redis  # provided by redis PyPI package installed in the image
except ImportError:
    print("redis package missing", file=sys.stderr)
    sys.exit(1)

INTERVAL_S = int(os.environ.get("REDIS_PRUNE_INTERVAL_S", "300"))
PRUNE_URL = os.environ.get("REDIS_PRUNE_URL", "unix:///tmp/redis.sock")
PREFIX = (os.environ.get("REDIS_KEY_PREFIX") or "marketflow").strip(":")

# Age retentions (seconds). The TIME vector is the decider.
RAW_RETENTION_S = int(os.environ.get("REDIS_PRUNE_RAW_RETENTION_S", "14400"))   # 4h
CALC_RETENTION_S = int(os.environ.get("REDIS_PRUNE_CALC_RETENTION_S", "86400")) # 24h

# Length-beam floor + guard: newest entries always kept so the live window
# survives; a hard entry-cap bounds a burst within the age window.
RAW_STREAM_ENTRIES = int(os.environ.get("REDIS_PRUNE_RAW_ENTRIES", "90"))
RAW_MAX_ENTRIES = int(os.environ.get("REDIS_PRUNE_RAW_MAX", "4096"))
POLL_CADENCE_S = int(os.environ.get("REDIS_PRUNE_POLL_CADENCE_S", "5"))

DRY_RUN = os.environ.get("REDIS_PRUNE_DRY_RUN", "true").lower() == "true"

log = logging.getLogger("redis_prune")
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

_client = redis.Redis.from_url(PRUNE_URL, decode_responses=True)


def _id_ms(entry_id: str) -> int | None:
    """Epoch-ms from a stream entry id (`ms-seq`)."""
    try:
        return int(str(entry_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return None


def _trim_stream(key: str, now_s: int, retention_s: int, report: dict) -> None:
    """Delete entries of one stream older than `retention_s`, oldest first.

    Keeps the newest RAW_STREAM_ENTRIES unconditionally (floor) and applies
    a hard length cap. XDEL is used so consumer-group read positions are
    never re-created/rewound the way deleting the stream itself would.
    """
    try:
        total = int(_client.xlen(key))
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"key": key, "step": "xlen", "error": str(e)})
        return
    if total <= 0:
        return

    try:
        rng = _client.xrange(key, count=total)
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"key": key, "step": "xrange", "error": str(e)})
        return

    entries = [(eid, _id_ms(eid)) for eid, _fields in rng]
    now_ms = int(now_s * 1000)

    # Hard length beam first: if the stream has exceeded the cap, trim the
    # oldest down to the cap regardless of age.
    to_trim: list[str] = []
    overflow = total - RAW_MAX_ENTRIES
    if overflow > 0:
        to_trim += [eid for eid, _ in entries[:overflow]]

    # Age retention: keep past the index of the newest `floor` entries and
    # past the retention window; anything older and not in the floor is a
    # candidate.
    floor_index = max(0, total - RAW_STREAM_ENTRIES)
    for idx, (eid, ms) in enumerate(entries):
        if idx < floor_index and (ms is None or (now_ms - ms) > retention_s * 1000):
            to_trim.append(eid)

    if not to_trim:
        report["kept"] = total
        report["retention_s"] = retention_s
        return

    report["candidates"] = len(to_trim)
    report["current_len"] = total
    report["retention_s"] = retention_s
    if DRY_RUN:
        report["dry_run"] = True
        return
    try:
        _client.xdel(key, *to_trim)
        report["del"] = len(to_trim)
        report["new_len"] = int(_client.xlen(key))
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"key": key, "step": "xdel", "error": str(e)})
        report["del"] = 0
        report["new_len"] = total


def _calc_stream(key: str, now_s: int, report: dict) -> None:
    """A calculation stream uses the 24h retention (with the same floor)."""
    _trim_stream(key, now_s, CALC_RETENTION_S, report)


def cycle(now_s: int | None = None) -> dict:
    now_s = int(now_s or time.time())
    report = {
        "cycle_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s)),
        "raw_retention_hours": RAW_RETENTION_S / 3600,
        "calc_retention_hours": CALC_RETENTION_S / 3600,
        "raw_entries_floor": RAW_STREAM_ENTRIES,
        "dump_in_progress": False,
        "poll_cadence_s": POLL_CADENCE_S,
        "dry_run": DRY_RUN,
        "evidence_streams": 0,
        "calc_streams": 0,
        "deleted_total": 0,
        "errors": [],
    }

    try:
        info_p = _client.info("persistence")
        report["dump_in_progress"] = bool(
            info_p.get("aof_rewrite_in_progress", 0)
            or info_p.get("rdb_bgsave_in_progress", 0)
        )
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"step": "info persistence", "error": str(e)})
        return report
    if report["dump_in_progress"]:
        log.info("redis_prune.cycle_skip busy persistence")
        return report

    # Enumerate keys once; classify by prefix.
    evidence_keys: list[str] = []
    calc_keys: list[str] = []
    try:
        for k in _client.scan_iter(count=500):
            if k.startswith(f"{PREFIX}:stream:raw:"):
                evidence_keys.append(k)
            elif k.startswith(f"{PREFIX}:stream:microstructure:"):
                evidence_keys.append(k)
            elif (
                k.startswith(f"{PREFIX}:stream:substrate:")
                or k.startswith(f"{PREFIX}:stream:domain:")
                or k.startswith(f"{PREFIX}:stream:collated:")
            ):
                calc_keys.append(k)
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"step": "scan", "error": str(e)})
        return report

    report["evidence_streams"] = len(evidence_keys)
    report["calc_streams"] = len(calc_keys)

    for key in sorted(evidence_keys):
        r = {"key": key}
        _trim_stream(key, now_s, RAW_RETENTION_S, r)
        report["deleted_total"] += r.get("del", 0)
        report.setdefault("streams", []).append(r)

    for key in sorted(calc_keys):
        r = {"key": key}
        _trim_stream(key, now_s, CALC_RETENTION_S, r)
        report["deleted_total"] += r.get("del", 0)
        report.setdefault("streams", []).append(r)

    return report


def main() -> int:
    log.info(
        "redis_prune.started interval_s=%d raw_h=%d calc_h=%d url=%s prefix=%s dry_run=%s",
        INTERVAL_S, RAW_RETENTION_S // 3600, CALC_RETENTION_S // 3600,
        PRUNE_URL, PREFIX, DRY_RUN,
    )
    while not _stop:
        try:
            rep = cycle()
            log.info("redis_prune.cycle %s", json.dumps(rep, default=str))
        except Exception as e:  # noqa: BLE001
            log.warning("redis_prune.cycle_error %s", e)
        for _ in range(INTERVAL_S):
            if _stop:
                break
            time.sleep(1)
    log.info("redis_prune.stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())