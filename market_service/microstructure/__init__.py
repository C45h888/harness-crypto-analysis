"""Deterministic microstructure measurement and isolated live capture.

This package is intentionally outside the existing five-second REST poller.
It owns Binance depth-event reconstruction and the paper-derived OFI/average
depth measurements; it does not fit models, interpret regimes, or trade.
Track D (D6-D11): pure statistical base only — no dispatch/engine/Redis/PG.

All public symbols from the five fitting substrates are exported here so
that ``import market_service.microstructure as fm`` provides access to the
full inference API (Route A, Route B, Route C, composer, and common).
"""

from .contracts import (
    BestQuoteState, DepthDelta, EVIDENCE_V2_VERSION, HYPOTHESIS_VERSION,
    FeatureVector, ForecastResult, ForwardFit, ForwardObservation,
    HypothesisEvidence, HypothesisLedger, MicrostructureEvidenceV2,
    OFIInterval, OrderBookEvent, QuoteMeasurement,
)
from .discipline import (
    COST_STATEMENT_TEMPLATE, DISCIPLINE_CHECKLIST, DISCIPLINE_VERSION,
    discipline_audit,
)
from .events import (
    ABSORPTION_VERSION, EVENT_ENVELOPE_VERSION, WALL_VERSION,
    AbsorptionEvent, EventEnvelope, WallLifecycle,
    detect_absorption, detect_walls, replay_agreement,
)
from .fitting_common import (
    MIN_DEPTH_BLOCKS, MIN_OBSERVATIONS, PRECISION, SCENARIO_HORIZONS,
    input_hash, reconstruct_events, replay_events_from_payloads,
    replay_intervals,
)
from .fitting_route_a import build_observations, fit_price_impact
from .fitting_route_b import fit_depth_scaling
from .fitting_route_c import (
    FORWARD_SCENARIO_VERSION,
    build_feature_vector, build_forward_observations,
    calibration_report, evaluate_forward_scenario, feature_schema_hash,
    fit_forward_ols, population_key, predict_distribution,
    replay_forward_window, skill_decay_report,
)
from .fitting_composer import (
    assemble_evidence, assemble_evidence_v2, assemble_forecast_result,
    compare_scenario_paths, derive_price_delta, evaluate_scenario,
)
from .hypothesis import test_hypothesis
from .microprice import (
    MICROPRICE_ESTIMATOR, displacement, displacement_bps, microprice, mid,
)
from .tick import TICK_TABLE, TICK_TABLE_VERSION, resolve_tick_size
from .ofi import OFIAggregator, event_contribution
from .orderbook import BookGapError, OrderBookReconstructor

__all__ = [
    "ABSORPTION_VERSION",
    "AbsorptionEvent",
    "BestQuoteState",
    "BookGapError",
    "COST_STATEMENT_TEMPLATE",
    "DISCIPLINE_CHECKLIST",
    "DISCIPLINE_VERSION",
    "DepthDelta",
    "EVENT_ENVELOPE_VERSION",
    "EVIDENCE_V2_VERSION",
    "EventEnvelope",
    "FeatureVector",
    "ForecastResult",
    "FORWARD_SCENARIO_VERSION",
    "ForwardFit",
    "ForwardObservation",
    "HYPOTHESIS_VERSION",
    "HypothesisEvidence",
    "HypothesisLedger",
    "MICROPRICE_ESTIMATOR",
    "MIN_DEPTH_BLOCKS",
    "MIN_OBSERVATIONS",
    "MicrostructureEvidenceV2",
    "OFIAggregator",
    "OFIInterval",
    "OrderBookEvent",
    "OrderBookReconstructor",
    "PRECISION",
    "QuoteMeasurement",
    "SCENARIO_HORIZONS",
    "TICK_TABLE",
    "TICK_TABLE_VERSION",
    "WALL_VERSION",
    "WallLifecycle",
    "assemble_evidence",
    "assemble_evidence_v2",
    "assemble_forecast_result",
    "build_feature_vector",
    "build_forward_observations",
    "build_observations",
    "calibration_report",
    "compare_scenario_paths",
    "derive_price_delta",
    "detect_absorption",
    "detect_walls",
    "discipline_audit",
    "displacement",
    "displacement_bps",
    "evaluate_forward_scenario",
    "evaluate_scenario",
    "event_contribution",
    "feature_schema_hash",
    "fit_depth_scaling",
    "fit_forward_ols",
    "fit_price_impact",
    "input_hash",
    "microprice",
    "mid",
    "population_key",
    "predict_distribution",
    "reconstruct_events",
    "replay_agreement",
    "replay_forward_window",
    "replay_events_from_payloads",
    "replay_intervals",
    "resolve_tick_size",
    "skill_decay_report",
    "test_hypothesis",
]