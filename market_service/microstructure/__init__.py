"""Deterministic microstructure measurement and isolated live capture.

This package is intentionally outside the existing five-second REST poller.
It owns Binance depth-event reconstruction and the paper-derived OFI/average
depth measurements; it does not fit models, interpret regimes, or trade.
Track D (D6-D11): pure statistical base only — no dispatch/engine/Redis/PG.
"""

from .contracts import BestQuoteState, DepthDelta, EVIDENCE_V2_VERSION, HYPOTHESIS_VERSION, FeatureVector, ForwardFit, ForwardObservation, HypothesisEvidence, HypothesisLedger, MicrostructureEvidenceV2, OFIInterval, OrderBookEvent, QuoteMeasurement
from .discipline import COST_STATEMENT_TEMPLATE, DISCIPLINE_CHECKLIST, DISCIPLINE_VERSION, discipline_audit
from .events import ABSORPTION_VERSION, EVENT_ENVELOPE_VERSION, WALL_VERSION, AbsorptionEvent, EventEnvelope, WallLifecycle, detect_absorption, detect_walls, replay_agreement
from .fitting import FORWARD_SCENARIO_VERSION, assemble_evidence_v2, build_feature_vector, build_forward_observations, calibration_report, compare_scenario_paths, evaluate_forward_scenario, fit_forward_ols, population_key, predict_distribution, skill_decay_report
from .hypothesis import test_hypothesis
from .microprice import MICROPRICE_ESTIMATOR, displacement, displacement_bps, microprice, mid
from .tick import TICK_TABLE, TICK_TABLE_VERSION, resolve_tick_size
from .ofi import OFIAggregator, event_contribution
from .orderbook import BookGapError, OrderBookReconstructor

__all__ = [
    "ABSORPTION_VERSION",
    "BestQuoteState",
    "BookGapError",
    "COST_STATEMENT_TEMPLATE",
    "DISCIPLINE_CHECKLIST",
    "DISCIPLINE_VERSION",
    "DepthDelta",
    "EVENT_ENVELOPE_VERSION",
    "EVIDENCE_V2_VERSION",
    "EventEnvelope",
    "AbsorptionEvent",
    "FeatureVector",
    "FORWARD_SCENARIO_VERSION",
    "ForwardFit",
    "ForwardObservation",
    "HYPOTHESIS_VERSION",
    "HypothesisEvidence",
    "HypothesisLedger",
    "MicrostructureEvidenceV2",
    "QuoteMeasurement",
    "WALL_VERSION",
    "WallLifecycle",
    "assemble_evidence_v2",
    "build_feature_vector",
    "build_forward_observations",
    "calibration_report",
    "compare_scenario_paths",
    "detect_absorption",
    "detect_walls",
    "discipline_audit",
    "TICK_TABLE",
    "TICK_TABLE_VERSION",
    "displacement",
    "displacement_bps",
    "evaluate_forward_scenario",
    "fit_forward_ols",
    "microprice",
    "mid",
    "population_key",
    "predict_distribution",
    "replay_agreement",
    "resolve_tick_size",
    "skill_decay_report",
    "test_hypothesis",
    "OFIAggregator",
    "OFIInterval",
    "OrderBookEvent",
    "OrderBookReconstructor",
    "event_contribution",
]
