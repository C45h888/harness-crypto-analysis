"""Track D D6-D11 tests: scenario, hypothesis, events, population, v2, discipline."""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import pytest

from market_service.microstructure import fitting as F
from market_service.microstructure.contracts import (
    BestQuoteState,
    FeatureVector,
    ForwardFit,
    ForwardObservation,
    HypothesisLedger,
)
from market_service.microstructure.discipline import discipline_audit
from market_service.microstructure.events import (
    detect_absorption,
    detect_walls,
    replay_agreement,
)
from market_service.microstructure.hypothesis import test_hypothesis

SYM = "BTCUSDT"
VEN = "spot"
TICK = Decimal("0.01")


def _quote(uid: int, ts: int, **kw) -> BestQuoteState:
    base = dict(symbol=SYM, venue=VEN, update_id=uid, exchange_ts_ms=ts,
                received_ts_ms=ts, bid_price=Decimal("100"),
                bid_qty=Decimal("10"), ask_price=Decimal("100.02"),
                ask_qty=Decimal("20"))
    base.update(kw)
    return BestQuoteState(**base)


def _vec(ts: int, ofi: str = "5", quality: str = "exact_feed") -> FeatureVector:
    q = _quote(1, ts)
    return F.build_feature_vector(symbol=SYM, venue=VEN, ts_ms=ts,
                                  ofi=Decimal(ofi), average_depth=Decimal("8"),
                                  quote=q, quality=quality)


def _fit_pairs(n: int = 60, horizon: int = 5_000):
    ofis = [(i % 7) - 3 for i in range(n)]
    vecs = [_vec(1_000 * (i + 1), ofi=str(ofis[i])) for i in range(n)]
    # Trending mid series correlated with OFI so the forward fit identifies
    # nonzero beta, nonzero residual variance, and defined OOS skill.
    prices: list[Decimal] = [Decimal("100")]
    for i in range(n + 20):
        step = Decimal("0.004") * Decimal(ofis[i] if i < n else 0) + Decimal("0.002")
        prices.append(prices[-1] + step)
    mids = [(1_000 * (i + 1), prices[i]) for i in range(n + 20)]
    pairs, _ = F.build_forward_observations(vecs, mids, tick_size=TICK, venue=VEN)
    fit, usable = F.fit_forward_ols(pairs, symbol=SYM, venue=VEN,
                                    horizon_ms=horizon, min_observations=10)
    return fit, pairs, usable


# ---- D6 ----
def test_forward_scenario_curves_and_bands():
    fit, _pairs, _ = _fit_pairs()
    x = _vec(5_000, ofi="4")
    spot = Decimal("100")
    out = F.evaluate_forward_scenario(
        fit, x, spot_price=spot, targets=[Decimal("100.10")],
        invalidations=[Decimal("99.90")], tick_size=TICK)
    assert out["kind"] == "forward-conditional (horizon-native)"
    assert out["scenario_version"] == "fwd-scenario-v1"
    t = out["targets"][0]
    assert t["p_ge"] is not None
    assert float(t["p_lo"]) <= float(t["p_ge"]) <= float(t["p_hi"])
    s = out["invalidations"][0]
    assert s["p_le"] is not None
    # T == spot edge -> d_req 0
    out2 = F.evaluate_forward_scenario(
        fit, x, spot_price=spot, targets=[spot],
        invalidations=[], tick_size=TICK)
    assert out2["targets"][0]["d_req_ticks"] == "0"


def test_forward_scenario_refusals():
    fit, _pairs, _ = _fit_pairs()
    x = _vec(5_000)
    bad, _ = F.fit_forward_ols([], symbol=SYM, venue=VEN,
                               horizon_ms=5_000, min_observations=10)
    with pytest.raises(ValueError, match="insufficient"):
        F.evaluate_forward_scenario(bad, x, spot_price=Decimal("100"),
                                    targets=[Decimal("101")],
                                    invalidations=[], tick_size=TICK)
    out = F.evaluate_forward_scenario(
        fit, x, spot_price=Decimal("100"), targets=[Decimal("101")],
        invalidations=[], tick_size=TICK,
        calibrated=False, calibration_refusal="miscalibrated")
    assert out["targets"][0]["p_ge"] is None
    assert out["calibration"]["refusal"] == "miscalibrated"
    with pytest.raises(ValueError):
        F.evaluate_forward_scenario(fit, x, spot_price=Decimal("100"),
                                    targets=[], invalidations=[],
                                    tick_size=TICK)


def test_compare_paths_reports_both():
    fit, _pairs, _ = _fit_pairs()
    x = _vec(5_000)
    fwd = F.evaluate_forward_scenario(
        fit, x, spot_price=Decimal("100"), targets=[Decimal("101")],
        invalidations=[], tick_size=TICK)
    legacy = {"direction": "up", "horizon": "1h"}
    cmp = F.compare_scenario_paths(legacy, fwd)
    assert cmp["agreement"] is not None
    assert len(cmp["disagreement"]) >= 1
    assert "ground truth" in cmp["note"]


# ---- D7 ----
def test_hypothesis_reject_and_null():
    fit, pairs, _ = _fit_pairs(n=80)
    ev = test_hypothesis(fit, pairs, hypothesis_id="H-test-001", m_tests=1)
    assert ev.hypothesis_id == "H-test-001"
    assert ev.h0.startswith("H0")
    assert ev.n > 0
    assert ev.multiplicity_adj in ("bonferroni-m=1", "none-prereg-single")
    assert not hasattr(ev, "signal") and "signal" not in ev.to_dict()
    # multiplicity divides
    ev4 = test_hypothesis(fit, pairs, hypothesis_id="H-test-004", m_tests=4)
    assert ev4.multiplicity_adj == "bonferroni-m=4"
    if ev.p_value is not None and ev4.p_value is not None:
        assert Decimal(ev4.p_value) >= Decimal(ev.p_value)
    # ledger idempotency + duplicate-bytes refusal
    led = HypothesisLedger(entries=())
    led2 = led.append(ev)
    assert led2.append(ev) is led2
    other = test_hypothesis(fit, pairs, hypothesis_id="H-test-001",
                            h1="H1: different", m_tests=1)
    # same inputs -> same bytes -> idempotent; force difference via m_tests
    other_diff = test_hypothesis(fit, pairs, hypothesis_id="H-test-001", m_tests=2)
    with pytest.raises(ValueError, match="duplicate"):
        led2.append(other_diff)
    with pytest.raises(ValueError, match="insufficient"):
        bad, _ = F.fit_forward_ols([], symbol=SYM, venue=VEN,
                                   horizon_ms=5_000, min_observations=10)
        test_hypothesis(bad, [], hypothesis_id="H-bad")


# ---- D8 ----
def _event_stream():
    from market_service.microstructure.contracts import OrderBookEvent
    evs = []
    q0 = _quote(1, 1_000, bid_qty=Decimal("100"))
    # persistent wall at bid 100 qty>=50, then absorption-like selling
    prev = q0
    for i in range(12):
        q = _quote(2 + i, 1_000 + (i + 1) * 2_000,
                   bid_price=Decimal("100"), bid_qty=Decimal(str(100 + (5 if i % 2 == 0 else -2))),
                   ask_price=Decimal("100.02"), ask_qty=Decimal("20"))
        from market_service.microstructure.ofi import event_contribution
        c = event_contribution(prev, q)
        evs.append(OrderBookEvent(prev, q, c))
        prev = q
    return evs


def test_events_and_envelope():
    evs = _event_stream()
    walls, wlog = detect_walls(evs, symbol=SYM, venue=VEN)
    assert wlog["scanned"] == len(evs)
    assert len(walls) >= 1
    w = walls[0]
    fields = {f.name for f in dataclasses.fields(w)}
    assert {"price", "side", "size", "distance_bps", "persistence_ms", "adds",
            "cancels", "executions", "replenishment", "flow_interaction",
            "response_ticks"} <= fields
    assert "institutional" not in json.dumps(w.to_dict())
    ab, alog = detect_absorption(evs, symbol=SYM, venue=VEN)
    assert alog["scanned"] == len(evs)
    assert "institutional" not in json.dumps([e.to_dict() for e in ab])
    agr = replay_agreement(evs, symbol=SYM, venue=VEN)
    assert agr["agreement"] is True
    assert agr["agreement_rate"] == 1.0
    # envelope type-only round trip
    from market_service.microstructure.events import EventEnvelope
    env = EventEnvelope(event=w, input_hash=w.input_hash,
                        detector_version=w.detector_version)
    rt = env.to_dict()
    assert rt["envelope_version"] == "event-envelope-v1"


# ---- D9 ----
def test_population_decay_and_nulls():
    fit, pairs, _ = _fit_pairs(n=60)
    key1 = F.population_key(pairs[0].x, 5_000, None)
    key2 = F.population_key(pairs[0].x, 60_000, None)
    assert key1 != key2
    rep = F.skill_decay_report(pairs, {5_000: fit})
    assert rep["decay_curve"][0]["h"] == 5_000
    assert "feed_resolution" in rep["feed_resolution_note"].lower() or "grain" in rep["feed_resolution_note"]
    # dead horizon listed as null
    dead, _ = F.fit_forward_ols([], symbol=SYM, venue=VEN,
                                horizon_ms=1_000, min_observations=10)
    rep2 = F.skill_decay_report(pairs, {1_000: dead, 5_000: fit})
    assert any("1_000" in r or "1000" in r for r in rep2["null_results"])
    assert 5_000 in rep2["finalized_horizons"]
    assert 1_000 not in rep2["finalized_horizons"]


# ---- D10 ----
def test_evidence_v2_compose_and_nulls():
    fit, pairs, _ = _fit_pairs()
    x = _vec(5_000, ofi="4")
    dist = F.predict_distribution(fit, x)
    scen = F.evaluate_forward_scenario(
        fit, x, spot_price=Decimal("100"), targets=[Decimal("100.10")],
        invalidations=[], tick_size=TICK)
    hyp = test_hypothesis(fit, pairs, hypothesis_id="H-v2-001")
    ev2 = F.assemble_evidence_v2(
        symbol=SYM, venue=VEN, evidence_id="ev2-test",
        generated_at_ms=99_000, tick_size=TICK, x=x, fit=fit,
        distribution=dist, scenario=scen, hypothesis=hyp,
        events=[{"note": "no_events"}], legacy_fit=None)
    d = ev2.to_dict()
    assert d["model_version"] == "evidence-v2"
    assert d["expected_dP_ticks"] is not None
    assert d["p_target"] is not None
    assert d["evidence"]["hypothesis_id"] == "H-v2-001"
    rt = json.loads(json.dumps(d))
    assert rt["evidence_id"] == "ev2-test"
    # unproven -> NULLs, never zeros
    ev_null = F.assemble_evidence_v2(
        symbol=SYM, venue=VEN, evidence_id="ev2-null",
        generated_at_ms=1, tick_size=TICK)
    dn = ev_null.to_dict()
    assert dn["expected_dP_ticks"] is None
    assert dn["p_target"] is None
    assert dn["hypothesis"] is None


# ---- D11 ----
def test_discipline_go_and_nogo():
    fit, pairs, _ = _fit_pairs()
    assert fit.comparator, "D4 comparator required for discipline"
    good = discipline_audit(
        forward_pairs=pairs, fits={5_000: fit},
        calibration_by_horizon={5_000: True},
        hypotheses=[{"m_tests": 1, "multiplicity_adj": "bonferroni-m=1"}],
        cost_statement="fees+spread+slippage downstream")
    assert good["verdict"] == "go"
    bad = discipline_audit(
        forward_pairs=pairs, fits={5_000: fit},
        calibration_by_horizon={5_000: False},
        hypotheses=[{"m_tests": 1, "multiplicity_adj": "bonferroni-m=1"}],
        cost_statement="x")
    assert bad["verdict"] == "no-go"
    assert "calibration" in bad["memo_template"]["open_violations"]


# ---- Consistency passes: replay seams, null-model gate, wall units ----
def test_replay_seams_round_trip():
    fit, pairs, _ = _fit_pairs(n=40)
    v = pairs[0].x
    assert FeatureVector.from_dict(v.to_dict()).input_hash == v.input_hash
    assert ForwardObservation.from_dict(pairs[0].to_dict()).x.input_hash == v.input_hash
    assert ForwardFit.from_dict(fit.to_dict()).fit_id == fit.fit_id
    assert ForwardFit.from_dict(fit.to_dict()).betas == fit.betas
    ev = test_hypothesis(fit, pairs, hypothesis_id="H-rt-001", m_tests=1)
    from market_service.microstructure.contracts import HypothesisEvidence
    assert HypothesisEvidence.from_dict(ev.to_dict()).input_hash == ev.input_hash


def test_intercept_only_model_is_insufficient():
    # All feature columns constant -> dropped -> null model must not validate.
    vecs = [
        F.build_feature_vector(symbol=SYM, venue=VEN, ts_ms=1000 + i * 1000,
                               ofi=Decimal("3"), average_depth=Decimal("1.5"))
        for i in range(40)
    ]
    mids = [(1000 + i * 1000, Decimal("100") + Decimal(i) * Decimal("0.01"))
            for i in range(40)]
    obs, _ = F.build_forward_observations(vecs, mids, tick_size=TICK, venue=VEN)
    fit, _ = F.fit_forward_ols(obs, symbol=SYM, venue=VEN, horizon_ms=1_000)
    assert fit.status == "insufficient"
    assert fit.betas.keys() == {"intercept"}


def test_wall_response_ticks_needs_tick_size():
    from market_service.microstructure.events import WallLifecycle
    evs = _event_stream()
    walls_none, _ = detect_walls(evs, symbol=SYM, venue=VEN)
    assert walls_none, "expected a wall fixture"
    assert walls_none[0].response_ticks is None  # no mislabeled quote units
    walls_tick, _ = detect_walls(evs, symbol=SYM, venue=VEN, tick_size=TICK)
    assert walls_tick[0].response_ticks is not None
    rt = WallLifecycle.from_dict(walls_tick[0].to_dict())
    assert rt.input_hash == walls_tick[0].input_hash
