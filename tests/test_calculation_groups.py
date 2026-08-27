"""Tests for the Pass 3 calculation-model groups (segregated command surface).

Covers:
- GROUP_MAP integrity (all section names are real run_calculations/run_analysis keys)
- resolve_calc_sections dependency expansion
- resolve_analysis_sections calculation-prerequisite merging
- sections_for_groups flattening + unknown-group error
- run_calculations / run_analysis sections filtering (only requested sections run)
"""

from __future__ import annotations

import unittest

from market_service.nooa_harness.pipeline import (
    GROUP_MAP,
    _ANALYSIS_CALC_DEPS,
    _CALC_SECTION_DEPS,
    resolve_analysis_sections,
    resolve_calc_sections,
    run_analysis,
    run_calculations,
    sections_for_groups,
)

# The full section inventories produced by run_calculations / run_analysis.
ALL_CALC_SECTIONS = {
    "flow", "bucketed_cvd", "correlation", "signal_inputs", "signals",
    "orderbook", "turnover", "volume_profile", "technical",
}
ALL_ANALYSIS_SECTIONS = {
    "auction", "oi", "wall_migration", "delta",
    "path_absorption", "demand", "regime", "stage",
}


class GroupMapIntegrityTests(unittest.TestCase):
    def test_group_sections_are_real(self):
        for group, spec in GROUP_MAP.items():
            for sec in spec["calculations"]:
                self.assertIn(sec, ALL_CALC_SECTIONS,
                              f"group {group}: unknown calc section {sec}")
            for sec in spec["analysis"]:
                self.assertIn(sec, ALL_ANALYSIS_SECTIONS,
                              f"group {group}: unknown analysis section {sec}")

    def test_all_four_groups_present(self):
        self.assertEqual(set(GROUP_MAP.keys()),
                         {"wall", "flow", "structure", "positioning"})


class ResolveCalcSectionsTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(resolve_calc_sections(None))

    def test_dependency_expansion(self):
        # turnover depends on flow
        r = resolve_calc_sections(frozenset({"turnover"}))
        self.assertIn("turnover", r)
        self.assertIn("flow", r)

    def test_signals_pulls_flow(self):
        r = resolve_calc_sections(frozenset({"signals"}))
        self.assertIn("flow", r)

    def test_no_expansion_for_independent_section(self):
        r = resolve_calc_sections(frozenset({"orderbook"}))
        self.assertEqual(r, frozenset({"orderbook"}))


class ResolveAnalysisSectionsTests(unittest.TestCase):
    def test_wall_migration_pulls_orderbook_calc(self):
        anal, calc = resolve_analysis_sections(
            frozenset({"wall_migration"}), frozenset(set()))
        self.assertEqual(anal, frozenset({"wall_migration"}))
        self.assertIn("orderbook", calc)

    def test_regime_pulls_flow_calc(self):
        anal, calc = resolve_analysis_sections(
            frozenset({"regime"}), frozenset(set()))
        self.assertIn("flow", calc)

    def test_none_analysis_passes_through(self):
        anal, calc = resolve_analysis_sections(None, frozenset({"orderbook"}))
        self.assertIsNone(anal)
        self.assertEqual(calc, frozenset({"orderbook"}))

    def test_no_deps_unchanged(self):
        anal, calc = resolve_analysis_sections(
            frozenset({"delta"}), frozenset({"flow"}))
        self.assertEqual(anal, frozenset({"delta"}))
        self.assertEqual(calc, frozenset({"flow"}))


class SectionsForGroupsTests(unittest.TestCase):
    def test_wall_group(self):
        calc, anal = sections_for_groups(("wall",))
        self.assertEqual(calc, frozenset({"orderbook"}))
        self.assertEqual(anal, frozenset({"wall_migration", "path_absorption", "oi"}))

    def test_multiple_groups_merge(self):
        calc, anal = sections_for_groups(("wall", "flow"))
        self.assertIn("orderbook", calc)
        self.assertIn("flow", calc)
        self.assertIn("wall_migration", anal)
        self.assertIn("demand", anal)

    def test_unknown_group_raises(self):
        with self.assertRaises(ValueError):
            sections_for_groups(("nonexistent",))


class RunCalculationsSectionsFilterTests(unittest.TestCase):
    """run_calculations with a sections filter only emits the requested sections."""

    def _minimal_evidence(self):
        # A tiny evidence dict — enough for sections to run or degrade cleanly.
        return {
            "spot": {"trades_normalized": [], "order_book": {"bids": [], "asks": []}},
            "futures": {"trades_normalized": [], "order_book": {"bids": [], "asks": []},
                        "klines": []},
        }

    def test_orderbook_only(self):
        r = run_calculations(self._minimal_evidence(), depth=50, window=900,
                             sections=frozenset({"orderbook"}))
        calc = r["calculations"]
        self.assertIn("orderbook", calc)
        # Unrequested sections are omitted entirely.
        self.assertNotIn("flow", calc)
        self.assertNotIn("volume_profile", calc)
        self.assertNotIn("technical", calc)

    def test_none_runs_all(self):
        r = run_calculations(self._minimal_evidence(), depth=50, window=900,
                             sections=None)
        calc = r["calculations"]
        # Full-cycle behavior: all canonical keys present.
        for key in ("flow", "orderbook", "volume_profile", "technical"):
            self.assertIn(key, calc)


class RunAnalysisSectionsFilterTests(unittest.TestCase):
    """run_analysis with a sections filter only emits the requested adapters."""

    def _minimal(self):
        evidence = {
            "spot": {"trades_normalized": [], "order_book": {"bids": [], "asks": []}},
            "futures": {"trades_normalized": [], "order_book": {"bids": [], "asks": []},
                        "klines": [], "open_interest": {}, "funding": {},
                        "mark_price": {}},
        }
        calculations = {"calculations": {"orderbook": {}, "flow": {}}}
        return evidence, calculations

    def test_delta_only(self):
        evidence, calc = self._minimal()
        r = run_analysis(evidence, calc, depth=50,
                         sections=frozenset({"delta"}))
        anal = r["analysis"]
        self.assertIn("delta", anal)
        self.assertNotIn("wall_migration", anal)
        self.assertNotIn("demand", anal)

    def test_none_runs_all(self):
        evidence, calc = self._minimal()
        r = run_analysis(evidence, calc, depth=50, sections=None)
        anal = r["analysis"]
        for key in ("auction", "open_interest", "wall_migration", "delta",
                    "path_absorption", "demand", "regime", "stage"):
            self.assertIn(key, anal)


class DependencyMapConsistencyTests(unittest.TestCase):
    """The dependency maps reference only real sections."""

    def test_calc_deps_are_real(self):
        for sec, deps in _CALC_SECTION_DEPS.items():
            self.assertIn(sec, ALL_CALC_SECTIONS)
            for d in deps:
                self.assertIn(d, ALL_CALC_SECTIONS)

    def test_analysis_deps_are_real(self):
        for sec, deps in _ANALYSIS_CALC_DEPS.items():
            self.assertIn(sec, ALL_ANALYSIS_SECTIONS)
            for d in deps:
                self.assertIn(d, ALL_CALC_SECTIONS)


if __name__ == "__main__":
    unittest.main()
