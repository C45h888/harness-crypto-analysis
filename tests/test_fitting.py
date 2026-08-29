"""Pass-3 fitting tests: determinism, quality gates, depth scaling, sensitivity.

Fixture-driven (hand-worked synthetic intervals) per the constitutional
validation gates: determinism of input_hash, the status trichotomy, the
multi-block requirement of depth scaling, the tautology-caveat sensitivity
fit, and evidence round-trip serialization.
"""

from __future__ import annotations

import json
from decimal import Decimal

from market_service.microstructure import fitting
from market_service.microstructure.contracts import (
    FIT_MODEL_VERSION,
    BestQuoteState,
    OFIInterval,
    OrderBookEvent,
    PriceImpactFit,
)
from market_service.microstructure.ofi import DEPTH_ESTIMATOR, OFIAggregator, _mid

SYMBOL = "BTCUSDT"
VENUE = "spot"
TICK = Decimal("0.01")


def make_interval(
    k: int, ofi: str, depth: str | None, delta_ticks: str,
    *, interval_ms: int = 10_000, quality: str = "exact_feed",
) -> OFIInterval:
    """One hand-worked interval with self-consistent boundary mids."""
    start = k * interval_ms
    end = start + interval_ms
    mid_start = Decimal(100)
    mid_end = mid_start + Decimal(delta_ticks) * TICK
    return OFIInterval(
        symbol=SYMBOL, venue=VENUE, start_ts_ms=start, end_ts_ms=end,
        event_count=1, ofi=Decimal(ofi),
        average_depth=Decimal(depth) if depth is not None else None,
        first_update_id=k, last_update_id=k, quality=quality,
        mid_start=mid_start, mid_end=mid_end,
        depth_estimator=DEPTH_ESTIMATOR,
    )


def make_block(
    beta: str, depth: str, *, n: int, scale: int = 10, noise: bool = True,
) -> list[OFIInterval]:
    """Synthetic block where ΔP_k = beta * OFI_k (+ tiny alternating noise)."""
    intervals = []
    for k in range(n):
        ofi = Decimal(scale * (k + 1))
        noise_ticks = Decimal("0.01") if (noise and k % 2) else Decimal(0)
        delta_ticks = Decimal(beta) * ofi + noise_ticks
        intervals.append(make_interval(k, str(ofi), depth, str(delta_ticks)))
    return intervals


def test_input_hash_is_deterministic_and_order_sensitive() -> None:
    block = make_block("0.001", "5", n=40)
    config = {"symbol": SYMBOL, "venue": VENUE}
    h1 = fitting.input_hash(block, config)
    h2 = fitting.input_hash(list(block), config)
    assert h1 == h2
    # Different ordering changes the hash (canonical serialization).
    assert fitting.input_hash(list(reversed(block)), config) != h1
    # Different config changes the hash.
    assert fitting.input_hash(block, {"symbol": SYMBOL, "venue": "futures"}) != h1


def test_fit_recovers_beta_and_passes_gates() -> None:
    block = make_block("0.002", "5", n=80)
    fit, observations = fitting.fit_price_impact(
        block, symbol=SYMBOL, venue=VENUE, tick_size=TICK, interval_seconds=10,
    )
    assert len(observations) == 80
    assert fit.n_observations == 80
    assert fit.excluded_observations == 0
    assert fit.price_unit == "ticks"
    assert fit.input_hash == fitting.input_hash(block, {
        "symbol": SYMBOL, "venue": VENUE, "tick_size": str(TICK),
        "interval_seconds": 10, "sensitivity": False, "min_observations": 30,
    })
    assert abs(float(fit.beta) - 0.002) < 1e-6
    assert fit.mean_ad == Decimal(5)
    assert fit.status == "validated", fit.status
    assert fit.r2 is not None and float(fit.r2) > Decimal("0.99")


def test_zero_ofi_variance_is_insufficient() -> None:
    block = [make_interval(k, "7", "5", "0") for k in range(40)]
    fit, _ = fitting.fit_price_impact(
        block, symbol=SYMBOL, venue=VENUE, tick_size=TICK, interval_seconds=10,
    )
    assert fit.status == "insufficient"
    assert fit.beta == Decimal(0)
    assert fit.stderr_beta is None


def test_fewer_than_min_observations_is_insufficient() -> None:
    block = make_block("0.001", "5", n=10)
    fit, observations = fitting.fit_price_impact(
        block, symbol=SYMBOL, venue=VENUE, tick_size=TICK, interval_seconds=10,
    )
    assert len(observations) == 10
    assert fit.status == "insufficient"
    assert fit.n_observations == 10


def test_insufficient_quality_intervals_are_excluded() -> None:
    good = make_block("0.001", "5", n=60)
    bad = make_interval(999, "3", "5", "0", quality="approximate")
    fit, observations = fitting.fit_price_impact(
        good + [bad], symbol=SYMBOL, venue=VENUE, tick_size=TICK,
        interval_seconds=10,
    )
    assert fit.excluded_observations == 1
    assert len(observations) == 60


def test_foreign_depth_estimator_label_is_rejected() -> None:
    block = make_block("0.001", "5", n=40)
    alien = OFIInterval(
        symbol=SYMBOL, venue=VENUE, start_ts_ms=0, end_ts_ms=10_000,
        event_count=1, ofi=Decimal(1), average_depth=Decimal(5),
        first_update_id=0, last_update_id=0, quality="exact_feed",
        mid_start=Decimal(100), mid_end=Decimal("100.01"),
        depth_estimator="some_other_estimator",
    )
    try:
        fitting.fit_price_impact(
            block + [alien], symbol=SYMBOL, venue=VENUE, tick_size=TICK,
            interval_seconds=10,
        )
        raise AssertionError("expected ValueError for foreign estimator label")
    except ValueError:
        pass


def test_depth_scaling_needs_multiple_blocks() -> None:
    fit_single = fitting.fit_depth_scaling(
        [], symbol=SYMBOL, venue=VENUE,
    )
    assert fit_single.status == "insufficient"
    assert fit_single.c is None and fit_single.lambda_ is None


def _synthetic_beta_fit(beta: str, mean_ad: str, fit_id: str) -> PriceImpactFit:
    """Minimal validated-style PriceImpactFit for depth-scaling inputs."""
    return PriceImpactFit(
        fit_id=fit_id, symbol=SYMBOL, venue=VENUE,
        window_start_ms=0, window_end_ms=1, interval_seconds=10,
        alpha=Decimal(0), beta=Decimal(beta), stderr_beta=Decimal("0.01"),
        robust_se_method="HC0", n_observations=100, excluded_observations=0,
        r2=Decimal("0.8"), residual_std=Decimal("0.1"),
        heteroskedasticity_flag=False, mean_ad=Decimal(mean_ad),
        price_unit="ticks", tick_size=TICK, input_hash="x",
        model_version=FIT_MODEL_VERSION, sensitivity=False, status="validated",
    )


def test_depth_scaling_recovers_lambda_across_blocks() -> None:
    # β_i = c / AD_i^λ with c=2, λ=0.5, AD ∈ {1, 4, 16} → β ∈ {2, 1, 0.5}.
    block_fits = [
        _synthetic_beta_fit("2", "1", "beta-a"),
        _synthetic_beta_fit("1", "4", "beta-b"),
        _synthetic_beta_fit("0.5", "16", "beta-c"),
    ]
    fit = fitting.fit_depth_scaling(block_fits, symbol=SYMBOL, venue=VENUE)
    assert fit.n_blocks == 3
    assert fit.depth_estimator == DEPTH_ESTIMATOR
    assert fit.fit_ids == ("beta-a", "beta-b", "beta-c")
    assert fit.c is not None and abs(float(fit.c) - 2.0) < 1e-6
    assert fit.lambda_ is not None and abs(float(fit.lambda_) - 0.5) < 1e-6
    assert fit.status == "provisional"  # n=3 < 2*min_blocks → not validated


def test_depth_scaling_insufficient_when_depths_identical() -> None:
    block_fits = [
        _synthetic_beta_fit("2", "4", "beta-a"),
        _synthetic_beta_fit("1", "4", "beta-b"),
        _synthetic_beta_fit("0.5", "4", "beta-c"),
    ]
    fit = fitting.fit_depth_scaling(block_fits, symbol=SYMBOL, venue=VENUE)
    assert fit.status == "insufficient"
    assert fit.c is None


def test_depth_scaling_excludes_negative_beta_inputs() -> None:
    good = [_synthetic_beta_fit("2", "1", "beta-a"),
            _synthetic_beta_fit("1", "4", "beta-b")]
    negative = _synthetic_beta_fit("-1", "9", "beta-neg")
    fit = fitting.fit_depth_scaling(good + [negative], symbol=SYMBOL, venue=VENUE)
    assert fit.status == "insufficient"  # only 2 usable blocks


def test_sensitivity_fit_excludes_price_changing_events() -> None:
    # Two intervals; events: one queue event (price unchanged) in interval 0
    # and one price-changing event in interval 1. Sensitivity OFI must drop
    # the price-changing contribution entirely.

    def quote(update_id: int, *, bid_qty: str = "10", ask_qty: str = "20",
              ts: int, bid_price: str = "100", ask_price: str = "101") -> BestQuoteState:
        return BestQuoteState(
            symbol=SYMBOL, venue=VENUE, update_id=update_id,
            exchange_ts_ms=ts, received_ts_ms=ts,
            bid_price=Decimal(bid_price), bid_qty=Decimal(bid_qty),
            ask_price=Decimal(ask_price), ask_qty=Decimal(ask_qty),
        )

    q0 = quote(1, ts=100)
    q1 = quote(2, bid_qty="15", ts=5_000)            # queue add, price same
    q2 = quote(3, bid_price="100.5", bid_qty="7", ts=15_000)  # price moves
    e_queue = OrderBookEvent(q0, q1, Decimal(5))
    e_price = OrderBookEvent(q1, q2, Decimal(7))
    assert not e_queue.price_changed
    assert e_price.price_changed

    intervals = fitting.replay_intervals([e_queue, e_price], interval_ms=10_000)
    assert len(intervals) == 2
    primary, _ = fitting.fit_price_impact(
        intervals, symbol=SYMBOL, venue=VENUE, tick_size=TICK,
        interval_seconds=10, min_observations=1,
    )
    sensitivity, _ = fitting.fit_price_impact(
        intervals, symbol=SYMBOL, venue=VENUE, tick_size=TICK,
        interval_seconds=10, min_observations=1,
        sensitivity=True, events=[e_queue, e_price],
    )
    assert primary.fit_id.startswith("beta-")
    assert sensitivity.fit_id.startswith("sens-")
    assert sensitivity.sensitivity is True
    # Sensitivity drops the price-changing event's OFI contribution: the two
    # intervals now have OFI 5 and 0 respectively (primary had 5 and 7).
    assert sensitivity.n_observations == primary.n_observations


def test_replay_round_trip_and_evidence_serialization() -> None:
    block = make_block("0.001", "5", n=60)
    evidence = fitting.assemble_evidence(
        block, symbol=SYMBOL, venue=VENUE, tick_size=TICK, interval_seconds=10,
        evidence_id="ev-test", generated_at_ms=block[-1].end_ts_ms,
    )
    payload = evidence.to_dict()
    # JSON round-trip must survive (Postgres JSONB + Redis projection).
    decoded = json.loads(json.dumps(payload))
    assert decoded["evidence_id"] == "ev-test"
    assert decoded["model_version"] == FIT_MODEL_VERSION
    assert decoded["depth_estimator"] == DEPTH_ESTIMATOR
    assert decoded["price_impact_fit"]["status"] in ("validated", "provisional", "insufficient")
    assert decoded["depth_scaling_fit"]["status"] == "insufficient"  # one block only
    assert decoded["depth_scaling_fit"]["c"] is None
    assert decoded["sensitivity_fit"] is None  # no raw events supplied
    assert decoded["status"] == decoded["price_impact_fit"]["status"]


def test_interval_contract_carries_estimator_and_mids() -> None:
    aggregator = OFIAggregator(10_000)
    q0 = BestQuoteState(
        symbol=SYMBOL, venue=VENUE, update_id=1, exchange_ts_ms=100,
        received_ts_ms=100, bid_price=Decimal(100), bid_qty=Decimal(10),
        ask_price=Decimal(101), ask_qty=Decimal(20),
    )
    q1 = BestQuoteState(
        symbol=SYMBOL, venue=VENUE, update_id=2, exchange_ts_ms=5_000,
        received_ts_ms=5_000, bid_price=Decimal(100), bid_qty=Decimal(15),
        ask_price=Decimal(101), ask_qty=Decimal(20),
    )
    event = OrderBookEvent(q0, q1, Decimal(5))
    aggregator.add(event)
    closed = aggregator.flush()
    assert closed is not None
    assert closed.depth_estimator == DEPTH_ESTIMATOR
    assert closed.mid_start == _mid(q0)
    assert closed.mid_end == _mid(q1)
    restored = OFIInterval.from_dict(closed.to_dict())
    assert restored == closed
