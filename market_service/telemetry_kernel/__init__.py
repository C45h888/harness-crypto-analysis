"""Governed, out-of-band runtime telemetry.

The owning FSM decides the state transition. This package validates and
replays a state-only receipt without changing that decision.
"""

from market_service.telemetry_kernel.contracts import (
    TELEMETRY_EVENT_SCHEMA_VERSION,
    TelemetryEvent,
)
from market_service.telemetry_kernel.kernel import (
    EmissionResult,
    TelemetryKernel,
    kernel_for_runtime,
)
from market_service.telemetry_kernel.projections import reduce_projection, rebuild_projection
from market_service.telemetry_kernel.store import TelemetryStore

__all__ = [
    "TELEMETRY_EVENT_SCHEMA_VERSION",
    "EmissionResult",
    "TelemetryEvent",
    "TelemetryKernel",
    "TelemetryStore",
    "kernel_for_runtime",
    "reduce_projection",
    "rebuild_projection",
]
