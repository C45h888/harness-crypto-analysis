"""Shared statistical primitives for deterministic microstructure inference.

Authority: pure computation — no I/O, no exchange access, no LLM. All
arithmetic is ``Decimal`` under a fixed precision context; SHA-256 digests
guarantee reproducibility.

This module is the shared foundation for Route A, Route B, Route C, and
the composer. It holds replay, hashing, and general math utilities that
do not belong to any single inference route.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext
from typing import Any

from .contracts import FIT_MODEL_VERSION, OFIInterval, OrderBookEvent
from .ofi import DEPTH_ESTIMATOR

# ---------------------------------------------------------------------------
# Frozen quality gates — also used by Route A, Route B, and scenario eval.
# ---------------------------------------------------------------------------

MIN_OBSERVATIONS = 30
MIN_DEPTH_BLOCKS = 3
PRECISION = 50

# Scenario evaluation — the interaction-plane question "can price hit X?".
# The fitted model supplies the REQUIREMENT (required horizon flow); the
# tape supplies the PROBABILITY (empirical exceedance over horizon-length
# OFI sums). Horizons live where the question lives (15m/1h/4h).
SCENARIO_HORIZONS = {"15m": 900, "1h": 3600, "4h": 14400}
MIN_SCENARIO_WINDOWS = 30
MAX_SCENARIO_INTERVALS = 50_000
_BETA_ZERO_EPS = Decimal("1e-18")


# ---------------------------------------------------------------------------
# Hashing & identity
# ---------------------------------------------------------------------------


def input_hash(intervals: list[OFIInterval], config: dict[str, Any]) -> str:
    """SHA-256 over canonical interval bytes + configuration.

    Identical fixture bytes, configuration, and model version yield an
    identical hash — the reproducibility guarantee required before any fit
    may be labelled ``validated``.
    """
    payload = {
        "intervals": [interval.to_dict() for interval in intervals],
        "config": config,
        "model_version": FIT_MODEL_VERSION,
        "depth_estimator": DEPTH_ESTIMATOR,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fit_id(kind: str, digest: str) -> str:
    return f"{kind}-{digest[:16]}"


# ---------------------------------------------------------------------------
# Deterministic replay — reconstruct typed objects from persisted bytes.
# ---------------------------------------------------------------------------


def replay_events_from_payloads(
    event_payloads: list[dict[str, Any]],
) -> tuple[list[OrderBookEvent], int]:
    """Rebuild typed OrderBookEvent objects from persisted transport dicts.

    Entries that fail to decode are skipped and counted, never fabricated.
    The result is ordered as stored; callers that need sequence-verified
    reconstruction should run the reconstructor instead.
    """
    events: list[OrderBookEvent] = []
    dropped = 0
    for payload in event_payloads:
        try:
            events.append(OrderBookEvent.from_dict(payload))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            dropped += 1
    return events, dropped


def replay_intervals(
    events: list[OrderBookEvent], *, interval_ms: int,
) -> list[OFIInterval]:
    """Deterministically re-aggregate events into closed OFI intervals.

    Pure replay: identical event bytes and interval_ms always produce
    identical interval rows (and therefore an identical input_hash). The
    final open interval is flushed so a window ending mid-interval still
    yields its partial measurement (marked by the aggregator's own logic).
    """
    from .ofi import OFIAggregator  # avoid circular on module init

    aggregator = OFIAggregator(interval_ms)
    closed: list[OFIInterval] = []
    for event in events:
        closed.extend(aggregator.add(event))
    tail = aggregator.flush()
    if tail is not None:
        closed.append(tail)
    return closed


def reconstruct_events(
    snapshot: dict[str, Any], delta_payloads: list[dict[str, Any]], *,
    symbol: str, venue: str, received_ts_ms: int = 0,
) -> list[OrderBookEvent]:
    """Full book reconstruction path: apply raw depth deltas in sequence.

    Bootstraps from one REST snapshot, then applies each persisted delta
    through the ``OrderBookReconstructor``. Any sequence gap raises
    ``BookGapError`` — the caller must treat the window as insufficient and
    never bridge a gap with inferred events. This is the strongest replay
    guarantee and is used when the raw delta stream is retained.
    """
    from .contracts import DepthDelta
    from .orderbook import BookGapError, OrderBookReconstructor

    reconstructor = OrderBookReconstructor(symbol, venue)
    reconstructor.bootstrap(snapshot, received_ts_ms=received_ts_ms)
    events: list[OrderBookEvent] = []
    for payload in delta_payloads:
        delta = DepthDelta.from_dict(payload)
        event = reconstructor.apply(delta)  # BookGapError propagates to the caller
        if event is not None:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Format & math utilities
# ---------------------------------------------------------------------------


def _s(value: Decimal) -> str:
    """Fixed-point string for scenario outputs (no exponent form in prompts)."""
    return format(value, "f")


def _norm_cdf(z: float) -> float:
    """Standard normal CDF via math.erf."""
    from math import erf, sqrt as _sqrt
    return 0.5 * (1.0 + erf(z / _sqrt(2.0)))


def _scenario_required_ofi(
    delta_req: Decimal, drift: Decimal, beta: Decimal,
) -> Decimal | None:
    """Required total horizon OFI, or None when β is indistinguishable from zero."""
    if abs(beta) < _BETA_ZERO_EPS:
        return None
    try:
        return (delta_req - drift) / beta
    except Exception:
        return None