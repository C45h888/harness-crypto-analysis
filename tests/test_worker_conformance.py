"""Worker conformance — purity, cadence declaration, registry coverage.

Pins the Phase 2 hard invariants:

1. A worker file imports exactly ONE ``calculations.substrates.*`` module
   (module-scope AST scan, same technique as test_substrate_graph.py).
2. No cross-worker imports; no harness/Binance/composition imports at module
   scope. Declared data dependencies travel via ``DEPENDENCIES`` and must be
   listed in ``ALLOWED_DEPENDENCIES``.
3. Every registered worker declares a ``CadenceProfile`` and a ``"raw"``
   input stream.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from market_service.substrate_worker import WORKER_REGISTRY
from market_service.substrate_worker.contracts import CadenceProfile

ROOT = Path(__file__).resolve().parent.parent
WORKER_DIR = ROOT / "market_service" / "substrate_worker"

# The single declared cross-worker data edge (signals → tape). Any other
# DEPENDENCIES value fails this suite — composition lives at the boundary,
# never inside a worker.
ALLOWED_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "signals": ("tape",),
}

# Module-scope imports a worker file may NEVER carry.
_BANNED_TARGETS = (
    "market_service.clients",
    "market_service.nooa_harness",
    "market_service.calculations.composition",
    "market_service.calculations.flow",
    "market_service.calculations.orderbook",
    "market_service.calculations.technical",
    "market_service.calculations.volume_profile",
    "market_service.calculations.delta",
    "market_service.calculations.signals",
    "binance",
)


def _worker_files() -> list[Path]:
    return sorted(p for p in WORKER_DIR.glob("*_worker.py")
                  if p.name not in ("core.py",))


def _module_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


class WorkerPurityTests(unittest.TestCase):
    def test_each_worker_imports_exactly_one_substrate(self):
        for path in _worker_files():
            imported = [m for m in _module_imports(path)
                        if "market_service.calculations.substrates." in m]
            modules = {m.split("market_service.calculations.substrates.")[1].split(".")[0]
                       for m in imported}
            self.assertEqual(
                len(modules), 1,
                f"{path.name} must import exactly one substrate module, saw {sorted(modules)}",
            )

    def test_no_banned_module_scope_imports(self):
        for path in _worker_files():
            for module in _module_imports(path):
                for banned in _BANNED_TARGETS:
                    self.assertFalse(
                        module == banned or module.startswith(banned + "."),
                        f"{path.name} imports banned module {module!r} at module scope",
                    )

    def test_no_cross_worker_imports(self):
        worker_modules = {p.stem for p in _worker_files()}
        for path in _worker_files():
            for module in _module_imports(path):
                if "market_service.substrate_worker." in module:
                    leaf = module.split("market_service.substrate_worker.")[1].split(".")[0]
                    self.assertNotIn(
                        leaf, worker_modules,
                        f"{path.name} imports sibling worker module {module!r}",
                    )


class DeclaredDependencyTests(unittest.TestCase):
    def test_dependencies_restricted_to_allow_map(self):
        for name, cls in WORKER_REGISTRY.items():
            declared = tuple(getattr(cls, "DEPENDENCIES", ()))
            self.assertEqual(
                declared, ALLOWED_DEPENDENCIES.get(name, ()),
                f"worker {name!r} DEPENDENCIES={declared!r} not in the allow-map",
            )


class CadenceDeclarationTests(unittest.TestCase):
    def test_every_worker_declares_cadence_and_raw_stream(self):
        self.assertTrue(WORKER_REGISTRY, "registry is empty")
        for name, cls in WORKER_REGISTRY.items():
            cadence = getattr(cls, "CADENCE", None)
            self.assertIsInstance(
                cadence, CadenceProfile,
                f"worker {name!r} must declare a CADENCE CadenceProfile",
            )
            self.assertIn(
                "raw", tuple(getattr(cls, "INPUT_STREAMS", ())),
                f"worker {name!r} must consume the raw input stream",
            )

    def test_full_registry_coverage(self):
        for name in ("density", "tape", "large_print", "delta",
                     "ladders", "anchors", "tiers", "volume_profile",
                     "technicals", "migration", "oi", "signals"):
            self.assertIn(name, WORKER_REGISTRY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
