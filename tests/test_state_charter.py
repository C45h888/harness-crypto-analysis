"""State-charter conformance tests — one namespace per state axis (S-2..S-5).

docs/STATE_CHARTER_SPEC.md:

* HORIZON axis: runtime/horizons.py is the tree-wide numeric owner; every
  claimant (fitting_route_c, horizon_bridge, task_directive, horizon_spans,
  fitting_common, interaction segments) resolves to it by IDENTITY — no
  module outside the owner may define a horizon numeric literal.
* DETERMINISTIC_STATE axis: the keyspace roots are frozen in
  context.DETERMINISTIC_STATE_ROOTS and every write site in the engine
  plane is pinned by AST scan.
* SCHEMA_VERSION axis: one registry in runtime/contracts.py; every
  module-level *_SCHEMA_VERSION assignment resolves to it; capture.py's
  hardcoded literal is gone.
* STATUS-FAMILY axis: each family's vocabulary is frozen and the families
  are pairwise disjoint (no axis borrows another's words).
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import market_service.microstructure.capture as capture
import market_service.microstructure.contracts as micro_contracts
import market_service.microstructure.fitting_common as fitting_common
import market_service.microstructure.fitting_route_c as fitting_route_c
import market_service.microstructure.horizon_bridge as horizon_bridge
import market_service.nooa_harness.engine.task_directive as task_directive
import market_service.runtime.contracts as runtime_contracts
import market_service.runtime.horizon_spans as horizon_spans
import market_service.runtime.horizons as horizons
import market_service.substrate_worker.contracts as substrate_contracts
from market_service.nooa_harness.engine.failure_substrate.membrane import FailureClass

_MARKET_DIR = pathlib.Path(horizons.__file__).parent.parent  # market_service/
_NUMERIC_HORIZON_VALUES = {
    1_000, 5_000, 30_000, 60_000,          # native
    900, 3_600, 14_400,                    # long (seconds)
    900_000, 3_600_000, 14_400_000,        # long (ms)
}


def _market_sources():
    for p in sorted(_MARKET_DIR.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        yield p, p.read_text()


class HorizonNamespaceTests(unittest.TestCase):
    """The {15m,1h,4h} + {1s,5s,30s,60s} numbers have ONE definition."""

    def test_owner_declares_the_full_vocabulary(self):
        self.assertEqual(
            horizons.LONG_HORIZONS_MS,
            {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000})
        self.assertEqual(
            horizons.NATIVE_HORIZONS_MS,
            {"1s": 1_000, "5s": 5_000, "30s": 30_000, "60s": 60_000})
        self.assertEqual(horizons.NATIVE_HORIZON_ORDER, (1_000, 5_000, 30_000, 60_000))
        self.assertEqual(horizons.LONG_HORIZON_ORDER, (900_000, 3_600_000, 14_400_000))
        self.assertEqual(horizons.HORIZON_NAMES, ("15m", "1h", "4h"))
        self.assertEqual(horizons.SPAN_HORIZON_NAMES,
                         ("1s", "5s", "30s", "60s", "15m", "1h", "4h"))

    def test_worker_cadence_widths_are_the_owner_alias(self):
        # The H1 worker cadence MUST be the long-horizon grain by identity —
        # the worker plane and the engine's task directives can never fork.
        self.assertIs(horizons.HORIZON_PERIOD_MS, horizons.LONG_HORIZONS_MS)
        # And the worker plane resolves through it (import identity).
        from market_service.substrate_worker.core import base as worker_base
        self.assertIs(worker_base.HORIZON_PERIOD_MS, horizons.LONG_HORIZONS_MS)

    def test_fitting_route_c_resolves_to_owner(self):
        self.assertIs(fitting_route_c.FORWARD_HORIZONS_MS,
                      horizons.NATIVE_HORIZON_ORDER)

    def test_horizon_bridge_resolves_to_owner(self):
        self.assertIs(horizon_bridge.NATIVE_HORIZONS_MS,
                      horizons.NATIVE_HORIZON_ORDER)
        self.assertIs(horizon_bridge.LONG_HORIZONS_MS,
                      horizons.LONG_HORIZON_ORDER)

    def test_task_directive_resolves_to_owner(self):
        self.assertIs(task_directive.NATIVE_HORIZONS_MS,
                      horizons.NATIVE_HORIZONS_MS)
        self.assertIs(task_directive.LONG_HORIZONS_MS,
                      horizons.LONG_HORIZONS_MS)
        self.assertIs(task_directive.LONG_HORIZONS_SECONDS,
                      horizons.LONG_HORIZONS_SECONDS)

    def test_fitting_common_scenario_horizons_resolve_to_owner(self):
        self.assertIs(fitting_common.SCENARIO_HORIZONS,
                      horizons.LONG_HORIZONS_SECONDS)

    def test_horizon_spans_names_resolve_to_owner(self):
        self.assertIs(horizon_spans.NATIVE_HORIZONS,
                      horizons.NATIVE_HORIZON_NAMES)
        self.assertIs(horizon_spans.LONG_HORIZONS, horizons.HORIZON_NAMES)
        self.assertIs(horizon_spans.SPAN_HORIZONS,
                      horizons.SPAN_HORIZON_NAMES)
        # Interaction plane's span slice vocabulary is the SAME object.
        from market_service.interaction_plane import segments as seg
        from market_service.runtime import horizon_spans as spans
        self.assertIs(seg.SPAN_HORIZONS, spans.SPAN_HORIZONS)

    def test_no_module_outside_the_owner_defines_horizon_numbers(self):
        """AST scan: any MODULE-LEVEL assignment whose target name mentions
        HORIZON and whose value embeds a horizon numeric literal must live
        in runtime/horizons.py — the charter owner. (Derived assignments
        whose value is a Name are fine everywhere.)"""
        violations = []
        owner = "runtime/horizons.py"
        for path, src in _market_sources():
            rel = str(path.relative_to(_MARKET_DIR))
            if rel == owner:
                continue  # the charter owner defines the numbers by design
            tree = ast.parse(src)
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = ([node.target] if isinstance(node, ast.AnnAssign)
                           else node.targets)
                for t in targets:
                    name = t.id if isinstance(t, ast.Name) else None
                    if not name or "HORIZON" not in name:
                        continue
                    if not isinstance(node.value, (ast.Dict, ast.Tuple, ast.List)):
                        continue  # Name-derived (imported) — charter-compliant
                    for sub in ast.walk(node.value):
                        if (isinstance(sub, ast.Constant)
                                and isinstance(sub.value, int)
                                and sub.value in _NUMERIC_HORIZON_VALUES):
                            violations.append((rel, name))
                            break
        self.assertEqual(violations, [],
                         "horizon numeric literals defined outside the charter owner")


class DeterministicRootsTests(unittest.TestCase):
    """The keyspace's roots are frozen in context.py and every write site
    in the engine plane is pinned by AST scan."""

    @staticmethod
    def _observed_roots():
        import market_service.nooa_harness.engine.core.runner as _r
        engine_dir = pathlib.Path(_r.__file__).parent.parent
        keys: set[str] = set()
        for p in engine_dir.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            tree = ast.parse(p.read_text())
            for node in ast.walk(tree):
                # deterministic_state["k"] = ... / st.deterministic_state["k"] = ...
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)
                        and isinstance(node.value, (ast.Name, ast.Attribute))
                        and (node.value.id if isinstance(node.value, ast.Name)
                             else node.value.attr) == "deterministic_state"):
                    keys.add(node.slice.value)
                # init dict: (Ann)Assign target named deterministic_state with Dict value
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = ([node.target] if isinstance(node, ast.AnnAssign)
                               else node.targets)
                    for t in targets:
                        name = t.id if isinstance(t, ast.Name) else (
                            t.attr if isinstance(t, ast.Attribute) else None)
                        if name == "deterministic_state" and isinstance(node.value, ast.Dict):
                            for k in node.value.keys:
                                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                    keys.add(k.value)
        return keys

    def test_registry_is_frozen(self):
        self.assertIsInstance(
            __import__("market_service.nooa_harness.engine.core.context",
                       fromlist=["x"]).DETERMINISTIC_STATE_ROOTS,
            frozenset)

    def test_every_observed_write_is_declared(self):
        observed = self._observed_roots()
        roots = __import__("market_service.nooa_harness.engine.core.context",
                           fromlist=["x"]).DETERMINISTIC_STATE_ROOTS
        undeclared = observed - roots
        self.assertEqual(undeclared, set(),
                         "deterministic_state writes outside the declared roots")

    def test_every_declared_root_is_written(self):
        observed = self._observed_roots()
        roots = __import__("market_service.nooa_harness.engine.core.context",
                           fromlist=["x"]).DETERMINISTIC_STATE_ROOTS
        orphaned = roots - observed
        self.assertEqual(orphaned, set(),
                         "declared roots with no engine write/read site")

    def test_no_engine_write_outside_the_keyspace_vocabulary(self):
        """A write through a DYNAMIC key must name its literal elsewhere —
        the AST scan above only pins literal keys; this asserts the total
        count of write sites matches the literal-keyed set size (i.e. no
        subscript writes with non-literal keys exist)."""
        import market_service.nooa_harness.engine.core.runner as _r
        engine_dir = pathlib.Path(_r.__file__).parent.parent
        dynamic = 0
        for p in engine_dir.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            tree = ast.parse(p.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Subscript) \
                        and not isinstance(node.slice, ast.Constant) \
                        and isinstance(node.value, (ast.Name, ast.Attribute)) \
                        and (node.value.id if isinstance(node.value, ast.Name)
                             else node.value.attr) == "deterministic_state":
                    dynamic += 1
        self.assertEqual(dynamic, 0,
                         "dynamic deterministic_state key writes bypass the charter")


class SchemaRegistryTests(unittest.TestCase):
    """One numeric authority for every payload contract's version."""

    @staticmethod
    def _module_version_assignments():
        found: dict[str, tuple[int, str]] = {}
        for path, src in _market_sources():
            rel = str(path.relative_to(_MARKET_DIR))
            tree = ast.parse(src)
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    t = node.targets[0]
                    if (isinstance(t, ast.Name) and t.id.endswith("_SCHEMA_VERSION")
                            and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, int)):
                        found[t.id] = (node.value.value, rel)
        return found

    def test_registry_covers_every_declared_version(self):
        from market_service.runtime.contracts import SCHEMA_VERSION_REGISTRY
        for name, (value, rel) in self._module_version_assignments().items():
            key = name[: -len("_SCHEMA_VERSION")].lower()
            self.assertIn(key, SCHEMA_VERSION_REGISTRY,
                          f"{name} ({rel}) missing from SCHEMA_VERSION_REGISTRY")
            self.assertEqual(SCHEMA_VERSION_REGISTRY[key], value,
                             f"{name} ({rel}) disagrees with the registry")

    def test_registry_values_are_frozen_by_identity(self):
        # The runtime-owned constants ARE the registry entries.
        self.assertIs(runtime_contracts.MARKET_RUN_SCHEMA_VERSION,
                      runtime_contracts.SCHEMA_VERSION_REGISTRY["market_run"])
        # Cross-plane constants resolve to the same registry entries.
        self.assertIs(substrate_contracts.SUBSTRATE_STATE_SCHEMA_VERSION,
                      runtime_contracts.SCHEMA_VERSION_REGISTRY["substrate_state"])
        self.assertIs(micro_contracts.MICROSTRUCTURE_SCHEMA_VERSION,
                      runtime_contracts.SCHEMA_VERSION_REGISTRY["microstructure"])

    def test_capture_status_payload_uses_the_contract_constant(self):
        src = inspect.getsource(capture)
        self.assertIn("MICROSTRUCTURE_SCHEMA_VERSION", src)
        self.assertNotIn('"schema_version": 1', src)


import inspect  # noqa: E402  (used by SchemaRegistryTests)


class StatusFamilyTests(unittest.TestCase):
    """Each status family's vocabulary is frozen — and the families are
    pairwise disjoint (no axis borrows another axis's words)."""

    def test_transport_liveness_family_frozen(self):
        self.assertEqual(
            set(capture.STATUS_TRANSITIONS),
            {"starting", "reconnecting", "stopped", "connected", "running", "gap"},
        )
        # Every _status("...") call site resolves to a member.
        src = pathlib.Path(capture.__file__).read_text()
        call_states = set()
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_status" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                call_states.add(node.args[0].value)
        self.assertTrue(call_states)
        self.assertEqual(call_states - set(capture.STATUS_TRANSITIONS), set())

    def test_payload_health_family_frozen(self):
        self.assertEqual(
            set(substrate_contracts.PAYLOAD_STATUSES),
            {"healthy", "degraded", "insufficient_data"},
        )

    def test_span_fit_family_frozen(self):
        self.assertEqual(
            set(horizon_spans.STATUSES),
            {"validated", "provisional", "insufficient"},
        )

    def test_failure_family_is_the_declared_enum(self):
        self.assertEqual(
            {member.value for member in FailureClass},
            {"narration", "tool", "gate", "dispatch", "memory",
             "validation", "inference", "infra"},
        )

    def test_families_are_pairwise_disjoint(self):
        families = {
            "payload_health": set(substrate_contracts.PAYLOAD_STATUSES),
            "span_fit": set(horizon_spans.STATUSES),
            "transport_liveness": set(capture.STATUS_TRANSITIONS),
            "failure_class": {m.value for m in FailureClass},
        }
        names = sorted(families)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertEqual(families[a] & families[b], set(),
                                 f"status families {a} and {b} share words")


if __name__ == "__main__":
    unittest.main()