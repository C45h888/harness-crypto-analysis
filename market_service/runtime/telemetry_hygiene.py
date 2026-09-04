"""Telemetry hygiene: redis dead-weight cleanup.

The runtime cleanup loop lives inside the redis container at
``docker-redis/telemetry_hygiene_local.py`` so it shares /data and
needs no socket. This shim exists so the canonical package has a
discoverable import for ``market_service.commands.run_all --json``
and for any future health-check exposed by the redis service.

Run the cleaner via:
    docker compose logs crypto-ai-anal-redis-1

To see the structured cycle reports, not via this module.
"""

from __future__ import annotations


def main() -> int:
    raise SystemExit(
        "telemetry_hygiene runs inside the redis container. "
        "See docker-redis/telemetry_hygiene_local.py for the runtime."
    )


if __name__ == "__main__":
    main()