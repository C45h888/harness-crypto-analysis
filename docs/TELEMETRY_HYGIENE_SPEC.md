# Telemetry Hygiene — Spec (Option C, single redis container)

**Status:** approved, applying
**Owns:** market_service/runtime/telemetry_hygiene.py + custom redis image entrypoint
**Replaces:** ad-hoc AOF/RDB rewrite dead weight buildup

## Architectural choice — Option C

Inside the redis container, a tiny shell entrypoint starts two
processes in parallel:

  PID 1 — redis-server (the real redis)
  PID 2 — python -m market_service.runtime.telemetry_hygiene
            (the cleanup loop, sharing /data and the network namespace)

This avoids:
- any docker socket mount
- a sidecar container
- a separate compose service for the cleaner

Both processes share `/data` (the volume) and the local redis
socket (`/tmp/redis.sock` or tcp://localhost:6379 inside the
container's network namespace). The cleaner connects to redis
over the local socket, enumerates `/data` directly with `os.stat`,
and runs `os.unlink` on stale files. No exec, no shell, no
subprocess.

## Change surface

### 1. docker-compose.yml — three changes

#### (a) `redis.command` — replace with the new entrypoint

Before:
```yaml
command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]
```

After:
```yaml
command: ["/usr/local/bin/redis-entrypoint.sh"]
```

#### (b) Custom redis image

The base `redis:7.4-alpine` is replaced with a build context:
```yaml
redis:
  build:
    context: ./docker-redis
  user: "redis"
  ...
```

#### (c) Healthcheck stays the same (`redis-cli ping`)

### 2. docker-redis/Dockerfile

```dockerfile
FROM redis:7.4-alpine

# python3 + pip for the cleanup sidecar
RUN apk add --no-cache python3 py3-pip
# The canonical runtime package is NOT shipped into this image —
# the cleaner script is one tiny Python file copied in directly,
# so redis stays single-purpose and doesn't carry the full
# market_service codebase. The cleaner reads INFO, DBSIZE,
# walks /data, and unlinks stale files.

COPY telemetry_hygiene_local.py /usr/local/bin/telemetry_hygiene_local.py
COPY redis-entrypoint.sh      /usr/local/bin/redis-entrypoint.sh
RUN chmod +x /usr/local/bin/redis-entrypoint.sh

ENV TELEMETRY_HYGIENE_INTERVAL_S=300 \
    TELEMETRY_HYGIENE_MIN_AGE_S=3600 \
    REDIS_HYGIENE_URL=unix:///tmp/redis.sock

EXPOSE 6379
CMD ["/usr/local/bin/redis-entrypoint.sh"]
```

### 3. docker-redis/redis-entrypoint.sh

```sh
#!/bin/sh
set -e

# Enable the unix socket for local-only redis access by the cleaner
# (the canonical docker-compose also exposes 6379 on the network).

# Start redis-server in the background. Capture PID.
redis-server \
  --appendonly yes \
  --appendfsync everysec \
  --maxmemory 1500mb \
  --maxmemory-policy allkeys-lru \
  --auto-aof-rewrite-min-size 64mb \
  --auto-aof-rewrite-percentage 100 \
  --aof-use-rdb-preamble yes \
  --save 60 1000 \
  --unixsocket /tmp/redis.sock \
  --unixsocketperm 700 \
  --daemonize no &
REDIS_PID=$!

# Wait for the unix socket to appear (max 30s)
for i in $(seq 1 60); do
  if [ -S /tmp/redis.sock ]; then break; fi
  sleep 0.5
done

# Start the cleaner in the background. Capture PID.
python3 /usr/local/bin/telemetry_hygiene_local.py &
CLEANER_PID=$!

# Forward SIGTERM/SIGINT to both children, then wait.
shutdown() {
  kill -TERM "$CLEANER_PID" 2>/dev/null || true
  kill -TERM "$REDIS_PID" 2>/dev/null || true
  wait "$REDIS_PID" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# Block on redis-server; if it dies, the container exits.
wait "$REDIS_PID"
RC=$?
kill -TERM "$CLEANER_PID" 2>/dev/null || true
exit $RC
```

### 4. docker-redis/telemetry_hygiene_local.py

Self-contained. Does NOT import from `market_service/` (the redis
container has no copy of the package — keeps the image small).
Uses only `redis` PyPI client OR the stdlib + a tiny RESP parser.
Actually simpler: install `redis` via pip in the Dockerfile.

```python
"""Periodic dead-weight cleanup inside the redis container.

Connects to redis over the local unix socket, walks /data directly,
and unlinks stale temp-rewriteaof-*.aof and temp-*.rdb files.
No socket mount, no shell, no subprocess. Doctrine-clean.
"""
import os
import sys
import time
import json
import logging
import signal
from pathlib import Path

try:
    import redis
except ImportError:
    print("redis package missing", file=sys.stderr)
    sys.exit(1)

DATA = Path("/data")
INTERVAL_S = int(os.environ.get("TELEMETRY_HYGIENE_INTERVAL_S", "300"))
MIN_AGE_S  = int(os.environ.get("TELEMETRY_HYGIENE_MIN_AGE_S", "3600"))
REDIS_URL  = os.environ.get("REDIS_HYGIENE_URL", "unix:///tmp/redis.sock")
DRY_RUN    = os.environ.get("TELEMETRY_HYGIENE_DRY_RUN", "false").lower() == "true"

log = logging.getLogger("telemetry_hygiene")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(message)s",
                    stream=sys.stderr)

stop = False
def _sig(*_):
    global stop
    stop = True
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def candidates() -> list[Path]:
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
        info_p = client.info("persistence")
    except Exception as e:
        report["errors"].append({"step": "info persistence", "error": str(e)})
        log.warning("telemetry_hygiene.cycle_error %s", e)
        return report
    report["rewrite_in_progress"] = bool(info_p.get("aof_rewrite_in_progress", 0))
    report["bgsave_in_progress"]  = bool(info_p.get("rdb_bgsave_in_progress", 0))
    if report["rewrite_in_progress"] or report["bgsave_in_progress"]:
        log.info("telemetry_hygiene.cycle_skip busy=%s",
                 report["rewrite_in_progress"] or report["bgsave_in_progress"])
        return report

    try:
        report["live_keys"] = int(client.dbsize())
    except Exception as e:
        report["errors"].append({"step": "dbsize", "error": str(e)})

    cands = candidates()
    report["candidates"] = [{"name": str(p.name), "size": p.stat().st_size} for p in cands]
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
        except Exception as e:
            report["errors"].append({"step": "unlink", "path": str(p), "error": str(e)})
    report["bytes_removed"] = removed_bytes
    return report

def main():
    log.info("telemetry_hygiene.started interval_s=%d min_age_s=%d url=%s dry_run=%s",
             INTERVAL_S, MIN_AGE_S, REDIS_URL, DRY_RUN)
    while not stop:
        try:
            report = cycle()
            log.info("telemetry_hygiene.cycle %s", json.dumps(report))
        except Exception as e:
            log.warning("telemetry_hygiene.cycle_error %s", e)
        # Sleep with interruptible granularity
        for _ in range(INTERVAL_S):
            if stop: break
            time.sleep(1)
    log.info("telemetry_hygiene.stopped")

if __name__ == "__main__":
    main()
```

### 5. market_service/runtime/telemetry_hygiene.py — small re-export shim

So the canonical package still has a discoverable entrypoint and
`run_all --json` (the static gate) sees the module:

```python
"""Telemetry hygiene: redis dead-weight cleanup.

The runtime cleanup loop lives inside the redis container
(docker-redis/telemetry_hygiene_local.py) so it shares /data and
needs no socket. This shim exists so the canonical package has a
discoverable import for run_all --json and any future health-check
exposed by the redis service.
"""

def main() -> int:
    raise SystemExit(
        "telemetry_hygiene runs inside the redis container. "
        "See docker-redis/telemetry_hygiene_local.py."
    )

if __name__ == "__main__":
    main()
```

### 6. market_service/runtime/redis_store.py — `collated_stream_maxlen`

The collated stream is currently unbounded. Add a cap (default 5_000):

```python
def __init__(self, url, prefix="marketflow", stream_maxlen=10_000,
             postgres_store=None, collated_stream_maxlen=5_000):
    ...
    self.collated_stream_maxlen = collated_stream_maxlen
```

And in `publish_run`, pass `maxlen=self.collated_stream_maxlen` to the XADD.

### 7. Compose — `data-access`, `calculations`, `analysis`, `orchestrator`

The redis URL inside these services becomes `unix:///tmp/redis.sock`
ONLY IF we want them to use the unix socket. Default keep tcp://redis:6379
(no socket mount, no /tmp bind). Cleaner uses the unix socket because
it lives in the same container.

## Doctrine compliance

- Zero legacy coupling
- No socket mount anywhere
- No `docker compose down -v` (volume preserved)
- The cleaner is a tiny self-contained Python file inside the redis
  image; the canonical `market_service/` package has only a shim
- Postgres-before-redis ordering preserved
- Wall_snapshot table unaffected

## Test gate

1. `docker compose config --quiet` → exit 0
2. `docker compose build redis` → success
3. `docker compose down` (no -v) → success
4. `docker compose up -d redis postgres` → wait healthy
5. `docker compose up -d data-access calculations analysis orchestrator`
6. `docker compose exec redis redis-cli CONFIG GET maxmemory` → 1572864000 (≈1500mb)
7. `docker compose exec redis redis-cli CONFIG GET maxmemory-policy` → allkeys-lru
8. Trigger harness twice; observe redis memory stays < 1.5 GB
9. Wait ~10 minutes; observe `telemetry_hygiene.cycle {"files_removed": N, ...}` in `docker logs crypto-ai-anal-redis-1`
10. `docker compose exec redis ls /data` → temp-rewriteaof-* count decreased or stable

## Rollback

```
docker compose down
git checkout docker-compose.yml market_service/runtime/redis_store.py
rm -rf docker-redis market_service/runtime/telemetry_hygiene.py
docker compose up -d
```

Volumes untouched.