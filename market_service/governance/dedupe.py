"""Governance-owned fire dedupe primitive — coordination lives here.

Absorbed from market_service/substrate_worker/core/fire.py::_dedupe and
core/reader.py::_DEDUPE_LUA (deleted). The supervisor Lua script is the
condition-identity collapse: same (high_water, trigger_source) twice in a
row suppresses the repeat. The FSM/governor is the legality owner; this
module is the fail-closed transport for that decision.

Fail-closed: script missing or eval failure SUPPRESSES the fire (returns
False) with a loud log, never fires anyway. Callers retry the script load
once on NOSCRIPT, then suppress with a short negative-TTL guard so a Redis
restart cannot turn into a fire storm.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

DEDUPE_LUA = r"""
-- deterministic substrate fire dedupe (condition-identity collapse)
-- KEYS[1] = supervisor key; ARGV[1] = candidate json; ARGV[2] = cooldown_ms;
-- ARGV[3] = high_water (consumed entry id or ''); ARGV[4] = trigger source
local current = redis.call("GET", KEYS[1])
if current then
    local ok, dec = pcall(cjson.decode, current)
    if ok and type(dec) == "table" then
        local last_water = tostring(dec.high_water or "")
        local last_source = tostring(dec.trigger_source or "")
        if tostring(ARGV[3]) == last_water and tostring(ARGV[4]) == last_source then
            return "0"
        end
    end
end
redis.call("SET", KEYS[1], ARGV[1])
return "1"
"""

# Short negative-TTL guard (seconds): after a dedupe transport failure we
# suppress fires for this long so a Redis restart + staleness heartbeats do
# not fan out one fire per worker per tick.
NEGATIVE_TTL_S = 5.0


def candidate_json(decision: Any, high_water: str, now_ms: int) -> str:
    predicates = getattr(decision, "predicates", {}) or {}
    try:
        keys = sorted(predicates)
    except TypeError:
        keys = list(predicates)
    return json.dumps(
        {
            "high_water": high_water,
            "trigger_source": getattr(decision, "source", ""),
            "fired_stamp_ms": now_ms,
            "now_ms": now_ms,
            "predicate_keys": keys,
        },
        separators=(",", ":"),
    )


async def evaluate(
    redis: Any,
    supervisor_key: str,
    decision: Any,
    high_water: str,
    now_ms: int,
    *,
    cooldown_ms: int = 0,
    state: dict[str, Any] | None = None,
) -> bool:
    """Fail-closed dedupe: True = may fire, False = suppressed.

    ``state`` is the worker-owned mutable slot holding ``dedupe_sha`` and
    ``dedupe_suppress_until``. Kept as a plain dict so workers own their
    slot without importing core transport.
    """
    if state is None:
        state = {}
    suppress_until = float(state.get("dedupe_suppress_until") or 0.0)
    if time.monotonic() < suppress_until:
        return False
    candidate = candidate_json(decision, high_water, now_ms)

    async def _load() -> str | None:
        try:
            sha = str(await redis.script_load(DEDUPE_LUA))
            state["dedupe_sha"] = sha
            return sha
        except Exception as exc:
            log.error("dedupe script registration failed (suppressing fire): %s", exc)
            state["dedupe_sha"] = None
            state["dedupe_suppress_until"] = time.monotonic() + NEGATIVE_TTL_S
            return None

    sha = state.get("dedupe_sha")
    if not sha:
        sha = await _load()
        if not sha:
            return False
    try:
        raw = await redis.evalsha(
            sha, 1, supervisor_key,
            candidate, str(int(cooldown_ms)),
            str(high_water), str(getattr(decision, "source", "")),
            str(int(now_ms)),
        )
    except Exception as exc:
        msg = str(exc).upper()
        if "NOSCRIPT" in msg:
            sha = await _load()
            if not sha:
                return False
            try:
                raw = await redis.evalsha(
                    sha, 1, supervisor_key,
                    candidate, str(int(cooldown_ms)),
                    str(high_water), str(getattr(decision, "source", "")),
                    str(int(now_ms)),
                )
            except Exception as exc2:
                log.error("dedupe eval failed after reload (suppressing fire): %s", exc2)
                state["dedupe_suppress_until"] = time.monotonic() + NEGATIVE_TTL_S
                return False
        else:
            log.error("dedupe eval failed (suppressing fire): %s", exc)
            state["dedupe_suppress_until"] = time.monotonic() + NEGATIVE_TTL_S
            return False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw == "1"
