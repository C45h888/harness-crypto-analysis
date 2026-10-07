"""Poller control plane — Redis control-key writes/reads only."""

from __future__ import annotations

from typing import Any

from market_service.interaction_plane import stores as _stores


async def poller_set(symbols: list[str]) -> dict[str, Any]:
    settings = _stores.settings_redis()
    store = _stores.open_redis(settings)
    try:
        cleaned = [s.strip().upper() for s in symbols if s.strip()]
        if not cleaned:
            return {"status": "error", "error": "no valid symbols provided"}
        await store.set_poller_symbols(cleaned)
        return {
            "status": "ok", "action": "set",
            "control_key": store.poller_control_key(),
            "active_symbols": sorted(set(cleaned)),
            "note": "poller picks this up within one poll interval",
        }
    finally:
        await store.close()


async def poller_reset() -> dict[str, Any]:
    settings = _stores.settings_redis()
    store = _stores.open_redis(settings)
    try:
        await store.clear_poller_symbols()
        return {
            "status": "ok", "action": "reset",
            "control_key": store.poller_control_key(),
            "fallback_symbols": list(settings.poll_symbols),
            "note": "poller reverted to POLL_SYMBOLS / SYMBOLS env",
        }
    finally:
        await store.close()


async def poller_status() -> dict[str, Any]:
    settings = _stores.settings_redis()
    store = _stores.open_redis(settings)
    try:
        status = await store.read_poller_status()
        if status is None:
            return {"status": "error", "action": "status",
                    "error": "no poller status found — is the poller container running?"}
        return {"status": "ok", "action": "status", **status}
    finally:
        await store.close()


__all__ = ["poller_reset", "poller_set", "poller_status"]
