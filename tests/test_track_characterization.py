"""Track characterisation — scripted walks measuring the hard-tracked loop.

Each walk runs a full narrate_cycle against the fake tape world and reads
metrics off meta + the artifact. The tape is constant-quote and gap-free,
so this characterises MACHINERY (positions, floors, steers, halts, budgets,
terminals) — not market behavior. Live-tape walks are a later pass.

Walks:
  healthy general      — full track, settles, one bounded repair.
  price-target         — scenario present, forward scenario refuses cleanly.
  jump-ahead           — early hypothesis call denied, track still completes.
  refusal-carry        — interpret refusal rides forward, no halt.
  halt                 — assemble refusal halts for the FSM retry decision.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from tests.test_engine import (
    _TRACK_ASSEMBLE,
    _TRACK_DISCIPLINE,
    _TRACK_HYPOTHESIZE,
    _TRACK_INTERPRET,
    _engine,
    _staged_narration,
    _track_tools,
    _wake,
)


def _evidence_prefix() -> list[str]:
    """Standard evidence reads (unchanged across walks)."""
    return [
        _staged_narration("P1", tools=[
            {"name": "calc.ofi.intervals", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
            {"name": "micro.ofi_intervals", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
        ]),
        _staged_narration("P2", tools=[
            {"name": "calc.depth.average", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
            {"name": "market.derivatives", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
        ]),
        _staged_narration("P5", tools=[
            {"name": "memory.recall_paper", "args": {"symbol": "BTCUSDT", "venue": "spot"}},
            {"name": "calc.price.delta", "args": {"symbol": "BTCUSDT", "venue": "spot", "ofi": "10"}},
        ]),
        _staged_narration("P6", final=True),
    ]


def _close() -> str:
    return _staged_narration("P6", final=True)


def _scenario_close(reason: str = "no_market_price") -> str:
    """Close turn for scenario walks: refusal cited, verdict unevaluable."""
    import json as _json

    payload = _json.loads(_staged_narration("P6", final=True))
    payload["evidence"] = list(payload.get("evidence") or []) + [{
        "path": "calc.scenario.evaluate → refusal",
        "value": reason,
        "interpretation": f"deterministic refusal ({reason}); no flow requirement computed",
        "metric_name": "refusal",
    }]
    payload["scenario"] = {
        "target_price": "100.50", "horizon": "15m",
        "verdict": "unevaluable",
        "rationale": (f"the scenario tool refused deterministically ({reason}) so no "
                        f"requirement or exceedance exists; recorded as unevaluable per null discipline"),
    }
    return _json.dumps(payload)


def _metrics(artifact: Any, meta: dict[str, Any]) -> dict[str, Any]:
    """Characterisation reading off one completed walk (pure projection)."""
    chain = (artifact.deterministic_state.get("statistical_chain") or {})
    positions = chain.get("positions") or {}
    log = artifact.capability_log or []
    return {
        "terminal": meta.get("terminal"),
        "llm_calls": meta.get("llm_calls"),
        "tool_rounds": meta.get("tool_rounds"),
        "repairs": meta.get("repairs"),
        "passed": bool((meta.get("final_validation") or {}).get("passed")),
        "reason_position": chain.get("reason_position"),
        "positions_complete": {
            name: bool((positions.get(name) or {}).get("complete"))
            for name in ("assemble", "interpret", "hypothesize")
        },
        "chain_complete": chain.get("complete"),
        "chain_missing": list(chain.get("missing") or []),
        "chain_refused": [r.get("tool") for r in (chain.get("refused") or [])],
        "chain_halt": artifact.deterministic_state.get("chain_halt"),
        "out_of_position": sum(
            1 for e in log
            if str(e.get("capability", "")).startswith("tool.out_of_position")),
        "suppressed": sum(
            1 for e in log
            if str(e.get("capability", "")).startswith("tool.suppressed")),
        "trace": [s.get("step") for s in
                  ((artifact.deterministic_state.get("forecast_result") or {})
                   .get("diagnostics", {}).get("chain_trace") or [])],
    }


def _report(name: str, metrics: dict[str, Any]) -> None:
    print(f"\n[characterisation:{name}] {json.dumps(metrics, default=str)}")


class HealthyTrackWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_general_walk_settles(self):
        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _close(),
            _track_tools(*_TRACK_DISCIPLINE),
            _close(),
        ])
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        m = _metrics(artifact, meta)
        _report("healthy-general", m)
        self.assertEqual(m["reason_position"], 3)
        self.assertTrue(all(m["positions_complete"].values()))
        self.assertEqual(m["terminal"], "settled")
        self.assertTrue(m["passed"])
        self.assertEqual(m["repairs"], 1)
        self.assertEqual(m["chain_halt"], None)
        self.assertEqual(m["out_of_position"], 0)
        # Budgets fit the floor with headroom (5/14/10 ceilings).
        passes = artifact.deterministic_state.get("loop_traversal", {})
        self.assertLessEqual(
            (passes.get("reasoning") or {}).get("passes_spent", 99), 5)
        self.assertLessEqual(m["llm_calls"], 14)
        self.assertLessEqual(m["tool_rounds"], 10)
        self.assertEqual(m["trace"], ["run_a", "run_b", "run_multivariate", "compare"])

    async def test_price_target_walk_settles_with_scenario_finding(self):
        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE, "calc.forward.scenario", "calc.scenario.evaluate"),
            _scenario_close(),
            _track_tools(*_TRACK_DISCIPLINE),
            _scenario_close(),
        ])
        scenario = {"target_price": "100.50", "horizon": "15m"}
        artifact, meta = await engine.narrate_cycle(
            _wake(), {"decision": "fire"}, scenario=scenario)
        m = _metrics(artifact, meta)
        _report("price-target", m)
        self.assertEqual(m["reason_position"], 3)
        self.assertTrue(all(m["positions_complete"].values()))
        # No collated runs in the fake world: forward scenario refuses
        # cleanly (attempted finding, never chain-missing).
        self.assertIn("calc.forward.scenario", m["chain_refused"])
        self.assertNotIn("calc.forward.scenario", m["chain_missing"])
        self.assertEqual(m["terminal"], "settled")
        self.assertTrue(m["passed"])


class JumpAheadWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_jump_ahead_denied_then_completes(self):
        # The jump call rides FIRST so the pass ceiling cannot defer it:
        # denial is by position, never by budget.
        jump_first = ["calc.hypothesis.test", "calc.ofi.intervals",
                      "calc.depth.average"]
        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(),
            _track_tools(*jump_first, hypothesis_id="H-early"),
            _track_tools("calc.forward.join"),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE, hypothesis_id="H-ontime"),
            _close(),
            _track_tools(*_TRACK_DISCIPLINE),
            _close(),
            _close(),  # spare: output composition may spend one turn
        ])
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        m = _metrics(artifact, meta)
        _report("jump-ahead", m)
        # The early call is denied log-only: pending, never classified.
        self.assertEqual(m["out_of_position"], 1)
        oop = [e for e in artifact.capability_log
               if str(e.get("capability", "")).startswith("tool.out_of_position")]
        self.assertEqual(oop[0].get("result"), "denied")
        self.assertEqual((oop[0].get("detail") or {}).get("active_position"), "assemble")
        self.assertEqual((oop[0].get("detail") or {}).get("tool_position"), "hypothesize")
        # The track still completes and settles on the on-time call.
        self.assertEqual(m["reason_position"], 3)
        self.assertEqual(m["terminal"], "settled")
        self.assertTrue(m["passed"])


class RefusalCarryWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_interpret_refusal_rides_forward_without_halt(self):
        import market_service.nooa_harness.engine.core as core_mod

        refusal = {
            "capability": "calc.decay.report",
            "scope": {}, "result": "ok",
            "detail": {"status": "refused", "reason": "no forward fits"},
        }
        real_execute = core_mod.context.execute_tool

        async def refusing_execute(store, name, args, **kwargs):
            if name == "calc.decay.report":
                return None, dict(refusal)
            return await real_execute(store, name, args, **kwargs)

        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _close(),
            _track_tools(*_TRACK_DISCIPLINE),
            _close(),
        ])
        core_mod.context.execute_tool = refusing_execute
        try:
            artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        finally:
            core_mod.context.execute_tool = real_execute
        m = _metrics(artifact, meta)
        _report("refusal-carry", m)
        self.assertEqual(m["chain_halt"], None)
        self.assertIn("calc.decay.report", m["chain_refused"])
        self.assertEqual(m["reason_position"], 3)
        self.assertEqual(m["terminal"], "settled")
        self.assertTrue(m["passed"])


class HaltWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_assemble_refusal_halts_for_fsm_retry(self):
        import market_service.nooa_harness.engine.core as core_mod

        refusal = {
            "capability": "calc.ofi.intervals",
            "scope": {}, "result": "ok",
            "detail": {"status": "refused", "reason": "tape gap at window open"},
        }
        real_execute = core_mod.context.execute_tool

        async def refusing_execute(store, name, args, **kwargs):
            if name == "calc.ofi.intervals":
                return None, dict(refusal)
            return await real_execute(store, name, args, **kwargs)

        engine, _s, _p, _m = _engine(llm_responses=[
            *_evidence_prefix(),
            _track_tools(*_TRACK_ASSEMBLE),
            _close(),
            _close(),
        ])
        core_mod.context.execute_tool = refusing_execute
        try:
            artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        finally:
            core_mod.context.execute_tool = real_execute
        m = _metrics(artifact, meta)
        _report("halt", m)
        # Halted at assemble: position never advances, synthesis never opens.
        self.assertEqual(m["chain_halt"], {
            "position": "assemble", "tool": "calc.ofi.intervals",
            "reason": "tape gap at window open"})
        self.assertEqual(m["reason_position"], 0)
        self.assertEqual(m["terminal"], "validation_failed")
        self.assertFalse(m["passed"])
        # The halt never clears itself: the reformulation offer runs the
        # assemble turn once more, where the same-args ofi re-attempt is
        # structurally suppressed and the halt persists into validation.
        ofi_entries = [
            e for e in artifact.capability_log
            if e.get("capability") == "calc.ofi.intervals"
        ]
        self.assertEqual(len(ofi_entries), 1)
        suppressed = [
            e for e in artifact.capability_log
            if str(e.get("capability", "")).startswith("tool.suppressed")
        ]
        self.assertEqual(len(suppressed), 1)
        missing = (meta.get("final_validation") or {}).get("missing") or []
        self.assertTrue(any("halted" in str(item) for item in missing))


class ReformulationWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_halt_clears_on_reformulated_reattempt(self):
        # The retry semantics end to end: assemble refuses at the default
        # window, the agent reformulates with a wider window (suppression
        # lifts for new args only), the halt clears, and the track settles.
        import market_service.nooa_harness.engine.core as core_mod

        real_execute = core_mod.context.execute_tool

        async def refusing_execute(store, name, args, **kwargs):
            if (name == "calc.ofi.intervals"
                    and int((args or {}).get("window_minutes") or 30) <= 30):
                return None, {
                    "capability": "calc.ofi.intervals", "scope": {},
                    "result": "ok", "detail": {
                        "status": "refused", "reason": "tape gap at window open"},
                }
            return await real_execute(store, name, args, **kwargs)

        def _reform():
            import json as _json
            payload = _json.loads(_track_tools("calc.ofi.intervals"))
            payload["tool_calls"][0]["args"]["window_minutes"] = 60
            return _json.dumps(payload)

        engine, _s, _p, _m = _engine(llm_responses=[
            # NOTE: evidence consumes exactly three turns, and the halt
            # fires on the first reasoning dispatch already (the refusal is
            # cycle-scoped) — so the reformulation offer is 4th, with no
            # empty close in between (that slot belongs to healthy flows).
            *_evidence_prefix()[:3],
            _reform(),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _close(),
            _track_tools(*_TRACK_DISCIPLINE),
            _close(),
        ])
        core_mod.context.execute_tool = refusing_execute
        try:
            artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        finally:
            core_mod.context.execute_tool = real_execute
        m = _metrics(artifact, meta)
        _report("reformulation", m)
        # Halt fired, then cleared by the re-evaluation — no halt survives.
        self.assertEqual(m["chain_halt"], None)
        self.assertEqual(m["reason_position"], 3)
        self.assertTrue(all(m["positions_complete"].values()))
        self.assertEqual(m["terminal"], "settled")
        self.assertTrue(m["passed"])
        # Same-args repeat would have suppressed; the wider window executed.
        # NOTE: the refusal above is logged under the intercepted dotted
        # name while genuine executions log the registry capability
        # "calc.ofi_intervals" — count evaluated executions, not strings.
        evaluated = [
            e for e in artifact.capability_log
            if e.get("capability") == "calc.ofi_intervals"
            and e.get("result") == "ok"
            and not (isinstance(e.get("detail"), dict)
                     and e.get("detail", {}).get("status") == "refused")
        ]
        self.assertGreaterEqual(len(evaluated), 1)
