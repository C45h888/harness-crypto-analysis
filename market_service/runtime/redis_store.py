"""Redis operational store for latest state, streams, and refresh commands."""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from .contracts import MarketEvent, MarketRunEnvelope, MarketStateEnvelope, RefreshCommand


class RedisRuntimeStore:
    """Small typed adapter; callers never need to know Redis key names."""

    def __init__(self, url: str, prefix: str = "marketflow", stream_maxlen: int = 10_000):
        self.redis: Redis = Redis.from_url(url, decode_responses=True)
        self.prefix = prefix.strip(":")
        self.stream_maxlen = stream_maxlen

    def latest_key(self, symbol: str, source: str) -> str:
        return f"{self.prefix}:latest:{symbol.upper()}:{source}"

    def telemetry_stream(self, symbol: str) -> str:
        return f"{self.prefix}:stream:market:{symbol.upper()}"

    def collated_latest_key(self, symbol: str) -> str:
        return f"{self.prefix}:latest:{symbol.upper()}:collated"

    def collated_stream(self, symbol: str) -> str:
        return f"{self.prefix}:stream:collated:{symbol.upper()}"

    @property
    def command_stream(self) -> str:
        return f"{self.prefix}:stream:commands"

    @property
    def result_stream(self) -> str:
        return f"{self.prefix}:stream:results"

    async def close(self) -> None:
        await self.redis.aclose()

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def publish_state(self, state: MarketStateEnvelope) -> str:
        payload = state.to_json()
        await self.redis.set(self.latest_key(state.symbol, state.source), payload)
        return await self.redis.xadd(
            self.telemetry_stream(state.symbol),
            {"event_type": "market_state", "symbol": state.symbol, "payload": payload},
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def read_latest_state(self, symbol: str, source: str) -> MarketStateEnvelope | None:
        raw = await self.redis.get(self.latest_key(symbol, source))
        return MarketStateEnvelope.from_mapping(json.loads(raw)) if raw else None

    async def read_recent_events(self, symbol: str, count: int = 100) -> list[dict[str, Any]]:
        rows = await self.redis.xrevrange(self.telemetry_stream(symbol), count=count)
        return [{"id": event_id, **fields} for event_id, fields in rows]

    async def request_refresh(self, command: RefreshCommand) -> str:
        return await self.redis.xadd(
            self.command_stream,
            command.to_fields(),
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def publish_result(self, event: MarketEvent) -> str:
        return await self.redis.xadd(
            self.result_stream,
            event.to_fields(),
            maxlen=self.stream_maxlen,
            approximate=True,
        )

    async def publish_run(self, envelope: MarketRunEnvelope) -> str:
        envelope.validate()
        payload = envelope.to_json()
        dedupe_key = f"{self.prefix}:run:{envelope.run_id}"
        # One Redis-side transaction makes the latest projection, stream entry,
        # and idempotency marker succeed or fail together.
        script = """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 'duplicate' end
        redis.call('SET', KEYS[2], ARGV[1])
        local id = redis.call('XADD', KEYS[3], '*',
            'event_type', 'market_run', 'run_id', ARGV[2],
            'symbol', ARGV[3], 'schema_version', ARGV[4], 'payload', ARGV[1])
        redis.call('SET', KEYS[1], ARGV[1])
        return id
        """
        return str(await self.redis.eval(
            script, 3, dedupe_key, self.collated_latest_key(envelope.symbol),
            self.collated_stream(envelope.symbol), payload, envelope.run_id,
            envelope.symbol, str(envelope.schema_version),
        ))

    async def read_latest_run(self, symbol: str) -> MarketRunEnvelope | None:
        raw = await self.redis.get(self.collated_latest_key(symbol))
        return MarketRunEnvelope.from_mapping(json.loads(raw)) if raw else None

    async def read_runs(self, symbol: str, count: int = 100) -> list[MarketRunEnvelope]:
        rows = await self.redis.xrevrange(self.collated_stream(symbol), count=count)
        return [MarketRunEnvelope.from_mapping(json.loads(fields["payload"])) for _, fields in rows]

    async def has_run(self, run_id: str) -> bool:
        return bool(await self.redis.exists(f"{self.prefix}:run:{run_id}"))
