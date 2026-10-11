"""Governance ledger — the durable + observable audit of FSM decisions.

SEMANTIC JURISDICTION: this file owns WHERE a governance decision is
recorded, never WHAT the decision is (the FSM owns that) and never the
machinery of asking (the governor owns that). Two surfaces, both additive:

* **Redis** (operational): the latest decision projection + a bounded
  decision stream + a denials-only stream, written with the same atomic
  SET+XADD discipline as the substrate and analysis planes. This is the
  always-fresh observability surface a reader (``/status``, NOOA, an
  operator) consults without touching Postgres.
* **Postgres** (institutional): the durable ``governance_ledger`` row. A
  DENIED decision is durable too — a denial is evidence about the system and
  must survive the process that produced it.

**Failures are counted, never silent.** The audit trail must not be able to
vanish quietly: every Redis and PG write failure increments a counter that
``stats()`` reports, and the container's ``/governance`` endpoint surfaces
it. ``pg_store=None`` degrades to Redis-only, honestly and loudly.

The ledger is I/O, so it lives here, outside the pure FSM.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from market_service.governance.contracts import (
    GovernanceLedgerRecord,
    GovernanceObservation,
    GovernanceVerdict,
)

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class LedgerStats:
    """Write accounting for the audit trail (a silent audit is no audit)."""

    decisions: int = 0
    allowed: int = 0
    denied: int = 0
    redis_written: int = 0
    redis_failures: int = 0
    pg_written: int = 0
    pg_failures: int = 0
    last_error: str | None = None
    last_decision_at_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": self.decisions,
            "allowed": self.allowed,
            "denied": self.denied,
            "redis_written": self.redis_written,
            "redis_failures": self.redis_failures,
            "pg_written": self.pg_written,
            "pg_failures": self.pg_failures,
            "last_error": self.last_error,
            "last_decision_at_ms": self.last_decision_at_ms,
            "durable": self.redis_failures == 0 and self.pg_failures == 0,
        }


class GovernanceLedger:
    """Write governance decisions to Redis (+ Postgres when available)."""

    def __init__(self, store: Any, pg_store: Any | None = None) -> None:
        self.store = store
        self.pg_store = pg_store
        self._stats = LedgerStats()

    def stats(self) -> dict[str, Any]:
        return self._stats.to_dict()

    async def record(
        self,
        *,
        plane: str,
        substrate: str,
        symbol: str,
        gate: str,
        verdict: GovernanceVerdict,
        observation: GovernanceObservation | None = None,
    ) -> str | None:
        """Persist one decision. Returns the Redis stream entry id (or None).

        Never raises: a governance ledger failure must not abort the gated
        action's own error handling, and must not be mistaken for a verdict.
        It IS counted, so it cannot vanish silently.
        """
        decided_at_ms = _now_ms()
        record = GovernanceLedgerRecord(
            plane=str(plane),
            substrate=str(substrate),
            symbol=str(symbol).upper(),
            gate=str(gate),
            allowed=bool(verdict.allowed),
            rule=verdict.rule,
            reason=verdict.reason,
            findings=tuple(verdict.findings),
            observation=(observation.to_dict() if observation is not None else {}),
            decided_at_ms=decided_at_ms,
        )
        self._stats.decisions += 1
        self._stats.last_decision_at_ms = decided_at_ms
        if record.allowed:
            self._stats.allowed += 1
        else:
            self._stats.denied += 1

        entry_id: str | None = None
        try:
            publisher = getattr(self.store, "publish_governance_decision", None)
            if callable(publisher):
                entry_id = await publisher(
                    record.plane, record.substrate, record.symbol,
                    record.to_dict(),
                )
                self._stats.redis_written += 1
            else:
                self._stats.redis_failures += 1
                self._stats.last_error = "redis: no governance keyspace surface"
        except Exception as exc:
            self._stats.redis_failures += 1
            self._stats.last_error = f"redis: {exc!r}"
            log.warning("governance redis ledger write failed for %s/%s",
                        record.plane, record.substrate, exc_info=True)

        if self.pg_store is not None:
            try:
                recorder = getattr(self.pg_store, "record_governance_decision", None)
                if callable(recorder):
                    await recorder(record.to_dict())
                    self._stats.pg_written += 1
                else:
                    self._stats.pg_failures += 1
                    self._stats.last_error = "pg: no governance ledger surface"
            except Exception as exc:
                self._stats.pg_failures += 1
                self._stats.last_error = f"pg: {exc!r}"
                log.warning("governance PG ledger write failed for %s/%s",
                            record.plane, record.substrate, exc_info=True)
        return entry_id

    async def inspect(self, plane: str, symbol: str) -> dict[str, Any]:
        """Read the plane's latest governance decisions (Redis-only, safe)."""
        reader = getattr(self.store, "read_governance_latest", None)
        if not callable(reader):
            return {"plane": plane, "symbol": symbol.upper(), "available": False}
        try:
            latest = await reader(plane, symbol)
        except Exception:
            latest = None
        return {
            "plane": plane,
            "symbol": symbol.upper(),
            "available": latest is not None,
            "latest": latest,
        }


__all__ = ["GovernanceLedger", "LedgerStats"]
