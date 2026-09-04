#!/bin/sh
# Telemetry hygiene spec — Option C.
# Two processes inside the redis container, sharing /data and the
# network namespace. No socket, no sidecar, no shell-out.
#
#   redis-server  ← the real redis (PID 1)
#   telemetry_hygiene_local.py  ← the cleaner (background)

set -e

# 1.5 GB hard cap with allkeys-lru eviction (Option C contract).
# 64 MB / 100% rewrite threshold + RDB-preamble AOF = small,
# frequent, crash-safe rewrites instead of one giant fragmented
# rewrite every few hours.

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

# Wait for the unix socket to come up (max 30s).
for i in $(seq 1 60); do
  if [ -S /tmp/redis.sock ]; then break; fi
  sleep 0.5
done

# Start the cleaner in the background.
python3 /usr/local/bin/telemetry_hygiene_local.py \
  >/proc/1/fd/2 2>&1 &
CLEANER_PID=$!

# Forward SIGTERM/SIGINT to both children, then wait.
shutdown() {
  kill -TERM "$CLEANER_PID" 2>/dev/null || true
  kill -TERM "$REDIS_PID"   2>/dev/null || true
  wait "$REDIS_PID" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# Block on redis-server; if it dies, the container exits and
# the orchestrator restart policy kicks in.
wait "$REDIS_PID"
RC=$?

kill -TERM "$CLEANER_PID" 2>/dev/null || true
exit $RC