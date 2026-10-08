"""Fix-plane coherence tests — 2026-09-27 long-horizon / tools / judgement pass.

What each block pins (regression = the live failure it came from):

  HorizonBridge      — 4h questions died on `ValueError: unsupported
                       horizon_ms: 14400000` deep in the forward stack.
  PlanBinding        — a long-horizon task bound no forecast horizon at all.
  HypothesisId       — `calc.hypothesis.test` refused forever
                       ("hypothesis_id required (pre-registration)") because
                       nothing produced an id.
  MergeJudgement     — final validation judged only the LAST turn and a bare
                       P6 declaration zeroed out the whole cycle.
  RootsIdentity      — evidence roots collapsed every calc.* tool into "calc",
                       so "≥2 distinct roots" was unreachable.
  SchemaParity       — NarrationTurn (structured probe) lacked keys the turn
                       contract demands (scenario / forward_scenario /
                       hypothesis_evidence).
  EmptyReadSurface   — market.read returned a bare null on empty, invisible to
                       the agent, so the empty read persisted.
  ArgContracts       — the model saw result shapes only and guessed arg values.
"""
import asyncio
import unittest

from market_service.microstructure.contracts import ForwardFit
from market_service.microstructure import horizon_bridge as hb


def _native_fit(**overrides) -> ForwardFit:
    base = dict(
        fit_id="fit-1", symbol="SOLUSDT", venue="futures", horizon_ms=60_000,
        betas={"intercept": "0.5", "ofi_10s": "1.2"},
        stderr={"intercept": "0.1", "ofi_10s": "0.2"},
        r2="0.31", resid_std="2.0", hetero_flag=False, n_obs=100, n_excluded=5,
        oos_skill="0.4", comparator={}, input_hash="h",
        model_version="forward-ols-v2", status="validated",
        n_train=70, n_oos=30, estimation_status="fitted",
        validation_status="validated", probability_status="validated",
    )
    base.update(overrides)
    return ForwardFit(**base)


class HorizonBridgeTests(unittest.TestCase):
    """The deterministic long-horizon adapter (native fit → 15m/1h/4h)."""

    def test_vocabulary_matches_the_two_base_planes(self):
        import market_service.microstructure as fm
        from market_service.nooa_harness.engine import task_directive as td

        self.assertEqual(tuple(fm.FORWARD_HORIZONS_MS), hb.NATIVE_HORIZONS_MS)
        self.assertEqual(tuple(sorted(td.LONG_HORIZONS_MS.values())),
                         tuple(sorted(hb.LONG_HORIZONS_MS)))

    def test_long_horizon_is_projected_not_refused(self):
        bridged = hb.bridge_forward_fit(_native_fit(), 14_400_000)
        self.assertEqual(bridged.horizon_ms, 14_400_000)
        self.assertEqual(bridged.estimation_status, "extrapolated")
        self.assertEqual(bridged.validation_status, "extrapolated")
        self.assertEqual(bridged.status, "provisional")  # never "validated"
        self.assertEqual(bridged.bridge["source_horizon_ms"], 60_000)
        # sigma scaled sqrt(H/h0) = sqrt(240) ≈ 15.4919…
        self.assertTrue(bridged.resid_std.startswith("30.98"))
        # skill decayed 1/sqrt(240) ≈ 0.0258…
        self.assertTrue(bridged.oos_skill.startswith("0.0258"))
        # drift expectation carried unchanged
        self.assertEqual(bridged.betas, _native_fit().betas)

    def test_bridged_fit_roundtrips(self):
        bridged = hb.bridge_forward_fit(_native_fit(), 3_600_000)
        back = ForwardFit.from_dict(bridged.to_dict())
        self.assertEqual(back.bridge["method"], hb.BRIDGE_METHOD)
        self.assertEqual(back.horizon_ms, 3_600_000)

    def test_insufficient_stays_insufficient(self):
        bridged = hb.bridge_forward_fit(
            _native_fit(status="insufficient", estimation_status="insufficient",
                        validation_status="unvalidated", oos_skill=None),
            900_000)
        self.assertEqual(bridged.status, "insufficient")
        self.assertIsNone(bridged.oos_skill)

    def test_unsupported_horizon_refusal_names_the_domain(self):
        with self.assertRaises(hb.UnsupportedHorizonError) as ctx:
            hb.require_supported(300_000)
        self.assertIn(300_000, [hb.LONG_HORIZONS_MS[0]] and [300_000])
        self.assertEqual(ctx.exception.supported, hb.supported_horizons())
        self.assertIn("supported", str(ctx.exception))

    def test_classify(self):
        self.assertEqual(hb.classify_horizon(5_000), "native")
        self.assertEqual(hb.classify_horizon(14_400_000), "long")
        self.assertIsNone(hb.classify_horizon(777))


class PlanBindingTests(unittest.TestCase):
    """Long-horizon tasks must bind the QUESTION horizon to the forecast."""

    def test_long_task_without_targets_binds_question_horizon(self):
        from market_service.nooa_harness.engine.task_directive import (
            build_plan, parse_task_directive,
        )
        d = parse_task_directive(
            "does the OFI state favour continuation over 4h? frame H0 vs H1",
            None)
        self.assertEqual(d.horizon_regime, "long")
        plan = build_plan(d)
        self.assertEqual(plan["forecast_horizon_ms"], 14_400_000)
        self.assertTrue(any("bridge" in n for n in plan["notes"]))

    def test_long_targets_route_still_ownes_exceedance(self):
        from market_service.nooa_harness.engine.task_directive import (
            build_plan, parse_task_directive,
        )
        d = parse_task_directive(None, {"target_price": "220", "horizon": "1h"})
        plan = build_plan(d)
        self.assertIn("calc.scenario.evaluate", plan["pre_acquire"])
        self.assertEqual(plan["forecast_horizon_ms"], 3_600_000)

    def test_native_task_binds_native_horizon(self):
        from market_service.nooa_harness.engine.task_directive import (
            build_plan, parse_task_directive,
        )
        d = parse_task_directive("can SOL hit $220 in the next 60 seconds?", None)
        plan = build_plan(d)
        self.assertEqual(plan["forecast_horizon_ms"], 60_000)


class HypothesisIdTests(unittest.TestCase):
    """Pre-registration identity is ENGINE-SUPPLIED and deterministic."""

    def test_derived_id_is_stable_and_seed_bearing(self):
        from market_service.nooa_harness.engine.core.driver import (
            _derived_hypothesis_id,
        )

        class _Ctx:
            task = "frame H0 vs H1 for the 4h question"

        class _St:
            task_directive = {"hypothesis_seed": {"H0": "E[dP|X] <= 0",
                                                  "H1": "E[dP|X] > 0"}}

        a = _derived_hypothesis_id(_Ctx(), _St())
        b = _derived_hypothesis_id(_Ctx(), _St())
        self.assertEqual(a[0], b[0])
        self.assertTrue(a[0].startswith("hyp-"))
        self.assertEqual(a[1], "E[dP|X] <= 0")

    def test_falls_back_to_task_text(self):
        from market_service.nooa_harness.engine.core.driver import (
            _derived_hypothesis_id,
        )

        class _Ctx:
            task = "some task text"

        class _St:
            task_directive = None

        hid, h0, h1 = _derived_hypothesis_id(_Ctx(), _St())
        self.assertTrue(hid.startswith("hyp-"))
        self.assertIsNone(h0)
        self.assertIsNone(h1)


class MergeJudgementTests(unittest.TestCase):
    """Final validation judges the MERGED interpretation, not the last turn."""

    def test_bare_declaration_turn_does_not_erase_content(self):
        from market_service.nooa_harness.engine.core.reasoning import (
            merge_interpretation,
        )
        turns = [
            {"phase": "P4", "summary": "x" * 250,
             "hypothesis": {"H0": "h0", "H1": "h1"},
             "evidence": [{"path": "calc.price.delta → data.delta_ticks",
                           "interpretation": "delta"}]},
            {"phase": "P6", "summary": None, "evidence": [],
             "tool_calls": [], "hypothesis": None},
        ]
        merged = merge_interpretation(turns)
        self.assertEqual(len(merged["summary"]), 250)
        self.assertEqual(merged["hypothesis"]["H0"], "h0")
        self.assertEqual(len(merged["evidence"]), 1)

    def test_evidence_union_dedupes_and_scenario_last_wins(self):
        from market_service.nooa_harness.engine.core.reasoning import (
            merge_interpretation,
        )
        entry = {"path": "calc.ofi.intervals → data[0].ofi",
                 "interpretation": "flow"}
        merged = merge_interpretation([
            {"evidence": [entry], "scenario": {"verdict": "reachable"}},
            {"evidence": [entry],
             "scenario": {"verdict": "unevaluable"}},
        ])
        self.assertEqual(len(merged["evidence"]), 1)
        self.assertEqual(merged["scenario"]["verdict"], "unevaluable")


class RootsIdentityTests(unittest.TestCase):
    """Two different calc.* tools are TWO roots (not one root 'calc')."""

    def test_tool_heads_count_distinct(self):
        from market_service.nooa_harness.engine.core.reasoning import (
            validate_final_turn,
        )
        parsed = {
            "summary": "x" * 250,
            "confidence": "medium",
            "hypothesis": {"H0": "h0", "H1": "h1"},
            "evidence": [
                {"path": "calc.price.delta → data.delta_ticks",
                 "interpretation": "delta"},
                {"path": "calc.ofi.intervals → data[0].ofi",
                 "interpretation": "flow"},
            ],
        }
        passed, missing = validate_final_turn(parsed, {
            "P1": {"calc.ofi.intervals"}, "P2": {"calc.depth.average"},
            "P3": {"market.read"}, "P5": {"calc.price.delta"}, "P6": {"declared"},
        })
        self.assertTrue(
            not any("roots" in m for m in missing), missing)

    def test_deterministic_state_only_is_still_rejected(self):
        from market_service.nooa_harness.engine.core.reasoning import (
            validate_final_turn,
        )
        parsed = {
            "summary": "x" * 250, "confidence": "low",
            "hypothesis": {"H0": "h0"},
            "evidence": [
                {"path": "deterministic_state.gate.status",
                 "interpretation": "state"},
                {"path": "deterministic_state.task_directive.kind",
                 "interpretation": "kind"},
            ],
        }
        _passed, missing = validate_final_turn(parsed, {
            "P1": {"calc.ofi.intervals"}, "P2": {"calc.depth.average"},
            "P3": {"market.read"}, "P5": {"calc.price.delta"}, "P6": {"declared"},
        })
        self.assertTrue(any("roots" in m for m in missing), missing)


class SchemaParityTests(unittest.TestCase):
    """NarrationTurn must carry every key the turn contract demands."""

    def test_structured_probe_cannot_drop_contract_keys(self):
        from market_service.nooa_harness.engine.schemas import NarrationTurn
        contract_keys = {
            "phase", "summary", "evidence", "confidence", "limitations",
            "model_separation", "hypothesis", "scenario", "forward_scenario",
            "hypothesis_evidence", "tool_calls", "memory_proposals",
        }
        fields = set(NarrationTurn.model_fields)
        self.assertEqual(contract_keys - fields, set())


class EmptyReadSurfaceTests(unittest.TestCase):
    """An empty read returns the surface inventory as DATA, never a null."""

    def test_empty_market_read_is_citable(self):
        from market_service.nooa_harness.inference.tooling.tools_market import (
            dispatch_market_read,
        )

        class _Redis:
            async def get(self, key):
                return None

            async def xlen(self, key):
                return 0

        class _Store:
            prefix = "marketflow"
            redis = _Redis()

            def collated_latest_key(self, symbol):
                return f"marketflow:latest:{symbol}:collated"

            def collated_stream(self, symbol):
                return f"marketflow:stream:collated:{symbol}"

            def domain_latest_key(self, symbol, source):
                return f"marketflow:latest:{symbol}:{source}"

            def wall_history_stream(self, symbol):
                return f"marketflow:history:{symbol}:walls"

            def keystone_history_stream(self, symbol):
                return f"marketflow:history:{symbol}:keystones"

        result, log = asyncio.run(dispatch_market_read(
            _Store(), "SOLUSDT", venue="futures"))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "empty")
        self.assertIn("checked", result)
        self.assertIn("read_tools", result)
        self.assertIn("market.read", result["read_tools"])
        self.assertEqual(log["detail"]["status"], "empty")

    def test_surfaces_mode_lists_every_plane(self):
        from market_service.nooa_harness.inference.tooling.tools_market import (
            dispatch_market_read,
        )

        class _Redis:
            async def get(self, key):
                return None

            async def xlen(self, key):
                return 3

        class _Store:
            redis = _Redis()

            def collated_latest_key(self, symbol):
                return "a"

            def collated_stream(self, symbol):
                return "b"

            def domain_latest_key(self, symbol, source):
                return f"d:{source}"

            def wall_history_stream(self, symbol):
                return "w"

            def keystone_history_stream(self, symbol):
                return "k"

        result, _log = asyncio.run(dispatch_market_read(
            _Store(), "SOLUSDT", venue="futures", mode="surfaces"))
        self.assertIn("surfaces", result)
        self.assertTrue(result["available_surfaces"])


class ArgContractTests(unittest.TestCase):
    """Arg domains are rendered from the deterministic base, not prose."""

    def test_arg_block_names_horizon_domain_and_read_modes(self):
        from market_service.nooa_harness.engine.tool_schemas import tool_arg_block
        block = tool_arg_block()
        self.assertIn("900000, 3600000, 14400000", block)
        self.assertIn("LONG-HORIZON BRIDGE", block)
        self.assertIn("surfaces", block)
        self.assertIn("ENGINE-SUPPLIED", block)

    def test_validation_retry_budget(self):
        from market_service.nooa_harness.engine.config import (
            VALIDATION_RETRY_PASSES,
        )
        self.assertEqual(VALIDATION_RETRY_PASSES, 2)

    def test_repair_prompt_carries_final_field_shapes(self):
        from market_service.nooa_harness.engine import kb
        self.assertIn("FINAL-TURN FIELD SHAPES", kb.FINAL_SHAPE_BLOCK)
        self.assertIn("hypothesis", kb.FINAL_SHAPE_BLOCK)


class ForcedFinalTests(unittest.IsolatedAsyncioTestCase):
    """The FORCED FINAL turn: tool-mode loops must still produce an artifact.

    Regression (live 2026-09-28): 12 in-loop turns returned tool_calls only —
    summary/evidence/hypothesis empty in EVERY turn — while the same model
    with a dedicated finalize request returns the complete final object.
    """

    async def test_forced_final_supplies_missing_interpretation(self):
        import json as _json

        from tests.test_engine import (
            _FETCH_TURN,
            _TRACK_ASSEMBLE, _TRACK_DISCIPLINE, _TRACK_HYPOTHESIZE,
            _TRACK_INTERPRET, _engine, _good_narration, _track_tools, _wake,
        )
        final = _json.dumps({
            "phase": "P6", "tool_calls": [],
            "summary": "x" * 250,
            "evidence": [
                {"path": "calc.price.delta → route_a_direct.delta_ticks",
                 "value": "0.017", "interpretation": "derived delta P"},
                {"path": "calc.ofi.intervals → data[0].ofi",
                 "value": "12.5", "interpretation": "order flow"},
            ],
            "confidence": "medium",
            "limitations": ["thin tape"],
            "model_separation": "beta and c/lambda read separately",
            "hypothesis": {"H0": "no continuation", "H1": "continuation",
                           "paper_refs": ["Cont 1011.6402"],
                           "evidence_refs": ["calc.ofi.intervals"]},
        })
        engine, _s, _p, _m = _engine(llm_responses=[
            _FETCH_TURN,
            _good_narration(),                 # thin, no tools
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _good_narration(),                 # thin close, no H0
            # repair #1 pulls the audit AND covers P3 (market reads)
            _track_tools("market.read", "market.derivatives",
                         *_TRACK_DISCIPLINE),
            _good_narration(),                 # repair #1 close
            _good_narration(),                 # repair #2 close (budget 2)
            final,                             # FORCED FINAL (tool-free)
            final,                             # output composition pass
        ])
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        self.assertTrue(
            meta["final_validation"]["passed"], meta["final_validation"])
        self.assertIsNotNone(artifact.interpretation)
        self.assertEqual(meta["repairs"], 2)

    def test_forced_final_prompt_names_missing_items_and_shapes(self):
        from market_service.nooa_harness.engine.kb import (
            compose_forced_final_prompt,
        )
        prompt = compose_forced_final_prompt(
            task="does 4h continuation hold?",
            missing=["P4 explanation too thin (0/200 chars)"],
            evidence_digest='{"calc.ofi.intervals": {"data": []}}',
        )
        self.assertIn("FINALIZE NOW", prompt)
        self.assertIn("P4 explanation too thin", prompt)
        self.assertIn("FINAL-TURN FIELD SHAPES", prompt)
        self.assertIn("calc.ofi.intervals", prompt)
        self.assertIn("tool_calls: []", prompt)
        strict = compose_forced_final_prompt(
            task="t", missing=[], evidence_digest="{}", strict=True)
        self.assertIn("STRICT RETRY", strict)

    async def test_empty_forced_final_is_discarded_and_retried_strictly(self):
        """A shell final is discarded; the strict retry supplies the content.

        Live 2026-09-28: the one-shot forced final returned an empty shell on
        one run and the cycle died with an empty judged view. Bounded retry.
        """
        import json as _json

        from tests.test_engine import (
            _FETCH_TURN,
            _TRACK_ASSEMBLE, _TRACK_DISCIPLINE, _TRACK_HYPOTHESIZE,
            _TRACK_INTERPRET, _engine, _good_narration, _track_tools, _wake,
        )
        shell = _json.dumps({"phase": "P6", "tool_calls": [],
                             "summary": None, "evidence": [],
                             "hypothesis": None})
        final = _json.dumps({
            "phase": "P6", "tool_calls": [],
            "summary": "y" * 250,
            "evidence": [
                {"path": "calc.price.delta → route_a_direct.delta_ticks",
                 "value": "0.02", "interpretation": "derived delta P"},
                {"path": "calc.ofi.intervals → data[0].ofi",
                 "value": "4.0", "interpretation": "order flow"},
            ],
            "confidence": "low",
            "hypothesis": {"H0": "no continuation", "H1": "continuation"},
        })
        engine, _s, _p, _m = _engine(llm_responses=[
            _FETCH_TURN,
            _good_narration(),
            _track_tools(*_TRACK_ASSEMBLE),
            _track_tools(*_TRACK_INTERPRET),
            _track_tools(*_TRACK_HYPOTHESIZE),
            _good_narration(),
            _track_tools("market.read", "market.derivatives",
                         *_TRACK_DISCIPLINE),
            _good_narration(),
            _good_narration(),
            shell,     # FORCED FINAL attempt 1 — empty shell, discarded
            final,     # FORCED FINAL attempt 2 (strict) — full content
            final,     # output composition pass
        ])
        artifact, meta = await engine.narrate_cycle(_wake(), {"decision": "fire"})
        self.assertTrue(
            meta["final_validation"]["passed"], meta["final_validation"])
        self.assertIsNotNone(artifact.interpretation)
        self.assertTrue((artifact.interpretation or {}).get("summary"))


class StructuredProbeOptInTests(unittest.TestCase):
    """The structured (output_model) probe is OFF unless explicitly enabled.

    Live 4/4 correlation (2026-09-28): probe succeeded -> tool_calls-only
    turns -> empty judged interpretation -> validation_failed; probe refused
    (raw text JSON) -> full summary/evidence/hypothesis -> settled. The
    engine's contract is TEXT JSON, so the probe is opt-in.
    """

    def test_probe_is_off_by_default_and_env_opt_in(self):
        import os

        from market_service.nooa_harness.engine.config import (
            STRUCTURED_OUTPUT_ENV,
            structured_output_enabled,
        )
        self.assertEqual(STRUCTURED_OUTPUT_ENV, "NOOA_STRUCTURED_OUTPUT")
        os.environ.pop(STRUCTURED_OUTPUT_ENV, None)
        self.assertFalse(structured_output_enabled())
        os.environ[STRUCTURED_OUTPUT_ENV] = "1"
        try:
            self.assertTrue(structured_output_enabled())
        finally:
            os.environ.pop(STRUCTURED_OUTPUT_ENV, None)


if __name__ == "__main__":
    unittest.main()
