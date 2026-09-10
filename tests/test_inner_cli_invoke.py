"""The inner CLI asks the calculation plane; it never builds a worker.

``nooa market substrate invoke`` stays — an operator needs it — but it shares
the one control-plane seam with the inference dispatch, so a CLI invocation
can no longer clobber the running calculation container's supervisor keys or
steal its stream entries.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from market_service.commands.nooa_cli_ext import command


class InnerCliInvokeTests(unittest.TestCase):
    def test_invoke_goes_through_the_calculation_plane(self):
        with patch("market_service.substrate_worker.control_client.request_invoke",
                   new_callable=AsyncMock) as plane:
            plane.return_value = {"symbol": "SOLUSDT", "invoked": 1, "fired": 1,
                                  "reports": []}
            result = CliRunner().invoke(
                command, ["substrate", "invoke", "SOLUSDT", "density"])

        self.assertEqual(result.exit_code, 0, result.output)
        plane.assert_awaited_once()
        self.assertEqual(plane.await_args.args[0], "SOLUSDT")
        self.assertEqual(list(plane.await_args.args[1]), ["density"])

    def test_never_builds_a_worker_in_this_process(self):
        def _boom(*_a, **_kw):
            raise AssertionError("the CLI built a worker in its own process")

        with patch("market_service.substrate_worker.control_client.request_invoke",
                   new_callable=AsyncMock) as plane, \
                patch("market_service.substrate_worker.tools.invoke_many", new=_boom), \
                patch("market_service.substrate_worker.tools.invoke", new=_boom):
            plane.return_value = {"symbol": "SOLUSDT", "invoked": 1, "fired": 0,
                                  "reports": []}
            result = CliRunner().invoke(
                command, ["substrate", "invoke", "SOLUSDT", "density"])

        self.assertEqual(result.exit_code, 0, result.output)

    def test_unreachable_plane_reports_instead_of_falling_back(self):
        from market_service.substrate_worker.control_client import CalcPlaneUnreachable

        with patch("market_service.substrate_worker.control_client.request_invoke",
                   new_callable=AsyncMock) as plane:
            plane.side_effect = CalcPlaneUnreachable(
                "calculation plane unreachable at http://calculation:8041/invoke")
            result = CliRunner().invoke(
                command, ["substrate", "invoke", "SOLUSDT", "density"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unreachable", result.output.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
