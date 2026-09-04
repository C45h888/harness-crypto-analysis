"""Check the configured Redis and PostgreSQL runtime dependencies."""

from __future__ import annotations

import asyncio
import json

from market_service.config import Settings
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore


async def check() -> dict[str, bool]:
    settings = Settings.from_env()
    redis = RedisRuntimeStore(settings.redis_url, settings.redis_key_prefix)
    postgres = PostgresRuntimeStore(settings.database_url)
    try:
        await postgres.connect()
        return {"redis": await redis.ping(), "postgres": await postgres.ping()}
    finally:
        await redis.close()
        await postgres.close()


def main() -> int:
    result = asyncio.run(check())
    print(json.dumps(result, sort_keys=True))
    return 0 if all(result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
