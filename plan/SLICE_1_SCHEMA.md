# Slice 1 — Plan (approved)

## Goal
Event-driven wake worker bounded on the Redis plane. No publish_wake XADD, no
read_pending_wakes XREVRANGE, no envelope JSON round-trip. Blocker-only streams;
WakeEnvelope is an in-memory assertion produced at fire time.

## Redis surfaces
NEW (worker + capture):
- marketflow:stream:microstructure:status:{venue}:{SYMBOL}
    capture writes ONLY on state change (XADD, maxlen 1000)
- marketflow:state:inference:wake:{venue}:{SYMBOL}:supervisor
    heartbeat + last_tick_ms + last_fire info (SET/GET, TTL 30s)

GONE (retired from active use):
- marketflow:stream:inference:wake:{venue}:{SYMBOL}  (write/read paths cease)

USED (existing, read-only):
- microstructure event stream (XLEN; status payload best_quote_events counter)
- microstructure status latest key (state / age)
- latest inference artifact (high-water via deterministic_state.coverage.events_total)

## Flow (async loop, single consumer per scope)
XREADGROUP BLOCK 5000 on (events_time_ms, status) for scope:
  1. events_time_ms crossing water event-time gates: status_state in running/gap/
     reconnecting/connected AND stream_age > 60s AND time_since_art > 60s
  2. status-state transition (recovery / cold-start), with appended stamp
  3. evaluate via evaluate_triggers(); append enhanced_status; fire via
     run_engine_async task (never awaited in the loop)

## Control / ids
- wake id  = hash("engine-cycle-<SYMBOL>-<venue>-<status_state>-<events_total>")
- dedupe   = supervisor lua Eval SHA. if events_total <= last_fired_events_total
              and status unchanged and fired_stamp absolute within 120s: skip
- cooldown = absolute positive check: now - last_fire_started_ms >= cooldown
- manual/control reader: optional, priority flows later

## Failure envelope
- RedisUnavailable catch: make no wake, log no busy-loop, mark liveness false
- non-redis exception: last_error entry, continue
- reconnection inside XREADGROUP: retry with backoff 0.5s→10s
- fires/ticks logged at INFO with details

## Runtime durations
- READ_BLOCK_MS 1000 ; TICK_RESET_MS 5000 ; SUPERVISOR_MS 5000
- cooldown/water defaults at module constants
