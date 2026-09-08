"""Substrate worker contracts — typed payload + trigger decision.

The substrate worker plane writes one ``SubstrateStatePayload`` per fire (the
always-fresh projection + bounded stream). The payload makes every fire
auditable: WHY it fired (trigger source + predicate values), on WHAT input
state (freshness fingerprint), and WHAT it computed (bounded output).

Null discipline: a worker that cannot compute honestly writes
``status="insufficient_data"`` + ``missing_inputs`` — never a fabricated
zero. The output arrays are bounded at emission (128-item cap with
``__truncated__`` markers, same convention as GroupEnvelope).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUBSTRATE_STATE_SCHEMA_VERSION = 1
OUTPUT_ARRAY_CAP = 128

# Rollover period lengths in milliseconds (stream entry-id time base).
ROLLOVER_PERIOD_MS: dict[str, int] = {
    "hour": 3_600_000,
    "bar_5m": 300_000,
    "bar_1h": 3_600_000,
}


@dataclass(frozen=True)
class CadenceProfile:
    """Per-worker cadence: time guards, rollover periods, input gates.

    ``cooldown_s`` gates ONLY probe-source fires (hygiene/boundary fires —
    staleness/rollover/cold_start/recovery — always bypass it). ``rollovers``
    declares period boundaries the core watches via stream entry ids
    (see ``ROLLOVER_PERIOD_MS``). ``min_book_depth`` / ``min_trade_count``
    form the minimum-data gate before a probe may fire. ``ws_input`` marks
    workers that also consume the microstructure event stream.
    """

    cooldown_s: int
    staleness_s: int
    rollovers: tuple[str, ...] = ()
    min_book_depth: int = 1
    min_trade_count: int = 0
    ws_input: bool = False

# Why a fire happened. L2 semantic probes report "probe"; the core's time /
# liveness guards report the others.
TRIGGER_SOURCES = ("probe", "staleness", "cold_start", "recovery", "rollover")
# Worker payload statuses (mirror the envelope statuses).
PAYLOAD_STATUSES = ("healthy", "degraded", "insufficient_data")


@dataclass(frozen=True)
class TriggerDecision:
    """The deterministic fire decision (L2 probe result + provenance).

    ``fired`` gates the fire; ``source`` records WHY; ``predicates`` carries
    the auditable values that tripped (e.g. keystone from/to/threshold).
    """

    fired: bool
    source: str = "probe"
    predicates: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in TRIGGER_SOURCES:
            raise ValueError(
                f"invalid trigger source {self.source!r}; expected one of {TRIGGER_SOURCES!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "predicates": dict(self.predicates)}


def bound_arrays(value: Any, cap: int = OUTPUT_ARRAY_CAP) -> Any:
    """Bound every list in a payload at ``cap`` items with explicit truncation.

    Same convention as the interpretation plane's ``_bound_arrays``: a group
    envelope / substrate payload must ALWAYS fit a reader context by
    construction. Truncation is explicit (``__truncated__`` marker) — never
    silent.
    """
    if isinstance(value, list):
        if len(value) > cap:
            return [*value[:cap], "__truncated__"]
        return [bound_arrays(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: bound_arrays(v, cap) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class SubstrateStatePayload:
    """One substrate worker fire — the persisted calculation state.

    ``freshness.input_fingerprint`` records the input window the output was
    computed from (trade span, counts, consumed stream high-water) so
    freshness is MEASURED, and a reader can compare "was this computed from
    the data I think it was".
    """

    schema_version: int
    substrate: str
    symbol: str
    status: str
    observed_at_ms: int | None
    computed_at_ms: int
    trigger: dict[str, Any]
    freshness: dict[str, Any]
    output: dict[str, Any]
    missing_inputs: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        substrate: str,
        symbol: str,
        output: dict[str, Any],
        trigger: TriggerDecision,
        freshness: dict[str, Any] | None = None,
        observed_at_ms: int | None = None,
        computed_at_ms: int | None = None,
        missing_inputs: tuple[str, ...] | list[str] = (),
    ) -> SubstrateStatePayload:
        observed = observed_at_ms if observed_at_ms is not None else (freshness or {}).get("observed_at_ms")
        return cls(
            schema_version=SUBSTRATE_STATE_SCHEMA_VERSION,
            substrate=substrate.lower(),
            symbol=symbol.upper(),
            status="healthy",
            observed_at_ms=int(observed) if observed is not None else None,
            computed_at_ms=int(computed_at_ms if computed_at_ms is not None else _now_ms()),
            trigger=trigger.to_dict(),
            freshness=dict(freshness or {}),
            output=bound_arrays(dict(output)),
            missing_inputs=tuple(missing_inputs),
            provenance={"substrates": [substrate.lower()]},
        )

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema_version != SUBSTRATE_STATE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported substrate state schema version: {self.schema_version}"
            )
        if not self.substrate:
            raise ValueError("substrate state requires substrate name")
        if not self.symbol:
            raise ValueError("substrate state requires symbol")
        if self.status not in PAYLOAD_STATUSES:
            raise ValueError(
                f"invalid substrate state status {self.status!r}; expected one of {PAYLOAD_STATUSES!r}"
            )
        if self.trigger.get("source") not in TRIGGER_SOURCES:
            raise ValueError(
                f"invalid trigger source {self.trigger.get('source')!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "substrate": self.substrate,
            "symbol": self.symbol,
            "status": self.status,
            "observed_at_ms": self.observed_at_ms,
            "computed_at_ms": self.computed_at_ms,
            "trigger": dict(self.trigger),
            "freshness": dict(self.freshness),
            "missing_inputs": list(self.missing_inputs),
            "output": dict(self.output),
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SubstrateStatePayload:
        return cls(
            schema_version=int(value.get("schema_version") or SUBSTRATE_STATE_SCHEMA_VERSION),
            substrate=str(value.get("substrate") or ""),
            symbol=str(value.get("symbol") or ""),
            status=str(value.get("status") or "healthy"),
            observed_at_ms=value.get("observed_at_ms"),
            computed_at_ms=int(value.get("computed_at_ms") or 0),
            trigger=dict(value.get("trigger") or {}),
            freshness=dict(value.get("freshness") or {}),
            missing_inputs=tuple(value.get("missing_inputs") or ()),
            output=dict(value.get("output") or {}),
            provenance=dict(value.get("provenance") or {}),
        )


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)