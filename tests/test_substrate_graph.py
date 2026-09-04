"""Substrate graph contract suite (decomposition pass).

Pins the invariants of the DECOMPOSED calculation layer:

1. A substrate NEVER imports another substrate (or any analysis module) —
   pure-math single-responsibility modules, composed only by the
   orchestrator (nooa_harness.bedrock).
2. The analysis layer does NOT import calculation modules at module scope
   (the layer violation is cut); ``analysis.market`` is the documented
   composition root (same class as ``nooa_harness.bedrock``) and is exempt.
3. Every section in GROUP_MAP is declared in SUBSTRATE_GRAPH, and every
   substrate named in the graph exists as a real module.
4. ``run_calculations`` / ``run_analysis`` emit ``substrate_provenance``
   keyed by section id, restricted to the sections that actually ran.
5. ``GroupEnvelope`` carries the provenance through to_dict/from_mapping.
6. The legacy module paths (calculations.*, analysis.wall_migration tier
   seam) resolve to the SAME objects as the substrates (move-don't-rewrite:
   byte-level identity, not copies).
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBSTRATE_DIR = ROOT / "market_service" / "calculations" / "substrates"
ANALYSIS_DIR = ROOT / "market_service" / "analysis"

# Modules that are allowed module-scope imports from the calculation layer:
#  - COMPOSITION_ROOTS: assemble raw evidence into a full product (same class
#    as nooa_harness.bedrock) — currently only the standalone analyzer/CLI.
#  - COMPAT_SEAMS: DEPRECATED re-export seams that exist ONLY so historical
#    import paths keep working after decomposition (same pattern as
#    market_service/signals.py); new code must import from the substrate
#    package.
COMPOSITION_ROOTS = {"market.py"}
COMPAT_SEAMS = {"wall_migration.py"}

_IMPORT_EXEMPT = COMPOSITION_ROOTS | COMPAT_SEAMS


def _substrate_modules() -> list[Path]:
    return sorted(SUBSTRATE_DIR.glob("*.py"))


def _analysis_modules() -> list[Path]:
    return sorted(ANALYSIS_DIR.glob("*.py"))


def _imports_into(module_path: Path, target: str) -> list[ast.AST]:
    """MODULE-LEVEL Import/ImportFrom statements whose path contains ``target``.

    Only the top-level statement list is scanned: a lazy import inside a
    function is a call-time load, not a module-scope coupling (the analysis
    layer keeps module imports calculation-free while lapsing into the
    calculation layer lazily for standalone callers).
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    hits: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if target in node.module:
                hits.append(node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if target in alias.name:
                    hits.append(node)
    return hits


class SubstrateIndependenceTests(unittest.TestCase):
    """Hard rule 1: substrates never import each other, never touch analysis."""

    def test_no_substrate_imports_another_substrate(self):
        for mod in _substrate_modules():
            if mod.name == "__init__.py":
                continue
            hits = _imports_into(mod, "market_service.calculations") + \
                _imports_into(mod, "market_service.analysis")
            self.assertEqual(
                hits, [],
                f"{mod.name} imports a sibling/analysis module at module scope:\n"
                f"{[ast.unparse(h) for h in hits]}",
            )

    def test_no_substrate_imports_runtime_or_clients(self):
        for mod in _substrate_modules():
            if mod.name == "__init__.py":
                continue
            for target in ("market_service.runtime", "market_service.clients",
                           "market_service.poller", "market_service.nooa_harness"):
                hits = _imports_into(mod, target)
                self.assertEqual(
                    hits, [],
                    f"{mod.name} imports the runtime/client layer (substrates are pure math): "
                    f"{[ast.unparse(h) for h in hits]}",
                )


class AnalysisLayerImportTests(unittest.TestCase):
    """Hard rule 2: analysis does not import calculations at module scope."""

    def test_no_analysis_module_imports_calculations(self):
        for mod in _analysis_modules():
            if mod.name in _IMPORT_EXEMPT:
                continue
            hits = _imports_into(mod, "market_service.calculations")
            self.assertEqual(
                hits, [],
                f"{mod.name} imports a calculation module at module scope; consume "
                f"calculation OUTPUT via injected providers instead: "
                f"{[ast.unparse(h) for h in hits]}",
            )


class SubstrateGraphConsistencyTests(unittest.TestCase):
    """Hard rule 3: GROUP_MAP sections are declared in SUBSTRATE_GRAPH."""

    @classmethod
    def setUpClass(cls):
        from market_service.nooa_harness import bedrock
        cls.bedrock = bedrock

    def test_every_group_section_is_in_graph(self):
        declared_calc = set()
        declared_anal = set()
        for spec in self.bedrock.GROUP_MAP.values():
            declared_calc.update(spec["calculations"])
            declared_anal.update(spec["analysis"])
        undeclared = (declared_calc | declared_anal) - set(self.bedrock.SUBSTRATE_GRAPH)
        self.assertEqual(
            undeclared, set(),
            "GROUP_MAP sections missing from SUBSTRATE_GRAPH: "
            f"{sorted(undeclared)}",
        )

    def test_every_graph_substrate_exists_as_a_module(self):
        known = {
            *[p.stem for p in _substrate_modules()],
            *[p.stem for p in _analysis_modules()],
        }
        for section_spec in self.bedrock.SUBSTRATE_GRAPH.values():
            for sub in section_spec["substrates"]:
                bare = sub.removeprefix("analysis.")
                self.assertIn(
                    bare, known,
                    f"graph substrate {sub!r} (section {section_spec!r}) has no module",
                )

    def test_substrate_for_and_section_inputs(self):
        for sec, spec in self.bedrock.SUBSTRATE_GRAPH.items():
            self.assertEqual(
                self.bedrock.substrate_for(sec),
                "/".join(spec["substrates"]),
                f"substrate_for({sec}) disagrees with graph",
            )
            self.assertEqual(self.bedrock.section_inputs(sec), spec["consumes"])
        self.assertIsNone(self.bedrock.substrate_for("no_such_section"))


class SubstrateProvenanceTests(unittest.TestCase):
    """Hard rule 4: run_* emit provenance for the sections that actually ran."""

    @classmethod
    def setUpClass(cls):
        from market_service.nooa_harness import bedrock
        cls.bedrock = bedrock
        cls.evidence = {
            "spot": {"trades_normalized": [], "order_book": {}},
            "futures": {"trades_normalized": [], "order_book": {},
                        "open_interest": {}, "funding": {}, "trades_raw": []},
            "errors": [], "fetch_window_ms": 900_000,
        }

    def test_full_cycle_provenance_covers_every_graph_section_that_runs(self):
        calc = self.bedrock.run_calculations(self.evidence, 20, 900)
        prov = calc["substrate_provenance"]
        # All calculation sections ran (sections=None means all).
        self.assertEqual(set(prov), set(self.bedrock.SUBSTRATE_GRAPH) -
                         set(self.bedrock.GROUP_MAP["wall"]["analysis"]) -
                         set(self.bedrock.GROUP_MAP["flow"]["analysis"]) -
                         set(self.bedrock.GROUP_MAP["structure"]["analysis"]) -
                         set(self.bedrock.GROUP_MAP["positioning"]["analysis"]))
        for sec, subs in prov.items():
            self.assertEqual(tuple(subs), self.bedrock.SUBSTRATE_GRAPH[sec]["substrates"])

    def test_restricted_sections_emit_only_requested_provenance(self):
        calc = self.bedrock.run_calculations(self.evidence, 20, 900,
                                             sections=frozenset({"flow", "technical"}))
        self.assertEqual(set(calc["substrate_provenance"]), {"flow", "technical"})

    def test_analysis_provenance_uses_section_ids(self):
        calc = self.bedrock.run_calculations(self.evidence, 20, 900)
        anal = self.bedrock.run_analysis(self.evidence, calc, depth=20)
        prov = anal["substrate_provenance"]
        # OI is keyed by its section id (``oi``), not the output key.
        self.assertIn("oi", prov)
        self.assertNotIn("open_interest", prov)


class GroupEnvelopeProvenanceTests(unittest.TestCase):
    """Hard rule 5: the interpretation plane carries provenance end-to-end."""

    def test_group_envelope_provenance_roundtrip(self):
        from market_service.nooa_harness.contracts import GroupEnvelope
        env = GroupEnvelope(
            kind="flow",
            symbol="SOLUSDT",
            status="healthy",
            generated_at="2026-09-04T00:00:00+00:00",
            run_id="run-1",
            window_minutes=15,
            coverage={},
            calculations={},
            analysis={},
            substrate_provenance={"flow": ("tape",), "delta": ("delta",)},
        )
        d = env.to_dict()
        self.assertEqual(d["substrate_provenance"], {"flow": ("tape",), "delta": ("delta",)})
        restored = GroupEnvelope.from_mapping(d)
        self.assertEqual(restored.substrate_provenance, {"flow": ("tape",), "delta": ("delta",)})

    def test_group_envelope_default_provenance(self):
        from market_service.nooa_harness.contracts import GroupEnvelope
        env = GroupEnvelope(
            kind="wall", symbol="SOLUSDT", status="healthy",
            generated_at="x", run_id="r", window_minutes=15,
            coverage={}, calculations={}, analysis={},
        )
        self.assertEqual(env.substrate_provenance, {})
        self.assertEqual(env.to_dict()["substrate_provenance"], {})


class LegacySeamIdentityTests(unittest.TestCase):
    """Hard rule 6: legacy paths resolve to the SAME objects as the substrates."""

    def _assert_identity(self, legacy_expr: str, substrate_expr: str):
        legacy_mod, _, legacy_attr = legacy_expr.rpartition(".")
        sub_mod, _, sub_attr = substrate_expr.rpartition(".")
        a = getattr(importlib.import_module(legacy_mod), legacy_attr)
        b = getattr(importlib.import_module(sub_mod), sub_attr)
        self.assertIs(
            a, b,
            f"{legacy_expr} is not the same object as {substrate_expr} "
            f"(move-don't-rewrite violated)",
        )

    def test_flow_seam_identity(self):
        for attr in ("summarize", "bucketed_cvd", "cvd_series_corr",
                     "microprice_skew_bps", "price_bucketed_flow"):
            self._assert_identity(f"market_service.calculations.flow.{attr}",
                                  f"market_service.calculations.substrates.tape.{attr}")

    def test_orderbook_seam_identity(self):
        for attr in ("find_keystone", "zone_ratio_grid", "hourly_keystone_migration",
                     "keystone_bid_stack", "ask_wall_ladder", "derive_round_anchors"):
            self._assert_identity(f"market_service.calculations.orderbook.{attr}",
                                  f"market_service.calculations.substrates.density.{attr}"
                                  if attr in ("find_keystone", "zone_ratio_grid")
                                  else f"market_service.calculations.substrates.migration.{attr}"
                                  if attr == "hourly_keystone_migration"
                                  else f"market_service.calculations.substrates.ladders.{attr}"
                                  if attr in ("keystone_bid_stack", "ask_wall_ladder")
                                  else f"market_service.calculations.substrates.anchors.{attr}")

    def test_technical_seam_identity(self):
        for attr in ("ema_series", "atr_pct_from_klines"):
            self._assert_identity(f"market_service.calculations.technical.{attr}",
                                  f"market_service.calculations.substrates.technicals.{attr}")
        for attr in ("tiered_large_flow", "seller_aggression_classify"):
            self._assert_identity(f"market_service.calculations.technical.{attr}",
                                  f"market_service.calculations.substrates.large_print.{attr}")

    def test_wall_migration_tier_seam_identity(self):
        for attr in ("TierConfig", "compute_bid_tiers_usd", "bid_tier_balance",
                     "mega_at_keystone", "compute_round_anchors"):
            self._assert_identity(f"market_service.analysis.wall_migration.{attr}",
                                  f"market_service.calculations.substrates.tiers.{attr}"
                                  if attr != "compute_round_anchors"
                                  else "market_service.calculations.substrates.anchors.compute_round_anchors")


if __name__ == "__main__":
    unittest.main(verbosity=2)