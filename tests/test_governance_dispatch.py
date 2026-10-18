"""Dispatch guard — pure FSM pre-fire ticket (no I/O, no workers)."""

from market_service.governance import (
    AnalysisGovernanceFSM,
    CalculationGovernanceFSM,
)
from market_service.governance.manifest import (
    build_analysis_manifest,
    build_calculation_manifest,
    known_graph_sections,
    plane_status_vocabulary,
)


def _calc_fsm() -> CalculationGovernanceFSM:
    return CalculationGovernanceFSM(
        build_calculation_manifest(),
        known_sections=known_graph_sections(),
        status_vocabulary=plane_status_vocabulary(),
    )


def _analysis_fsm() -> AnalysisGovernanceFSM:
    return AnalysisGovernanceFSM(
        build_analysis_manifest(),
        known_sections=known_graph_sections(),
        status_vocabulary=plane_status_vocabulary(),
    )


def _ages(fsm, value):
    return {name: value for name in fsm.substrates()}


def test_cold_start_all_due_batch0():
    fsm = _calc_fsm()
    ticket = fsm.decide_dispatch(
        symbol="SOLUSDT", requested=None, ages_ms=_ages(fsm, None))
    assert not ticket.deferred
    assert set(ticket.allowed) == set(fsm.substrates())
    assert len(ticket.batches) == 1  # all cold → single priority batch
    assert ticket.rule == "dispatch-allow"


def test_cooldown_hot_denied_without_compute():
    fsm = _calc_fsm()
    ticket = fsm.decide_dispatch(
        symbol="SOLUSDT", requested=["tape"], ages_ms={"tape": 1_000})
    assert ticket.allowed == ()
    assert ticket.rule == "dispatch-none-due"
    assert ticket.denied[0].rule == "cooldown"
    assert (ticket.denied[0].retry_after_ms or 0) > 0


def test_staleness_beats_probe_batch_order():
    fsm = _calc_fsm()
    manifests = {m.substrate: m for m in fsm.manifests()}
    stale_name = "tape"
    probe_name = "anchors"
    ages = {
        stale_name: manifests[stale_name].staleness_s * 1000 + 1,
        probe_name: manifests[probe_name].cooldown_s * 1000 + 1,
    }
    # Ensure probe age is below staleness so it lands batch-1.
    assert ages[probe_name] < manifests[probe_name].staleness_s * 1000
    ticket = fsm.decide_dispatch(
        symbol="SOLUSDT", requested=[stale_name, probe_name], ages_ms=ages)
    assert list(ticket.allowed)[0] == stale_name
    assert len(ticket.batches) == 2


def test_quarantine_skips_binding_drift():
    fsm = _calc_fsm()
    ticket = fsm.decide_dispatch(
        symbol="SOLUSDT", requested=["tape", "density"],
        ages_ms={"tape": 10_000_000, "density": 10_000_000},
        quarantined=frozenset({"tape"}))
    assert "tape" not in ticket.allowed
    assert any(d.substrate == "tape" and d.rule == "quarantined-binding"
               for d in ticket.denied)


def test_unknown_substrate_denied():
    fsm = _calc_fsm()
    ticket = fsm.decide_dispatch(
        symbol="SOLUSDT", requested=["nope"], ages_ms={})
    assert ticket.allowed == ()
    assert ticket.denied[0].rule == "unknown-substrate"


def test_cross_plane_defer_awaits():
    fsm = _calc_fsm()
    ticket = fsm.decide_dispatch(
        symbol="SOLUSDT", requested=None, ages_ms=_ages(fsm, None),
        other_plane_busy=True)
    assert ticket.deferred
    assert ticket.allowed == ()
    assert ticket.rule == "dispatch-deferred-cross-plane"


def test_quarantine_from_denials_pure():
    fsm = _calc_fsm()
    assert fsm.quarantine_from_denials([
        {"substrate": "oi", "rule": "binding-drift"},
        {"substrate": "tape", "rule": "allow"},
        {"substrate": "signals", "rule": "schema-drift"},
    ]) == frozenset({"oi", "signals"})


def test_analysis_batches_separately():
    fsm = _analysis_fsm()
    if not fsm.substrates():
        return
    ticket = fsm.decide_dispatch(
        symbol="SOLUSDT", requested=None,
        ages_ms={n: None for n in fsm.substrates()})
    assert not ticket.deferred
    assert set(ticket.allowed) == set(fsm.substrates())
    assert fsm.dispatch_intra_parallel() == 2
    assert _calc_fsm().dispatch_intra_parallel() == 4


def test_token_budget_walls():
    from market_service.nooa_harness.engine.config import token_budget_breakdown
    budget = token_budget_breakdown()
    assert budget["max_tokens"] >= 2_000
    assert budget["reasoning_reserve"] >= 2_000
    assert budget["final_floor"] >= 0
    assert budget["reasoning_reserve"] + budget["final_floor"] == budget["max_tokens"]
