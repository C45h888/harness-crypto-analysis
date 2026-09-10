"""The invariant: only the calculation plane constructs substrate workers.

Worker identity on the Redis plane is ``(substrate, symbol)`` and never the
process. So a second process that builds a ``SubstrateWorkerCore`` against a
running calculation container will:

* overwrite its supervisor heartbeat with ``fired: 0`` on a short TTL — and if
  that TTL lapses while the real worker is blocked in ``XREADGROUP``,
  ``calc_healthcheck`` reports the container UNHEALTHY;
* destroy the fire-dedupe ``high_water`` / ``fired_stamp_ms`` state, which
  lives under that same key;
* consume raw-stream entries from the shared consumer group with
  ``noack=True`` — removing them from the real worker's delivery,
  unrecoverably.

None of that is detectable at runtime; it just quietly corrupts the plane.
So it is enforced statically here: everything outside
``market_service/substrate_worker/`` must go through
``substrate_worker.control_client``, which asks the calculation container to
run the fire in the workers' own process.

This test is the reason the regression cannot come back silently.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE = _REPO / "market_service"
_WORKER_PLANE = _PACKAGE / "substrate_worker"

# The in-process tool surface. ``control_client.request_invoke`` is
# deliberately absent — it is the sanctioned seam.
_FORBIDDEN_CALLS = {"invoke", "invoke_many"}
_FORBIDDEN_NAMES = {"SubstrateWorkerCore"}
_TOOL_MODULES = {"tools", "substrate_tools"}


def _python_files_outside_the_worker_plane():
    for path in _PACKAGE.rglob("*.py"):
        if _WORKER_PLANE in path.parents or path == _WORKER_PLANE:
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _label(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(_REPO))
    except ValueError:
        return str(path)


def _violations(path: pathlib.Path) -> list[str]:
    """Find real worker construction/invocation — AST, so prose can't trip it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = _label(path)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            found.append(f"{where}:{node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            found.append(f"{where}:{node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr in _FORBIDDEN_CALLS
                    and isinstance(func.value, ast.Name)
                    and func.value.id in _TOOL_MODULES):
                found.append(f"{where}:{node.lineno}: {func.value.id}.{func.attr}()")
    return found


class WorkerConstructionBoundaryTests(unittest.TestCase):
    def test_no_module_outside_the_worker_plane_builds_a_worker(self):
        violations: list[str] = []
        for path in _python_files_outside_the_worker_plane():
            violations.extend(_violations(path))

        self.assertEqual(
            violations, [],
            "These modules reach past the calculation plane and would corrupt "
            "the live workers' consumer groups and supervisor keys. Route them "
            "through market_service.substrate_worker.control_client instead:\n  "
            + "\n  ".join(violations),
        )

    def test_the_scan_actually_catches_a_violation(self):
        """Guard against a boundary test that passes because it matches nothing."""
        import tempfile

        offender = (
            "from market_service.substrate_worker import tools\n"
            "async def go(store):\n"
            "    return await tools.invoke_many(store, 'SOLUSDT', ['density'])\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(offender)
            probe = pathlib.Path(fh.name)
        try:
            self.assertTrue(_violations(probe), "the scan missed a real violation")
        finally:
            probe.unlink()

    def test_the_sanctioned_seam_exists_and_is_reachable(self):
        from market_service.substrate_worker.control_client import request_invoke
        self.assertTrue(callable(request_invoke))

    def test_the_calculation_plane_itself_is_exempt(self):
        """The container that owns the workers must still drive them."""
        source = (_WORKER_PLANE / "dain_container.py").read_text(encoding="utf-8")
        self.assertIn("invoke_many", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
